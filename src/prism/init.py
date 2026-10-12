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
from urllib.parse import urlsplit

from . import cfo_actions, check, local_env, results
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


def _input(prompt: str, strip: bool = True) -> str:
    try:
        value = input(prompt)
        return value.strip() if strip else value
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


def _entire_token() -> str | None:
    try:
        result = subprocess.run(["entire", "auth", "token"], capture_output=True,
                                text=True, timeout=15)
    except FileNotFoundError:
        print("Entire CLI is not installed or not on PATH.")
        return None
    except OSError:
        print("entire auth token could not start.")
        return None
    except subprocess.TimeoutExpired:
        print("entire auth token timed out.")
        return None
    if result.returncode:
        print(f"entire auth token failed (exit {result.returncode}); sign in with entire login.")
        return None
    token = result.stdout.strip()
    if not token:
        print("entire auth token returned no token; sign in with entire login.")
        return None
    if any(character.isspace() for character in token):
        print("entire auth token returned unexpected output; no token was saved.")
        return None
    return token


def _repo_name(value: str) -> str:
    error = "Use owner/name or a github.com link (bare, http(s), git://, ssh://, or git@github.com:)."
    if not isinstance(value, str):
        raise ValueError(error)
    value = value.strip().strip("<>").strip()
    if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", value):
        name = value.removesuffix(".git")
        if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name):
            return name
    if re.match(r"(?i)^git@(?:www\.)?github\.com:", value):
        value = "ssh://" + value.replace(":", "/", 1)
    elif re.match(r"(?i)^(?:www\.)?github\.com/", value):
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ValueError(error) from None
    if parts.scheme.lower() not in ("http", "https", "git", "ssh") or host not in (
            "github.com", "www.github.com"):
        raise ValueError(error)
    segments = parts.path.strip("/").split("/")
    if len(segments) < 2:
        raise ValueError(error)
    repo = f"{segments[0]}/{segments[1].removesuffix('.git')}"
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(error)
    return repo


def _gh_reason(stderr: str, secret: str = "") -> str:
    line = next((line.strip() for line in stderr.splitlines() if line.strip()), "gh returned no details")
    if secret:
        line = line.replace(secret, "[REDACTED]")
    return re.sub(r"[A-Za-z0-9_-]{24,}", "[REDACTED]", results.redact(line))[:180]


def _verify_github(repo: str) -> str:
    gh_env = check.gh_env()
    try:
        auth = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"],
                              capture_output=True, text=True, timeout=15, env=gh_env)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("GitHub CLI is unavailable; install gh and run gh auth login") from None
    if auth.returncode:
        raise RuntimeError(f"GitHub CLI is not logged in; run gh auth login ({_gh_reason(auth.stderr)})")
    try:
        result = subprocess.run(["gh", "api", f"repos/{repo}"], capture_output=True,
                                text=True, timeout=15, env=gh_env)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"could not reach {repo} with gh; check the connection and repository link") from None
    if result.returncode:
        raise RuntimeError(f"GitHub repository {repo} is not reachable with this account ({_gh_reason(result.stderr)})")
    try:
        details = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("gh returned invalid repository details") from None
    if not isinstance(details, dict):
        raise RuntimeError("gh returned invalid repository details")
    canonical = details.get("full_name")
    if not isinstance(canonical, str) or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", canonical):
        raise RuntimeError("gh returned no valid canonical repository name")
    permissions = details.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise RuntimeError("gh returned invalid repository permissions")
    if not (permissions.get("admin") or permissions.get("maintain") or details.get("role_name") in ("admin", "maintain")):
        raise RuntimeError(f"{repo}: admin or maintain permission is needed to set Actions secrets")
    if not permissions.get("push"):
        raise RuntimeError(f"{repo}: push permission is needed to add caller workflow files")
    print(f"GitHub access verified for {canonical}: repository, secrets role, and push permission.")
    return canonical


