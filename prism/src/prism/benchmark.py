import asyncio
import json
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import github_context, models, pipeline, results, tracing
from .types import Prediction


def _prediction_result(prediction: Prediction, truth: set[str]) -> dict:
    result = asdict(prediction)
    result["tokens"] = result.pop("usage")
    predicted = set(prediction.predicted)
    matched = predicted & truth
    result.update({
        "matched": sorted(matched),
        "missed": sorted(truth - predicted),
        "extra": sorted(predicted - truth),
        "precision": len(matched) / len(predicted) if predicted else 0,
        "recall": len(matched) / len(truth),
        "exact_match": predicted == truth,
    })
    return result


def _trace_id(span) -> str | None:
    context = getattr(span, "get_span_context", lambda: None)()
    trace_id = getattr(context, "trace_id", None)
    return format(trace_id, "032x") if trace_id is not None else None


class _CaseFailure(Exception):
    """A case that failed after some predictions finished; those stay ungraded."""

    def __init__(self, error: Exception, predictions: list[Prediction], durations: dict):
        super().__init__(str(error))
        self.error = error
        self.predictions = predictions
        self.durations = durations


async def _timed(coroutine, durations: dict, name: str):
    started = time.monotonic()
    try:
        return await coroutine
    finally:
        durations[f"{name}_seconds"] = round(time.monotonic() - started, 2)


async def _case(case: dict) -> dict:
    durations = {}
    predictions = []
    try:
        started = time.monotonic()
        with tracing.step("collect-context", "TOOL"):
            ctx = await _timed(asyncio.to_thread(
                github_context.build_context,
                case["issue_url"],
                entire_repo=f"gh/{case['fork']}",
                pr_url=case["pr_url"],
                pre_merge_sha=case["pre_merge_sha"],
            ), durations, "context")
        # Both model submissions complete before grading can fetch the fixing PR files.
        outcomes = await asyncio.gather(
            _timed(pipeline.predict(ctx, "claude"), durations, "claude"),
            _timed(pipeline.predict(ctx, "openai"), durations, "openai"),
            return_exceptions=True,
        )
        predictions = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        if error := next((outcome for outcome in outcomes if isinstance(outcome, Exception)), None):
            raise error
        claude, openai = outcomes
        with tracing.step("grade", "TOOL"):
            truth = await _timed(
                asyncio.to_thread(github_context.true_files, case["pr_url"], ctx.known_paths), durations, "grade",
            )
        if not truth:
            raise ValueError("No modified existing files in target PR")
        return {
            **_case_identity(case),
            "status": "ok",
            "wall_seconds": round(time.monotonic() - started, 2),
            "durations": durations,
            "true_paths": sorted(truth),
            "claude": _prediction_result(claude, truth),
            "openai": _prediction_result(openai, truth),
        }
    except Exception as error:
        raise _CaseFailure(error, predictions, durations) from error
    finally:
        await models.close()


def _case_identity(case: dict) -> dict:
    return {
        "case_id": results.case_id(case),
        "issue": case["issue_url"],
        "pr": case["pr_url"],
        "fork": case["fork"],
        "pre_merge_sha": case["pre_merge_sha"],
    }


def _failed_case(case: dict, error: Exception) -> dict:
    """A failed case keeps what was spent on it; its predictions are never graded."""
    failure = error if isinstance(error, _CaseFailure) else None
    result = {
        **_case_identity(case),
        "status": "failed",
        "error": results.error_details(failure.error if failure else error),
        "durations": failure.durations if failure else {},
    }
    for prediction in failure.predictions if failure else []:
        ungraded = asdict(prediction)
        ungraded["tokens"] = ungraded.pop("usage")
        result[prediction.provider] = {**ungraded, "graded": False}
    return result


