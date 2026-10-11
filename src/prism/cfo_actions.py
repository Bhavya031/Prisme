"""Allocate one saved Prism estimate in the CFO.ai tables named by the repository's .github/prism.json."""

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path


MCP = "https://api.cfo.ai/mcp"
TOKEN = "https://complete-cottage-73.authkit.app/oauth2/token"
CFO_KEYS = ("org_id", "scenario", "page", "budget_table", "request_table", "decision_table", "column_prefix")
CFO = {}  # the "cfo" section of --config, loaded in main()
REQUEST_COLUMNS = (
    "request repository", "issue URL", "pinned commit", "worker model",
    "predicted input tokens", "predicted uncached input tokens", "predicted cached input tokens",
    "predicted output tokens", "predicted total tokens", "estimated USD", "decision",
    "allocation ID", "allocated USD cap", "reservation USD", "available balance USD", "reason",
)


def github_repository():
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise RuntimeError("GITHUB_REPOSITORY is not set; it names the repository whose budget and secrets are used")
    return repository


def refresh_access_token():
    repository = github_repository()
    client_id = os.environ.get("CFO_OAUTH_CLIENT_ID")
    refresh_token = os.environ.get("CFO_OAUTH_REFRESH_TOKEN")
    writer = os.environ.get("CFO_SECRET_WRITE_TOKEN")
    if not all((client_id, refresh_token, writer)):
        raise RuntimeError("CFO_OAUTH_CLIENT_ID, CFO_OAUTH_REFRESH_TOKEN, and CFO_SECRET_WRITE_TOKEN are required")
    env = os.environ.copy()
    env["GH_TOKEN"] = writer
    # Prove secret writes work before rotating the only stored refresh token.
    preflight = subprocess.run(
        ["gh", "secret", "set", "CFO_SECRET_WRITE_TOKEN", "--repo", repository],
        input=writer, text=True, capture_output=True, env=env,
    )
    if preflight.returncode:
        detail = preflight.stderr.strip() if preflight.stderr else f"gh exit {preflight.returncode}"
        raise RuntimeError(f"CFO secret-write token cannot update {repository} Actions secrets; OAuth refresh was not attempted: {detail}")
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token,
    }).encode()
    request = urllib.request.Request(TOKEN, payload, {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            reason = json.load(error).get("error", "unknown_error")
        except (ValueError, TypeError):
            reason = "unknown_error"
        raise RuntimeError(f"CFO OAuth refresh failed: {reason}; browser authorization may be required") from None
    if not result.get("access_token") or not result.get("refresh_token"):
        raise RuntimeError("CFO OAuth refresh returned no access or refresh token")
    # Refresh tokens rotate; persist the replacement before using the access token.
    for attempt in range(3):
        saved = subprocess.run(
            ["gh", "secret", "set", "CFO_OAUTH_REFRESH_TOKEN", "--repo", repository],
            input=result["refresh_token"], text=True, capture_output=True, env=env,
        )
        if saved.returncode == 0:
            return result["access_token"]
    detail = saved.stderr.strip() if saved.stderr else f"gh exit {saved.returncode}"
    raise RuntimeError(f"CFO refresh succeeded but the rotated credential could not be saved to {repository} Actions secrets; reauthorization may be required: {detail}")


def mcp(access_token, method, params, request_id):
    config = (
        f'url = "{MCP}"\nrequest = "POST"\n'
        'header = "Content-Type: application/json"\n'
        'header = "Accept: application/json, text/event-stream"\n'
        'header = "MCP-Protocol-Version: 2025-06-18"\n'
        f'header = "Authorization: Bearer {access_token}"\n'
    )
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    response = subprocess.run(
        ["curl", "--config", "-", "--silent", "--show-error", "--max-time", "90",
         "--data", json.dumps(message), "--write-out", "\nHTTP_STATUS:%{http_code}"],
        input=config, text=True, capture_output=True,
    )
    body, marker, status = response.stdout.rpartition("\nHTTP_STATUS:")
    if response.returncode or not marker or status != "200":
        raise RuntimeError(f"CFO MCP {method} failed (HTTP {status or 'unknown'}); check token and workspace access")
    events = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    result = json.loads(events[-1] if events else body)
    if "error" in result or result.get("result", {}).get("isError"):
        raise RuntimeError(f"CFO MCP {method} returned an error; no allocation was confirmed")
    return result.get("result", {})


def read_table(access_token, name, request_id):
    data = mcp(access_token, "tools/call", {"name": "inspect_table_blocks", "arguments": {
        "org_id": CFO["org_id"], "read": {"target": name},
    }}, request_id).get("structuredContent", {})
    if (data.get("table", {}).get("display_name") != name
            or data.get("page", {}).get("display_name") != CFO["page"]
            or data.get("scenario") != CFO["scenario"]
            or data.get("coverage", {}).get("calculation") != "complete"
            or data.get("coverage", {}).get("delivery") != "complete"
            or data.get("next")):
        # shortcut: fail closed on paginated tables; add continuation when they grow.
        raise RuntimeError(f"CFO {name} read is incomplete or outside the configured page")
    columns = [column["display_name"] for column in data["columns"]]
    rows = []
    for row in data["rows"]:
        if "member" not in row:
            continue
        values = row.get("values", [])
        if len(values) != len(columns):
            raise RuntimeError(f"CFO {name} row is incomplete")
        rows.append((row["member"], {column: value.get("value") if isinstance(value, dict) else value
                                    for column, value in zip(columns, values)}))
    return data, rows


def usd(value, column):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RuntimeError(f"CFO {column} is not a valid USD amount") from None
    if not amount.is_finite() or amount < 0:
        raise RuntimeError(f"CFO {column} is not a valid USD amount")
    return amount


def allocate(estimate_path, run_id, issue_number):
    if run_id <= 0 or issue_number <= 0:
        raise ValueError("run ID and issue number must be positive")
    estimate = json.loads(Path(estimate_path).read_text())
    commit = estimate.get("repository_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Saved estimate has no pinned commit")
    central = estimate["solve_token_estimate"]["central"]
    repository = github_repository()
    request_id = f"{repository}/issue{issue_number}/run{run_id}"
    request = {
        "request_id": request_id,
        "repository": repository,
        "issue_url": f"https://github.com/{repository}/issues/{issue_number}",
        "pinned_commit": commit,
        "predicted_tokens": {key: central[key] for key in ("input", "uncached_input", "cached_input", "output", "total")},
    }
    access_token = refresh_access_token()
    if "capabilities" not in mcp(access_token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "prism-cfo-actions", "version": "0.1"},
    }, 1):
        raise RuntimeError("CFO MCP initialization failed")
    PREFIX = CFO["column_prefix"]
    decision_data, decision_rows = read_table(access_token, CFO["decision_table"], 2)
    inputs = {"K2 test repository": repository, "K2 test issue URL": request["issue_url"],
              "K2 test uncached input tokens": central["uncached_input"],
              "K2 test cached input tokens": central["cached_input"],
              "K2 test output tokens": central["output"]}
    matches = [row for member, row in decision_rows if member == request_id]
    if len(matches) > 1 or (matches and any(matches[0].get(key) != value for key, value in inputs.items())):
        raise RuntimeError("CFO decision request ID already has different inputs")
    if not matches:
        mcp(access_token, "tools/call", {"name": "edit_table_blocks", "arguments": {
            "org_id": CFO["org_id"], "scenario": CFO["scenario"], "update": {
                "target": CFO["decision_table"], "based_on": decision_data["as_of"],
                "dimensions": [{"target": "K2 decision test request", "add_items": [request_id]}],
                "values": {"columns": list(inputs), "rows": [[request_id, *inputs.values()]]},
            },
        }}, 3)
    _, decision_rows = read_table(access_token, CFO["decision_table"], 4)
    matches = [row for member, row in decision_rows if member == request_id]
    if len(matches) != 1 or any(matches[0].get(key) != value for key, value in inputs.items()):
        raise RuntimeError("CFO decision read-back did not confirm the request inputs")
    row = matches[0]
    decision = row.get("K2 test decision")
    model = row.get("K2 test chosen model")
    reason = row.get("K2 test reason")
    if decision not in ("approved", "blocked") or not isinstance(reason, str) or not reason:
        raise RuntimeError("CFO decision or reason is missing or invalid")
    recommendations = []
    for index in range(1, 4):
        name = row.get(f"K2 test recommendation {index}")
        cost = row.get(f"K2 test recommendation {index} cost USD")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"CFO recommendation {index} is missing")
        recommendations.append({"model": name, "usd": float(usd(cost, f"recommendation {index} cost"))})
    recommended = {item["model"] for item in recommendations}
    other_models = []
    for column, value in row.items():
        if column.startswith("cost — ") and column.endswith(" (USD)") and value not in (None, ""):
            name = column[7:-6]
            if name not in recommended:
                other_models.append({"model": name, "usd": float(usd(value, column))})
    other_models.sort(key=lambda item: (item["usd"], item["model"]))
    summary = {"budget_usd": row.get("K2 test budget USD"), "spent_usd": row.get("K2 test spending USD"),
               "reserved_before_usd": row.get("K2 test existing reservations USD"),
               "max_budget_fraction": row.get("K2 test max budget fraction"),
               "available_usd": row.get("K2 test available USD"),
               "request_limit_usd": row.get("K2 test request limit USD"),
               "chosen_model": model, "recommendations": recommendations, "other_models": other_models}
    if decision == "blocked":
        return {"request_id": request_id, "decision": decision, "usd_cap": None, "reason": reason} | summary
    amount = usd(row.get("K2 test allocated USD"), "allocated USD")
    available = usd(row.get("K2 test available USD"), "available USD")
    limit = usd(row.get("K2 test request limit USD"), "request limit USD")
    if not (0 < amount <= available and amount <= limit):
        raise RuntimeError("CFO approved allocation must be > 0 and <= available USD and request limit USD")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeError("CFO approved decision has no chosen model")
    request["model"] = model
    request["estimated_usd"] = float(amount)
    request["reason"] = reason
    request_data, rows = read_table(access_token, CFO["request_table"], 5)
    if [column["display_name"] for column in request_data["columns"]] != [PREFIX + name for name in REQUEST_COLUMNS]:
        raise RuntimeError("CFO request table shape changed")
    if not any(member == request_id for member, _ in rows):
        columns = [PREFIX + name for name in REQUEST_COLUMNS]
        values = [repository, request["issue_url"], commit, model,
                  *(central[key] for key in ("input", "uncached_input", "cached_input", "output", "total")),
                  float(amount), "approved", f"allocation/{request_id}", float(amount), float(amount),
                  float(available - amount), reason]
        mcp(access_token, "tools/call", {"name": "edit_table_blocks", "arguments": {
            "org_id": CFO["org_id"], "scenario": CFO["scenario"], "update": {
                "target": CFO["request_table"], "based_on": request_data["as_of"],
                "dimensions": [{"target": PREFIX + "request ID", "add_items": [request_id]}],
                "values": {"columns": columns, "rows": [[request_id, *values]]},
            },
        }}, 6)
    return allocate_existing(access_token, request, request_id) | summary