def _warn_env_ignore() -> None:
    try:
        result = subprocess.run(["git", "check-ignore", "-q", "--", ".env"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode:
        print("Warning: .env is not git-ignored in this folder. Add .env to .gitignore before committing.")


def _checkout(repo: str) -> Path | None:
    path = _input(f"Local checkout of {repo} (Enter to skip): ")
    if not path:
        return None
    checkout = Path(path).expanduser().resolve()
    result = subprocess.run(["git", "-C", str(checkout), "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=15)
    if result.returncode or _repo_name(result.stdout.strip()).casefold() != repo.casefold():
        raise ValueError(f"{checkout} is not a checkout of {repo}")
    return checkout


def _secret(repo: str | None, name: str, value: str, repos: list[str] | None = None,
            github: bool = True, local: bool = False) -> None:
    targets = (repos or [repo]) if github else []
    targets = [repo if _repo_name(target).casefold() == repo.casefold()
               else _verify_github(_repo_name(target)) for target in targets]
    for target in targets:
        try:
            result = subprocess.run(["gh", "secret", "set", name, "--repo", target],
                                    input=value, text=True, capture_output=True, timeout=30, env=check.gh_env())
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError(f"could not save {name} in {target}; check gh login") from None
        if result.returncode:
            raise RuntimeError(f"could not save {name} in {target}: {_gh_reason(result.stderr, value)}")
    if local:
        local_env.save(name, value)
    print(f"Saved {name} " + ("as an Actions secret" if github else "to .env") + (" and .env." if github and local else "."))


def _save_config(path: Path, config: dict, changed: bool, checkout: Path | None, local: bool = False) -> Path:
    if path == Path(".github/prism.json") and checkout:
        if (checkout / path).exists():
            raise ValueError("target config already exists; rerun with --config pointing to it")
        path = checkout / path
    if changed:
        if path == Path(".github/prism.json") and checkout is None and not local:
            raise RuntimeError("No local checkout was selected; config settings were not saved")
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


def _configure_cfo(repo: str | None, config: dict) -> bool:
    cfo = config["cfo"]
    changed = False
    print(check.link(check.GUIDE + "how-to-get-cfo-ai"))
    examples = json.loads((ROOT / "configs/portfolio.json").read_text())["cfo"]
    explanations = {
        "org_id": "Your CFO.ai workspace number, shown in the app address after /org/",
        "scenario": "The scenario containing your tables, shown in the scenario selector",
        "page": "The page containing the tables, shown in the page sidebar",
        "budget_table": "The repository budget table, named at the top of its table",
        "request_table": "The allocation request table, named at the top of its table",
        "decision_table": "The saved rule's decision table, named at the top of its table",
        "column_prefix": "The exact start of the table's column names, including a trailing space",
    }
    for key in cfo_actions.CFO_KEYS:
        if not isinstance(cfo.get(key), str) or not cfo[key] or cfo[key] == "PENDING":
            print(f"{key}: {explanations[key]} · example {examples[key]!r}")
            value = _input("Enter value (Enter to skip): ", strip=key != "column_prefix")
            if value.strip():
                cfo[key] = value
                changed = True
    if repo and not cfo.get("shared_login_repos"):
        print("The CFO.ai sign-in changes every run, so every repository using the same sign-in must be listed here or the others stop working.")
        defaults = cfo.get("shared_login_repos") or [repo]
        value = _input(f"Repositories sharing this login, comma separated [{', '.join(defaults)}]: ")
        repos = [item.strip() for item in value.split(",")] if value else defaults
        if repo not in repos or len(repos) != len(set(repos)) or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos):
            raise ValueError("shared login repositories must be unique owner/name values including --repo")
        cfo["shared_login_repos"] = repos
        changed = True
    return changed


def run(repo: str | None, config_path: Path | None) -> bool:
    _screen("How do you want to set up Prism?")
    choice = _menu([("Set it up for me", "guided, one step at a time"),
                    ("I will do it myself", "show me the guide")], selected=0)
    if choice == 1:
        print(check.link(check.GUIDE + "configuration"))
        print(check.link(check.GUIDE + "how-to-get-entire"))
        print(check.link(check.GUIDE + "how-to-get-cfo-ai"))
        print(check.link(check.GUIDE + "how-to-get-github-token"))
        print(f"prism check --repo {repo or 'owner/name'} --config {config_path or '.github/prism.json'}")
        if repo or os.environ.get("PRISM_LOCAL_SECRETS") == "1":
            local_only = os.environ.get("PRISM_LOCAL_SECRETS") == "1"
            check.summary(check.inspect(_repo_name(repo) if repo else None, config_path, local=local_only))
        return True

    _screen("Where will Prism run?", "Choose where these credentials and setup files should go.")
    destination = _menu([("GitHub repository", "runs on issues through Actions"),
                         ("This computer", "run prism from the command line"),
                         ("Both", "set up Actions and this computer")])
    github = destination in (0, 2)
    local = destination in (1, 2)
    if github:
        _screen("GitHub repository", "Enter the repository that will run Prism.")
        repo = _repo_name(_input(f"Repository link{f' [{repo}]' if repo else ''}: ") or repo or "")
        print(f"Repository: {repo}")
        repo = _verify_github(repo)
    elif repo:
        repo = _repo_name(repo)
    else:
        entered = _input("Repository for Entire search (owner/name, Enter to set later): ")
        repo = _repo_name(entered) if entered else None
    if local:
        _warn_env_ignore()
        local_env.save("PRISM_LOCAL_SECRETS", "1")
        if repo:
            local_env.save("GITHUB_REPOSITORY", repo)

    print()
    rows, source = check.stream(repo, config_path, local=not github)
    config_path = source.path
    if sys.stdin.isatty():
        _input("Press Enter to continue setup...")
    markers = {}
    portfolio = ROOT / "configs/portfolio.json"
    shared_repos = json.loads(portfolio.read_text())["cfo"]["shared_login_repos"]
    if (repo and any(repo.casefold() == target.casefold() for target in shared_repos)
            and any(row[0] in ("Config", "CFO.ai") and row[2] == "BLOCK" for row in rows)):
        _screen("CFO.ai settings", "This repository can use the existing shared Prisme config.")
        if _menu([("Use the shared Prisme settings (configs/portfolio.json)", "no table names to enter"),
                  ("Enter my own settings", "use a config for this repository")]) == 0:
            config_path = portfolio
            rows = check.inspect(repo, config_path, local=not github)
            markers["CFO.ai"] = "checked"
    checkout = None
    can_save_config = not source.remote
    needs_config_edit = (_row(rows, "Config")[2] == "BLOCK" or
                         any(row[0] in ("OpenAI", "Anthropic", "Model provider") and
                             ("Provider not configured" in row[3] or "Model has no built-in price" in row[3])
                             for row in rows) or
                         "config incomplete" in _row(rows, "CFO.ai")[3].lower() or
                         _row(rows, "GitHub")[3] == "Shared repository list invalid")
    if github and source.remote and needs_config_edit:
        if config_path == Path(".github/prism.json"):
            _status_screen("Config", "A local checkout is needed before entering settings to save in the repository.",
                           _row(rows, "Config"))
            checkout = _checkout(repo)
            if checkout:
                config_path = checkout / config_path
                if config_path.exists():
                    source = check.load_config(None, config_path, local=True)
                    rows = check.inspect(repo, config_path, local=not github, source=source)
                else:
                    source = check.ConfigSource(source.data, config_path, False, str(config_path))
                can_save_config = True
            else:
                print("No checkout selected. Config settings will not be requested or saved.")
        else:
            print("Shared Prisme settings must be edited in Prisme; this setup will not ask for unsavable values.")
    config = json.loads(json.dumps(source.data)) if source.data is not None else {"cfo": {}, "estimate": {}}
    config.setdefault("cfo", {})
    config.setdefault("estimate", {})
    if github:
        shared = config["cfo"].get("shared_login_repos", [])
        changed = False
        if can_save_config and isinstance(shared, list) and all(isinstance(target, str) for target in shared):
            canonical = [repo if _repo_name(target).casefold() == repo.casefold()
                         else _verify_github(_repo_name(target)) for target in shared]
            if canonical != shared:
                config["cfo"]["shared_login_repos"] = canonical
                changed = True
    else:
        changed = False
    model_row = next(row for row in rows if row[0] in ("OpenAI", "Anthropic", "Model provider"))
    pending_updates = set()
    if model_row[2] != "PASS" and can_save_config:
        _status_screen("Model provider", "Prism uses this key to predict files and estimate cost.", model_row)
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
                pending_updates.add("Model provider")
            key = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}[provider]
            needs_key = not os.getenv(key) or (provider == current and "Key rejected" in model_row[3])
            if needs_key and _menu([("Enter an API key", f"save {key} for this setup"),
                                    ("Skip for now", "leave the key unchanged")]) == 0:
                value = _hidden(key)
                if value:
                    _secret(repo, key, value, github=github, local=local)
                    markers["Model provider"] = "updated"
                else:
                    markers.setdefault("Model provider", "skipped")
            elif needs_key:
                markers.setdefault("Model provider", "skipped")
        else:
            markers["Model provider"] = "skipped"
    if can_save_config:
        for key, value in {"cooldown_minutes": 10, "max_cost_usd": 0.05, "max_output_tokens": 4000,
                           "max_extra_rounds": 1, "timeout_seconds": 180}.items():
            if key not in config["estimate"] or config["estimate"][key] == "PENDING":
                config["estimate"][key] = value
                changed = True
                pending_updates.add("Model provider")

    saved_entire = False
    entire_row = _row(rows, "Entire")
    if entire_row[2] != "PASS":
        _status_screen("Entire", "Entire searches the indexed repository before a model call.", entire_row)
        choice = _menu([("Refresh it from my Entire login", "use entire auth token"),
                        ("Paste a token", "enter it hidden"),
                        ("Skip for now", "leave the current token unchanged")])
        if choice == 0:
            token = _entire_token()
            if not token:
                if _menu([("Run entire login", "sign in, then retry the token"),
                          ("Skip for now", "leave the current token unchanged")]) == 0:
                    try:
                        print("Complete Entire sign-in in your browser.")
                        login_result = subprocess.run(["entire", "login"], capture_output=True,
                                                      text=True, timeout=300)
                        if login_result.returncode:
                            print(f"entire login failed (exit {login_result.returncode}).")
                        else:
                            token = _entire_token()
                    except (OSError, subprocess.TimeoutExpired):
                        print("entire login did not complete.")
            if token:
                _secret(repo, "ENTIRE_TOKEN", token, github=github, local=local)
                markers["Entire"] = "updated"
                saved_entire = github and not local
            else:
                print("Entire token was not refreshed. Run entire login and try again.")
                markers["Entire"] = "skipped"
        elif choice == 1:
            value = _hidden("ENTIRE_TOKEN")
            if value:
                _secret(repo, "ENTIRE_TOKEN", value, github=github, local=local)
                markers["Entire"] = "updated"
                saved_entire = github and not local
            else:
                markers["Entire"] = "skipped"
        else:
            markers["Entire"] = "skipped"
        entire_row = _row(check.inspect(repo, config_path, local=not github, source=source,
                                        saved_entire=saved_entire), "Entire")
        print(check.status_text(entire_row[2], f"Entire: {entire_row[3]}"))

    cfo_row = _row(rows, "CFO.ai")
    local_sign_in_missing = local and not all(local_env.present(name) for name in (
        "CFO_OAUTH_CLIENT_ID", "CFO_OAUTH_REFRESH_TOKEN"))
    if cfo_row[2] == "BLOCK" or local_sign_in_missing:
        if local_sign_in_missing and cfo_row[2] != "BLOCK":
            _screen("CFO.ai", "CFO.ai decides whether the estimate fits the budget.", "BLOCK", "Local sign-in required")
        else:
            _status_screen("CFO.ai", "CFO.ai decides whether the estimate fits the budget.", cfo_row)
        if can_save_config and "config incomplete" in cfo_row[3].lower():
            if _menu([("Enter table settings", "use the names from your CFO.ai page"),
                      ("Skip for now", "leave the config unchanged")]) == 0:
                if _configure_cfo(repo, config):
                    changed = True
                    pending_updates.add("CFO.ai")
            else:
                markers.setdefault("CFO.ai", "skipped")
        if cfo_row[3].startswith("Sign-in required") or local_sign_in_missing:
            if _menu([("Sign in now", "opens your browser"),
                      ("Skip for now", "sign in later")]) == 0:
                from .cfo_login import login
                repos = config["cfo"].get("shared_login_repos", [repo])
                if github:
                    client_id, refresh = login(SimpleNamespace(repo=repos, client_id=None, port=None,
                                                               timeout=300, gh_env=check.gh_env()))
                    os.environ["CFO_OAUTH_CLIENT_ID"] = client_id
                    os.environ["CFO_OAUTH_REFRESH_TOKEN"] = refresh
                if local:
                    if github:
                        print("Local runs need a separate CFO.ai sign-in because each refresh token rotates on use.")
                    client_id, refresh = login(SimpleNamespace(repo=[], client_id=None, port=None,
                                                               timeout=300, gh_env=check.gh_env()))
                    local_env.save("CFO_OAUTH_CLIENT_ID", client_id)
                    local_env.save("CFO_OAUTH_REFRESH_TOKEN", refresh)
                markers["CFO.ai"] = "updated"
            else:
                markers.setdefault("CFO.ai", "skipped")

    if github:
        github_row = _row(rows, "GitHub")
        if github_row[2] == "BLOCK":
            _status_screen("GitHub token", "This token lets Prism save each rotated CFO.ai sign-in.", github_row)
    if github and github_row[2] == "BLOCK" and github_row[3] == "Shared repository list invalid":
        if _menu([("Fix shared repositories", "enter owner/name values, comma separated"),
                  ("Skip for now", "keep the current list")]) == 0:
            value = _input(f"Shared repositories (include {repo}): ")
            repos = [item.strip() for item in value.split(",")]
            if repo not in repos or len(repos) != len(set(repos)) or any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos):
                raise ValueError("shared login repositories must be unique owner/name values including --repo")
            config["cfo"]["shared_login_repos"] = repos
            changed = True
            pending_updates.add("GitHub")
        else:
            markers["GitHub"] = "skipped"
    if github and github_row[2] == "BLOCK" and github_row[3] != "Shared repository list invalid":
        if _menu([("Enter a GitHub token", "Secrets: Read and write for shared repositories"),
                  ("Skip for now", "leave the token unchanged")]) == 0:
            value = _hidden("CFO_SECRET_WRITE_TOKEN")
            if value:
                _secret(repo, "CFO_SECRET_WRITE_TOKEN", value,
                        config["cfo"].get("shared_login_repos", [repo]), github=True, local=local)
                markers["GitHub"] = "updated"
            else:
                markers["GitHub"] = "skipped"
        else:
            markers["GitHub"] = "skipped"

    neat_row = _row(rows, "NeatLogs")
    if neat_row[2] == "MISSING":
        _status_screen("NeatLogs", "NeatLogs can trace agent activity; it is optional.", neat_row)
        if _menu([("Enter a NeatLogs key", "save it as an Actions secret"),
                  ("Skip for now", "leave tracing disabled")]) == 0:
            value = _hidden("NEATLOGS_API_KEY")
            if value:
                _secret(repo, "NEATLOGS_API_KEY", value, github=github, local=local)
                markers["NeatLogs"] = "updated"
            else:
                markers["NeatLogs"] = "skipped"
        else:
            markers["NeatLogs"] = "skipped"
    if _row(rows, "Glance")[2] == "MISSING":
        markers["Glance"] = "skipped"

    complete = bool(checkout and all((checkout / ".github/workflows" / name).exists()
                                     for name in ("prism-estimate.yml", "prism-cfo-allocation.yml")))
    if not github:
        config_path = _save_config(config_path, config, changed, checkout, local=True)
        if changed and config_path.exists():
            markers["Config"] = "updated"
            markers.update({name: "updated" for name in pending_updates})
        print()
        return check.run(repo, config_path, local=True, markers=markers)
    if not complete:
        _screen("Workflow files", "Two small caller files connect your repository to Prism.",
                "MISSING", "Local checkout needed to inspect or add files")
        workflow_choice = _menu([("Add caller workflows", "copy the two templates to a local checkout"),
                                 ("Skip for now", "add the files later")])
        if workflow_choice == 0:
            if checkout is None:
                checkout = _checkout(repo)
            if checkout:
                config_path = _save_config(config_path, config, changed, checkout)
                if changed and config_path.exists():
                    markers["Config"] = "updated"
                    markers.update({name: "updated" for name in pending_updates})
                _workflows(repo, checkout, config_path)
                changed = False
    config_path = _save_config(config_path, config, changed, checkout)
    if changed and config_path.exists():
        markers["Config"] = "updated"
        markers.update({name: "updated" for name in pending_updates})
    print()
    return check.run(repo, None if source.remote else config_path, markers=markers,
                     saved_entire=saved_entire)
