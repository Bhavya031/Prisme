"""The GitHub issue bot: who may start a run, how often, and the comment it posts.

Issue titles, bodies and comments are untrusted. Nothing here runs them or puts them in the comment; the
comment is built from Prism's own JSON, with every value from outside escaped.
"""

from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import json
from urllib.parse import urlencode

from . import results, solve

DEFAULT_COOLDOWN_MINUTES = 10
COMMAND = "/prism estimate"
INSIDERS = ("OWNER", "MEMBER", "COLLABORATOR")
BOT_LOGIN = "github-actions[bot]"
# Marks the one comment the bot keeps up to date and the reservation before spending.
MARKER = "<!-- prism-estimate -->"
LOADING = "<!-- prism-loading -->"
JOB_TIMEOUT_MINUTES = 15


def source_marker(event: dict, commit: str) -> str:
    issue = event["issue"]
    issue_text = json.dumps([issue["title"], issue.get("body") or ""], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(issue_text.encode()).hexdigest()
    return f"<!-- prism-source:sha256={digest};commit={commit} -->"


def refusal(event: dict, *, allow_anyone: bool = False) -> str | None:
    """Why this webhook event must not start a run, or None when it may."""
    issue, comment = event.get("issue") or {}, event.get("comment")
    if "pull_request" in issue:
        return "pull requests are not estimated"
    if comment is not None:
        if (comment.get("user") or {}).get("type") == "Bot":
            return "bot comments do not request estimates"
        body = str(comment.get("body") or "")
        command = body == COMMAND
        if event.get("action") != "created" or not command:
            return f"not a new {COMMAND} comment"
        author = comment
    elif event.get("action") == "opened":
        if (issue.get("user") or {}).get("type") == "Bot":
            return "bot issues do not request estimates"
        author = issue
    else:
        return "not a newly opened issue"
    if not allow_anyone and author.get("author_association") not in INSIDERS:
        return "the author is not an owner, member or collaborator of the repository"
    return None


def is_ours(comment: dict) -> bool:
    return (comment.get("user") or {}).get("login") == BOT_LOGIN and str(comment.get("body") or "").startswith(MARKER)


def recent_comments_path(repo: str, now: datetime, page: int, cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES) -> str:
    since = (now - timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"repos/{repo}/issues/comments?{urlencode({'since': since, 'sort': 'updated', 'direction': 'desc', 'per_page': 100, 'page': page})}"


def retry_after(comments: list[dict], now: datetime, cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES) -> datetime | None:
    """When the latest Prism reservation or result allows another run."""
    updates = [datetime.fromisoformat(comment["updated_at"].replace("Z", "+00:00"))
               for comment in comments if is_ours(comment)]
    allowed = max(updates) + timedelta(minutes=cooldown_minutes) if updates else now
    return allowed if allowed > now else None


def gate(event: dict, fetch, now: datetime | None = None, *, commit: str | None = None,
         cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES, allow_anyone: bool = False) -> str | None:
    """Refuse unauthorized, duplicate, or repository-rate-limited issue requests before model calls."""
    if reason := refusal(event, allow_anyone=allow_anyone):
        raise PermissionError(reason)
    now = now or datetime.now(timezone.utc)
    if commit:
        repo = event["repository"]["full_name"]
        issue_number = event["issue"]["number"]
        marker = source_marker(event, commit)
        for page in range(1, 101):
            comments = fetch(f"repos/{repo}/issues/{issue_number}/comments?per_page=100&page={page}")
            for comment in comments:
                if not is_ours(comment):
                    continue
                body = comment["body"]
                link = comment.get("html_url") or f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{comment['id']}"
                updated = datetime.fromisoformat(comment["updated_at"].replace("Z", "+00:00"))
                if LOADING in body and now - updated < timedelta(minutes=JOB_TIMEOUT_MINUTES):
                    return f"Prism is already estimating this issue: {link}. No new model call was made."
                if marker in body and "### Prism solve-cost estimate" in body:
                    return f"Prism already estimated this issue text at this commit: {link}. No new model call was made."
            if len(comments) < 100:
                break
        else:
            raise RuntimeError("too many issue comments to verify duplicate estimates")
    if cooldown_minutes == 0:
        return None
    for page in range(1, 101):
        comments = fetch(recent_comments_path(event["repository"]["full_name"], now, page, cooldown_minutes))
        if allowed := retry_after(comments, now, cooldown_minutes):
            break
        if len(comments) < 100:
            return None
    else:
        raise RuntimeError("too many recent issue comments to verify the rate limit")
    if allowed is None:
        return None
    return (f"Prism runs at most once every {cooldown_minutes} minutes in this repository and made no model call "
            f"for this request. Try again after {allowed:%H:%M} UTC with `{COMMAND}`.")


def _code(value) -> str:
    """A value from outside as an inline code span that cannot break the table or the span."""
    return "`" + escape(str(value), quote=False).replace("`", "'").replace("|", "/").replace("\n", " ").replace("\r", " ") + "`"


def _usd(value) -> str:
    return "not counted" if value is None else f"${value:,.4f}"


def _levels(values: dict, show) -> str:
    return " / ".join(show(values[name]) for name in solve.LEVELS)


def _unknown_spend_failures(prediction: dict | None) -> list[str]:
    return [provider for provider in results.PROVIDERS
            if isinstance((prediction or {}).get(provider), dict)
            and prediction[provider].get("status") == "failed"
            and ("estimated_cost_usd" not in prediction[provider]
                 or (prediction[provider].get("budget") or {}).get("stopped_by") == "timeout_seconds")]


def _spent(prediction: dict | None) -> str:
    if not prediction:
        return "unknown; no token usage was recorded, and a call in flight may still be billed"
    try:
        discovery = solve.discovery_cost(prediction)
    except (ValueError, TypeError, AttributeError):
        return "unknown; no token usage was recorded, and a call in flight may still be billed"
    calls = sum(run["model_calls"] or 0 for run in discovery["runs"].values())
    unknown = _unknown_spend_failures(prediction)
    subtotal = "known subtotal " if unknown else ""
    extra = f"; additional spend unknown for {', '.join(unknown)}" if unknown else ""
    return (f"{subtotal}{_usd(discovery['usd'])} on file discovery ({calls} model call(s)); "
            f"estimated from token counts at list prices{extra}")


def _caps(prediction: dict | None) -> list[str]:
    notes = []
    for run in (prediction or {}).values():
        budget = run.get("budget") if isinstance(run, dict) else None
        if budget and budget.get("stopped_by"):
            notes.append(f"stopped by the {_code(budget['stopped_by'])} cap")
        elif budget and budget.get("over_max_cost"):
            notes.append(f"reached the ${budget['max_cost_usd']:g} cost cap")
    return notes


def render(prediction: dict | None = None, estimate: dict | None = None, error: str | None = None,
           run_url: str | None = None, source: str | None = None) -> str:
    """The issue comment: the estimate table, or what stopped the run and what it had spent."""
    footer = [f"[Workflow run]({run_url})"] if run_url else []
    if estimate is None:
        failed = [f"{provider}: {run['error'].get('message')}" for provider, run in (prediction or {}).items()
                  if isinstance(run, dict) and run.get("status") == "failed" and isinstance(run.get("error"), dict)]
        lines = (error or "").strip().splitlines()
        stopped = results.redact("; ".join(failed) or (lines[-1] if lines else "no error message was captured"))[:600]
        body = ["### Prism estimate did not finish", "",
                f"**Stopped by:** {_code(stopped)}" + "".join(f" ({note})" for note in _caps(prediction)), "",
                f"**Spent before stopping:** {_spent(prediction)}."]
        try:
            paths, _ = solve.predicted_files(prediction or {})
            body += ["", "**Predicted files (no solve estimate was made for them):** " + ", ".join(map(_code, paths))]
        except ValueError:
            pass
        return results.redact("\n".join([MARKER, *body, "", *footer])) + "\n"
    measurement = estimate["assumptions"]["measurement"]
    discovery = estimate["discovery_actual_cost"]
    files = ", ".join(map(_code, measurement["included_paths"])) or "none"
    if excluded := measurement.get("excluded_paths"):
        files += "; not measured: " + ", ".join(_code(item["path"]) for item in excluded)
    calls = sum(run.get("model_calls") or 0 for run in discovery["runs"].values())
    unknown = _unknown_spend_failures(prediction)
    spent = ("known subtotal " if unknown else "") + _usd(discovery["usd"])
    spent += (f" ({calls} model call(s))" if discovery["runs"] else "")
    if unknown:
        spent += f"; additional spend unknown for {', '.join(unknown)}"
    spent += "".join(f"; {note}" for note in _caps(prediction))
    spread = " / ".join(solve.LEVELS)
    rows = [
        ("Predicted files" if discovery["runs"] else "Files (given, not predicted)", files),
        ("Measured selected-context tokens", f"{estimate['measured_context_tokens']:,} ({_code(measurement['encoding'])})"),
        (f"Solve token estimate ({spread})", _levels(estimate["solve_token_estimate"], lambda level: f"{level['total']:,}")),
        (f"Solve cost estimate ({spread})", _levels(estimate["solve_cost_estimate"], lambda level: _usd(level["usd"]))),
        ("Discovery actual cost (separate from solving)", spent),
        (f"End-to-end estimate ({spread})", _levels(estimate["end_to_end_cost_estimate"], lambda level: _usd(level["usd"]))
         + ("; excludes unknown discovery spend" if unknown else "")),
        ("Worker model", _code(estimate["worker_model"])),
        ("Repository commit", _code(estimate["repository_commit"])),
    ]
    body = ["### Prism solve-cost estimate", "", "| | |", "|---|---|",
            *(f"| {name} | {value} |" for name, value in rows), "",
            "These are unvalidated guesses."]
    return results.redact("\n".join([MARKER, *([source] if source else []), *body, "", *footer])) + "\n"
