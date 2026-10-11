"""Estimate model costs from the token counts of completed Prism runs."""

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import models, results
from .types import Usage

CATALOG_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
QUICK_MODELS = ("claude-sonnet-5-5", "gpt-6-sol", "deepseek-flash", "zai/glm-5.3-flash", "dashscope/qwen3.8-flash")


def load_catalog(url: str = CATALOG_URL) -> tuple[dict, str]:
    """Fetch the public price catalog; tests pass their own catalog to build_report instead."""
    request = urllib.request.Request(url, headers={"User-Agent": "prism-cost"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        detail = getattr(error, "reason", None) or error
        raise RuntimeError(f"Price catalog unavailable ({detail}): {url}. "
                           "Retry, or pass a saved copy with --catalog.") from None


def observed(data: dict) -> dict:
    """Normalize a saved benchmark result or one `prism predict --format json` output."""
    if "cases" in data:
        cases = [case for case in data["cases"] if case.get("status", "ok") == "ok"]
        return {"kind": "benchmark", "run_id": data.get("run_id"), "stage": data.get("stage"), "cases": cases,
                "models": dict(data["settings"]["models"]), "summary": data.get("summary") or {}}
    predictions = {provider: value for provider, value in data.items()
                   if provider in results.PROVIDERS and isinstance(value, dict) and "usage" in value}
    if not predictions:
        raise ValueError("Expected a benchmark result.json or `prism predict --format json` output")
    # A provider stopped by a budget spent tokens but is not a full case to forecast from.
    predictions = {provider: value for provider, value in predictions.items() if value.get("status") != "failed"}
    if not predictions:
        raise ValueError("The predict output has no completed prediction to price")
    return {"kind": "prediction", "run_id": None, "stage": None,
            "cases": [{provider: {"tokens": value["usage"]} for provider, value in predictions.items()}],
            "models": {provider: value.get("model") or models._model(provider) for provider, value in predictions.items()},
            "summary": {}}


def token_profile(cases: list[dict], provider: str) -> dict:
    fields = ("input", "cached_input", "cache_write", "output", "reasoning")
    counts = {field: 0 for field in fields}
    for case in cases:
        tokens = case[provider]["tokens"]
        for field in fields:
            value = tokens.get(field) or 0
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"Invalid {provider} {field} token count")
            counts[field] += value
    if counts["cached_input"] > counts["input"] and provider == "openai":
        raise ValueError("OpenAI cached tokens exceed total input tokens")
    # Anthropic reports cached input apart from input; OpenAI's input already includes it.
    counts["uncached_input"] = counts["input"] - counts["cached_input"] if provider == "openai" else counts["input"]
    return counts


def model_cost(prices: dict, profile: dict) -> tuple[float, dict]:
    # Reasoning tokens are already included in output; charging them twice overstates cost.
    rates = {
        "uncached_input": prices["input_cost_per_token"],
        "cached_input": prices.get("cache_read_input_token_cost", prices["input_cost_per_token"]),
        "cache_write": prices.get("cache_creation_input_token_cost", prices["input_cost_per_token"]),
        "output": prices["output_cost_per_token"],
    }
    return sum(profile[field] * rate for field, rate in rates.items()), rates


def _largest_input(cases: list[dict], provider: str) -> int:
    """Largest input one case sent across all its calls: an upper bound on any single call."""
    return max(
        case[provider]["tokens"]["input"] + (
            case[provider]["tokens"]["cached_input"] + case[provider]["tokens"]["cache_write"] if provider == "claude" else 0)
        for case in cases
    )


def build_report(data: dict, catalog: dict, catalog_sha: str | None, new_cases: int) -> dict:
    run = observed(data)
    cases = run["cases"]
    if not cases or new_cases < 0:
        raise ValueError("Need completed cases and a nonnegative new-case count")
    providers = [provider for provider in results.PROVIDERS if all(provider in case for case in cases)]
    if not providers:
        raise ValueError("No provider has token counts for every completed case")
    profiles = {provider: token_profile(cases, provider) for provider in providers}
    largest_input = {provider: _largest_input(cases, provider) for provider in providers}
    tested = {}
    for provider in providers:
        profile, summary = profiles[provider], run["summary"].get(provider, {})
        usage = Usage(input=profile["input"], output=profile["output"], cached_input=profile["cached_input"],
                      cache_write=profile["cache_write"], reasoning=None)
        tested[provider] = {
            "model": run["models"][provider],
            "basis": "measured",
            "observed_cases": len(cases),
            "mean_precision": summary.get("mean_precision"),
            "mean_recall": summary.get("mean_recall"),
            "exact_matches": summary.get("exact_matches"),
            # Prism's own list prices (models.PRICES), the figure saved with every run.
            "estimated_usd_per_case": round(models.estimate_cost(provider, usage) / len(cases), 6),
            "largest_observed_input_tokens": largest_input[provider],
        }
    model_rows = []
    for name, prices in catalog.items():
        if not isinstance(prices, dict):
            continue
        provider = prices.get("litellm_provider")
        if provider not in ("anthropic", "openai", "deepseek", "zai", "dashscope") or prices.get("mode") not in ("chat", "responses"):
            continue
        if provider == "dashscope" and "/qwen" not in name:
            continue
        if not all(isinstance(prices.get(key), (int, float)) for key in ("input_cost_per_token", "output_cost_per_token")):
            continue
        # shortcut: unseen providers use the observed OpenAI token mix; replace with pilot counts before budgeting a deployment.
        source = "claude" if provider == "anthropic" else "openai"
        if source not in profiles:
            source = providers[0]
        measured = tested[source] if tested[source]["model"] == name else None
        cost_observed, rates = model_cost(prices, profiles[source])
        max_input = prices.get("max_input_tokens")
        model_rows.append({
            "model": name, "provider": provider,
            "basis": "measured" if measured else "extrapolated",
            "token_counts_from": source,
            "mean_precision": measured["mean_precision"] if measured else None,
            "mean_recall": measured["mean_recall"] if measured else None,
            "structured_output_supported": prices.get("supports_response_schema"),
            "usd_per_million_tokens": {key: round(value * 1_000_000, 6) for key, value in rates.items()},
            "estimated_usd_observed_cases": round(cost_observed, 6),
            "estimated_usd_per_case": round(cost_observed / len(cases), 6),
            "estimated_usd_new_cases": round(cost_observed * new_cases / len(cases), 6),
            "max_input_tokens": max_input,
            "max_output_tokens": prices.get("max_output_tokens"),
            "observed_input_fits": largest_input[source] <= max_input if isinstance(max_input, int) else None,
            "price_source": prices.get("source") or CATALOG_URL,
        })
    model_rows.sort(key=lambda row: (row["estimated_usd_new_cases"], row["provider"], row["model"]))
    by_name = {row["model"]: row for row in model_rows}
    pair = [tested[provider]["model"] for provider in providers]
    if missing := [model for model in pair if model not in by_name]:
        raise ValueError("Public catalog lacks a model used by the run: " + ", ".join(missing))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cost_type": results.COST_TYPE,
        "cost_note": results.COST_NOTE,
        "source": {key: run[key] for key in ("kind", "run_id", "stage")},
        "catalog": {"url": CATALOG_URL, "sha256": catalog_sha, "note": "Community catalog; verify final rates with provider billing"},
        "observed_cases": len(cases), "new_cases_forecast": new_cases,
        "assumptions": [
            "Every dollar figure is an estimate from token counts and list prices, not a billed amount.",
            "Anthropic uses Claude's observed token counts; all other providers use OpenAI's observed four billable counts.",
            "Only models with basis 'measured' were run; 'extrapolated' rows reuse another model's token counts and have no accuracy data.",
            "Model choice can change tokenization, reasoning, latency, availability, and accuracy; this is a cost scenario, not a rerun.",
            "Standard direct-API text rates only; no tool fees, regional uplift, batch discount, or long-context tier.",
            "observed_input_fits compares the largest input one observed case sent across all its calls with the catalog's max_input_tokens.",
        ],
        "token_profiles": profiles,
        "tested_models": tested,
        "current_model_pair": {"models": pair,
                               "estimated_usd_new_cases": round(sum(by_name[model]["estimated_usd_new_cases"] for model in pair), 6)},
        "models": model_rows,
    }
    if all("wall_seconds" in case for case in cases):
        seconds = sum(case["wall_seconds"] for case in cases) / len(cases) * new_cases
        report["five_hour_view"] = {
            "estimated_paired_runtime_minutes": round(seconds / 60, 2),
            "runtime_fits_5_hours": seconds <= 5 * 3600,
            "scope": "Inference and Entire search only; excludes fork creation and mirror indexing",
            "subscription_quota_fit": None,
            "subscription_note": "The measured runs used API keys, which do not spend Codex or Claude Code five-hour subscription allowance. Token counts cannot predict subscription quota use.",
        }
    return report


