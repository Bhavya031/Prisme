import asyncio
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from . import entire, github_context, models, tracing
from .types import Context, Prediction, Usage


_CATEGORIES = ("fix", "tests", "changelog", "docs", "config")
_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "problem": {"type": "string"},
        "suspected_cause": {"type": "string"},
        "search_terms": {"type": "array", "items": {"type": "string"}},
        "expected_file_kinds": {"type": "array", "items": {"type": "string"}},
        "brief": {"type": "string"},
    },
    "required": ["problem", "suspected_cause", "search_terms", "expected_file_kinds", "brief"],
    "additionalProperties": False,
}
_DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        **{category: {"type": "array", "items": {"type": "string"}} for category in _CATEGORIES},
        "need_more": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": [*_CATEGORIES, "need_more"],
    "additionalProperties": False,
}


BUDGET_NOTE = ("Cost is an estimate from token counts and list prices, checked after every model call. "
               "One call can cross max_cost_usd before its usage is known; further calls stop once the estimate reaches it.")


def _safe_prompt(value: str, target_pr: int | None, provider: str | None = None,
                 max_output_tokens: int = models.MAX_TOKENS) -> str:
    if target_pr is not None and github_context._target_mention(target_pr).search(value):
        raise ValueError("Target PR reference leaked into model prompt")
    limits = models.LIMITS.get(models._model(provider)) if provider else None
    # Bytes plus overhead is a conservative token count, not provider-exact tokenization.
    if limits and len(value.encode()) + len(models.SYSTEM.encode()) + max_output_tokens + 1024 > limits["context"]:
        raise ValueError("Input and reserved output exceed model context window")
    return value


def _grouped(terms: list[str], ctx: Context, plan: dict) -> dict:
    hits_by_term = entire.search_many(terms, ctx.entire_repo)
    files: dict[str, dict] = defaultdict(lambda: {"hit_count": 0, "terms": set(), "lines": []})
    for term in terms:
        for hit in hits_by_term.get(term, []):
            if hit.path not in ctx.known_paths:
                continue
            entry = files[hit.path]
            entry["hit_count"] += 1
            entry["terms"].add(term)
            if len(entry["lines"]) < 2:
                entry["lines"].append(f"{hit.line}: {hit.text.strip()[:180]}")

    source_stems = {Path(path).stem for path in files if path.startswith("src/")}
    named = " ".join(plan["expected_file_kinds"])
    extras = set(ctx.bundle.path_hints)
    extras.update(path for path in ctx.known_paths if path in named)
    extras.update(
        path for path in ctx.known_paths
        if path in ("pyproject.toml", "setup.cfg")
        or ("/" not in path and Path(path).name.lower().startswith(("changes", "changelog")))
    )
    extras.update(
        path for path in ctx.known_paths
        if path.startswith(("tests/", "test/", "testing/"))
        and any(stem in Path(path).stem for stem in source_stems)
    )
    ranked = sorted(files, key=lambda path: (-len(files[path]["terms"]), -files[path]["hit_count"], path))

    def extra_rank(path: str) -> tuple[int, str]:
        stem = Path(path).stem.lower()
        return (
            0 if path in ctx.bundle.path_hints or path in named else
            1 if path == "pyproject.toml" else
            2 if stem in ("changes", "changelog") else 3,
            path,
        )

    candidates = ranked[:25] + sorted(extras - set(ranked[:25]), key=extra_rank)[:15]
    return {
        "files": [
            {"path": path, "hit_count": files[path]["hit_count"], "terms": sorted(files[path]["terms"]), "lines": files[path]["lines"]}
            for path in candidates[:40]
        ],
        "searched_terms": terms,
    }


def _combined_usage(parts: list[Usage], provider: str) -> Usage:
    return Usage(
        input=sum(part.input for part in parts),
        output=sum(part.output for part in parts),
        cached_input=sum(part.cached_input for part in parts),
        cache_write=sum(part.cache_write for part in parts),
        reasoning=sum(part.reasoning or 0 for part in parts) if provider == "openai" else None,
    )


async def predict(ctx: Context, provider: str, *, max_cost_usd: float | None = None,
                  max_output_tokens: int = models.MAX_TOKENS, max_extra: int = 1,
                  timeout_seconds: float | None = None) -> Prediction:
    """Predict files for one provider, optionally bounded by cost, output, retrieval and time budgets."""
    if (max_cost_usd is not None and max_cost_usd <= 0) or (timeout_seconds is not None and timeout_seconds <= 0) \
            or not 1 <= max_output_tokens <= 128_000 or max_extra < 0:
        raise ValueError("Cost and time budgets must be positive")
    run = _predict(ctx, provider, max_cost_usd, max_output_tokens, max_extra, timeout_seconds)
    if timeout_seconds is None:
        return await run
    try:
        return await asyncio.wait_for(run, timeout=timeout_seconds)
    except TimeoutError:
        raise RuntimeError(f"Prediction timed out after {timeout_seconds:g}s; "
                           "model calls already made are still billed") from None


