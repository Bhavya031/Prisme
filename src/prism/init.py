"""Interactive setup using the same checks as prism check."""

import getpass
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

from . import cfo_actions, check
from .models import PRICES


ROOT = Path(__file__).resolve().parents[2]


def _yes(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _checkout(repo: str) -> Path | None:
    path = input(f"Local checkout of {repo} (Enter to skip files): ").strip()
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


def _config(repo: str, path: Path, checkout: Path | None) -> Path:
    if path == Path(".github/prism.json") and checkout:
        path = checkout / path
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            raise ValueError("config must be a JSON object")
        existing_cfo = config.get("cfo")
        existing_estimate = config.get("estimate")
        if (isinstance(existing_cfo, dict) and isinstance(existing_estimate, dict)
                and all(isinstance(existing_cfo.get(key), str) and existing_cfo[key] not in ("", "PENDING")
                        for key in cfo_actions.CFO_KEYS)
                and existing_estimate.get("provider") in ("openai", "claude")
                and existing_estimate.get("worker_model") in {price["model"] for price in PRICES.values()}
                and all(key in existing_estimate and existing_estimate[key] != "PENDING" for key in (
                    "cooldown_minutes", "max_cost_usd", "max_output_tokens", "max_extra_rounds", "timeout_seconds"))):
            return path
        if not _yes(f"Fill missing settings in {path}?"):
            return path
    except FileNotFoundError:
        config = {"cfo": {}, "estimate": {}}
        if checkout is None and path == Path(".github/prism.json"):
            print("Choose a local target checkout before creating .github/prism.json.")
            return path
        if not _yes(f"Create {path}? {check.GUIDE}configuration"):
            return path
    cfo = config.setdefault("cfo", {})
    estimate = config.setdefault("estimate", {})
    if not isinstance(cfo, dict) or not isinstance(estimate, dict):
        raise ValueError("config needs cfo and estimate objects")
    changed = False
    defaults = {"scenario": "Main Scenario"}
    for key in cfo_actions.CFO_KEYS:
        if not isinstance(cfo.get(key), str) or not cfo[key] or cfo[key] == "PENDING":
            value = input(f"CFO.ai {key} (Enter to skip, {check.GUIDE}how-to-get-cfo-ai): ").strip()
            if value:
                cfo[key] = value
                changed = True
            elif key in defaults and not cfo.get(key):
                cfo[key] = defaults[key]
                changed = True
    if estimate.get("provider") not in ("openai", "claude"):
        value = input("Model provider, openai or claude (Enter to skip): ").strip()
        if value in ("openai", "claude"):
            estimate["provider"] = value
            changed = True
    if estimate.get("worker_model") not in {price["model"] for price in PRICES.values()}:
        default = "gpt-6-sol" if estimate.get("provider") == "openai" else "claude-sonnet-5-5"
        value = input(f"Worker model [{default}] (Enter for default): ").strip() or default
        estimate["worker_model"] = value
        changed = True
    for key, value in {"cooldown_minutes": 10, "max_cost_usd": 0.05, "max_output_tokens": 4000,
                       "max_extra_rounds": 1, "timeout_seconds": 180}.items():
        if key not in estimate or estimate[key] == "PENDING":
            estimate[key] = value
            changed = True
    if "shared_login_repos" not in cfo:
        value = input(f"Other repositories sharing this CFO.ai login, comma separated (Enter for {repo} only): ").strip()
        if value:
            repos = [item.strip() for item in value.split(",")]
            if repo not in repos or len(repos) != len(set(repos)) or any(
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) for item in repos):
                raise ValueError("shared login repositories must be unique owner/name values including --repo")
            cfo["shared_login_repos"] = repos
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        print(f"Saved {path}. Review and commit this config in its repository.")
    return path


