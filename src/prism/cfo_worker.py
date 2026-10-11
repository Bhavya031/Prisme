"""Allocation gate and per-call USD limit for a future coding worker."""

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from . import models


class BudgetStop(RuntimeError):
    """The next model call cannot fit inside the allocation."""


class BudgetedWorker:
    def __init__(self, estimate: dict, allocation: dict, *, request_id: str, repository: str,
                 issue_url: str, pinned_commit: str, worker_model: str):
        issue = urlparse(issue_url)
        expected = {"request_id": request_id, "repository": repository, "issue_url": issue_url,
                    "pinned_commit": pinned_commit, "issue": int(issue.path.rsplit("/", 1)[-1])
                    if issue.path.rsplit("/", 1)[-1].isdigit() else None}
        if (allocation.get("decision") != "allocated" or not allocation.get("allocation_id")
                or any(not value or allocation.get(key) != value for key, value in expected.items())
                or issue.scheme != "https" or issue.netloc != "github.com"
                or issue.path != f"/{repository}/issues/{expected['issue']}"
                or estimate.get("repository_commit") != pinned_commit
                or allocation.get("chosen_model") != worker_model):
            raise ValueError("Worker run needs an allocated decision matching its request and estimate")
        matches = [name for name, price in models.PRICES.items() if price["model"] == worker_model]
        if len(matches) != 1:
            raise ValueError(f"No worker usage price for {worker_model!r}")
        self.provider = matches[0]
        if models._model(self.provider) != worker_model:
            raise ValueError("Configured provider model differs from the allocated worker model")
        self.prices = models.PRICES[self.provider]
        try:
            self.cap = Decimal(str(allocation["usd_cap"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError("Allocation has no valid USD cap") from None
        if not self.cap.is_finite() or self.cap <= 0:
            raise ValueError("Allocation USD cap must be positive")
        self.allocation_id = allocation["allocation_id"]
        self.spent = Decimal(0)
        self.calls = 0
        self.tokens = {key: 0 for key in ("input", "output", "cached_input", "cache_write")}
        self.status = "running"

    def _cost(self, usage) -> Decimal:
        p = self.prices
        uncached = usage.input if self.provider == "claude" else usage.input - usage.cached_input
        if min(uncached, usage.cached_input, usage.cache_write, usage.output) < 0:
            raise ValueError("Provider reported invalid token usage")
        return sum(Decimal(count) * Decimal(str(p[price])) for count, price in (
            (uncached, "input"), (usage.cached_input, "cached_input"),
            (usage.cache_write, "cache_write"), (usage.output, "output"))) / p["per"]

    async def call(self, stage: str, prompt: str, schema: dict, *, max_input_tokens: int,
                   max_output_tokens: int, system: str = models.SYSTEM, call=models.call):
        if self.status != "running":
            raise BudgetStop("Worker run has stopped")
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 1
               for n in (max_input_tokens, max_output_tokens)):
            raise ValueError("Each call needs positive input and output token limits")
        p = self.prices
        # shortcut: UTF-8 bytes plus framing room bound input conservatively; verify against real provider usage before a paid run.
        max_input_tokens = max(max_input_tokens, len((system + prompt + json.dumps(schema)).encode()) + 1024)
        # Reserve the costliest input category and the full provider output limit before dispatch.
        ceiling = (Decimal(max_input_tokens) * Decimal(str(max(p["input"], p["cache_write"])))
                   + Decimal(max_output_tokens) * Decimal(str(p["output"]))) / p["per"]
        if self.spent + ceiling > self.cap:
            self.status = "capped"
            raise BudgetStop("Next model call could exceed the allocated USD cap")
        try:
            result, usage = await call(self.provider, stage, prompt, schema, max_output_tokens, system=system)
        except models.BilledError as error:
            self.status = "failed"
            self._add_usage(error.usage)
            if self.spent > self.cap:
                self.status = "capped"
            raise
        except Exception:
            self.status = "failed"
            raise
        try:
            self._add_usage(usage)
        except (ValueError, TypeError, AttributeError):
            self.status = "failed"
            raise
        total_input = usage.input + (usage.cached_input + usage.cache_write if self.provider == "claude" else 0)
        if total_input > max_input_tokens or usage.output > max_output_tokens:
            self.status = "capped"
            raise BudgetStop("Provider usage exceeded the declared call token limits")
        if self.spent > self.cap:
            self.status = "capped"
            raise BudgetStop("Provider usage exceeded the declared call ceiling and allocation")
        return result

    def _add_usage(self, usage):
        self.spent += self._cost(usage)
        self.calls += 1
        for key in self.tokens:
            self.tokens[key] += getattr(usage, key)

    def result(self) -> dict:
        return {"allocation_id": self.allocation_id, "status": self.status,
                "usd_cap": str(self.cap), "spent_usd": str(self.spent),
                "remaining_usd": str(self.cap - self.spent), "model_calls": self.calls,
                "actual_tokens": dict(self.tokens),
                "cost_basis": "provider-reported token usage priced at Prism list prices; not a billed amount"}

    def finish(self) -> dict:
        if self.status == "running":
            self.status = "completed"
        return self.result()
