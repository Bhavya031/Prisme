import asyncio
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from . import github_context, models, pipeline, tracing
from .types import Prediction


def _prediction_result(prediction: Prediction, truth: set[str]) -> dict:
    result = asdict(prediction)
    result["tokens"] = result.pop("usage")
    predicted = set(prediction.predicted)
    matched = predicted & truth
    result.update({
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


async def _case(case: dict) -> dict:
    try:
        started = time.monotonic()
        with tracing.step("collect-context", "TOOL"):
            ctx = await asyncio.to_thread(
                github_context.build_context,
                case["issue_url"],
                entire_repo=f"gh/{case['fork']}",
                pr_url=case["pr_url"],
                pre_merge_sha=case["pre_merge_sha"],
            )
        # Both model submissions complete before grading can fetch the fixing PR files.
        claude, openai = await asyncio.gather(pipeline.predict(ctx, "claude"), pipeline.predict(ctx, "openai"))
        with tracing.step("grade", "TOOL"):
            truth = await asyncio.to_thread(github_context.true_files, case["pr_url"], ctx.known_paths)
        if not truth:
            raise ValueError("No modified existing files in target PR")
        return {
            "issue": case["issue_url"],
            "pr": case["pr_url"],
            "fork": case["fork"],
            "pre_merge_sha": case["pre_merge_sha"],
            "wall_seconds": round(time.monotonic() - started, 2),
            "true_paths": sorted(truth),
            "claude": _prediction_result(claude, truth),
            "openai": _prediction_result(openai, truth),
        }
    finally:
        await models.close()


def _summary(results: list[dict]) -> dict:
    return {
        provider: {
            "mean_precision": round(sum(case[provider]["precision"] for case in results) / len(results), 3),
            "mean_recall": round(sum(case[provider]["recall"] for case in results) / len(results), 3),
            "exact_matches": sum(case[provider]["exact_match"] for case in results),
            "model_calls": sum(case[provider]["model_calls"] for case in results),
            "estimated_cost_usd": round(sum(case[provider]["estimated_cost_usd"] for case in results), 6),
            "missed_files": {case["issue"]: case[provider]["missed"] for case in results},
        }
        for provider in ("claude", "openai")
    }


def _assert_no_target_leak(ctx) -> None:
    if ctx.target_pr is not None and re.search(
        rf"(?<!\d)#{ctx.target_pr}(?!\d)|/pull/{ctx.target_pr}(?!\d)", json.dumps(asdict(ctx.bundle)), re.I
    ):
        raise ValueError("Target PR reference leaked into context")


def run(manifest: Path, *, one: bool, dry_run: bool) -> dict:
    cases = json.loads(manifest.read_text())
    if len(cases) != 5:
        raise ValueError("Expected five benchmark cases")
    selected = [case for case in cases if "/pytest/" in case["issue_url"]] if one else cases
    if dry_run:
        contexts = []
        for case in selected:
            ctx = github_context.build_context(
                case["issue_url"], entire_repo=f"gh/{case['fork']}", pr_url=case["pr_url"], pre_merge_sha=case["pre_merge_sha"]
            )
            _assert_no_target_leak(ctx)
            contexts.append({"issue": case["issue_url"], "model_context": asdict(ctx.bundle)})
        return {"cases": contexts}

    tracing.init()
    try:
        results = []
        for case in selected:
            with tracing.workflow("issue-to-files-v2") as span:
                result = asyncio.run(_case(case))
                result["neatlogs_trace_id"] = _trace_id(span)
                results.append(result)
        return {"cases": results, "summary": _summary(results)}
    finally:
        tracing.shutdown()
