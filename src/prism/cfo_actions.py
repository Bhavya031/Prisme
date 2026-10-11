"""Allocate one saved k-2 estimate in the isolated CFO.ai synthetic model."""

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from prism.cfo_allocation import decide


MCP = "https://api.cfo.ai/mcp"
TOKEN = "https://complete-cottage-73.authkit.app/oauth2/token"
ORG_ID = "102909"
PAGE = "SYNTHETIC prism-test k-2"
BUDGET_TABLE = "SYNTHETIC prism-test k-2 budget and policy"
REQUEST_TABLE = "SYNTHETIC prism-test k-2 request allocations"
PREFIX = "SYNTHETIC prism-test "
REQUEST_COLUMNS = (
    "request repository", "issue URL", "pinned commit", "worker model",
    "predicted input tokens", "predicted uncached input tokens", "predicted cached input tokens",
    "predicted output tokens", "predicted total tokens", "estimated USD", "decision",
    "allocation ID", "allocated USD cap", "reservation USD", "available balance USD", "reason",
)


def refresh_access_token():
    client_id = os.environ.get("CFO_OAUTH_CLIENT_ID")
    refresh_token = os.environ.get("CFO_OAUTH_REFRESH_TOKEN")
    writer = os.environ.get("CFO_SECRET_WRITE_TOKEN")
    if not all((client_id, refresh_token, writer)):
        raise RuntimeError("CFO_OAUTH_CLIENT_ID, CFO_OAUTH_REFRESH_TOKEN, and CFO_SECRET_WRITE_TOKEN are required")
    env = os.environ.copy()
    env["GH_TOKEN"] = writer
    # Prove secret writes work before rotating the only stored refresh token.
    preflight = subprocess.run(
        ["gh", "secret", "set", "CFO_SECRET_WRITE_TOKEN", "--repo", "Bhavya031/k-2"],
        input=writer, text=True, capture_output=True, env=env,
    )
    if preflight.returncode:
        raise RuntimeError("CFO secret-write token cannot update k-2 Actions secrets; OAuth refresh was not attempted")
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
            ["gh", "secret", "set", "CFO_OAUTH_REFRESH_TOKEN", "--repo", "Bhavya031/k-2"],
            input=result["refresh_token"], text=True, capture_output=True, env=env,
        )
        if saved.returncode == 0:
            return result["access_token"]
    raise RuntimeError("CFO refresh succeeded but the rotated credential could not be saved to k-2 Actions secrets; reauthorization may be required")


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
        "org_id": ORG_ID, "read": {"target": name},
    }}, request_id).get("structuredContent", {})
    if (data.get("table", {}).get("display_name") != name
            or data.get("page", {}).get("display_name") != PAGE
            or data.get("scenario") != "Main Scenario"
            or data.get("coverage", {}).get("calculation") != "complete"
            or data.get("coverage", {}).get("delivery") != "complete"
            or data.get("next")):
        # shortcut: fail closed on paginated synthetic tables; add continuation when they grow.
        raise RuntimeError(f"CFO {name} read is incomplete or outside the synthetic page")
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