def _workflows(repo: str, checkout: Path, config_path: Path) -> None:
    config_name = config_path.as_posix()
    shared = config_path.resolve().is_relative_to(ROOT) and config_path.resolve() != (
        checkout / ".github/prism.json").resolve()
    if shared:
        config_name = config_path.resolve().relative_to(ROOT).as_posix()
    for name in ("prism-estimate.yml", "prism-cfo-allocation.yml"):
        target = checkout / ".github/workflows" / name
        if target.exists():
            continue
        if not _yes(f"Add {target}?"):
            continue
        source = (ROOT / "examples" / name).read_text()
        if shared:
            source = source.replace("      prisme_ref: main\n",
                                    f"      prisme_ref: main\n      config: {config_name}\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        print(f"Added {target}. Review and commit it in {repo}.")


def run(repo: str, config_path: Path) -> bool:
    if not os.isatty(0):
        raise RuntimeError("run prism init in a terminal so keys can be entered hidden")
    check.env_notice()
    rows = check.inspect(repo, config_path)
    check.show(rows)
    failed = {name for name, _, status, _, _ in rows if status == "FAILED"}
    checkout = None
    if any(name == "Config" and status == "FAILED" for name, _, status, _, _ in rows):
        checkout = _checkout(repo)
        config_path = _config(repo, config_path, checkout)
    else:
        config_path = _config(repo, config_path, None)
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError):
        config = {}
    cfo = config.get("cfo") or {}
    repos = cfo.get("shared_login_repos", [repo])
    if not isinstance(repos, list) or repo not in repos:
        repos = [repo]

    entire_problem = next((detail for name, _, _, detail, _ in rows if name == "Entire"), "")
    if "Entire" in failed and entire_problem != "repository not ready":
        print(f"Entire searches the indexed repository. {check.GUIDE}how-to-get-entire")
        saved_entire = False
        if _yes("Fetch your current token with entire auth token?"):
            try:
                result = subprocess.run(["entire", "auth", "token"], capture_output=True,
                                        text=True, timeout=15)
                if result.returncode == 0 and result.stdout.strip():
                    _secret(repo, "ENTIRE_TOKEN", result.stdout.strip())
                    saved_entire = True
            except (OSError, subprocess.TimeoutExpired):
                print("Entire CLI could not supply a token. Sign in with entire login first.")
        if not saved_entire and _yes("Enter an Entire token instead?"):
            value = getpass.getpass("ENTIRE_TOKEN: ")
            if value:
                _secret(repo, "ENTIRE_TOKEN", value)
    provider = (config.get("estimate") or {}).get("provider")
    key = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(provider)
    if key and "Model key" in failed:
        print(f"{key} lets Prism predict files. {check.GUIDE}how-to-get-model-key")
        if _yes(f"Save {key}?"):
            value = getpass.getpass(f"{key}: ")
            if value:
                _secret(repo, key, value)
    if "GitHub token" in failed and (not os.getenv("CFO_SECRET_WRITE_TOKEN") or
                                      any("cannot" in detail for name, _, _, detail, _ in rows if name == "GitHub token")):
        print(f"This GitHub token saves each rotated CFO.ai login. {check.GUIDE}how-to-get-github-token")
        if _yes("Save CFO_SECRET_WRITE_TOKEN?"):
            value = getpass.getpass("CFO_SECRET_WRITE_TOKEN: ")
            if value:
                _secret(repo, "CFO_SECRET_WRITE_TOKEN", value, repos)
    if not (os.getenv("CFO_OAUTH_CLIENT_ID") and os.getenv("CFO_OAUTH_REFRESH_TOKEN")):
        print(f"CFO.ai allocates the budget; set up its tables first. {check.GUIDE}how-to-get-cfo-ai")
        if _yes("Sign in to CFO.ai now?"):
            from .cfo_login import login
            client_id, refresh = login(SimpleNamespace(repo=repos, client_id=None, port=None, timeout=300))
            os.environ["CFO_OAUTH_CLIENT_ID"] = client_id
            os.environ["CFO_OAUTH_REFRESH_TOKEN"] = refresh
    if not os.getenv("NEATLOGS_API_KEY") and _yes("Add optional NeatLogs tracing key?"):
        value = getpass.getpass("NEATLOGS_API_KEY: ")
        if value:
            _secret(repo, "NEATLOGS_API_KEY", value)
    if checkout is None:
        checkout = _checkout(repo)
    if checkout:
        _workflows(repo, checkout, config_path)
    print("\nFinal checks:")
    return check.run(repo, config_path)
