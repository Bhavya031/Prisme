"""Durable, per-run storage for benchmark results."""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import models

DEFAULT_OUTPUT_DIR = Path("benchmark-runs")
COST_TYPE = "estimated_list_price"
COST_NOTE = "Estimated from token counts and list prices. Not a cost recorded or billed by a provider."
# Stages already in the showcase dataset; their rows must never be reused.
EXISTING_STAGES = ("s0-exploration", "s1-agent-loop", "s2-pipeline-tuned", "s3-pipeline-clean")
PROVIDERS = ("claude", "openai")

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key|api[-_]?key)\b(\"|')?\s*[:=]\s*(\"|')?"
                r"(?:(?:bearer|basic)\s+)?[^\s\"',}]+"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), "[REDACTED]"),
)


def _secret_values() -> list[str]:
    values = {value for name, value in os.environ.items() if _SECRET_NAME.search(name) and len(value) >= 8}
    return sorted(values, key=len, reverse=True)


def redact(value, _secrets=None):
    """Return value with every string scrubbed of credentials; nothing else is changed."""
    found = _secret_values() if _secrets is None else _secrets
    if isinstance(value, str):
        for secret in found:
            value = value.replace(secret, "[REDACTED]")
        for pattern, replacement in _SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {key: redact(item, found) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, found) for item in value]
    return value


def error_details(error: BaseException) -> dict:
    return {"type": type(error).__name__, "message": redact(str(error))[:2000]}


def write_json(path: Path, data) -> None:
    """Write redacted JSON through a temporary file so readers never see a partial file."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w") as file:
            json.dump(redact(data), file, indent=2, sort_keys=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), *args], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def source_state() -> dict:
    """The commit Prism is running from; `dirty` is null when that can't be determined."""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain") if commit else None
    changed = [line[3:] for line in status.splitlines()] if status is not None else []
    return {
        "commit": commit.strip() if commit else None,
        "dirty": bool(changed) if status is not None else None,
        "dirty_files": changed[:50],
    }


def settings() -> dict:
    """Model settings for this run. Credentials are reported only as present or absent."""
    return {
        "models": {provider: models._model(provider) for provider in PROVIDERS},
        "apis": dict(models.APIS),
        "effort": models.EFFORT,
        "max_tokens": models.MAX_TOKENS,
        "cutoff": "before target PR created_at",
        "context_threshold_estimated_tokens": 8000,
        "parallel_per_case": True,
        "prices_usd_per_million_tokens": {provider: dict(models.PRICES[provider]) for provider in PROVIDERS},
        "entire_transport": os.getenv("PRISM_ENTIRE_TRANSPORT") or "cli",
        "neatlogs_enabled": bool(os.getenv("NEATLOGS_API_KEY")),
        "python": sys.version.split()[0],
    }


def case_id(case: dict) -> str:
    """`repo#issue`, the join key used by the showcase dataset."""
    owner_repo, _, number = case["issue_url"].rstrip("/").partition("/issues/")
    return f"{owner_repo.rsplit('/', 1)[-1]}#{number}"


def _tokens(provider: str, tokens: dict) -> dict:
    # Anthropic reports cache tokens apart from input; OpenAI's input already includes cached input.
    total = tokens["input"] + tokens["output"]
    if provider == "claude":
        total += tokens["cached_input"] + tokens["cache_write"]
    row = {
        "tokens_input": tokens["input"],
        "tokens_cached_input": tokens["cached_input"],
        "tokens_cache_write": tokens["cache_write"],
        "tokens_output": tokens["output"],
        "tokens_total": total,
    }
    if tokens.get("reasoning") is not None:
        row["tokens_reasoning"] = tokens["reasoning"]
    return row


def export_rows(stage: str, cases: list[dict], run_settings: dict) -> list[dict]:
    """Graded cases as rows in the showcase `runs.json` schema, one per case and provider."""
    rows = []
    for case in cases:
        if case["status"] != "ok":
            continue
        truth = set(case["true_paths"])
        for provider in PROVIDERS:
            result = case[provider]
            rows.append({
                "stage": stage,
                "case_id": case["case_id"],
                "provider": provider,
                "model": f"{run_settings['models'][provider]}, effort {run_settings['effort']} ({run_settings['apis'][provider]})",
                "predicted": result["predicted"],
                "true_files": case["true_paths"],
                "matched": sorted(truth & set(result["predicted"])),
                "missed": result["missed"],
                "extra": result["extra"],
                "predicted_count": len(result["predicted"]),
                "true_count": len(truth),
                "precision": round(result["precision"], 4),
                "recall": round(result["recall"], 4),
                "exact_match": result["exact_match"],
                "model_calls": result["model_calls"],
                "wall_seconds": case["wall_seconds"],
                "neatlogs_trace_id": case["neatlogs_trace_id"],
                "cost_usd": result["estimated_cost_usd"],
                "cost_type": COST_TYPE,
                **_tokens(provider, result["tokens"]),
                "need_more_used": result["need_more_used"],
                "condensed": result["condensed"],
                "by_category": result["by_category"],
            })
    return rows