def allocate(estimate_path, run_id):
    estimate = json.loads(Path(estimate_path).read_text())
    commit = estimate.get("repository_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Saved estimate has no pinned commit")
    central = estimate["solve_token_estimate"]["central"]
    request_id = f"SYNTHETIC/prism-ci-k2-issue40-run{run_id}"
    request = {
        "request_id": request_id,
        "repository": "Bhavya031/k-2",
        "issue_url": "https://github.com/Bhavya031/k-2/issues/40",
        "pinned_commit": commit,
        "model": estimate["worker_model"],
        "estimated_usd": estimate["end_to_end_cost_estimate"]["central"]["usd"],
        "predicted_tokens": {key: central[key] for key in ("input", "uncached_input", "cached_input", "output", "total")},
    }
    access_token = refresh_access_token()
    if "capabilities" not in mcp(access_token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "prism-cfo-actions", "version": "0.1"},
    }, 1):
        raise RuntimeError("CFO MCP initialization failed")
    budget_data, budget_rows = read_table(access_token, BUDGET_TABLE, 2)
    request_data, rows = read_table(access_token, REQUEST_TABLE, 3)
    if (len(budget_rows) != 1 or
            [column["display_name"] for column in request_data["columns"]]
            != [PREFIX + name for name in REQUEST_COLUMNS]):
        raise RuntimeError("CFO synthetic budget or request table shape changed")
    _, budget_values = budget_rows[0]
    budget = {"repository": budget_values[PREFIX + "repository"],
              "budget_usd": budget_values[PREFIX + "budget USD"],
              "spending_usd": budget_values[PREFIX + "spending USD"],
              "existing_reservations_usd": budget_values[PREFIX + "existing reservations USD"]}
    policy = {"max_budget_fraction": budget_values[PREFIX + "max budget fraction"]}
    allocations = []
    for member, values in rows:
        allocations.append({
            "request_id": member,
            "repository": values[PREFIX + "request repository"],
            "issue_url": values[PREFIX + "issue URL"],
            "pinned_commit": values[PREFIX + "pinned commit"],
            "model": values[PREFIX + "worker model"],
            "estimated_usd": values[PREFIX + "estimated USD"],
            "decision": values[PREFIX + "decision"],
            "allocation_id": values[PREFIX + "allocation ID"],
            "allocated_usd_cap": values[PREFIX + "allocated USD cap"],
            "reservation_usd": values[PREFIX + "reservation USD"],
            "available_balance": values[PREFIX + "available balance USD"],
            "reason": values[PREFIX + "reason"],
        })
    budget["existing_reservations_usd"] += sum(row["reservation_usd"] for row in allocations
                                                if row["repository"] == request["repository"] and row["decision"] == "allocated")
    decision, new_row = decide(request, budget, policy, allocations)
    if decision["decision"] != "allocated":
        raise RuntimeError("CFO allocation stopped: " + decision["reason"])
    if new_row:
        # Re-read immediately before the additive write; only this namespaced member is touched.
        latest_data, latest_rows = read_table(access_token, REQUEST_TABLE, 4)
        if any(member == request_id for member, _ in latest_rows):
            return allocate_existing(access_token, request, request_id)
        if latest_data["as_of"] != request_data["as_of"] or latest_rows != rows:
            raise RuntimeError("CFO request table changed before write; rerun to recalculate the balance")
        columns = [PREFIX + name for name in REQUEST_COLUMNS]
        values = [request["repository"], request["issue_url"], commit, request["model"],
                  *(central[key] for key in ("input", "uncached_input", "cached_input", "output", "total")),
                  request["estimated_usd"], decision["decision"], decision["allocation_id"],
                  decision["allocated_usd_cap"], new_row["reservation_usd"],
                  decision["available_balance"], decision["reason"]]
        mcp(access_token, "tools/call", {"name": "edit_table_blocks", "arguments": {
            "org_id": ORG_ID, "scenario": "Main Scenario", "update": {
                "target": REQUEST_TABLE,
                "based_on": request_data["as_of"],
                "dimensions": [{"target": "SYNTHETIC prism-test request ID", "add_items": [request_id]}],
                "values": {"columns": columns, "rows": [[request_id, *values]]},
            },
        }}, 5)
    return allocate_existing(access_token, request, request_id)


def allocate_existing(access_token, request, request_id):
    _, rows = read_table(access_token, REQUEST_TABLE, 6)
    matches = [values for member, values in rows if member == request_id]
    if len(matches) != 1:
        raise RuntimeError("CFO allocation read-back did not find exactly one request row")
    row = matches[0]
    for key, column in (("repository", "request repository"), ("issue_url", "issue URL"),
                        ("pinned_commit", "pinned commit"), ("model", "worker model"),
                        ("estimated_usd", "estimated USD")):
        if row[PREFIX + column] != request[key]:
            raise RuntimeError("CFO allocation read-back has different request inputs")
    if row[PREFIX + "decision"] != "allocated" or not row[PREFIX + "allocation ID"]:
        raise RuntimeError("CFO allocation read-back is not allocated")
    if row[PREFIX + "allocated USD cap"] != request["estimated_usd"] or row[PREFIX + "reservation USD"] != request["estimated_usd"]:
        raise RuntimeError("CFO allocation read-back has the wrong USD cap or reservation")
    return {"request_id": request_id, "allocation_id": row[PREFIX + "allocation ID"],
            "repository": request["repository"], "issue": 40, "issue_url": request["issue_url"],
            "pinned_commit": request["pinned_commit"],
            "usd_cap": row[PREFIX + "allocated USD cap"],
            "decision": row[PREFIX + "decision"], "reason": row[PREFIX + "reason"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("estimate_json")
    parser.add_argument("run_id", type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = allocate(args.estimate_json, args.run_id)
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    except (KeyError, ValueError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