async def _predict(ctx: Context, provider: str, max_cost_usd: float | None, max_output_tokens: int,
                   max_extra: int, timeout_seconds: float | None) -> Prediction:
    def safe_prompt(value: str) -> str:
        return _safe_prompt(value, ctx.target_pr, provider, max_output_tokens)

    def spent(parts: list[Usage]) -> float:
        return models.estimate_cost(provider, _combined_usage(parts, provider))

    bundle = asdict(ctx.bundle)
    condensed = len(json.dumps(bundle)) // 4 > 8000
    plan_prompt = (
        "PLAN the likely file changes. Return 6-10 literal search terms, including alternative identifiers. "
        "List expected file kinds. "
        + ("The context is large: also produce an EXTRACTIVE brief preserving exact quotes, identifiers, paths and errors. " if condensed else "Set brief to an empty string. ")
        + "\n\nContext:\n" + json.dumps(bundle, ensure_ascii=False)
    )
    plan, first_usage = await models.call(provider, "plan", safe_prompt(plan_prompt), _PLAN_SCHEMA, max_output_tokens)
    if max_cost_usd is not None and spent([first_usage]) >= max_cost_usd:
        raise RuntimeError(f"Model cost budget reached after plan: estimated ${spent([first_usage]):.6f} "
                           f"of ${max_cost_usd:g}, no prediction made")
    terms = list(dict.fromkeys(term.strip() for term in plan["search_terms"] if term.strip()))[:10]
    if not terms:
        raise ValueError(f"{provider} returned no search terms")

    with tracing.step(f"{provider}-search", "TOOL"):
        grouped = await asyncio.to_thread(_grouped, terms, ctx, plan)
    context = plan["brief"] if condensed and plan["brief"] else bundle
    decide_prompt = (
        "Predict ALL existing files a maintainer would modify in the fixing PR. "
        "Consider direct implementation, callers and entry points, regression tests, public docs, "
        "deprecation notes, release notes and configuration when relevant. Consider each category, even if empty. "
        "Use repository-relative paths from the tree/search context. "
        "If one referenced item is essential, set need_more to '#123 comments' or '#123 files'; otherwise null. "
        "Never request the fixing PR.\n\nIssue context:\n" + json.dumps(context, ensure_ascii=False)
        + "\n\nPlan:\n" + json.dumps(plan, ensure_ascii=False)
        + "\n\nSearch results and file candidates:\n" + json.dumps(grouped, ensure_ascii=False)
    )
    answer, second_usage = await models.call(provider, "decide", safe_prompt(decide_prompt), _DECIDE_SCHEMA, max_output_tokens)
    usages = [first_usage, second_usage]
    stopped_by = None
    extra_prompt = decide_prompt
    seen_requests = set()
    for _ in range(max_extra):
        need_more = answer.get("need_more")
        if not isinstance(need_more, str) or not need_more.strip() or need_more in seen_requests:
            break
        # Checked after every call: the call that crosses the limit is already spent.
        if max_cost_usd is not None and spent(usages) >= max_cost_usd:
            stopped_by = "max_cost_usd"
            break
        seen_requests.add(need_more)
        with tracing.step(f"{provider}-reference", "TOOL"):
            extra = await asyncio.to_thread(github_context.fetch_reference, need_more, ctx)
        if not extra:
            break
        extra_prompt += "\n\nAdditional referenced item:\n" + json.dumps(extra)
        answer, more_usage = await models.call(provider, "decide", safe_prompt(extra_prompt), _DECIDE_SCHEMA, max_output_tokens)
        usages.append(more_usage)
    used_more = len(usages) > 2

    by_category = {category: sorted(set(answer[category])) for category in _CATEGORIES}
    all_paths = set().union(*by_category.values())
    dropped = sorted(all_paths - ctx.known_paths)
    by_category = {category: sorted(set(paths) & ctx.known_paths) for category, paths in by_category.items()}
    predicted = sorted(set().union(*by_category.values()))
    usage = _combined_usage(usages, provider)
    return Prediction(
        provider=provider,
        by_category=by_category,
        predicted=predicted,
        dropped_nonexistent=dropped,
        model_calls=len(usages),
        need_more_used=used_more,
        condensed=condensed,
        usage=usage,
        estimated_cost_usd=models.estimate_cost(provider, usage),
        budget={
            "max_cost_usd": max_cost_usd,
            "max_output_tokens": max_output_tokens,
            "max_extra_rounds": max_extra,
            "timeout_seconds": timeout_seconds,
            "extra_rounds_used": len(usages) - 2,
            "stopped_by": stopped_by,
            "over_max_cost": max_cost_usd is not None and spent(usages) >= max_cost_usd,
            "note": BUDGET_NOTE,
        },
    )
