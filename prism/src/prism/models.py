from .types import Usage


async def call(provider: str, stage: str, prompt: str, schema: dict) -> tuple[dict, Usage]:
    raise NotImplementedError


def estimate_cost(provider: str, usage: Usage) -> float:
    raise NotImplementedError