def markdown_table(report: dict) -> str:
    cases = report["observed_cases"]
    rows = [
        "# Issue-to-files cost forecast", "",
        f"{report['new_cases_forecast']} new cases; prices in USD per million tokens. Costs are estimates that reuse "
        f"the {cases} observed case{'s' if cases != 1 else ''} and are not quality predictions.", "",
        "## Token inputs from completed cases", "",
        "| Observed provider | Uncached input | Cached input | Cache write | Output |",
        "|---|---:|---:|---:|---:|",
    ]
    for provider, profile in report["token_profiles"].items():
        rows.append(f"| {provider} | {profile['uncached_input']:,} | {profile['cached_input']:,} | {profile['cache_write']:,} | {profile['output']:,} |")
    rows += ["", "## Quick comparison", "",
             f"| Model | Basis | Input $/M | Output $/M | Est. {report['new_cases_forecast']}-case cost |",
             "|---|---|---:|---:|---:|"]
    by_name = {model["model"]: model for model in report["models"]}
    for name in dict.fromkeys((*report["current_model_pair"]["models"], *QUICK_MODELS)):
        if name in by_name:
            model = by_name[name]
            rates = model["usd_per_million_tokens"]
            rows.append(f"| {name} | {model['basis']} | ${rates['uncached_input']:g} | ${rates['output']:g} | ${model['estimated_usd_new_cases']:.4f} |")
    rows += ["", "## Full catalog comparison", "",
             "| Provider | Model | Input | Cached input | Cache write | Output | Est. cost |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for model in report["models"]:
        rates = model["usd_per_million_tokens"]
        rows.append(f"| {model['provider']} | {model['model']} | ${rates['uncached_input']:g} | ${rates['cached_input']:g} | ${rates['cache_write']:g} | ${rates['output']:g} | ${model['estimated_usd_new_cases']:.4f} |")
    rows += ["", "Prices: [LiteLLM public catalog](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json). "
             "Check provider billing before purchase; only models marked measured were run, and extrapolated rows reuse their token counts.", ""]
    return "\n".join(rows)
