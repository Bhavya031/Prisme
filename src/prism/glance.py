"""Measure a solve's starting context with Glance, from file contents at one recorded commit.

Glance's `estimate` takes neither a file list nor a commit: it picks files itself by scoring them against a
GitHub issue it fetches, and with no issue it keeps only the first file. So Prism reads its own predicted files
at the commit, writes issue text, paths, separators and contents into one bundle, and has Glance tokenize that.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

from . import github_context

ENV = "PRISM_GLANCE"
BUNDLE = "prism-context.txt"
ENCODINGS = ("o200k_base", "cl100k_base")
_SHA = re.compile(r"[0-9a-f]{40}")
# Glance's own scan limits; the bundle is one file, so both are raised to fit it.
_GLANCE_MAX_FILE_BYTES = 1 << 20
_GLANCE_MAX_BYTES = 32 << 20


class GlanceError(RuntimeError):
    """Glance is missing, failed, or did not measure exactly the bundle Prism gave it."""


def locate(explicit: str | None = None) -> str:
    """Path of the Glance binary: the --glance value, then PRISM_GLANCE, then `glance` on PATH."""
    for source, value in (("--glance", explicit), (ENV, os.environ.get(ENV))):
        if value:
            if os.path.isfile(value) and os.access(value, os.X_OK):
                return value
            raise GlanceError(f"Glance binary from {source} is not an executable file: {value}")
    if found := shutil.which("glance"):
        return found
    raise GlanceError(
        f"Glance not found. Pass --glance /path/to/glance, set {ENV}, or put `glance` on PATH "
        "(go install github.com/Bhavya031/Glance/cmd/glance@latest). "
        "Prism does not fall back to byte counts or file names for the context measurement.")


def resolve_commit(commit: str, checkout: str | Path | None = None) -> str:
    """The full SHA the context is read at. Without a checkout the SHA must already be complete."""
    if checkout is None:
        if not _SHA.fullmatch(commit):
            raise ValueError("--commit must be a full 40-character SHA when reading from GitHub")
        return commit
    result = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"Commit {commit} not found in checkout {checkout}")
    return result.stdout.strip()


def _text(raw: bytes, path: str) -> str:
    try:
        return raw.decode()
    except UnicodeDecodeError:
        raise ValueError(f"Selected file is not UTF-8 text and cannot be counted: {path}") from None


def read_files(paths: list[str], commit: str, *, checkout: str | Path | None = None,
               github_repo: str | None = None, fetch=None) -> dict[str, str]:
    """Contents of each path at `commit`, from a local checkout's object store or the GitHub API."""
    if (checkout is None) == (github_repo is None):
        raise ValueError("Give exactly one source for file contents: --checkout or --github-repo")
    if not paths:
        raise ValueError("No files selected; file names alone are not a context measurement")
    files = {}
    for path in dict.fromkeys(paths):
        if checkout is not None:
            # Reads the blob from the object store; the working tree is neither used nor changed.
            result = subprocess.run(["git", "-C", str(checkout), "show", f"{commit}:{path}"], capture_output=True, timeout=60)
            if result.returncode:
                raise ValueError(f"{path} does not exist at commit {commit}")
            files[path] = _text(result.stdout, path)
            continue
        fetch = fetch or github_context.api_v2
        try:
            item = fetch(f"repos/{github_repo}/contents/{urllib.parse.quote(path)}?ref={commit}")
            if isinstance(item, list) or item.get("type") != "file":
                raise ValueError(f"{path} is not a file at commit {commit}")
            if not item.get("content") and item.get("size"):
                # The contents API omits bodies over 1 MB; the blob API returns them.
                item = fetch(f"repos/{github_repo}/git/blobs/{item['sha']}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError(f"{path} does not exist in {github_repo} at commit {commit}") from None
            raise
        files[path] = _text(base64.b64decode(item["content"]), path)
    return files


def assemble(issue_text: str, files: dict[str, str]) -> str:
    """The measured text: the issue, then every file's path, separator and exact contents."""
    parts = ["Issue:\n", issue_text.strip(), "\n\n"]
    for path, content in files.items():
        parts += ["File: ", path, "\n---\n", content, "\n---\n\n"]
    return "".join(parts)


def measure(text: str, *, glance: str, encoding: str = "o200k_base", run=subprocess.run) -> dict:
    """Token count of `text` as Glance tokenizes it; raises unless Glance counted exactly this bundle."""
    if encoding not in ENCODINGS:
        raise ValueError(f"Glance supports only these encodings: {', '.join(ENCODINGS)}")
    size = len(text.encode())
    with tempfile.TemporaryDirectory(prefix="prism-glance-") as folder:
        bundle_dir = Path(folder, "context")
        bundle_dir.mkdir()
        (bundle_dir / BUNDLE).write_text(text)
        config = Path(folder, "glance.json")
        config.write_text(json.dumps({"max_file_bytes": max(_GLANCE_MAX_FILE_BYTES, size + 1),
                                      "max_bytes": max(_GLANCE_MAX_BYTES, 2 * size + 4096)}))
        command = [glance, "estimate", "--json", "--encoding", encoding, "--config", str(config), str(bundle_dir)]
        try:
            # Glance's own config and records stay untouched; no issue or repo is named, so it makes no network call.
            result = run(command, capture_output=True, text=True, timeout=300,
                         env={**os.environ, "GLANCE_CONFIG": str(config), "GLANCE_RECORDS": str(Path(folder, "records.jsonl"))})
        except FileNotFoundError:
            raise GlanceError(f"Glance binary not found: {glance}") from None
        except subprocess.TimeoutExpired:
            raise GlanceError("Glance timed out measuring the context") from None
    if result.returncode:
        raise GlanceError(f"Glance failed: {(result.stderr or result.stdout).strip()[:500]}")
    try:
        data = json.loads(result.stdout)
        selected = [item["path"] for item in data["selected_files"]]
        tokens = data["context_tokens"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise GlanceError("Glance returned output Prism cannot read; expected `glance estimate --json` schema 2") from None
    if selected != [BUNDLE] or data.get("excluded_files") or data.get("truncated") or not isinstance(tokens, int) or tokens <= 0:
        raise GlanceError(f"Glance did not measure the whole context bundle (selected {selected}, "
                          f"excluded {data.get('excluded_files')}, truncated {data.get('truncated')})")
    return {
        "context_tokens": tokens,
        "encoding": data.get("encoding", encoding),
        "glance_schema_version": data.get("schema_version"),
        "context_bytes": size,
        # Glance's standard-profile whole-run range, kept for reference only; Prism's estimate does not use it.
        "glance_profile_range_not_used": {"input_tokens": data.get("estimated_input_tokens"),
                                          "output_tokens": data.get("estimated_output_tokens")},
    }
