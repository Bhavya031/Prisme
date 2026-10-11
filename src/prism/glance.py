"""Measure a solve's starting context with Glance, from file contents at one recorded commit.

Prism owns file selection and context assembly; Glance owns token counting. Prism reads its predicted files at
the commit, writes issue text, paths, separators and contents into one bundle, and pipes exactly those bytes to
`glance count --json --encoding <name> -`. Glance adds nothing, reads no repository or config and uses no network.
Prism accepts the count only when the byte length and SHA-256 Glance reports match its own bundle. There is no
fallback to `glance estimate`, byte counts or file names.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path

from . import github_context

ENV = "PRISM_GLANCE"
ENCODINGS = ("o200k_base", "cl100k_base")
COUNT_SCHEMA = "1"
# `glance count` refuses larger input.
MAX_BUNDLE_BYTES = 8 << 20
_SHA = re.compile(r"[0-9a-f]{40}")


class GlanceError(RuntimeError):
    """Glance is missing, has no usable `count` command, or did not count exactly the bytes Prism sent."""


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
        # `cat-file blob` fails on a directory, where `git show` would print a listing that looks like content.
        result = subprocess.run(["git", "-C", str(checkout), "cat-file", "blob", f"{commit}:{path}"],
                                capture_output=True, timeout=60)
        if result.returncode:
            raise _Excluded("not a file at the recorded commit")
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
    """Token count of exactly `text` from `glance count`; raises unless Glance counted these very bytes."""
    if encoding not in ENCODINGS:
        raise ValueError(f"Glance supports only these encodings: {', '.join(ENCODINGS)}")
    try:
        data = text.encode()
    except UnicodeEncodeError:
        raise ValueError("The issue text is not valid UTF-8 and cannot be counted") from None
    if len(data) > MAX_BUNDLE_BYTES:
        raise GlanceError(f"The context bundle is {len(data)} bytes, over the {MAX_BUNDLE_BYTES}-byte limit of "
                          "`glance count`. Select fewer files; Prism does not truncate the measured context.")
    digest = hashlib.sha256(data).hexdigest()
    try:
        result = run([glance, "count", "--json", "--encoding", encoding, "-"], input=data, capture_output=True, timeout=300)
    except FileNotFoundError:
        raise GlanceError(f"Glance binary not found: {glance}") from None
    except subprocess.TimeoutExpired:
        raise GlanceError("glance count timed out measuring the context") from None
    if result.returncode:
        detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()[:500]
        # Glance's wording for a missing subcommand is not relied on: every failure carries the upgrade hint.
        raise GlanceError(f"glance count failed: {detail or 'no output'}. If this Glance ({glance}) predates the `count` "
                          "command, upgrade it (go install github.com/Bhavya031/Glance/cmd/glance@latest); Prism does not "
                          "fall back to `glance estimate`, byte counts or file names.")
    try:
        found = json.loads(result.stdout)
        schema, version, used, tokens, size, reported = (found[key] for key in (
            "schema_version", "glance_version", "encoding", "context_tokens", "input_bytes", "input_sha256"))
    except (ValueError, KeyError, TypeError):
        raise GlanceError(f"glance count returned output Prism cannot read; expected schema {COUNT_SCHEMA} JSON") from None
    if schema != COUNT_SCHEMA:
        raise GlanceError(f"glance count schema {schema!r} is not the supported schema {COUNT_SCHEMA!r}")
    if used != encoding:
        raise GlanceError(f"glance count used encoding {used!r}, not the requested {encoding!r}")
    if size != len(data) or reported != digest:
        raise GlanceError(f"glance count measured different input than Prism sent: {size} bytes, sha256 {reported}; "
                          f"expected {len(data)} bytes, sha256 {digest}")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise GlanceError(f"glance count returned an invalid token count: {tokens!r}")
    return {"context_tokens": tokens, "encoding": encoding, "glance_schema_version": schema, "glance_version": version,
            "input_bytes": len(data), "input_sha256": digest}
