from .types import Hit


def search(term: str, repo: str, limit: int = 30) -> list[Hit]:
    raise NotImplementedError


def search_many(terms: list[str], repo: str) -> dict[str, list[Hit]]:
    raise NotImplementedError