def allocate_existing(access_token, request, request_id):
    PREFIX = CFO["column_prefix"]
    _, rows = read_table(access_token, CFO["request_table"], 6)
    matches = [values for member, values in rows if member == request_id]
    if len(matches) != 1:
        raise RuntimeError("CFO allocation read-back did not find exactly one request row")
    row = matches[0]
    for key, column in (("repository", "request repository"), ("issue_url", "issue URL"),
                        ("pinned_commit", "pinned commit"), ("model", "worker model"),
                        ("estimated_usd", "estimated USD")):
        actual, expected = row[PREFIX + column], request[key]
        if key == "estimated_usd":
            actual, expected = usd(actual, "read-back estimate"), usd(expected, "allocated USD")
        if actual != expected:
            raise RuntimeError("CFO allocation read-back has different request inputs")
    if row[PREFIX + "decision"] != "approved" or row[PREFIX + "allocation ID"] != f"allocation/{request_id}":
        raise RuntimeError("CFO allocation read-back is not approved")
    if (usd(row[PREFIX + "allocated USD cap"], "read-back cap") != usd(request["estimated_usd"], "allocated USD")
            or usd(row[PREFIX + "reservation USD"], "read-back reservation") != usd(request["estimated_usd"], "allocated USD")):
        raise RuntimeError("CFO allocation read-back has the wrong USD cap or reservation")
    if row[PREFIX + "reason"] != request["reason"]:
        raise RuntimeError("CFO allocation read-back has a different reason")
    return {"request_id": request_id, "allocation_id": row[PREFIX + "allocation ID"],
            "repository": request["repository"], "issue": int(request["issue_url"].rsplit("/", 1)[-1]),
            "issue_url": request["issue_url"],
            "pinned_commit": request["pinned_commit"],
            "usd_cap": row[PREFIX + "allocated USD cap"],
            "decision": row[PREFIX + "decision"], "reason": row[PREFIX + "reason"],
            "request_row_count": len(matches), "table_row_count": len(rows),
            "request_reservation_usd": sum(item[PREFIX + "reservation USD"] or 0 for item in matches),
            "table_reservation_usd": sum(item[PREFIX + "reservation USD"] or 0 for _, item in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("estimate_json")
    parser.add_argument("run_id", type=int)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--config", required=True, help="the repository's .github/prism.json")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        CFO.update(json.loads(Path(args.config).read_text()).get("cfo") or {})
        missing = [key for key in CFO_KEYS if not isinstance(CFO.get(key), str) or not CFO[key]]
        if missing:
            raise RuntimeError(f"{args.config} needs a non-empty string for cfo." + ", cfo.".join(missing))
        result = allocate(args.estimate_json, args.run_id, args.issue_number)
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        if result["decision"] != "approved":
            parser.exit(1, "CFO allocation stopped: " + result["reason"] + "\n")
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