def _summary(cases: list[dict]) -> dict:
    graded = [case for case in cases if case["status"] == "ok"]
    if not graded:
        return {}
    return {
        provider: {
            "cases": len(graded),
            "mean_precision": round(sum(case[provider]["precision"] for case in graded) / len(graded), 3),
            "mean_recall": round(sum(case[provider]["recall"] for case in graded) / len(graded), 3),
            "exact_matches": sum(case[provider]["exact_match"] for case in graded),
            "model_calls": sum(case[provider]["model_calls"] for case in graded),
            "tokens": {
                key: sum(case[provider]["tokens"][key] or 0 for case in graded)
                for key in ("input", "output", "cached_input", "cache_write", "reasoning")
            },
            "estimated_cost_usd": round(sum(case[provider]["estimated_cost_usd"] for case in graded), 6),
            "missed_files": {case["issue"]: case[provider]["missed"] for case in graded},
        }
        for provider in ("claude", "openai")
    }


def _assert_no_target_leak(ctx) -> None:
    if ctx.target_pr is not None and re.search(
        rf"(?<!\d)#{ctx.target_pr}(?!\d)|/pull/{ctx.target_pr}(?!\d)", json.dumps(asdict(ctx.bundle)), re.I
    ):
        raise ValueError("Target PR reference leaked into context")


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(manifest: Path, *, one: bool, dry_run: bool, output_dir: Path | None = None, stage: str | None = None) -> dict:
    cases = json.loads(manifest.read_text())
    if not cases:
        raise ValueError("Manifest has no benchmark cases")
    # --one prefers the pytest case and falls back to the first for other manifests.
    selected = ([case for case in cases if "/pytest/" in case["issue_url"]] or cases)[:1] if one else cases
    if dry_run:
        # A dry run is free and leaves no run record.
        contexts = []
        for case in selected:
            ctx = github_context.build_context(
                case["issue_url"], entire_repo=f"gh/{case['fork']}", pr_url=case["pr_url"], pre_merge_sha=case["pre_merge_sha"]
            )
            _assert_no_target_leak(ctx)
            contexts.append({"issue": case["issue_url"], "model_context": asdict(ctx.bundle)})
        return {"cases": contexts}

    if stage in results.EXISTING_STAGES:
        raise ValueError(f"Stage {stage} already has results; choose a new stage name")
    store = results.RunStore(output_dir or results.DEFAULT_OUTPUT_DIR, manifest, cases, selected, stage)
    _progress(f"run {store.run_id}: {len(selected)} case(s), saving to {store.path}")
    started = time.monotonic()
    run_error = None
    try:
        tracing.init()
        for index, case in enumerate(selected, 1):
            started_at = results.now()
            trace_id = None
            try:
                with tracing.workflow("issue-to-files-v2") as span:
                    trace_id = _trace_id(span)
                    result = asyncio.run(_case(case))
            except Exception as error:
                result = _failed_case(case, error)
            result = {**result, "started_at": started_at, "neatlogs_trace_id": trace_id}
            path = store.save_case(result)
            if result["status"] == "ok":
                scores = ", ".join(
                    f"{provider} recall {result[provider]['recall']:.2f} precision {result[provider]['precision']:.2f}"
                    for provider in ("claude", "openai")
                )
                _progress(f"[{index}/{len(selected)}] {result['case_id']}: {scores}; saved {path}")
            else:
                error = result["error"]
                _progress(f"[{index}/{len(selected)}] {result['case_id']}: FAILED {error['type']}: {error['message']}; saved {path}")
    except BaseException as error:
        # Ctrl-C or a run-level error ends the run but keeps every case saved so far.
        run_error = error
        _progress(f"run stopped: {results.error_details(error)}")
    finally:
        try:
            tracing.shutdown()
        except Exception as error:
            _progress(f"tracing shutdown failed: {results.error_details(error)}")
    final = store.finish(_summary(store.cases), time.monotonic() - started, run_error)
    _progress(f"run {store.run_id}: {final['status']}, {final['cases_completed']}/{len(selected)} case(s) graded; results in {store.path}")
    return final
