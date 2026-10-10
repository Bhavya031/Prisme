import json
import os
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from .types import Hit


def ensure_ready(repo: str) -> None:
    """Fail before model calls unless Entire reports a ready repository."""
    view_ref = f"/{repo}" if repo.startswith(("gh/", "et/")) else repo
    try:
        result = subprocess.run(
            ["entire", "repo", "view", view_ref, "--json"],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except FileNotFoundError:
        raise RuntimeError("Entire CLI is not installed or is not on PATH") from None
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Entire repository check failed"
        raise RuntimeError(
            f"Cannot use Entire repository {repo}: {detail}. Authenticate with `entire login` or set ENTIRE_TOKEN."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Entire returned invalid repository status for {repo}") from None

    statuses = []
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            statuses.extend(item for key, item in value.items() if key.lower() == "status" and isinstance(item, str))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if not any(status.lower() == "ready" for status in statuses):
        shown = ", ".join(sorted(set(statuses))) or "unknown"
        raise RuntimeError(f"Entire repository {repo} is not ready (status: {shown})")


def _hits(payload: dict) -> list[Hit]:
    return [
        Hit(path=item["path"], line=item["line"], text=item["context_line"])
        for item in payload["results"]
    ]


def _request(url: str) -> dict | list:
    token = os.environ.get("ENTIRE_TOKEN")
    if not token:
        raise RuntimeError("ENTIRE_TOKEN is required for the Entire HTTP transport")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


@lru_cache
def _repo_details(repo: str) -> tuple[str, str]:
    repos = _request("https://us.console.entire.io/api/v1/repos")
    wanted = repo.removeprefix("gh/")
    entries = repos.get("repos", repos) if isinstance(repos, dict) else repos
    for entry in entries:
        name = entry.get("name", entry.get("repo", entry.get("full_name", ""))).removeprefix("gh/")
        if name == wanted:
            return entry["id"], entry["cell"]
    raise RuntimeError(f"Entire repository not found: {repo}")


def _search_http(term: str, repo: str, limit: int) -> list[Hit]:
    repo_id, cell = _repo_details(repo)
    query = urllib.parse.urlencode({"q": term, "max_results": limit, "repo": repo_id})
    return _hits(_request(f"https://{cell}.api.entire.io/api/v1/search/api/search?{query}"))


def search(term: str, repo: str, limit: int = 30) -> list[Hit]:
    if os.environ.get("PRISM_ENTIRE_TRANSPORT") == "http":
        return _search_http(term, repo, limit)
    result = subprocess.run(
        ["entire", "search", "--code", term, "--repo", repo, "--json", "--limit", str(limit)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Entire search failed")
    return _hits(json.loads(result.stdout))


def search_many(terms: list[str], repo: str) -> dict[str, list[Hit]]:
    unique_terms = list(dict.fromkeys(terms))
    with ThreadPoolExecutor(max_workers=len(unique_terms) or 1) as executor:
        hits = executor.map(lambda term: search(term, repo), unique_terms)
        return dict(zip(unique_terms, hits))
