"""GitHub context collection shared by live predictions and benchmarks."""

import hashlib
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from .types import Bundle, Context


CACHE = Path.home() / ".cache" / "entire-file-finding-v2"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "prism"}
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
_IMMUTABLE_PATH = re.compile(r"repos/[\w.-]+/[\w.-]+/(?:commits|git/trees)/[0-9a-f]{40}(?:\?recursive=1)?$")
# Bounds applied to referenced issues and PRs only; None disables a bound.
REFERENCE_BODY_LIMIT: int | None = 2000
REFERENCE_COMMENT_LIMIT: int | None = 500
REFERENCE_COMMENTS: int | None = 4
WORKERS = 8
_requests = threading.BoundedSemaphore(WORKERS)
_gh_token_lock = threading.Lock()
_gh_token_checked = False
_gh_token: str | None = None


def api_v2(path: str):
    """Fetch GitHub responses, caching only immutable SHA-addressed data."""
    CACHE.mkdir(parents=True, exist_ok=True)
    destination = CACHE / f"{hashlib.sha256(path.encode()).hexdigest()}.json"
    cacheable = bool(_IMMUTABLE_PATH.fullmatch(path))
    if cacheable and destination.exists():
        return json.loads(destination.read_text())
    headers = dict(HEADERS)
    if token := _github_token():
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    with _requests, urllib.request.urlopen(request, timeout=40) as response:
        data = json.load(response)
    if cacheable:
        # Write-then-rename so a concurrent reader never sees a partial file.
        partial = destination.with_name(f"{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        partial.write_text(json.dumps(data))
        os.replace(partial, destination)
    return data


class _Once:
    """Fetch each path at most once per run; every caller gets the same response or the same error."""

    def __init__(self):
        self._lock = threading.Lock()
        self._results: dict[str, Future] = {}

    def __call__(self, path: str):
        with self._lock:
            result = self._results.get(path)
            first = result is None
            if first:
                result = self._results[path] = Future()
        if first:
            try:
                result.set_result(api_v2(path))
            except BaseException as error:
                result.set_exception(error)
        return result.result()


def _github_token() -> str | None:
    global _gh_token_checked, _gh_token
    if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return token
    with _gh_token_lock:
        if _gh_token_checked:
            return _gh_token
        try:
            result = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True, timeout=10)
        except FileNotFoundError:
            return None
        finally:
            _gh_token_checked = True
        if result.returncode == 0:
            _gh_token = result.stdout.strip()
        return _gh_token


def _comments_path(root: str, number: int, page: int) -> str:
    return f"{root}/issues/{number}/comments?per_page=100&page={page}"


def comments_v2(root: str, number: int, cutoff: str | None, fetch=None) -> list[dict]:
    comments = []
    for page in range(1, 101):
        batch = (fetch or api_v2)(_comments_path(root, number, page))
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


def _reference_excerpt(value: str | None, target_number: int | None, limit: int | None = 2000) -> str:
    # Cut only after target lines are gone, so the excerpt cannot bring one back.
    text = clean_v2(value, target_number)
    if limit is None or len(text) <= limit:
        return text
    # Cutting inside a longer number can leave the target number behind; stop before it.
    if target_number is not None and (cut := _target_mention(target_number).search(text[:limit])):
        limit = cut.start()
    return text[:limit] + "\n[Reference truncated; original is longer]"


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


def _tree_path(root: str, sha: str) -> str:
    return f"{root}/git/trees/{sha}?recursive=1"


def _tree(fetch, root: str, sha: str) -> tuple[set[str], dict]:
    tree = fetch(_tree_path(root, sha))
    if tree.get("truncated"):
        raise ValueError("GitHub tree truncated; cannot validate paths")
    return repo_map_v2(tree)


def _head_sha(fetch, root: str) -> str:
    branch = urllib.parse.quote(fetch(root)["default_branch"], safe="")
    return fetch(f"{root}/commits/{branch}")["sha"]


def _parent_sha(fetch, root: str, pr: dict) -> str:
    return fetch(f"{root}/commits/{pr['merge_commit_sha']}")["parents"][0]["sha"]


def _issue_text(fetch, root: str, number: int, issue: dict, target: int | None, cutoff: str | None):
    title = clean_v2(issue["title"], target)
    body = clean_v2(issue.get("body"), target)
    comments = [clean_v2(comment.get("body"), target) for comment in comments_v2(root, number, cutoff, fetch)]
    return title, body, comments, "\n".join([title, body, *comments])


