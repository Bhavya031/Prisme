from .types import Context


def build_context(
    issue_url: str,
    *,
    entire_repo: str,
    pr_url: str | None = None,
    pre_merge_sha: str | None = None,
) -> Context:
    raise NotImplementedError


def fetch_reference(request: str, ctx: Context) -> dict | None:
    raise NotImplementedError


def true_files(pr_url: str, known_paths: set[str]) -> set[str]:
    raise NotImplementedError
