from dataclasses import dataclass
from pathlib import Path


@dataclass
class Bundle:
    """What the models see; never includes the target PR in benchmark mode."""

    issue: dict
    references: list[dict]
    path_hints: list[str]
    repo_map: dict


@dataclass
class Context:
    bundle: Bundle
    known_paths: set[str]
    safe_refs: set[tuple[str, str, int]]
    cutoff: str | None
    target_pr: int | None
    entire_repo: str
    search_commit: str | None = None
    search_checkout: Path | None = None


@dataclass
class Hit:
    path: str
    line: int
    text: str


@dataclass
class Usage:
    input: int
    output: int
    cached_input: int
    cache_write: int
    reasoning: int | None


@dataclass
class Prediction:
    provider: str
    by_category: dict[str, list[str]]
    predicted: list[str]
    dropped_nonexistent: list[str]
    model_calls: int
    need_more_used: bool
    condensed: bool
    usage: Usage
    estimated_cost_usd: float
    budget: dict | None = None
