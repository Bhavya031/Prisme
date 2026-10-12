"""Read-only checks for credentials used by the estimate and allocation runs."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import cfo_actions, entire, glance, solve


TIMEOUT = 15
GUIDE = "https://github.com/Bhavya031/Prisme/blob/main/docs/SETUP.md#"


def _get(url: str, headers: dict) -> None:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read(1)


def inspect(repo: str, config_path: Path) -> list[tuple[str, str, str, str, str]]:
    """Return (name, purpose, status, detail, fix); no credential values leave this function."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("--repo must be owner/name")
    rows = []

    def row(name, purpose, status, detail="", fix=""):
        rows.append((name, purpose, status, detail, fix))

    try:
        config = json.loads(config_path.read_text())
        if not isinstance(config, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError):
        config = None
        row("Config", "run settings", "FAILED", f"cannot read {config_path}",
            f"Create or select a Prism config. {GUIDE}configuration")

    if not os.getenv("ENTIRE_TOKEN"):
        row("Entire", "repository search", "FAILED", "token missing",
            f"Sign in and save ENTIRE_TOKEN. {GUIDE}how-to-get-entire")
    else:
        try:
            entire.ensure_ready(f"gh/{repo}", timeout=TIMEOUT)
            row("Entire", "repository search", "OK")
        except (OSError, RuntimeError) as error:
            detail = str(error).lower()
            if "401" in detail or "expired" in detail:
                row("Entire", "repository search", "FAILED", "token expired",
                    f"Refresh with entire auth token and save ENTIRE_TOKEN. {GUIDE}how-to-get-entire")
            elif "not ready" in detail:
                row("Entire", "repository search", "FAILED", "repository not ready",
                    f"Add and index {repo} in Entire. {GUIDE}how-to-get-entire")
            else:
                row("Entire", "repository search", "FAILED", "cannot access repository",
                    f"Check Entire login and repository access. {GUIDE}how-to-get-entire")

    estimate = config.get("estimate") if config else None
    provider = estimate.get("provider") if isinstance(estimate, dict) else None
    if provider in ("openai", "claude"):
        key_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        key = os.getenv(key_name)
        if not key:
            row("Model key", "file prediction", "FAILED", f"{key_name} missing",
                f"Create and save {key_name}. {GUIDE}how-to-get-model-key")
        else:
            url = "https://api.openai.com/v1/models" if provider == "openai" else "https://api.anthropic.com/v1/models"
            headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {
                "x-api-key": key, "anthropic-version": "2023-06-01"}
            try:
                _get(url, headers)
                row("Model key", "file prediction", "OK")
            except urllib.error.HTTPError as error:
                row("Model key", "file prediction", "FAILED", f"key rejected (HTTP {error.code})",
                    f"Replace {key_name}. {GUIDE}how-to-get-model-key")
            except (OSError, TimeoutError):
                row("Model key", "file prediction", "FAILED", "could not verify key",
                    f"Check network access, then retry. {GUIDE}how-to-get-model-key")
    else:
        row("Model key", "file prediction", "FAILED", "provider not configured",
            f"Set estimate.provider to openai or claude. {GUIDE}configuration")

    worker_model = estimate.get("worker_model") if isinstance(estimate, dict) else None
    try:
        solve.worker_prices(worker_model)
        row("Model price", "cost estimate", "OK")
    except (ValueError, TypeError):
        row("Model price", "cost estimate", "FAILED", "no built-in price",
            f"Choose a built-in estimate.worker_model. {GUIDE}configuration")

    row("NeatLogs", "optional tracing", "SKIPPED",
        "key present; no read-only key check" if os.getenv("NEATLOGS_API_KEY") else "optional key absent")

    cfo = config.get("cfo") if config else None
    missing = [key for key in ("CFO_OAUTH_CLIENT_ID", "CFO_OAUTH_REFRESH_TOKEN") if not os.getenv(key)]
    missing_config = [key for key in cfo_actions.CFO_KEYS if not isinstance(cfo, dict)
                      or not isinstance(cfo.get(key), str) or not cfo[key]]
    pending = config is not None and '"PENDING"' in json.dumps(config)
    if missing or missing_config or pending:
        problems = []
        if missing:
            problems.append("login missing")
        if missing_config:
            problems.append("table settings missing")
        if pending:
            problems.append("PENDING setting")
        row("CFO.ai", "budget allocation", "FAILED", ", ".join(problems) + "; login tested only by a real run",
            f"Set up tables and run prism cfo-login. {GUIDE}how-to-get-cfo-ai")
    else:
        row("CFO.ai", "budget allocation", "OK", "login tested only by a real run")

    repos = cfo.get("shared_login_repos", [repo]) if isinstance(cfo, dict) else None
    if (not isinstance(repos, list) or not repos or repo not in repos
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos)
            or len(repos) != len(set(repos))):
        row("GitHub token", "secret rotation", "FAILED", "shared repository list invalid",
            f"List unique owner/name repositories including {repo}. {GUIDE}configuration")
    elif not (token := os.getenv("CFO_SECRET_WRITE_TOKEN")):
        row("GitHub token", "secret rotation", "FAILED", "write token missing",
            f"Create and save CFO_SECRET_WRITE_TOKEN. {GUIDE}how-to-get-github-token")
    else:
        for target in repos:
            try:
                _get(f"https://api.github.com/repos/{target}/actions/secrets/public-key", {
                    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                    "User-Agent": "prism-check"})
            except urllib.error.HTTPError as error:
                row("GitHub token", "secret rotation", "FAILED", f"cannot read {target} (HTTP {error.code})",
                    f"Grant Secrets access to {target}. {GUIDE}how-to-get-github-token")
                break
            except (OSError, TimeoutError):
                row("GitHub token", "secret rotation", "FAILED", f"cannot reach {target}",
                    f"Check network access, then retry. {GUIDE}how-to-get-github-token")
                break
        else:
            row("GitHub token", "secret rotation", "OK", "read access confirmed; write access untested")

    try:
        glance.locate()
        row("Glance", "token counting", "OK")
    except glance.GlanceError:
        row("Glance", "token counting", "SKIPPED", "binary absent; needed for local estimates")
    return rows


def show(rows: list[tuple[str, str, str, str, str]]) -> bool:
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    icons = {"OK": ("✓", "32"), "FAILED": ("✗", "31"), "SKIPPED": ("–", "33")}
    for name, purpose, status, detail, fix in rows:
        icon, code = icons[status]
        mark = f"\033[{code}m{icon}\033[0m" if color else icon
        print(f"{mark} {name:<13} {purpose:<20} {status}" + (f" — {detail}" if detail else ""))
        if status == "FAILED":
            print(f"  Fix: {fix}")
    count = sum(status == "FAILED" for _, _, status, _, _ in rows)
    print("Ready to run" if not count else f"{count} thing{'s' if count != 1 else ''} to fix before a run")
    return count == 0


def env_notice() -> None:
    names = ("ENTIRE_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NEATLOGS_API_KEY",
             "CFO_OAUTH_CLIENT_ID", "CFO_OAUTH_REFRESH_TOKEN", "CFO_SECRET_WRITE_TOKEN")
    if not any(os.getenv(name) for name in names) and not Path(".env").is_file():
        print("No .env found in this folder. Prism reads tokens from .env or the environment.\n")


def run(repo: str, config_path: Path) -> bool:
    env_notice()
    return show(inspect(repo, config_path))