def _reference(fetch, ref: tuple[str, str, int], target: int | None, cutoff: str | None) -> dict | None:
    """One referenced issue or PR, or None when it is the target, missing or past the cutoff."""
    ref_owner, ref_repo, number = ref
    if target is not None and number == target:
        return None
    ref_root = f"repos/{ref_owner}/{ref_repo}"
    try:
        item = fetch(f"{ref_root}/issues/{number}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if cutoff is not None and item["created_at"] >= cutoff:
        return None
    label = f"{ref_owner}/{ref_repo}#{number}"
    if "pull_request" in item:
        detail = fetch(f"{ref_root}/pulls/{number}")
        return {
            "reference": label, "type": "PR", "title": clean_v2(detail["title"], target),
            "body": _reference_excerpt(detail.get("body"), target, REFERENCE_BODY_LIMIT),
            "pointer": f"PR #{number}: {item.get('comments', 0)} comments, {detail.get('changed_files', 0)} files changed (not included)",
        }
    comments = comments_v2(ref_root, number, cutoff, fetch)
    reference = {
        "reference": label, "type": "issue", "title": clean_v2(item["title"], target),
        "body": _reference_excerpt(item.get("body"), target, REFERENCE_BODY_LIMIT),
        "comments": [_reference_excerpt(comment.get("body"), target, REFERENCE_COMMENT_LIMIT)
                     for comment in comments[:REFERENCE_COMMENTS]],
    }
    if len(reference["comments"]) < len(comments):
        reference["pointer"] = f"issue #{number}: first {len(reference['comments'])} of {len(comments)} comments included"
    return reference


def _prefetch(pool: ThreadPoolExecutor, fetch, root: str, issue_number: int, owner: str, repo: str,
              target: int | None, entire_repo: str, commit: str | None = None) -> None:
    """Start the requests build_context is about to make, so its in-order pass finds them ready.

    Results and errors are kept by ``fetch`` and read again by that pass, so nothing here
    decides what the context contains or which error is raised.
    """
    issue_path = f"{root}/issues/{issue_number}"
    target_path = f"{root}/pulls/{target}"

    def tree():
        sha = (commit or _head_sha(fetch, root)) if target is None else _parent_sha(fetch, root, fetch(target_path))
        fetch(_tree_path(root, sha))

    def references():
        cutoff = None if target is None else fetch(target_path)["created_at"]
        context = _issue_text(fetch, root, issue_number, fetch(issue_path), target, cutoff)[3]
        for ref in refs_v2(context, owner, repo):
            pool.submit(_reference, fetch, ref, target, cutoff)

    pool.submit(fetch, issue_path)
    pool.submit(fetch, _comments_path(root, issue_number, 1))
    pool.submit(tree)
    if target is not None and (fork := re.fullmatch(r"gh/([\w.-]+)/([\w.-]+)", entire_repo)):
        pool.submit(_head_sha, fetch, f"repos/{fork.group(1)}/{fork.group(2)}")
    pool.submit(references)


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
    commit: str | None = None,
    issue_override: dict | None = None,
    search_checkout: Path | None = None,
) -> Context:
    issue_match = ISSUE_URL.fullmatch(issue_url)
    if not issue_match:
        raise ValueError("Bad issue URL")
    owner, repo, issue_text_number = issue_match.groups()
    if pr_url is None and entire_repo != f"gh/{owner}/{repo}":
        raise ValueError("Entire repository must match the issue repository (gh/owner/repo)")
    issue_number = int(issue_text_number)
    root = f"repos/{owner}/{repo}"
    pr_match = PR_URL.fullmatch(pr_url) if pr_url is not None else None
    paired = bool(pr_match) and pr_match.group(1, 2) == (owner, repo)
    original_fetch = _Once()
    issue_path = f"{root}/issues/{issue_number}"
    fetch = (lambda path: issue_override if path == issue_path else original_fetch(path)) if issue_override else original_fetch
    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="prism-github")
    try:
        if pr_url is None or paired:
            _prefetch(pool, fetch, root, issue_number, owner, repo,
                      int(pr_match.group(3)) if paired else None, entire_repo, commit)
        ctx = _collect(fetch, root, issue_number, owner, repo, pr_url is not None, pr_match if paired else None,
                       pre_merge_sha, entire_repo, commit)
        ctx.search_commit, ctx.search_checkout = commit or pre_merge_sha, search_checkout
        return ctx
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _collect(fetch, root: str, issue_number: int, owner: str, repo: str, benchmark: bool,
             pr_match: re.Match | None, pre_merge_sha: str | None, entire_repo: str,
             commit: str | None = None) -> Context:
    issue = fetch(f"{root}/issues/{issue_number}")

    target: int | None = None
    cutoff: str | None = None
    if benchmark:
        if not pr_match:
            raise ValueError("Bad issue/PR pair")
        target = int(pr_match.group(3))
        pr = fetch(f"{root}/pulls/{target}")
        if issue["state"] != "closed" or not pr.get("merged_at"):
            raise ValueError("Issue must be closed and PR merged")
        cutoff = pr["created_at"]
        before = _parent_sha(fetch, root, pr)
        if pre_merge_sha is not None and before != pre_merge_sha:
            raise ValueError("Target PR parent does not match pre_merge_sha")
        fork_owner, fork_repo = _github_repo(entire_repo)
        if pre_merge_sha is None and _head_sha(fetch, f"repos/{fork_owner}/{fork_repo}") != before:
            raise ValueError("Entire repository default branch does not match the pre-merge commit")
        known, repo_map = _tree(fetch, root, before)
    else:
        known, repo_map = _tree(fetch, root, commit or _head_sha(fetch, root))

    title, body, issue_comments, issue_context = _issue_text(fetch, root, issue_number, issue, target, cutoff)
    references: list[dict] = []
    safe_refs: set[tuple[str, str, int]] = set()
    for ref in refs_v2(issue_context, owner, repo):
        if reference := _reference(fetch, ref, target, cutoff):
            safe_refs.add(ref)
            references.append(reference)
    paths = sorted(set(re.findall(r"(?<![\w./-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+", issue_context)) & known)
    bundle = Bundle(
        issue={"title": title, "body": body, "comments": issue_comments},
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
