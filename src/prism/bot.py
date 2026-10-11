"""The GitHub issue bot: who may start a run, how often, and the comment it posts.

Issue titles, bodies and comments are untrusted. Nothing here runs them or puts them in the comment; the
comment is built from Prism's own JSON, with every value from outside escaped.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

from . import results, solve

# One run per this many minutes for the whole repository, to protect API credit.
RATE_LIMIT_MINUTES = 10
COMMAND = "/prism estimate"
INSIDERS = ("OWNER", "MEMBER", "COLLABORATOR")
BOT_LOGIN = "github-actions[bot]"
# Marks the one comment the bot keeps up to date and the reservation before spending.
MARKER = "<!-- prism-estimate -->"


def refusal(event: dict) -> str | None:
    """Why this webhook event must not start a run, or None when it may."""
    issue, comment = event.get("issue") or {}, event.get("comment")
    if "pull_request" in issue:
        return "pull requests are not estimated"
    if comment is not None:
        body = str(comment.get("body") or "")
        command = body.startswith(COMMAND) and (len(body) == len(COMMAND) or body[len(COMMAND)].isspace())
        if event.get("action") != "created" or not command:
            return f"not a new {COMMAND} comment"
        author = comment
    elif event.get("action") == "opened":
        author = issue
    else:
        return "not a newly opened issue"
    if author.get("author_association") not in INSIDERS:
        return "the author is not an owner, member or collaborator of the repository"
    return None


def is_ours(comment: dict) -> bool:
    return (comment.get("user") or {}).get("login") == BOT_LOGIN and str(comment.get("body") or "").startswith(MARKER)


def recent_comments_path(repo: str, now: datetime, page: int) -> str:
    since = (now - timedelta(minutes=RATE_LIMIT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"repos/{repo}/issues/comments?{urlencode({'since': since, 'sort': 'updated', 'direction': 'desc', 'per_page': 100, 'page': page})}"


def retry_after(comments: list[dict], now: datetime) -> datetime | None:
    """When the latest Prism reservation or result allows another run."""
    updates = [datetime.fromisoformat(comment["updated_at"].replace("Z", "+00:00"))
               for comment in comments if is_ours(comment)]
    allowed = max(updates) + timedelta(minutes=RATE_LIMIT_MINUTES) if updates else now
    return allowed if allowed > now else None


def gate(event: dict, fetch, now: datetime | None = None) -> str | None:
    """Raise PermissionError for an event that must not run; return a try-again note when rate limited, else None."""
    if reason := refusal(event):
        raise PermissionError(reason)
    now = now or datetime.now(timezone.utc)
    for page in range(1, 101):
        comments = fetch(recent_comments_path(event["repository"]["full_name"], now, page))
        if allowed := retry_after(comments, now):
            break
        if len(comments) < 100:
            return None
    else:
        raise RuntimeError("too many recent issue comments to verify the rate limit")
    if allowed is None:
        return None
    return (f"Prism runs at most once every {RATE_LIMIT_MINUTES} minutes in this repository and made no model call "
            f"for this request. Try again after {allowed:%H:%M} UTC with `{COMMAND}`.")


def _code(value) -> str:
    """A value from outside as an inline code span that cannot break the table or the span."""
    return "`" + escape(str(value), quote=False).replace("`", "'").replace("|", "/").replace("\n", " ").replace("\r", " ") + "`"


def _usd(value) -> str:
    return "not counted" if value is None else f"${value:,.4f}"


def _levels(values: dict, show) -> str:
    return " / ".join(show(values[name]) for name in solve.LEVELS)


def _unpriced_failures(prediction: dict | None) -> list[str]:
    return [provider for provider in results.PROVIDERS
            if isinstance((prediction or {}).get(provider), dict)
            and prediction[provider].get("status") == "failed"
            and "estimated_cost_usd" not in prediction[provider]]


def _spent(prediction: dict | None) -> str:
    if not prediction:
        return "unknown; no token usage was recorded, and a call in flight may still be billed"
    try:
        discovery = solve.discovery_cost(prediction)
    except (ValueError, TypeError, AttributeError):
        return "unknown; no token usage was recorded, and a call in flight may still be billed"
    calls = sum(run["model_calls"] or 0 for run in discovery["runs"].values())
    unknown = _unpriced_failures(prediction)
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
           run_url: str | None = None) -> str:
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
    unknown = _unpriced_failures(prediction)
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
        (f"End-to-end estimate ({spread})", _levels(estimate["end_to_end_cost_estimate"], lambda level: _usd(level["usd"]))),
        ("Worker model", _code(estimate["worker_model"])),
        ("Repository commit", _code(estimate["repository_commit"])),
        ("Confidence", _code(estimate["confidence"])),
    ]
    body = ["### Prism solve-cost estimate", "", "| | |", "|---|---|",
            *(f"| {name} | {value} |" for name, value in rows), "",
            "These are unvalidated guesses."]
    return results.redact("\n".join([MARKER, *body, "", *footer])) + "\n"
