from .types import Context, Prediction


async def predict(ctx: Context, provider: str) -> Prediction:
    raise NotImplementedError
