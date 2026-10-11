"""Measure a solve's starting context with Glance, from file contents at one recorded commit.

Glance's `estimate` takes neither a file list nor a commit: it picks files itself by scoring them against a
GitHub issue it fetches, and with no issue it keeps only the first file. So Prism reads its own predicted files
at the commit, writes issue text, paths, separators and contents into one bundle, and has Glance tokenize that.
"""

import base64
import hashlib
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


class _Excluded(Exception):
    """A selected path that cannot be part of the measured context; the message is the reason."""


def _text(raw: bytes) -> str:
    try:
        return raw.decode()
    except UnicodeDecodeError:
        raise _Excluded("not UTF-8 text") from None


def _read(path: str, commit: str, checkout, github_repo, fetch) -> str:
    if checkout is not None:
        # Reads the blob from the object store; the working tree is neither used nor changed.
        result = subprocess.run(["git", "-C", str(checkout), "show", f"{commit}:{path}"], capture_output=True, timeout=60)
        if result.returncode:
            raise _Excluded("missing at the recorded commit")
        return _text(result.stdout)
    try:
        item = fetch(f"repos/{github_repo}/contents/{urllib.parse.quote(path)}?ref={commit}")
        if isinstance(item, list) or item.get("type") != "file":
            raise _Excluded("not a regular file at the recorded commit")
        if not item.get("content") and item.get("size"):
            # The contents API omits bodies over 1 MB; the blob API returns them.
            item = fetch(f"repos/{github_repo}/git/blobs/{item['sha']}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise _Excluded("missing at the recorded commit") from None
        raise
    return _text(base64.b64decode(item["content"]))


def read_files(paths: list[str], commit: str, *, checkout: str | Path | None = None,
               github_repo: str | None = None, fetch=None) -> tuple[dict[str, str], list[dict]]:
    """Contents of every path at the one `commit`, plus the paths left out and why.

    A path that is missing or is not text is reported as excluded, never counted by name alone.
    """
    if (checkout is None) == (github_repo is None):
        raise ValueError("Give exactly one source for file contents: --checkout or --github-repo")
    files, excluded = {}, []
    for path in dict.fromkeys(paths):
        try:
            files[path] = _read(path, commit, checkout, github_repo, fetch or github_context.api_v2)
        except _Excluded as reason:
            excluded.append({"path": path, "reason": str(reason)})
    if not files:
        raise ValueError("No selected file could be read at commit " + commit + "; file names alone are not a context "
                         "measurement" + "".join(f"; {item['path']}: {item['reason']}" for item in excluded))
    return files, excluded


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
    # Glance must have read every byte of the one bundle and nothing else, or the count is not of what Prism sent.
    if (selected != [BUNDLE] or data.get("excluded_files") or data.get("truncated") or data.get("scanned_files") != 1
            or data.get("scanned_bytes") != size or data.get("encoding") != encoding
            or isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0):
        raise GlanceError(f"Glance did not measure the whole context bundle (selected {selected}, "
                          f"excluded {data.get('excluded_files')}, truncated {data.get('truncated')}, scanned "
                          f"{data.get('scanned_files')} file(s) and {data.get('scanned_bytes')} of {size} bytes, "
                          f"encoding {data.get('encoding')})")
    return {
        "context_tokens": tokens,
        "encoding": encoding,
        "glance_schema_version": data.get("schema_version"),
        "bundle_bytes": size,
        "bundle_sha256": hashlib.sha256(text.encode()).hexdigest(),
        # Glance's standard-profile whole-run range, kept for reference only; Prism's estimate does not use it.
        "glance_profile_range_not_used": {"input_tokens": data.get("estimated_input_tokens"),
                                          "output_tokens": data.get("estimated_output_tokens")},
    }


def wrapper(*, glance: str, encoding: str = "o200k_base", run=subprocess.run) -> dict:
    """What `glance estimate` puts around the bundle, sized by measuring an empty bundle."""
    return {"text_before": f"Instructions:\n\n\nFile: {BUNDLE}\n---\n", "text_after": "\n---\n\n",
            "tokens_for_empty_bundle": measure("", glance=glance, encoding=encoding, run=run)["context_tokens"],
            "note": "Glance adds this fixed frame to every count; it is included in the measured tokens, not subtracted."}
