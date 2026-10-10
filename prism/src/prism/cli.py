import argparse
import asyncio
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path


MANIFEST = Path(__file__).parents[2] / "data" / "entire-file-finding-manifest.json"


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(prog="prism")
    commands = command_parser.add_subparsers(dest="command", required=True)

    predict = commands.add_parser("predict")
    predict.add_argument("issue_url")
    predict.add_argument("--repo", required=True)
    predict.add_argument("--provider", choices=("claude", "openai", "both"))
    predict.add_argument("--format", choices=("text", "json", "markdown"))

    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--one", action="store_true")
    commands.add_parser("dry-run")
    return command_parser


def _prediction_data(value):
    return asdict(value) if is_dataclass(value) else value


def _print_predictions(predictions, output_format):
    data = {_prediction_data(prediction)["provider"]: _prediction_data(prediction)
            for prediction in predictions}
    if output_format == "json":
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    if output_format == "markdown":
        for provider, prediction in data.items():
            print(f"## Prism prediction ({provider})")
            for category in ("fix", "tests", "changelog", "docs", "config"):
                print(f"### {category}")
                for path in prediction["by_category"].get(category, []):
                    print(f"- `{path}`")
            print(f"Model calls: {prediction['model_calls']}")
            print(f"Estimated cost: ${prediction['estimated_cost_usd']:.8f}")
        return
    for provider, prediction in data.items():
        print(provider + ":")
        for path in prediction["predicted"]:
            print(path)


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    if args.command == "predict":
        if args.provider is None:
            raise SystemExit("predict requires --provider until the default model choice is decided")
        from .github_context import build_context
        from .pipeline import predict

        ctx = build_context(args.issue_url, entire_repo=args.repo)
        providers = ("claude", "openai") if args.provider == "both" else (args.provider,)
        predictions = asyncio.run(_predict(ctx, providers))
        _print_predictions(predictions, args.format or "text")
        return
    from .benchmark import run

    if args.command == "benchmark":
        print(json.dumps(run(MANIFEST, one=args.one, dry_run=False), indent=2, sort_keys=True))
    else:
        print(json.dumps(run(MANIFEST, one=False, dry_run=True), indent=2, sort_keys=True))


async def _predict(ctx, providers):
    from .pipeline import predict

    return await asyncio.gather(*(predict(ctx, provider) for provider in providers))
