"""GitHub context collection shared by live predictions and benchmarks."""

import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from .types import Bundle, Context


CACHE = Path.home() / ".cache" / "entire-file-finding-v2"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "prism"}
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def api_v2(path: str):
    """Fetch and cache a public GitHub response without caching credentials."""
    CACHE.mkdir(parents=True, exist_ok=True)
    destination = CACHE / f"{hashlib.sha256(path.encode()).hexdigest()}.json"
    if destination.exists() and not path.endswith("/commits/main"):
        return json.loads(destination.read_text())
    headers = dict(HEADERS)
    if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=40) as response:
        data = json.load(response)
    destination.write_text(json.dumps(data))
    return data


def comments_v2(root: str, number: int, cutoff: str | None) -> list[dict]:
    comments = []
    for page in range(1, 101):
        batch = api_v2(f"{root}/issues/{number}/comments?per_page=100&page={page}")
        comments.extend(comment for comment in batch if cutoff is None or comment["created_at"] < cutoff)
        if len(batch) < 100 or (cutoff and batch and batch[-1]["created_at"] >= cutoff):
            break
    return comments


def clean_v2(value: str | None, target_number: int | None) -> str:
    value = re.split(r"(?im)^#{1,3}\s*(?:system information|environment information)\s*$", value or "")[0]
    if target_number is None:
        return value.strip()
    target = _target_mention(target_number)
    return "\n".join(line for line in value.splitlines() if not target.search(line)).strip()


def refs_v2(text: str, default_owner: str, default_repo: str) -> list[tuple[str, str, int]]:
    refs = set()
    for owner, repo, _kind, number in re.findall(r"https://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)", text):
        refs.add((owner, repo, int(number)))
    for owner, repo, number in re.findall(r"(?<![\w/])([\w.-]+)/([\w.-]+)#(\d+)", text):
        refs.add((owner, repo, int(number)))
    for number in re.findall(r"(?<![\w/])#(\d+)\b", text):
        refs.add((default_owner, default_repo, int(number)))
    return sorted(refs)


def repo_map_v2(tree: dict) -> tuple[set[str], dict]:
    known = {item["path"] for item in tree["tree"] if item["type"] == "blob"}
    directories = {path.rpartition("/")[0] for path in known if "/" in path}
    conventional = sorted(path for path in known if re.search(
        r"(^|/)(?:CHANGES[^/]*|CHANGELOG[^/]*|pyproject\.toml)$", path, re.I))
    return known, {
        "top_level": sorted({path.split("/")[0] for path in known})[:100],
        "directories": sorted(directories)[:160],
        "conventional_files": conventional[:40],
        "test_directories": sorted(path for path in directories if path.split("/")[0] in ("tests", "test", "testing"))[:40],
        "doc_directories": sorted(path for path in directories if path.split("/")[0] in ("docs", "doc"))[:40],
        "file_count": len(known),
    }


def _github_repo(entire_repo: str) -> tuple[str, str]:
    match = re.fullmatch(r"gh/([\w.-]+)/([\w.-]+)", entire_repo)
    if not match:
        raise ValueError("Benchmark mode requires entire_repo in gh/owner/repo form")
    return match.group(1), match.group(2)


def _tree(root: str, sha: str) -> tuple[set[str], dict]:
    tree = api_v2(f"{root}/git/trees/{sha}?recursive=1")
    if tree.get("truncated"):
        raise ValueError("GitHub tree truncated; cannot validate paths")
    return repo_map_v2(tree)


def _safe_bundle(bundle: Bundle, target: int | None) -> None:
    if target is not None and _target_mention(target).search(json.dumps(bundle.__dict__)):
        raise ValueError("Target PR reference leaked into context")


def _target_mention(target: int) -> re.Pattern:
    return re.compile(
        rf"(?<!\d)(?:#{target}|(?:pull\s+request|pr)\s*#?\s*{target})(?!\d)|/pull/{target}(?!\d)",
        re.I,
    )


