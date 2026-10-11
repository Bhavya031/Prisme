import json
import os
import subprocess
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from . import results
from .types import Hit


def ensure_ready(repo: str) -> None:
    """Fail before model calls unless Entire reports a ready repository."""
    if os.environ.get("PRISM_ENTIRE_TRANSPORT") == "http":
        payload = _repo_details(repo)
    else:
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
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Entire repository check timed out for {repo}") from None
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "Entire repository check failed"
            raise RuntimeError(
                f"Cannot use Entire repository {repo}: {detail}. Authenticate with `entire login` or set ENTIRE_TOKEN."
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Entire returned invalid repository status for {repo}") from None

    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, str) or status.lower() != "ready":
        raise RuntimeError(f"Entire repository {repo} is not ready (status: {status or 'unknown'})")


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
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError("Entire HTTP authentication failed; check ENTIRE_TOKEN") from None
        raise RuntimeError(f"Entire HTTP request failed with status {error.code}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Entire HTTP request failed: {error.reason}") from None
    except TimeoutError:
        raise RuntimeError("Entire HTTP request timed out") from None
    except json.JSONDecodeError:
        raise RuntimeError("Entire HTTP response was not valid JSON") from None


@lru_cache
def _repo_details(repo: str) -> dict:
    repos = _request("https://us.console.entire.io/api/v1/repos")
    wanted = repo.removeprefix("gh/")
    entries = repos.get("repos", repos) if isinstance(repos, dict) else repos
    for entry in entries:
        name = entry.get("name", entry.get("repo", entry.get("full_name", ""))).removeprefix("gh/")
        if name == wanted:
            return entry
    raise RuntimeError(f"Entire repository not found: {repo}")


def _search_http(term: str, repo: str, limit: int) -> list[Hit]:
    details = _repo_details(repo)
    query = urllib.parse.urlencode({"q": term, "max_results": limit, "repo": details["id"]})
    return _hits(_request(f"https://{details['cell']}.api.entire.io/api/v1/search/api/search?{query}"))


def search(term: str, repo: str, limit: int = 30) -> list[Hit]:
    if os.environ.get("PRISM_ENTIRE_TRANSPORT") == "http":
        return _search_http(term, repo, limit)
    try:
        result = subprocess.run(
            # Terms go after "--" so one starting with "-" is not read as a flag.
            ["entire", "search", "--code", "--repo", repo, "--json", "--limit", str(limit), "--", term],
            capture_output=True,
            text=True,
            timeout=45,
        )
        # Entire's own error text is shown, so scrub anything credential-shaped from it.
        detail = (results.redact(result.stderr.strip()) or "no error output") if result.returncode else None
        hits = None if detail else _hits(json.loads(result.stdout))
    except FileNotFoundError:
        detail = "the Entire CLI is not installed or is not on PATH"
    except subprocess.TimeoutExpired:
        detail = "the search timed out"
    except (ValueError, KeyError, TypeError):
        detail = "the search returned an unexpected response"
    if detail:
        raise RuntimeError(
            f"Entire search failed for {repo}: {detail}. Check that ENTIRE_TOKEN is current and that "
            f"`entire repo view /{repo} --json` reports ready. Git search was not used as a fallback."
        )
    return hits


def search_many(terms: list[str], repo: str) -> dict[str, list[Hit]]:
    unique_terms = list(dict.fromkeys(terms))
    with ThreadPoolExecutor(max_workers=len(unique_terms) or 1) as executor:
        hits = executor.map(lambda term: search(term, repo), unique_terms)
        return dict(zip(unique_terms, hits))


def check_checkout(checkout: Path, commit: str) -> None:
    """Refuse a pinned prediction before any model call if its source commit is unavailable."""
    try:
        result = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", f"{commit}^{{commit}}"],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode:
        raise ValueError(f"Pinned search commit {commit} is unavailable in {checkout}")


def search_many_at_commit(terms: list[str], checkout: Path, commit: str,
                          paths: list[str] | None = None) -> dict[str, list[Hit]]:
    """Search only Git objects at the pinned commit; the current Entire index may be newer.

    ``paths`` limits the search to those files; an empty list searches nothing.
    """
    if paths is not None and not paths:
        return {}
    grouped = {}
    for term in dict.fromkeys(terms):
        try:
            result = subprocess.run(
                ["git", "--literal-pathspecs", "-C", str(checkout), "grep", "-n", "-z", "-i", "-I", "-F", "-m", "2",
                 "-e", term, commit, "--", *(paths or [])], capture_output=True, text=True, timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Pinned source search failed") from None
        if result.returncode not in (0, 1):
            raise RuntimeError("Pinned source search failed")
        hits = []
        for row in result.stdout.splitlines():
            path, line, value = row.split("\0", 2)
            hits.append(Hit(path=path.removeprefix(f"{commit}:"), line=int(line), text=value))
            if len(hits) == 30:
                break
        grouped[term] = hits
    return grouped
