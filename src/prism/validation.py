"""Check a solve estimate against a real coding-worker run.

The estimate is saved before the run starts. The run's actual usage, cost and verified outcome are recorded
against it afterwards. A capped, interrupted or failed run is stored as incomplete: what it spent is a lower
bound on an unfinished attempt, never the cost of solving the issue.
"""

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

from . import cost, results, solve

ESTIMATE_SCHEMA = "prism.solve-estimate/1"
RECORD_SCHEMA = "prism.solve-run/1"
RUN_STATUSES = ("completed", "capped", "interrupted", "failed")
OUTCOMES = ("resolved", "unresolved", "unverified")
NO_ACCURACY_CLAIM = ("Counts over recorded runs only. One task, or file-discovery recall, is not evidence that the "
                     "estimator is accurate.")


def _digest(estimate: dict) -> str:
    return hashlib.sha256(json.dumps(estimate, sort_keys=True).encode()).hexdigest()


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError(f"Not an ISO 8601 time: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"Time needs a UTC offset or Z: {value!r}")
    return parsed


def save_estimate(estimate: dict, path: Path, task_id: str) -> dict:
    """Write the pre-run estimate. An existing file is never replaced, so the saved estimate cannot be revised later."""
    path = Path(path)
    if path.exists():
        raise ValueError(f"{path} already exists; a saved pre-run estimate is never overwritten")
    estimate = results.redact(estimate)
    saved = {"schema": ESTIMATE_SCHEMA, "task_id": task_id, "saved_at": results.now(),
             "estimate_sha256": _digest(estimate), "estimate": estimate}
    path.parent.mkdir(parents=True, exist_ok=True)
    results.write_json(path, saved)
    return saved


def record_run(saved: dict, *, started_at: str, run_status: str, verified_outcome: str, worker_model: str,
               uncached_input: int, cached_input: int, cache_write: int, output: int,
               billed_usd: float | None = None, usage_source: str = "unspecified", notes: str | None = None) -> dict:
    """Join one worker run's actual figures to the estimate saved before it."""
    if saved.get("schema") != ESTIMATE_SCHEMA or _digest(saved["estimate"]) != saved.get("estimate_sha256"):
        raise ValueError("Saved estimate is not a prism pre-run estimate or was changed after it was saved")
    estimate = saved["estimate"]
    if run_status not in RUN_STATUSES or verified_outcome not in OUTCOMES:
        raise ValueError(f"run_status must be one of {RUN_STATUSES} and verified_outcome one of {OUTCOMES}")
    if _time(started_at) < _time(saved["saved_at"]):
        raise ValueError(f"The run started at {started_at}, before the estimate was saved at {saved['saved_at']}; "
                         "only an estimate recorded before execution can be validated")
    if worker_model != estimate["worker_model"]:
        raise ValueError(f"The run used {worker_model!r} but the estimate is for {estimate['worker_model']!r}")
    actual = {"uncached_input": uncached_input, "cached_input": cached_input, "cache_write": cache_write, "output": output}
    for name, value in actual.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Actual {name} tokens must be a nonnegative integer")
    if billed_usd is not None and billed_usd < 0:
        raise ValueError("Billed cost must not be negative")
    if run_status != "completed":
        outcome_class = "incomplete"
    elif verified_outcome == "resolved":
        outcome_class = "solved"
    else:
        outcome_class = "completed_" + verified_outcome
    total_input = uncached_input + cached_input + cache_write
    rates = estimate["assumptions"]["pricing"]["usd_per_million_tokens"]
    # Cache writes are priced as uncached input here: the saved pricing carries no cache-write rate.
    per_token = {"input_cost_per_token": rates["uncached_input"] / 1e6, "output_cost_per_token": rates["output"] / 1e6}
    if rates.get("cached_input") is not None:
        per_token["cache_read_input_token_cost"] = rates["cached_input"] / 1e6
    list_price, _ = cost.model_cost(per_token, {"uncached_input": uncached_input + cache_write, "cached_input": cached_input,
                                                "cache_write": 0, "output": output})
    spent = billed_usd if billed_usd is not None else round(list_price, 6)
    record = {
        "schema": RECORD_SCHEMA, "task_id": saved["task_id"], "recorded_at": results.now(),
        "estimate_saved_at": saved["saved_at"], "estimate_sha256": saved["estimate_sha256"],
        "estimation_method": estimate["estimation_method"], "worker_model": worker_model,
        "run": {"started_at": started_at, "status": run_status, "verified_outcome": verified_outcome,
                "usage_source": usage_source, "notes": notes},
        "actual_tokens": {**actual, "input": total_input, "total": total_input + output},
        "actual_cost": {"usd": spent,
                        "basis": "billed amount reported by the caller" if billed_usd is not None else
                                 "actual tokens priced at the estimate's list prices; not a billed amount",
                        "list_price_usd": round(list_price, 6)},
        "outcome_class": outcome_class,
        "context_measurement": {"repository_commit": estimate["repository_commit"],
                                "measured_context_tokens": estimate["measured_context_tokens"],
                                **{key: estimate["assumptions"]["measurement"].get(key) for key in (
                                    "encoding", "glance_version", "glance_schema_version", "input_sha256", "input_bytes",
                                    "included_paths", "excluded_paths")}},
        "estimate": {"solve_token_estimate": estimate["solve_token_estimate"],
                     "solve_cost_estimate": estimate["solve_cost_estimate"]},
    }
    if outcome_class == "solved":
        record["solve_cost_usd"] = spent
        record["comparison"] = {
            name: _compare(value, {level: _pick(estimate, name, level) for level in solve.LEVELS})
            for name, value in (("input_tokens", total_input), ("output_tokens", output), ("cost_usd", spent))}
    else:
        # A censored observation: the spend is a lower bound on an attempt that did not solve the issue.
        record["solve_cost_usd"] = None
        record["spend_before_stop_usd"] = spent
        record["comparison"] = None
        record["already_above_high_estimate"] = {
            "input_tokens": total_input > _pick(estimate, "input_tokens", "high"),
            "output_tokens": output > _pick(estimate, "output_tokens", "high"),
            "cost_usd": spent > _pick(estimate, "cost_usd", "high")}
        record["note"] = ("Not a solved run. Its tokens and spend are not the cost of solving the issue and are "
                          "excluded from every estimate-versus-actual comparison.")
    return record


def _pick(estimate: dict, name: str, level: str):
    if name == "cost_usd":
        return estimate["solve_cost_estimate"][level]["usd"]
    return estimate["solve_token_estimate"][level]["input" if name == "input_tokens" else "output"]


def _compare(actual, estimated: dict) -> dict:
    return {"actual": actual, **estimated,
            "within_range": estimated["low"] <= actual <= estimated["high"],
            "position": "below_low" if actual < estimated["low"] else "above_high" if actual > estimated["high"] else "within",
            "actual_over_central": round(actual / estimated["central"], 4) if estimated["central"] else None}


def compare(records: list[dict]) -> dict:
    """Counts across recorded runs. Reports what was observed and claims nothing about accuracy."""
    if any(record.get("schema") != RECORD_SCHEMA for record in records):
        raise ValueError("Expected records written by `prism solve-record`")
    solved = [record for record in records if record["outcome_class"] == "solved"]
    classes = sorted({record["outcome_class"] for record in records})
    summary = {
        "runs": len(records),
        "by_outcome_class": {name: sum(record["outcome_class"] == name for record in records) for name in classes},
        "solved_runs": len(solved),
        "tasks": sorted({record["task_id"] for record in records}),
        "within_range_solved_runs": {
            name: sum(record["comparison"][name]["within_range"] for record in solved)
            for name in ("input_tokens", "output_tokens", "cost_usd")} if solved else None,
        "actual_over_central": {
            name: [record["comparison"][name]["actual_over_central"] for record in solved]
            for name in ("input_tokens", "output_tokens", "cost_usd")} if solved else None,
        "incomplete_runs_excluded": sum(record["outcome_class"] != "solved" for record in records),
        "accuracy_claim": None,
        "note": NO_ACCURACY_CLAIM,
    }
    return summary


def glance_record_args(record: dict, records_path: str | Path) -> list[str]:
    """Arguments for `glance record`, so `glance calibrate` can use the run. Incomplete runs are marked censored."""
    tokens, estimated = record["actual_tokens"], record["estimate"]["solve_token_estimate"]
    censored = {"solved": "none", "incomplete": "budget-stopped"}.get(record["outcome_class"], "failed")
    if record["run"]["status"] == "failed":
        censored = "failed"
    args = ["record", "--id", record["task_id"], "--records", str(records_path),
            "--category", record["estimation_method"],
            "--predicted-low", str(estimated["low"]["total"]), "--predicted-high", str(estimated["high"]["total"]),
            # Glance has no cache-write column; those tokens are billed as input and are counted as uncached.
            "--actual-uncached", str(tokens["uncached_input"] + tokens["cache_write"]),
            "--actual-cached", str(tokens["cached_input"]), "--actual-output", str(tokens["output"]),
            "--outcome", record["run"]["verified_outcome"], "--censored", censored,
            "--provenance", record["run"]["usage_source"]]
    if record["actual_cost"]["basis"].startswith("billed"):
        args += ["--spend", str(record["actual_cost"]["usd"])]
    return args


def mirror_to_glance(record: dict, glance_binary: str, records_path: str | Path, run=subprocess.run) -> None:
    result = run([glance_binary, *glance_record_args(record, records_path)], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"glance record failed: {(result.stderr or result.stdout).strip()[:500]}")
