"""Read-only setup checks shared by prism check and prism init."""

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


def _stored_secret(repo: str, name: str, token: str) -> bool:
    try:
        _get(f"https://api.github.com/repos/{repo}/actions/secrets/{name}", {
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "User-Agent": "prism-check"})
        return True
    except (OSError, TimeoutError):
        return False


def inspect(repo: str, config_path: Path) -> list[tuple]:
    """Return (service, purpose, state, words, fix) without exposing credentials."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("--repo must be owner/name")
    rows = []

    def row(name, purpose, state, words, fix=None):
        rows.append((name, purpose, state, words, fix))

    try:
        config = json.loads(config_path.read_text())
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
    except (OSError, ValueError):
        config = None
        row("Config", "Run settings", "BLOCK", "Config missing or unreadable",
            ("create the config", f"Create {config_path} or choose one with --config.",
             GUIDE + "configuration"))

    estimate = config.get("estimate") if config else None
    provider = estimate.get("provider") if isinstance(estimate, dict) else None
    worker_model = estimate.get("worker_model") if isinstance(estimate, dict) else None
    model_name = {"openai": "OpenAI", "claude": "Anthropic"}.get(provider, "Model provider")
    key_name = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(provider)
    key_problem = None
    if key_name is None:
        key_problem = "Provider not configured"
    elif not (key := os.getenv(key_name)):
        key_problem = "Key missing"
    else:
        url = "https://api.openai.com/v1/models" if provider == "openai" else "https://api.anthropic.com/v1/models"
        headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {
            "x-api-key": key, "anthropic-version": "2023-06-01"}
        try:
            _get(url, headers)
        except urllib.error.HTTPError as error:
            key_problem = f"Key rejected (HTTP {error.code})"
        except (OSError, TimeoutError):
            key_problem = "Key check unavailable"
    try:
        solve.worker_prices(worker_model)
        price_problem = False
    except (ValueError, TypeError):
        price_problem = True
    if key_problem or price_problem:
        words = "; ".join(part for part in (key_problem, "Model has no built-in price" if price_problem else None) if part)
        action = (f"Set {key_name} and choose a priced worker model." if key_name and price_problem
                  else f"Set or replace {key_name}." if key_name else "Set estimate.provider to openai or claude.")
        if price_problem and not key_problem:
            action = "Choose a worker model with a built-in price."
        row(model_name, "Predict files and estimate cost", "BLOCK", words,
            ("complete model setup", action, GUIDE + ("how-to-get-model-key" if key_problem else "configuration")))
    else:
        row(model_name, "Predict files and estimate cost", "PASS", "Key and pricing checks passed")

    if not os.getenv("ENTIRE_TOKEN"):
        row("Entire", "Search repository", "BLOCK", "Token missing",
            ("add your token", "Run entire login, then save the result of entire auth token as ENTIRE_TOKEN.",
             GUIDE + "how-to-get-entire"))
    else:
        try:
            entire.ensure_ready(f"gh/{repo}", timeout=TIMEOUT)
            row("Entire", "Search repository", "PASS", "Token and repository ready")
        except (OSError, RuntimeError) as error:
            detail = str(error).lower()
            if "401" in detail or "expired" in detail:
                row("Entire", "Search repository", "BLOCK", "Token expired",
                    ("refresh your token", "Run entire auth token and replace the ENTIRE_TOKEN Actions secret.",
                     GUIDE + "how-to-get-entire"))
            elif "not ready" in detail:
                row("Entire", "Search repository", "BLOCK", "Repository not ready",
                    ("index the repository", f"Add and index {repo} in Entire.",
                     GUIDE + "how-to-get-entire"))
            else:
                row("Entire", "Search repository", "BLOCK", "Repository access failed",
                    ("check repository access", "Sign in to Entire and confirm the repository mirror is ready.",
                     GUIDE + "how-to-get-entire"))

    cfo = config.get("cfo") if config else None
    missing_config = [key for key in cfo_actions.CFO_KEYS if not isinstance(cfo, dict)
                      or not isinstance(cfo.get(key), str) or not cfo[key]]
    pending = config is not None and '"PENDING"' in json.dumps(config)
    writer = os.getenv("CFO_SECRET_WRITE_TOKEN")
    stored_client = bool(writer and _stored_secret(repo, "CFO_OAUTH_CLIENT_ID", writer))
    stored_refresh = bool(writer and _stored_secret(repo, "CFO_OAUTH_REFRESH_TOKEN", writer))
    client_id = bool(os.getenv("CFO_OAUTH_CLIENT_ID")) or stored_client
    if missing_config or pending or not client_id:
        words = ("Sign-in required" if not client_id else "Config incomplete")
        if missing_config or pending:
            words += "; config incomplete" if not client_id else ""
        row("CFO.ai", "Allocate budget", "BLOCK", words,
            ("complete sign-in and tables", "Complete the CFO.ai table settings and run prism cfo-login.",
             GUIDE + "how-to-get-cfo-ai"))
    else:
        words = ("Sign-in stored on GitHub; tested by a real run" if stored_client and stored_refresh
                 else "Client ID configured; sign-in tested by a real run")
        row("CFO.ai", "Allocate budget", "WARN", words)

    repos = cfo.get("shared_login_repos", [repo]) if isinstance(cfo, dict) else None
    if (not isinstance(repos, list) or not repos or repo not in repos
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos)
            or len(repos) != len(set(repos))):
        row("GitHub", "Repository access and secret updates", "BLOCK", "Shared repository list invalid",
            ("fix shared repositories", f"List each shared repository once and include {repo}.",
             GUIDE + "configuration"))
    elif not writer:
        row("GitHub", "Repository access and secret updates", "BLOCK", "Secret-write token missing",
            ("add a secret-write token", "Save CFO_SECRET_WRITE_TOKEN for every shared repository.",
             GUIDE + "how-to-get-github-token"))
    else:
        for target in repos:
            try:
                _get(f"https://api.github.com/repos/{target}/actions/secrets/public-key", {
                    "Authorization": f"Bearer {writer}", "Accept": "application/vnd.github+json",
                    "User-Agent": "prism-check"})
            except (OSError, TimeoutError):
                row("GitHub", "Repository access and secret updates", "BLOCK", f"Cannot read {target}",
                    ("grant secret access", f"Grant Actions secrets access to {target}.",
                     GUIDE + "how-to-get-github-token"))
                break
        else:
            row("GitHub", "Repository access and secret updates", "WARN", "Read verified; write untested")

    if os.getenv("NEATLOGS_API_KEY"):
        row("NeatLogs", "Trace agent activity", "WARN", "Key present; connection unverified")
    else:
        row("NeatLogs", "Trace agent activity", "MISSING", "Disabled")
    try:
        glance.locate()
        row("Glance", "Count tokens locally", "WARN", "Installed; counting untested")
    except glance.GlanceError:
        row("Glance", "Count tokens locally", "MISSING", "Not installed")
    return rows


def show(rows: list[tuple], repo: str, config_path: Path) -> bool:
    terminal = sys.stdout.isatty()
    color = terminal and "NO_COLOR" not in os.environ

    def style(value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if color else value

    def status(state: str, words: str) -> str:
        icon, code = {"PASS": ("🟢", "32"), "BLOCK": ("🔴", "31"),
                      "WARN": ("🟡", "33"), "MISSING": ("⚪", "90")}[state]
        return f"{icon} {style(words, code)}"

    blockers = [name for name, _, state, _, _ in rows if state == "BLOCK"]
    if blockers:
        names = ", ".join(blockers[:-1]) + (" and " if len(blockers) > 1 else "") + blockers[-1]
        headline = status("BLOCK", f"Not ready — fix {names} before running")
    else:
        headline = status("PASS", "Ready to run")
    print("Prism · Setup check")
    print(f"Repository: {repo}")
    print(headline)
    print()
    print(f"{'Service':<17} | {'Purpose':<37} | Status")
    for name, purpose, state, words, _ in rows:
        print(f"{style(f'{name:<17}', '1')} | {purpose:<37} | {status(state, words)}")
        if name in ("OpenAI", "Anthropic", "Model provider"):
            try:
                model = json.loads(config_path.read_text()).get("estimate", {}).get("worker_model")
            except (OSError, ValueError, AttributeError):
                model = None
            print(f"  {style(str(model or 'No model selected'), '2')}")
    try:
        estimate = json.loads(config_path.read_text()).get("estimate", {})
    except (OSError, ValueError, AttributeError):
        estimate = {}
    if not isinstance(estimate, dict):
        estimate = {}
    provider = {"openai": "OpenAI", "claude": "Anthropic"}.get(estimate.get("provider"), "Not set")
    cap = estimate.get("max_cost_usd")
    cap_text = f"${cap:g}" if isinstance(cap, (int, float)) else "Not set"
    print("\nConfigured")
    print(f"  Provider: {provider}")
    print(f"  Model: {estimate.get('worker_model') or 'Not set'}")
    print(f"  Spending cap per estimate: {cap_text} (configured, not a verified balance)")
    if blockers:
        print()
        for index, (name, _, state, _, fix) in enumerate((row for row in rows if row[2] == "BLOCK"), 1):
            title, action, guide = fix
            print(style(f"{index}. {name} — {title}", "1"))
            print(action)
            print(guide)
            if index < len(blockers):
                print()
    return not blockers


def run(repo: str, config_path: Path) -> bool:
    return show(inspect(repo, config_path), repo, config_path)
