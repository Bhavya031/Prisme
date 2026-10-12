"""Read-only checks for credentials used by the estimate and allocation runs."""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from . import cfo_actions, entire, glance, solve


TIMEOUT = 15


def _get(url: str, headers: dict) -> None:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        response.read(1)


def run(repo: str, config_path: Path) -> bool:
    failed = False

    def line(name: str, status: str, reason: str = "") -> None:
        nonlocal failed
        print(f"{name}: {status}" + (f": {reason}" if reason else ""))
        failed |= status == "FAILED"

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("--repo must be owner/name")
    try:
        config = json.loads(config_path.read_text())
        if not isinstance(config, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError) as error:
        config = None
        line("Config", "FAILED", f"cannot read {config_path}: {type(error).__name__}")

    if not os.getenv("ENTIRE_TOKEN"):
        line("Entire", "FAILED", "ENTIRE_TOKEN is missing")
    else:
        try:
            entire.ensure_ready(f"gh/{repo}", timeout=TIMEOUT)
            line("Entire", "OK")
        except (OSError, RuntimeError) as error:
            detail = str(error).lower()
            if "401" in detail or "expired" in detail:
                line("Entire", "FAILED", f"token expired; fix: entire auth token | gh secret set ENTIRE_TOKEN -R {repo}")
            elif "not ready" in detail:
                line("Entire", "FAILED", "repository is not ready")
            elif "not found" in detail:
                line("Entire", "FAILED", "repository not found")
            else:
                line("Entire", "FAILED", "repository or credential check failed; check Entire login and repository access")

    estimate = config.get("estimate") if config else None
    provider = estimate.get("provider") if isinstance(estimate, dict) else None
    if provider in ("openai", "claude"):
        key_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        key = os.getenv(key_name)
        if not key:
            line("Model provider", "FAILED", f"{key_name} is missing")
        else:
            url = "https://api.openai.com/v1/models" if provider == "openai" else "https://api.anthropic.com/v1/models"
            headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {
                "x-api-key": key, "anthropic-version": "2023-06-01"}
            try:
                _get(url, headers)
                line("Model provider", "OK")
            except urllib.error.HTTPError as error:
                line("Model provider", "FAILED", f"{key_name} rejected (HTTP {error.code})")
            except (OSError, TimeoutError) as error:
                line("Model provider", "FAILED", f"validation unavailable ({type(error).__name__})")
    else:
        line("Model provider", "FAILED", "estimate.provider must be openai or claude" if config else "config unavailable")

    worker_model = estimate.get("worker_model") if isinstance(estimate, dict) else None
    try:
        solve.worker_prices(worker_model)
        line("Worker model price", "OK")
    except (ValueError, TypeError):
        line("Worker model price", "FAILED", "estimate.worker_model has no built-in price")

    line("NeatLogs", "SKIPPED", "key present; installed client has no read-only validation" if os.getenv("NEATLOGS_API_KEY")
         else "key absent; optional tracing disabled")

    cfo = config.get("cfo") if config else None
    pending = config is not None and '"PENDING"' in json.dumps(config)
    missing_config = [key for key in cfo_actions.CFO_KEYS if not isinstance(cfo, dict)
                      or not isinstance(cfo.get(key), str) or not cfo[key]]
    missing = [key for key in ("CFO_OAUTH_CLIENT_ID", "CFO_OAUTH_REFRESH_TOKEN") if not os.getenv(key)]
    if not isinstance(cfo, dict):
        line("CFO.ai", "FAILED", "cfo config unavailable; refresh token not verified because checking it would use it up")
    elif pending or missing or missing_config:
        line("CFO.ai", "FAILED", f"missing credentials: {', '.join(missing) or 'none'}; missing cfo config: {', '.join(missing_config) or 'none'}; PENDING value: {'yes' if pending else 'no'}; refresh token not verified because checking it would use it up")
    else:
        line("CFO.ai", "OK", "refresh token not verified because checking it would use it up")

    repos = cfo.get("shared_login_repos", [repo]) if isinstance(cfo, dict) else None
    if (not isinstance(repos, list) or not repos or repo not in repos
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos)
            or len(repos) != len(set(repos))):
        line("GitHub secret write", "FAILED", "cfo.shared_login_repos must list unique owner/name values including --repo")
    elif not (token := os.getenv("CFO_SECRET_WRITE_TOKEN")):
        line("GitHub secret write", "FAILED", "CFO_SECRET_WRITE_TOKEN is missing")
    else:
        for target in repos:
            try:
                _get(f"https://api.github.com/repos/{target}/actions/secrets/public-key", {
                    "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                    "User-Agent": "prism-check"})
            except urllib.error.HTTPError as error:
                line("GitHub secret write", "FAILED", f"cannot read Actions secrets public key for {target} (HTTP {error.code})")
                break
            except (OSError, TimeoutError) as error:
                line("GitHub secret write", "FAILED", f"cannot read Actions secrets public key for {target} ({type(error).__name__})")
                break
        else:
            line("GitHub secret write", "OK", "public keys readable; write permission not verified")

    try:
        glance.locate()
        line("Glance", "OK")
    except glance.GlanceError:
        line("Glance", "SKIPPED", "binary not found; install Glance or set PRISM_GLANCE before a run")
    return not failed
