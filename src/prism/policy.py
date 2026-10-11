"""Describe measured model capability and bound a file-finding run."""

import json
import re
import shlex

from . import cost, github_context, models, results

PROMPT_INSTRUCTION = ("Predict existing implementation, test, docs, changelog, and config files affected by this issue. "
                      "Search the pre-merge Entire mirror again if evidence is insufficient. Return JSON with fix, "
                      "tests, docs, changelog, config, and need_more fields. Use only paths that exist in the pre-merge tree.")


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def capability(cases: list[dict], provider: str) -> dict:
    small = [case for case in cases if len(case["true_paths"]) <= 3]
    broad = [case for case in cases if len(case["true_paths"]) > 3]
    return {
        "evaluated_task": "Predict existing files changed by a fixing PR from a pre-merge issue and repository search",
        "small_change_evidence": {"cases": len(small), "full_recall_cases": sum(case[provider]["recall"] == 1 for case in small),
                                  "mean_precision": _mean([case[provider]["precision"] for case in small])},
        "broad_change_evidence": {"cases": len(broad), "mean_recall": _mean([case[provider]["recall"] for case in broad]),
                                  "mean_precision": _mean([case[provider]["precision"] for case in broad])},
        "scope_note": "File counts are retrospective ground truth, not a difficulty classifier available at prediction time",
        "not_evaluated": ["writing code", "DeepSeek/GLM/Qwen in this pipeline", "new repository families"],
    }


def context_budget(model: str, input_tokens: int, output_tokens: int, retrieval_rounds_used: int, max_retrieval_rounds: int) -> dict:
    if model not in models.LIMITS:
        raise ValueError(f"No context limits recorded for model {model}")
    limits = models.LIMITS[model]
    if min(input_tokens, output_tokens, retrieval_rounds_used) < 0 or max_retrieval_rounds < 0:
        raise ValueError("Token and retrieval counts must be nonnegative")
    if output_tokens > limits["max_output"]:
        raise ValueError("Requested output exceeds the model maximum")
    remaining = limits["context"] - input_tokens - output_tokens
    return {
        "context_window_tokens": limits["context"],
        "limits_source": limits["source"],
        "input_tokens": input_tokens,
        "reserved_output_tokens": output_tokens,
        "remaining_for_retrieval_tokens": max(0, remaining),
        "input_plus_output_fits": remaining >= 0,
        "additional_retrieval_rounds_used": retrieval_rounds_used,
        "max_additional_retrieval_rounds": max_retrieval_rounds,
        "can_retrieve_more": remaining > 0 and retrieval_rounds_used < max_retrieval_rounds,
        "note": "Input includes instructions, issue, tool schemas, conversation, and retrieved results. Recalculate after every tool result; the requested output reservation includes reasoning tokens.",
    }


