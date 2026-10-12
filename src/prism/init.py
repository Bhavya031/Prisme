"""Interactive setup using the same checks as prism check."""

import getpass
import json
import os
import re
import select
import subprocess
import sys
import termios
import tty
from pathlib import Path
from types import SimpleNamespace

from . import cfo_actions, check
from .models import PRICES


ROOT = Path(__file__).resolve().parents[2]


class Cancelled(RuntimeError):
    pass


def _screen(title: str, sentence: str = "", state: str = "", words: str = "") -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    check.banner()
    print()
    print(check.paint(title, "1;36"))
    if sentence:
        print(sentence)
    if state:
        print(check.status_text(state, words))
    print()


def _menu(options: list[tuple[str, str]], selected: int = 0) -> int:
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    def draw() -> None:
        for index, (label, description) in enumerate(options):
            marker = "❯" if interactive and index == selected else " "
            choice = f"{marker} {index + 1}. {label}"
            if interactive and index == selected:
                print(check.paint(f"{choice} · {description}", check.spectrum_code(5)))
            else:
                print(choice + (check.paint(f" · {description}", "2") if interactive else f" · {description}"))

    draw()
    if not interactive:
        while True:
            try:
                choice = input("Choose a number (q to cancel): ").strip().lower()
            except EOFError:
                raise Cancelled("Setup cancelled.") from None
            if choice in ("q", "\x1b"):
                raise Cancelled("Setup cancelled.")
            if not choice:
                return selected
            if choice.isdigit() and 1 <= int(choice) <= len(options):
                return int(choice) - 1
            print("Choose one of the listed numbers.")

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = os.read(fd, 1)
            if not key:
                raise Cancelled("Setup cancelled.")
            if key in (b"q", b"Q", b"\x03"):
                raise Cancelled("Setup cancelled.")
            if key in (b"\r", b"\n"):
                return selected
            if key == b"\x1b":
                if not select.select([fd], [], [], 0.08)[0]:
                    raise Cancelled("Setup cancelled.")
                if os.read(fd, 1) not in (b"[", b"O") or not select.select([fd], [], [], 0.08)[0]:
                    raise Cancelled("Setup cancelled.")
                arrow = os.read(fd, 1)
                if arrow == b"A":
                    selected = (selected - 1) % len(options)
                elif arrow == b"B":
                    selected = (selected + 1) % len(options)
                else:
                    continue
            elif key.isdigit() and 1 <= int(key) <= len(options):
                return int(key) - 1
            else:
                continue
            sys.stdout.write(f"\033[{len(options)}A")
            for _ in options:
                sys.stdout.write("\r\033[2K\n")
            sys.stdout.write(f"\033[{len(options)}A")
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def _input(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise Cancelled("Setup cancelled.") from None


def _hidden(name: str) -> str:
    if not sys.stdin.isatty():
        print("Hidden key entry needs a terminal. Skipping this step.")
        return ""
    try:
        return getpass.getpass(f"{name}: ").strip()
    except EOFError:
        raise Cancelled("Setup cancelled.") from None


def _checkout(repo: str) -> Path | None:
    path = _input(f"Local checkout of {repo} (Enter to skip): ")
    if not path:
        return None
    checkout = Path(path).expanduser().resolve()
    result = subprocess.run(["git", "-C", str(checkout), "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode or result.stdout.strip().removesuffix(".git") not in (
            f"https://github.com/{repo}", f"git@github.com:{repo}"):
        raise ValueError(f"{checkout} is not a checkout of {repo}")
    return checkout


def _secret(repo: str, name: str, value: str, repos: list[str] | None = None) -> None:
    for target in repos or [repo]:
        try:
            result = subprocess.run(["gh", "secret", "set", name, "--repo", target],
                                    input=value, text=True, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError(f"could not save {name} in {target}; check gh login") from None
        if result.returncode:
            raise RuntimeError(f"could not save {name} in {target}; check Actions secret permission")
    os.environ[name] = value
    print(f"Saved {name} as an Actions secret.")


def _read_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        return {"cfo": {}, "estimate": {}}
    if not isinstance(config, dict) or not isinstance(config.get("cfo", {}), dict) or not isinstance(
            config.get("estimate", {}), dict):
        raise ValueError("config needs cfo and estimate objects")
    config.setdefault("cfo", {})
    config.setdefault("estimate", {})
    return config


def _save_config(path: Path, config: dict, changed: bool, checkout: Path | None) -> Path:
    if path == Path(".github/prism.json") and checkout:
        if (checkout / path).exists():
            raise ValueError("target config already exists; rerun with --config pointing to it")
        path = checkout / path
    if changed:
        if path == Path(".github/prism.json") and checkout is None:
            print("Choose a local target checkout to save .github/prism.json.")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(config, indent=2) + "\n")
            print(f"Saved {path}. Review and commit it in its repository.")
    return path


def _workflows(repo: str, checkout: Path, config_path: Path) -> None:
    shared = config_path.resolve().is_relative_to(ROOT) and config_path.resolve() != (
        checkout / ".github/prism.json").resolve()
    config_name = config_path.resolve().relative_to(ROOT).as_posix() if shared else ""
    for name in ("prism-estimate.yml", "prism-cfo-allocation.yml"):
        target = checkout / ".github/workflows" / name
        if target.exists():
            print(f"Already present: {target}")
            continue
        source = (ROOT / "examples" / name).read_text()
        if shared:
            source = source.replace("      prisme_ref: main\n",
                                    f"      prisme_ref: main\n      config: {config_name}\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        print(f"Added {target}. Review and commit it in {repo}.")


def _row(rows: list[tuple], name: str) -> tuple:
    return next(row for row in rows if row[0] == name)


def _status_screen(title: str, sentence: str, row: tuple) -> None:
    _screen(title, sentence, row[2], row[3])


def _configure_cfo(repo: str, config: dict) -> bool:
    cfo = config["cfo"]
    changed = False
    print(check.link(check.GUIDE + "how-to-get-cfo-ai"))
    for key in cfo_actions.CFO_KEYS:
        if not isinstance(cfo.get(key), str) or not cfo[key] or cfo[key] == "PENDING":
            value = _input(f"{key} (Enter to skip): ")
            if value:
                cfo[key] = value
                changed = True
    if "shared_login_repos" not in cfo:
        value = _input(f"Repositories sharing this login, comma separated (Enter for {repo} only): ")
        if value:
            repos = [item.strip() for item in value.split(",")]
            if repo not in repos or len(repos) != len(set(repos)) or any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos):
                raise ValueError("shared login repositories must be unique owner/name values including --repo")
            cfo["shared_login_repos"] = repos
            changed = True
    return changed


def run(repo: str, config_path: Path) -> bool:
    _screen("How do you want to set up Prism?")
    choice = _menu([("Set it up for me", "guided, one step at a time"),
                    ("I will do it myself", "show me the guide")], selected=0)
    if choice == 1:
        print(check.link(check.GUIDE + "configuration"))
        print(check.link(check.GUIDE + "how-to-get-entire"))
        print(check.link(check.GUIDE + "how-to-get-cfo-ai"))
        print(check.link(check.GUIDE + "how-to-get-github-token"))
        print(f"prism check --repo {repo} --config {config_path}")
        check.summary(check.inspect(repo, config_path))
        return True

    checkout = None
    if config_path == Path(".github/prism.json") and not config_path.exists():
        _screen("Model provider", "First, choose the local checkout that will hold your Prism config.")
        checkout = _checkout(repo)
        if checkout:
            config_path = checkout / config_path
    config = _read_config(config_path)
    rows = check.inspect(repo, config_path)
    changed = False
    model_row = next(row for row in rows if row[0] in ("OpenAI", "Anthropic", "Model provider"))
    _status_screen("Model provider", "Prism uses this key to predict files and estimate cost.", model_row)
    if model_row[2] != "PASS" or _menu([("Keep it", "use the current provider and key"),
                                         ("Change it", "choose a provider or replace its key")]) == 1:
        current = config["estimate"].get("provider")
        choice = _menu([("OpenAI", "gpt-6-sol"), ("Anthropic", "claude-sonnet-5-5"),
                        ("Skip for now", "leave the provider as it is")],
                       0 if current == "openai" else 1 if current == "claude" else 0)
        if choice < 2:
            provider = ("openai", "claude")[choice]
            model = PRICES[provider]["model"]
            if config["estimate"].get("provider") != provider or config["estimate"].get("worker_model") != model:
                config["estimate"].update(provider=provider, worker_model=model)
                changed = True
            key = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}[provider]
            needs_key = not os.getenv(key) or (provider == current and "Key rejected" in model_row[3])
            if needs_key and _menu([("Enter an API key", f"save {key} as an Actions secret"),
                                    ("Skip for now", "leave the key unchanged")]) == 0:
                value = _hidden(key)
                if value:
                    _secret(repo, key, value)
    for key, value in {"cooldown_minutes": 10, "max_cost_usd": 0.05, "max_output_tokens": 4000,
                       "max_extra_rounds": 1, "timeout_seconds": 180}.items():
        if key not in config["estimate"] or config["estimate"][key] == "PENDING":
            config["estimate"][key] = value
            changed = True

    entire_row = _row(rows, "Entire")
    _status_screen("Entire", "Entire searches the indexed repository before a model call.", entire_row)
    if entire_row[2] != "PASS" or _menu([("Keep it", "current token and repository are ready"),
                                          ("Change it", "replace the Actions secret")]) == 1:
        choice = _menu([("Fetch the token from my Entire login", "use entire auth token"),
                        ("Paste a token", "enter it hidden"),
                        ("Skip for now", "leave the current token unchanged")])
        if choice == 0:
            try:
                result = subprocess.run(["entire", "auth", "token"], capture_output=True,
                                        text=True, timeout=15)
                if result.returncode == 0 and result.stdout.strip():
                    _secret(repo, "ENTIRE_TOKEN", result.stdout.strip())
                else:
                    print("Entire CLI could not supply a token. Sign in with entire login first.")
            except (OSError, subprocess.TimeoutExpired):
                print("Entire CLI could not supply a token. Sign in with entire login first.")
        elif choice == 1:
            value = _hidden("ENTIRE_TOKEN")
            if value:
                _secret(repo, "ENTIRE_TOKEN", value)

    cfo_row = _row(rows, "CFO.ai")
    _status_screen("CFO.ai", "CFO.ai decides whether the estimate fits the budget.", cfo_row)
    if "config incomplete" in cfo_row[3].lower():
        if _menu([("Enter table settings", "use the names from your CFO.ai page"),
                  ("Skip for now", "leave the config unchanged")]) == 0:
            changed |= _configure_cfo(repo, config)
    if cfo_row[3].startswith("Sign-in required"):
        cfo_choice = _menu([("Sign in now", "opens your browser"),
                            ("Skip for now", "sign in later")])
    else:
        cfo_choice = _menu([("Keep it", "sign-in is configured, not live-tested"),
                            ("Sign in now", "replace the stored sign-in")])
    if (cfo_row[3].startswith("Sign-in required") and cfo_choice == 0) or (
            not cfo_row[3].startswith("Sign-in required") and cfo_choice == 1):
        from .cfo_login import login
        repos = config["cfo"].get("shared_login_repos", [repo])
        client_id, refresh = login(SimpleNamespace(repo=repos, client_id=None, port=None, timeout=300))
        os.environ["CFO_OAUTH_CLIENT_ID"] = client_id
        os.environ["CFO_OAUTH_REFRESH_TOKEN"] = refresh

    github_row = _row(rows, "GitHub")
    _status_screen("GitHub token", "This token lets Prism save each rotated CFO.ai sign-in.", github_row)
    if github_row[3] == "Shared repository list invalid":
        if _menu([("Fix shared repositories", "enter owner/name values, comma separated"),
                  ("Skip for now", "keep the current list")]) == 0:
            value = _input(f"Shared repositories (include {repo}): ")
            repos = [item.strip() for item in value.split(",")]
            if repo not in repos or len(repos) != len(set(repos)) or any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos):
                raise ValueError("shared login repositories must be unique owner/name values including --repo")
            config["cfo"]["shared_login_repos"] = repos
            changed = True
    if (github_row[3] != "Shared repository list invalid" and
            (github_row[2] != "WARN" or _menu([("Keep it", "read access is confirmed"),
                                               ("Change it", "replace the Actions secret")]) == 1)):
        if _menu([("Enter a GitHub token", "Secrets: Read and write for shared repositories"),
                  ("Skip for now", "leave the token unchanged")]) == 0:
            value = _hidden("CFO_SECRET_WRITE_TOKEN")
            if value:
                _secret(repo, "CFO_SECRET_WRITE_TOKEN", value,
                        config["cfo"].get("shared_login_repos", [repo]))

    neat_row = _row(rows, "NeatLogs")
    _status_screen("NeatLogs", "NeatLogs can trace agent activity; it is optional.", neat_row)
    if neat_row[2] != "WARN" or _menu([("Keep it", "leave the current key"),
                                        ("Change it", "replace the optional key")]) == 1:
        if _menu([("Enter a NeatLogs key", "save it as an Actions secret"),
                  ("Skip for now", "leave tracing disabled")]) == 0:
            value = _hidden("NEATLOGS_API_KEY")
            if value:
                _secret(repo, "NEATLOGS_API_KEY", value)

    complete = bool(checkout and all((checkout / ".github/workflows" / name).exists()
                                     for name in ("prism-estimate.yml", "prism-cfo-allocation.yml")))
    _screen("Workflow files", "Two small caller files connect your repository to Prism.",
            "WARN" if complete else "MISSING",
            "Caller files present; contents unverified" if complete else "Local checkout needed to inspect or add files")
    if complete:
        workflow_choice = _menu([("Keep it", "both caller files are present"),
                                 ("Change it", "inspect missing caller files")])
    else:
        workflow_choice = _menu([("Add caller workflows", "copy the two templates to a local checkout"),
                                 ("Skip for now", "add the files later")])
    if (workflow_choice == 0 and not complete) or (workflow_choice == 1 and complete):
        if checkout is None:
            checkout = _checkout(repo)
        if checkout:
            config_path = _save_config(config_path, config, changed, checkout)
            _workflows(repo, checkout, config_path)
    else:
        config_path = _save_config(config_path, config, changed, checkout)
    print()
    return check.run(repo, config_path)
