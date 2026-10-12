"""Read-only setup checks shared by prism check and prism init."""

import json
import itertools
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import cfo_actions, entire, glance, solve


TIMEOUT = 15
GUIDE = "https://github.com/Bhavya031/Prisme/blob/main/docs/SETUP.md#"
BANNER = """██████╗  ██████╗  ██╗ ███████╗ ███╗   ███╗
██╔══██╗ ██╔══██╗ ██║ ██╔════╝ ████╗ ████║
██████╔╝ ██████╔╝ ██║ ███████╗ ██╔████╔██║
██╔═══╝  ██╔══██╗ ██║ ╚════██║ ██║╚██╔╝██║
██║      ██║  ██║ ██║ ███████║ ██║ ╚═╝ ██║
╚═╝      ╚═╝  ╚═╝ ╚═╝ ╚══════╝ ╚═╝     ╚═╝"""
SPECTRUM = ((255, 95, 109), (255, 166, 87), (255, 221, 89),
            (117, 221, 114), (101, 220, 222), (187, 148, 255))


def paint(value: str, code: str) -> str:
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033[{code}m{value}\033[0m"
    return value


def link(url: str) -> str:
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        return f"\033]8;;{url}\033\\\033[1;94;4m{url}\033[0m\033]8;;\033\\"
    return url


def spectrum_code(index: int) -> str:
    if "truecolor" in os.getenv("COLORTERM", "").lower() or "24bit" in os.getenv("COLORTERM", "").lower():
        return "1;38;2;" + ";".join(map(str, SPECTRUM[index]))
    if "256color" in os.getenv("TERM", ""):
        return f"1;38;5;{(203, 215, 227, 120, 80, 177)[index]}"
    return f"1;{(91, 33, 93, 92, 96, 95)[index]}"


def banner() -> None:
    width = max(map(len, BANNER.splitlines()))
    for line in BANNER.splitlines():
        print("".join(paint(line[index * width // 6:(index + 1) * width // 6], spectrum_code(index))
                      for index in range(6)))
    print(paint("automated estimation and allocation", "1"))


def status_text(state: str, words: str) -> str:
    icon, code = {"PASS": ("🟢", "32"), "BLOCK": ("🔴", "31"),
                  "WARN": ("🟡", "33"), "MISSING": ("⚪", "90")}[state]
    return f"{icon} {paint(words, code)}"


def gh_env() -> dict:
    env = os.environ.copy()
    dotenv = {}
    if not env.get("PRISM_SKIP_DOTENV") and Path(".env").is_file():
        for line in Path(".env").read_text().splitlines():
            name, separator, value = line.partition("=")
            name = name.strip().removeprefix("export ").strip()
            if separator and name in ("GH_TOKEN", "GITHUB_TOKEN"):
                dotenv[name] = value.strip().strip("\"'")
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if env.get(name) and (env[name] == dotenv.get(name) or env[name] == env.get("CFO_SECRET_WRITE_TOKEN")):
            env.pop(name)
    return env


@dataclass
class ConfigSource:
    data: dict | None
    path: Path
    remote: bool
    label: str


def github_file(repo: str, path: str, ref: str | None = None) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or path.startswith("/") or ".." in path:
        return None
    endpoint = f"repos/{repo}/contents/{path}"
    if ref:
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ".." in ref:
            return None
        endpoint += "?ref=" + urllib.parse.quote(ref, safe="")
    try:
        result = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw",
             endpoint],
            capture_output=True, text=True, timeout=TIMEOUT, env=gh_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def load_config(repo: str | None, path: Path | None, local: bool = False) -> ConfigSource:
    if path is not None or local or not repo:
        chosen = path or Path(".github/prism.json")
        try:
            raw = chosen.read_text()
        except OSError:
            raw = None
        remote = False
        label = str(chosen)
    else:
        caller = github_file(repo, ".github/workflows/prism-estimate.yml") or ""
        match = re.search(r"(?m)^\s+config:\s*['\"]?([A-Za-z0-9_./-]+)['\"]?\s*(?:#.*)?$", caller)
        if match:
            shared = match.group(1)
            ref_match = re.search(r"(?m)^\s+prisme_ref:\s*['\"]?([A-Za-z0-9_./-]+)['\"]?\s*(?:#.*)?$", caller)
            ref = ref_match.group(1) if ref_match else "main"
            raw = github_file("Bhavya031/Prisme", shared, ref)
            chosen = Path(shared)
            label = f"Prisme {shared}@{ref}"
        else:
            raw = github_file(repo, ".github/prism.json")
            chosen = Path(".github/prism.json")
            label = f"{repo}/.github/prism.json"
        remote = True
    try:
        data = json.loads(raw) if raw is not None else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("cfo", {}), dict) or not isinstance(data.get("estimate", {}), dict):
        data = None
    return ConfigSource(data, chosen, remote, label)


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


def github_secret(repo: str, name: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return False
    try:
        result = subprocess.run(["gh", "api", f"repos/{repo}/actions/secrets/{name}"],
                                capture_output=True, timeout=TIMEOUT, env=gh_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def inspect(repo: str | None, config_path: Path | None, on_start=None, on_row=None, local: bool = False,
            source: ConfigSource | None = None, saved_entire: bool = False) -> list[tuple]:
    """Return (service, purpose, state, words, fix) without exposing credentials."""
    if (repo is None and not local) or (repo is not None and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)):
        raise ValueError("--repo must be owner/name")
    rows = []

    def row(name, purpose, state, words, fix=None):
        rows.append((name, purpose, state, words, fix))
        if on_row:
            on_row(rows[-1])

    source = source or load_config(repo, config_path, local)
    config = source.data
    if config is None:
        row("Config", "Run settings", "BLOCK", "Config missing or unreadable",
            ("create the config", f"Create {source.label} or choose one with --config.",
             GUIDE + "configuration"))
    else:
        row("Config", "Run settings", "PASS", f"Loaded {source.label}")

    estimate = config.get("estimate") if config else None
    provider = estimate.get("provider") if isinstance(estimate, dict) else None
    worker_model = estimate.get("worker_model") if isinstance(estimate, dict) else None
    model_name = {"openai": "OpenAI", "claude": "Anthropic"}.get(provider, "Model provider")
    if on_start:
        on_start(model_name, "Predict files and estimate cost")
    key_name = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(provider)
    key_problem = None
    stored_key = False
    if key_name is None:
        key_problem = "Provider not configured"
    elif not (key := os.getenv(key_name)):
        stored_key = bool(not local and repo and github_secret(repo, key_name))
        if not stored_key:
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
    elif stored_key:
        row(model_name, "Predict files and estimate cost", "WARN",
            "Key stored on GitHub; cannot be read back to test; pricing passed")
    else:
        row(model_name, "Predict files and estimate cost", "PASS", "Key and pricing checks passed")

    if on_start:
        on_start("Entire", "Search repository")
    if not repo:
        row("Entire", "Search repository", "BLOCK", "Repository needed for Entire check",
            ("name the repository", "Run prism init with --repo owner/name to check Entire readiness.",
             GUIDE + "how-to-get-entire"))
    elif saved_entire and not local:
        row("Entire", "Search repository", "WARN", "Saved to GitHub (cannot be read back to test)")
    elif not os.getenv("ENTIRE_TOKEN"):
        if not local and github_secret(repo, "ENTIRE_TOKEN"):
            row("Entire", "Search repository", "WARN", "Token stored on GitHub; cannot be read back to test")
        else:
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
    if on_start:
        on_start("CFO.ai", "Allocate budget")
    missing_config = [key for key in cfo_actions.CFO_KEYS if not isinstance(cfo, dict)
                      or not isinstance(cfo.get(key), str) or not cfo[key]]
    pending = config is not None and '"PENDING"' in json.dumps(config)
    writer = os.getenv("CFO_SECRET_WRITE_TOKEN")
    stored_client = bool(not local and github_secret(repo, "CFO_OAUTH_CLIENT_ID"))
    stored_refresh = bool(not local and github_secret(repo, "CFO_OAUTH_REFRESH_TOKEN"))
    client_id = bool(os.getenv("CFO_OAUTH_CLIENT_ID")) or stored_client
    sign_in_missing = not client_id or (local and not os.getenv("CFO_OAUTH_REFRESH_TOKEN"))
    if missing_config or pending or sign_in_missing:
        words = ("Sign-in required" if sign_in_missing else "Config incomplete")
        if missing_config or pending:
            words += "; config incomplete" if sign_in_missing else ""
        row("CFO.ai", "Allocate budget", "BLOCK", words,
            ("complete sign-in and tables", "Complete the CFO.ai table settings and run prism cfo-login.",
             GUIDE + "how-to-get-cfo-ai"))
    else:
        words = ("Sign-in stored on GitHub; tested by a real run" if stored_client and stored_refresh
                 else "Client ID configured; sign-in tested by a real run")
        row("CFO.ai", "Allocate budget", "WARN", words)

    if on_start:
        on_start("GitHub", "Repository access and secret updates")
    repos = cfo.get("shared_login_repos", [repo]) if isinstance(cfo, dict) else None
    if local:
        row("GitHub", "Repository access and secret updates", "MISSING", "Local setup; Actions secrets not used")
    elif (not isinstance(repos, list) or not repos or repo.casefold() not in (
            item.casefold() for item in repos if isinstance(item, str))
            or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos)
            or len(repos) != len({item.casefold() for item in repos if isinstance(item, str)})):
        row("GitHub", "Repository access and secret updates", "BLOCK", "Shared repository list invalid",
            ("fix shared repositories", f"List each shared repository once and include {repo}.",
             GUIDE + "configuration"))
    elif not writer:
        if github_secret(repo, "CFO_SECRET_WRITE_TOKEN"):
            row("GitHub", "Repository access and secret updates", "WARN",
                "Secret-write token stored on GitHub; write access untested")
        else:
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

    if on_start:
        on_start("NeatLogs", "Trace agent activity")
    if os.getenv("NEATLOGS_API_KEY"):
        row("NeatLogs", "Trace agent activity", "WARN", "Key present; connection unverified")
    else:
        row("NeatLogs", "Trace agent activity", "MISSING", "Disabled")
    if on_start:
        on_start("Glance", "Count tokens locally")
    try:
        glance.locate()
        row("Glance", "Count tokens locally", "WARN", "Installed; counting untested")
    except glance.GlanceError:
        row("Glance", "Count tokens locally", "MISSING", "Not installed")
    return rows


def _header(repo: str) -> None:
    banner()
    print()
    print(paint("Setup check", "1"))
    print(f"Repository: {repo or 'Not set'}")
    print()
    print(paint(f"{'Service':<17} | {'Purpose':<37} | Status", "1"))


def _print_row(row: tuple, source: ConfigSource, marker: str | None = None) -> None:
    name, purpose, state, words, _ = row
    code = {"PASS": "1;32", "BLOCK": "1;31", "WARN": "1;33", "MISSING": "1;90"}[state]
    note = f" {paint('· ' + marker, '2')}" if marker else ""
    print(f"{paint(f'{name:<17}', code)} | {purpose:<37} | {status_text(state, words)}{note}")
    if name in ("OpenAI", "Anthropic", "Model provider"):
        estimate = (source.data or {}).get("estimate")
        model = estimate.get("worker_model") if isinstance(estimate, dict) else None
        print(f"  {paint(str(model or 'No model selected'), '2')}")


def show(rows: list[tuple], repo: str, source: ConfigSource, streamed: bool = False) -> bool:
    blockers = [name for name, _, state, _, _ in rows if state == "BLOCK"]
    if not streamed:
        _header(repo)
        for row in rows:
            _print_row(row, source)
    estimate = (source.data or {}).get("estimate", {})
    if not isinstance(estimate, dict):
        estimate = {}
    provider = {"openai": "OpenAI", "claude": "Anthropic"}.get(estimate.get("provider"), "Not set")
    cap = estimate.get("max_cost_usd")
    cap_text = f"${cap:g}" if isinstance(cap, (int, float)) else "Not set"
    print("\n" + paint("Configured", "1"))
    print(f"  Provider: {provider}")
    print(f"  Model: {estimate.get('worker_model') or 'Not set'}")
    print(f"  Spending cap per estimate: {cap_text} (configured, not a verified balance)")
    if blockers:
        print()
        for index, (name, _, state, _, fix) in enumerate((row for row in rows if row[2] == "BLOCK"), 1):
            title, action, guide = fix
            print(paint(f"{index}. {name} — {title}", "1"))
            print(action)
            print(link(guide))
            if index < len(blockers):
                print()
    summary(rows)
    return not blockers


def summary(rows: list[tuple]) -> None:
    missing = [(name, words) for name, _, state, words, _ in rows if state == "BLOCK"]
    print("\n" + paint("─" * 60, "2"))
    if missing:
        count = len(missing)
        print(status_text("BLOCK", f"{count} thing{'s' if count != 1 else ''} still missing:"))
        for index, (name, words) in enumerate(missing, 1):
            print(f"  {index}. {name} — {words}")
    else:
        print(status_text("PASS", "Ready to run"))


def stream(repo: str | None, config_path: Path | None, local: bool = False,
           markers: dict[str, str] | None = None, saved_entire: bool = False) -> tuple[list[tuple], ConfigSource]:
    _header(repo)
    source = load_config(repo, config_path, local)
    spinning = sys.stdout.isatty()
    stopped = threading.Event()
    thread = None

    def start(name: str, purpose: str) -> None:
        nonlocal thread
        if not spinning:
            return
        stopped.clear()

        def spin() -> None:
            for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
                if stopped.is_set():
                    break
                sys.stdout.write(f"\r\033[2K{name:<17} | {purpose:<37} | {frame} Checking...")
                sys.stdout.flush()
                stopped.wait(0.1)

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()

    def done(row: tuple) -> None:
        if thread:
            stopped.set()
            thread.join()
            sys.stdout.write("\r\033[2K")
        name = "Model provider" if row[0] in ("OpenAI", "Anthropic", "Model provider") else row[0]
        default = "checked" if row[2] == "PASS" or (name == "GitHub" and row[2] == "WARN") else "skipped"
        _print_row(row, source, markers.get(name, default) if markers is not None else None)

    try:
        rows = inspect(repo, config_path, start, done, local=local, source=source,
                       saved_entire=saved_entire)
    finally:
        stopped.set()
        if thread:
            thread.join()
    return rows, source


def run(repo: str | None, config_path: Path | None, local: bool = False,
        markers: dict[str, str] | None = None, saved_entire: bool = False) -> bool:
    rows, source = stream(repo, config_path, local, markers, saved_entire)
    return show(rows, repo, source, streamed=True)