def recommend(data: dict, task: str, complexity: str, risk: str, max_cost_usd: float, timeout_seconds: float,
              input_tokens: int = 0, output_tokens: int = models.MAX_TOKENS, retrieval_rounds_used: int = 0,
              max_retrieval_rounds: int = 6, issue_url: str | None = None, repo: str | None = None,
              pr_url: str | None = None, context=None, retrieval=None) -> dict:
    """Pick the best affordable tested model from a saved benchmark result."""
    if max_cost_usd <= 0 or timeout_seconds <= 0:
        raise ValueError("Cost and time limits must be positive")
    policy = {
        "task": task,
        "complexity": complexity,
        "risk": risk,
        "max_cost_usd": max_cost_usd,
        "timeout_seconds": timeout_seconds,
        "require_human_review": complexity == "complex" or risk == "high",
        "guardrails": [
            "Keep the target PR and its diff out of model context until predictions are final",
            "Accept only paths present in the pre-merge tree",
            "Do not let the model write to benchmark forks",
            "Check cost after every model turn, stop further calls at the threshold, and time out prediction",
        ],
    }
    if task != "file-finding":
        return policy | {"status": "needs_evaluation", "model": None,
                         "reason": "No coding-task accuracy results exist for these models"}

    run = cost.observed(data)
    cases = run["cases"]
    if run["kind"] != "benchmark" or not cases or not run["summary"]:
        raise ValueError("Need a benchmark result with graded cases")
    policy["evidence_source"] = {"run_id": run["run_id"], "stage": run["stage"], "cost_type": results.COST_TYPE}
    candidates = []
    for provider, model in run["models"].items():
        summary = run["summary"][provider]
        candidates.append({
            "model": model,
            "provider": provider,
            "mean_recall": summary["mean_recall"],
            "mean_precision": summary["mean_precision"],
            "observed_cases": len(cases),
            "estimated_usd_per_case": round(summary["estimated_cost_usd"] / len(cases), 6),
        })
    affordable = [item for item in candidates if item["estimated_usd_per_case"] <= max_cost_usd]
    if not affordable:
        return policy | {"status": "over_budget", "model": None,
                         "reason": "No tested model has an average cost below this limit", "candidates": candidates}
    choice = max(affordable, key=lambda item: (item["mean_recall"], item["mean_precision"], -item["estimated_usd_per_case"]))
    if retrieval is not None and context is None:
        raise ValueError("Retrieved evidence needs issue context")
    prompt = None
    if context is not None:
        retrieval = {} if retrieval is None else retrieval
        prompt = (PROMPT_INSTRUCTION + "\n\nINPUT: safe issue and repository context\n"
                  + json.dumps(context, ensure_ascii=False) + "\n\nINPUT: retrieved evidence\n"
                  + json.dumps(retrieval, ensure_ascii=False)
                  + "\n\nOUTPUT: JSON with fix, tests, docs, changelog, config arrays and need_more string or null.")
        if pr_url is not None:
            match = re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)", pr_url)
            if not match:
                raise ValueError("Expected a GitHub pull request URL")
            if github_context._target_mention(int(match.group(1))).search(prompt):
                raise ValueError("Target PR reference leaked into the prompt")
        # shortcut: byte count plus overhead is a conservative token budget, not provider-exact tokenization.
        input_tokens = max(input_tokens, len(prompt.encode()) + 1024)
    budget = context_budget(choice["model"], input_tokens, output_tokens,
                            retrieval_rounds_used, max_retrieval_rounds)
    result = policy | {
        "status": "context_overflow" if not budget["input_plus_output_fits"] else
                  "review_required" if policy["require_human_review"] else "recommended",
        "model": choice["model"],
        "provider": choice["provider"],
        "evidence": choice,
        "candidates": candidates,
        "capability": capability(cases, choice["provider"]),
        "input_output_budget": budget,
        "agent_command": {
            "input_parts": ["issue text and pre-cutoff references", "pre-merge repository map",
                            "Entire search results gathered so far"],
            "instruction": PROMPT_INSTRUCTION,
            "output_format": "JSON file categories plus need_more",
            "tool": "Entire code search on the pre-merge fork",
            "max_additional_retrieval_rounds": max_retrieval_rounds,
        },
        "retrieval_rule": "If evidence is insufficient, request more pre-merge evidence, count its result as input, and stop at the cost, time, context, or retrieval limit",
        "budget_note": "Average cost is an estimate. A model call can cross the dollar threshold before its usage is known; prism predict stops further calls and times out prediction.",
    }
    if issue_url is not None or repo is not None:
        if not (issue_url and repo):
            raise ValueError("Issue URL and Entire repo must be supplied together")
        result["ready_to_run_cli"] = shlex.join([
            "prism", "predict", issue_url, "--repo", repo, "--provider", choice["provider"], "--format", "json",
            "--max-cost-usd", f"{max_cost_usd:g}", "--timeout-seconds", f"{timeout_seconds:g}",
            "--max-output-tokens", str(output_tokens),
            # Only the rounds still unused; the recommendation may come partway through a run.
            "--max-extra-rounds", str(max(0, max_retrieval_rounds - retrieval_rounds_used))])
        result["cli_note"] = "Runs the chosen provider against the Entire repository; prediction timeout and post-call cost checks apply. One model call can cross the cost threshold before it can be stopped. API charges apply."
    if prompt is not None:
        result["complete_model_prompt"] = prompt
        result["prompt_note"] = "Review prompt assembled from supplied context; prism predict builds its own stage prompts"
    return result