def stage_summary(rows: list[dict]) -> list[dict]:
    """Per-provider rows in the `summary` format of the showcase `stages.json`."""
    summary = []
    for provider in PROVIDERS:
        own = [row for row in rows if row["provider"] == provider]
        if not own:
            continue
        summary.append({
            "stage": own[0]["stage"],
            "provider": provider,
            "cases": len(own),
            "mean_precision": round(sum(row["precision"] for row in own) / len(own), 3),
            "mean_recall": round(sum(row["recall"] for row in own) / len(own), 3),
            "exact_matches": sum(row["exact_match"] for row in own),
            "total_tokens": sum(row["tokens_total"] for row in own),
            "total_cost_usd": round(sum(row["cost_usd"] for row in own), 6),
            "cost_type": COST_TYPE,
            "mean_wall_seconds": round(sum(row["wall_seconds"] for row in own) / len(own), 2),
        })
    return summary


class RunStore:
    """One benchmark run in its own directory under the output directory."""

    def __init__(self, output_dir: Path, manifest: Path, cases: list[dict], selected: list[dict], stage: str | None):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        while True:
            # The random suffix separates runs started in the same second; mkdir refuses an existing run.
            run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
            try:
                (output_dir / run_id).mkdir()
                break
            except FileExistsError:
                continue
        self.run_id = run_id
        self.path = (output_dir / run_id).resolve()
        (self.path / "cases").mkdir()
        self.cases: list[dict] = []
        self.settings = settings()
        self.run = {
            "run_id": run_id,
            "stage": stage or f"run-{run_id}",
            "status": "running",
            "started_at": now(),
            "finished_at": None,
            "duration_seconds": None,
            "source": source_state(),
            "settings": self.settings,
            "manifest": {
                "path": str(Path(manifest).resolve()),
                "sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
                "total_cases": len(cases),
                "selected_cases": [case_id(case) for case in selected],
            },
            "cases_selected": len(selected),
            "cases_completed": 0,
            "cases_failed": 0,
            "error": None,
        }
        write_json(self.path / "manifest.json", cases)
        self._write_run()

    def _write_run(self) -> None:
        write_json(self.path / "run.json", self.run)

    def save_case(self, result: dict) -> Path:
        """Persist one finished case, graded or failed, before the next one starts."""
        index = len(self.cases) + 1
        result = {"run_id": self.run_id, "index": index, **result}
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", result["case_id"])
        path = self.path / "cases" / f"{index:03d}-{name}.json"
        write_json(path, result)
        self.cases.append(result)
        self.run["cases_completed"] = sum(case["status"] == "ok" for case in self.cases)
        self.run["cases_failed"] = sum(case["status"] != "ok" for case in self.cases)
        self._write_run()
        return path

    def finish(self, summary: dict, duration: float, error: BaseException | None = None) -> dict:
        """Write the final status and exports, keeping every case saved so far."""
        completed = self.run["cases_completed"]
        if completed == self.run["cases_selected"] and error is None:
            status = "completed"
        else:
            status = "partial" if completed else "failed"
        graded = [case for case in self.cases if case["status"] == "ok"]
        estimated = round(sum(case[provider]["estimated_cost_usd"] for case in self.cases for provider in PROVIDERS
                              if isinstance(case.get(provider), dict)), 6)
        self.run.update({
            "status": status,
            "finished_at": now(),
            "duration_seconds": round(duration, 2),
            "error": error_details(error) if error is not None else None,
            "cost": {"type": COST_TYPE, "estimated_usd": estimated, "recorded_usd": None, "note": COST_NOTE},
        })
        rows = export_rows(self.run["stage"], graded, self.settings)
        saved = {
            "run_dir": str(self.path),
            "run": str(self.path / "run.json"),
            "manifest": str(self.path / "manifest.json"),
            "cases_dir": str(self.path / "cases"),
            "result": str(self.path / "result.json"),
            "runs_export": str(self.path / "runs.json"),
            "stage_export": str(self.path / "stage.json"),
        }
        result = {**self.run, "cases": self.cases, "summary": summary, "saved": saved}
        write_json(self.path / "runs.json", rows)
        write_json(self.path / "stage.json", {
            "stage": self.run["stage"],
            "date": self.run["started_at"][:10],
            "run_id": self.run_id,
            "status": status,
            "summary": stage_summary(rows),
        })
        write_json(self.path / "result.json", result)
        self._write_run()
        return redact(result)