def build_context(
    issue_url: str,
    *,
    entire_repo: str,
    pr_url: str | None = None,
    pre_merge_sha: str | None = None,
) -> Context:
    issue_match = ISSUE_URL.fullmatch(issue_url)
    if not issue_match:
        raise ValueError("Bad issue URL")
    owner, repo, issue_text_number = issue_match.groups()
    issue_number = int(issue_text_number)
    root = f"repos/{owner}/{repo}"
    issue = api_v2(f"{root}/issues/{issue_number}")

    target: int | None = None
    cutoff: str | None = None
    if pr_url is not None:
        pr_match = PR_URL.fullmatch(pr_url)
        if not pr_match or pr_match.group(1, 2) != (owner, repo):
            raise ValueError("Bad issue/PR pair")
        target = int(pr_match.group(3))
        pr = api_v2(f"{root}/pulls/{target}")
        if issue["state"] != "closed" or not pr.get("merged_at"):
            raise ValueError("Issue must be closed and PR merged")
        cutoff = pr["created_at"]
        before = api_v2(f"{root}/commits/{pr['merge_commit_sha']}")["parents"][0]["sha"]
        if pre_merge_sha is not None and before != pre_merge_sha:
            raise ValueError("Target PR parent does not match pre_merge_sha")
        fork_owner, fork_repo = _github_repo(entire_repo)
        if api_v2(f"repos/{fork_owner}/{fork_repo}/commits/main")["sha"] != before:
            raise ValueError("Entire repository main does not match the pre-merge commit")
        known, repo_map = _tree(root, before)
    else:
        repository = api_v2(root)
        branch = urllib.parse.quote(repository["default_branch"], safe="")
        sha = api_v2(f"{root}/commits/{branch}")["sha"]
        known, repo_map = _tree(root, sha)

    body = clean_v2(issue.get("body"), target)
    issue_comments = [clean_v2(comment.get("body"), target) for comment in comments_v2(root, issue_number, cutoff)]
    issue_context = "\n".join([clean_v2(issue["title"], target), body, *issue_comments])
    references: list[dict] = []
    safe_refs: set[tuple[str, str, int]] = set()
    for ref_owner, ref_repo, number in refs_v2(issue_context, owner, repo):
        if target is not None and number == target:
            continue
        ref_root = f"repos/{ref_owner}/{ref_repo}"
        try:
            item = api_v2(f"{ref_root}/issues/{number}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        if cutoff is not None and item["created_at"] >= cutoff:
            continue
        safe_refs.add((ref_owner, ref_repo, number))
        label = f"{ref_owner}/{ref_repo}#{number}"
        if "pull_request" in item:
            detail = api_v2(f"{ref_root}/pulls/{number}")
            references.append({
                "reference": label, "type": "PR", "title": clean_v2(detail["title"], target),
                "body": clean_v2(detail.get("body"), target),
                "pointer": f"PR #{number}: {item.get('comments', 0)} comments, {detail.get('changed_files', 0)} files changed (not included)",
            })
        else:
            references.append({
                "reference": label, "type": "issue", "title": clean_v2(item["title"], target),
                "body": clean_v2(item.get("body"), target),
                "comments": [clean_v2(comment.get("body"), target) for comment in comments_v2(ref_root, number, cutoff)],
            })
    paths = sorted(set(re.findall(r"(?<![\w./-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+", issue_context)) & known)
    bundle = Bundle(
        issue={"title": clean_v2(issue["title"], target), "body": body, "comments": issue_comments},
        references=references, path_hints=paths, repo_map=repo_map,
    )
    _safe_bundle(bundle, target)
    return Context(bundle=bundle, known_paths=known, safe_refs=safe_refs, cutoff=cutoff,
                   target_pr=target, entire_repo=entire_repo)


def fetch_reference(request: str, ctx: Context) -> dict | None:
    match = re.fullmatch(r"(?:(?:([\w.-]+)/([\w.-]+))?#)?(\d+)\s+(comments|files)", request.strip(), re.I)
    if not match:
        return None
    owner, repo, number_text, kind = match.groups()
    number = int(number_text)
    choices = [(ref_owner, ref_repo, ref_number) for ref_owner, ref_repo, ref_number in ctx.safe_refs
               if ref_number == number and (not owner or (ref_owner, ref_repo) == (owner, repo))]
    if len(choices) != 1:
        return None
    owner, repo, _ = choices[0]
    root = f"repos/{owner}/{repo}"
    item = api_v2(f"{root}/issues/{number}")
    if ctx.cutoff is not None and item["created_at"] >= ctx.cutoff:
        return None
    if kind.lower() == "comments":
        return {"reference": f"{owner}/{repo}#{number}", "comments": [
            clean_v2(comment.get("body"), ctx.target_pr) for comment in comments_v2(root, number, ctx.cutoff)
        ]}
    if "pull_request" not in item:
        return None
    detail = api_v2(f"{root}/pulls/{number}")
    if ctx.cutoff is not None and detail["created_at"] >= ctx.cutoff:
        return None
    paths: list[str] = []
    for page in range(1, 101):
        batch = api_v2(f"{root}/pulls/{number}/files?per_page=100&page={page}")
        paths.extend(file["filename"] for file in batch)
        if len(batch) < 100:
            break
    return {"reference": f"{owner}/{repo}#{number}", "files": paths}


def true_files(pr_url: str, known_paths: set[str]) -> set[str]:
    match = PR_URL.fullmatch(pr_url)
    if not match:
        raise ValueError("Bad PR URL")
    owner, repo, number = match.groups()
    root = f"repos/{owner}/{repo}"
    files = []
    for page in range(1, 101):
        batch = api_v2(f"{root}/pulls/{number}/files?per_page=100&page={page}")
        files.extend(batch)
        if len(batch) < 100:
            break
    return {file["filename"] for file in files if file["status"] == "modified" and file["filename"] in known_paths}
