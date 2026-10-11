"""Run a small coding loop at an allocated commit and prepare spend reconciliation."""

import argparse
import asyncio
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path, PurePosixPath

from . import cfo_actions, glance, models
from .cfo_worker import BudgetedWorker, BudgetStop


EDIT_SCHEMA = {"type": "object", "properties": {
    "edits": {"type": "array", "items": {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"], "additionalProperties": False}},
    "done": {"type": "boolean"}}, "required": ["edits", "done"], "additionalProperties": False}
SYSTEM = "Edit only the provided repository files to address the issue. Return complete replacement file contents."


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"Git checkout check failed: {' '.join(args)}")
    return result.stdout.strip()


def _selected_path(checkout: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or ".git" in path.parts
            or path.as_posix() != name):
        raise ValueError(f"Unsafe selected path: {name!r}")
    target = checkout / name
    if not target.resolve().is_relative_to(checkout.resolve()) or target.is_symlink() or not target.is_file():
        raise ValueError(f"Selected path is not a regular checkout file: {name!r}")
    return target


async def run(estimate: dict, allocation: dict, event: dict, checkout: Path, *, call=models.call) -> dict:
    """The only model path is BudgetedWorker.call; tests inject an offline fake."""
    checkout = Path(checkout)
    repository = event.get("repository", {}).get("full_name")
    issue = event.get("issue", {})
    issue_url = issue.get("html_url")
    commit = estimate["repository_commit"]
    worker = BudgetedWorker(estimate, allocation, request_id=allocation.get("request_id"), repository=repository,
                            issue_url=issue_url, pinned_commit=commit, worker_model=allocation.get("chosen_model"))
    if (issue.get("number") != allocation["issue"] or
            _git(checkout, "rev-parse", "HEAD") != commit or
            _git(checkout, "status", "--porcelain", "--untracked-files=all") or
            _git(checkout, "remote", "get-url", "origin").removesuffix(".git") not in
            (f"https://github.com/{repository}", f"git@github.com:{repository}")):
        raise ValueError("Checkout, repository, or issue does not match the allocation at the pinned commit")
    issue_text = f"{issue.get('title') or ''}\n\n{issue.get('body') or ''}"
    paths = estimate["assumptions"]["measurement"]["included_paths"]
    files = {name: _selected_path(checkout, name).read_text() for name in paths}
    digest = hashlib.sha256(glance.assemble(issue_text, files).encode()).hexdigest()
    if not files or digest != estimate["assumptions"]["measurement"]["input_sha256"]:
        raise ValueError("Issue text or selected files differ from the saved estimate")
    changed = set()
    failure = None
    for _ in range(20):
        prompt = json.dumps({"issue": issue_text, "files": files}, ensure_ascii=False)
        try:
            answer = await worker.call("code", prompt, EDIT_SCHEMA, max_input_tokens=1,
                                       max_output_tokens=4000, system=SYSTEM, call=call)
        except BudgetStop:
            break
        except Exception as error:
            worker.status = "failed"
            failure = type(error).__name__
            break
        edits = answer.get("edits") if isinstance(answer, dict) else None
        if (not isinstance(answer, dict) or not isinstance(answer.get("done"), bool) or not isinstance(edits, list)
                or any(not isinstance(item, dict) or item.get("path") not in files
                       or not isinstance(item.get("content"), str) for item in edits)
                or len({item["path"] for item in edits}) != len(edits)):
            worker.status = "failed"
            failure = "InvalidWorkerResponse"
            break
        for item in edits:
            _selected_path(checkout, item["path"]).write_text(item["content"])
            files[item["path"]] = item["content"]
            changed.add(item["path"])
        if answer["done"]:
            break
    else:
        worker.status = "interrupted"
    usage = worker.finish()
    reconciliation = {"request_id": allocation["request_id"], "allocation_id": allocation["allocation_id"],
                      "usd_cap": usage["usd_cap"], "actual_usd": usage["spent_usd"],
                      "unused_reservation_usd": usage["remaining_usd"]}
    return {"schema": "prism.cfo-run/1", "repository": repository, "issue": issue["number"],
            "pinned_commit": commit, "status": usage["status"], "verified_outcome": "unverified",
            "changed_paths": sorted(changed), "usage": usage, "reconciliation": reconciliation,
            **({"error_type": failure} if failure else {})}


def writeback_plan(reconciliation: dict, budget_snapshot: tuple, request_snapshot: tuple) -> dict:
    """Prepare CFO edit_table_blocks arguments; applying both updates needs atomic support."""
    budget_data, budget_rows = budget_snapshot
    request_data, request_rows = request_snapshot
    request_id = reconciliation["request_id"]
    matches = [(member, row) for member, row in request_rows if member == request_id]
    if len(budget_rows) != 1 or len(matches) != 1:
        raise ValueError("CFO snapshots need exactly one budget and one allocation row")
    member, budget = budget_rows[0]
    _, row = matches[0]
    prefix = cfo_actions.PREFIX
    cap = Decimal(reconciliation["usd_cap"])
    actual = Decimal(reconciliation["actual_usd"])
    if (not cap.is_finite() or not actual.is_finite() or actual < 0 or actual > cap
            or Decimal(reconciliation["unused_reservation_usd"]) != cap - actual
            or row[prefix + "allocation ID"] != reconciliation["allocation_id"]
            or row[prefix + "decision"] != "allocated"
            or Decimal(str(row[prefix + "allocated USD cap"])) != cap
            or Decimal(str(row[prefix + "reservation USD"])) != cap
            or budget[prefix + "repository"] != row[prefix + "request repository"]):
        raise ValueError("CFO snapshot does not match the reconciliation")
    spending = Decimal(str(budget[prefix + "spending USD"])) + actual
    def edit(target, as_of, columns, values):
        return {"name": "edit_table_blocks", "arguments": {"org_id": cfo_actions.ORG_ID,
                "scenario": "Main Scenario", "update": {"target": target, "based_on": as_of,
                "values": {"columns": columns, "rows": [values]}}}}
    return {"requires_atomic_application": True, "updates": [
        edit(cfo_actions.BUDGET_TABLE, budget_data["as_of"], [prefix + "spending USD"], [member, str(spending)]),
        edit(cfo_actions.REQUEST_TABLE, request_data["as_of"], [prefix + "decision", prefix + "reservation USD"],
             [request_id, "reconciled", "0"])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("estimate_json", type=Path)
    parser.add_argument("allocation_json", type=Path)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Run output already exists")
    estimate = json.loads(args.estimate_json.read_text(), parse_float=Decimal)
    allocation = json.loads(args.allocation_json.read_text(), parse_float=Decimal)
    event = json.loads(args.event.read_text())
    async def execute():
        try:
            return await run(estimate, allocation, event, args.checkout)
        finally:
            await models.close()
    result = asyncio.run(execute())
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
