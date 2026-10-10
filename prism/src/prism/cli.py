import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path


MANIFEST = Path(__file__).with_name("data") / "entire-file-finding-manifest.json"


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(prog="prism")
    commands = command_parser.add_subparsers(dest="command", required=True)

    predict = commands.add_parser("predict")
    predict.add_argument("issue_url")
    predict.add_argument("--repo", required=True)
    predict.add_argument("--provider", choices=("claude", "openai", "both"), default="both")
    predict.add_argument("--format", choices=("text", "json", "markdown"))

    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--one", action="store_true")
    benchmark.add_argument("--manifest", type=Path, default=MANIFEST)
    commands.add_parser("dry-run").add_argument("--manifest", type=Path, default=MANIFEST)
    return command_parser


def _prediction_data(value):
    return asdict(value) if is_dataclass(value) else value


def _print_predictions(predictions, output_format, trace_id=None):
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
            if trace_id:
                print(f"Neatlogs trace: {trace_id}")
        return
    for provider, prediction in data.items():
        print(provider + ":")
        for path in prediction["predicted"]:
            print(path)


def _load_env(path=".env") -> None:
    """Fill unset variables from a .env in the working directory."""
    if not os.path.isfile(path):
        return
    for line in Path(path).read_text().splitlines():
        name, sep, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name, value.strip().strip("\"'"))


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    _load_env()
    if args.command == "predict":
        from . import entire, tracing
        from .pipeline import predict
        from .github_context import build_context

        try:
            entire.ensure_ready(args.repo)
        except RuntimeError as error:
            raise SystemExit(str(error)) from None
        tracing.init()
        try:
            with tracing.workflow("issue-to-files") as span:
                with tracing.step("collect-context", "TOOL"):
                    ctx = build_context(args.issue_url, entire_repo=args.repo)
                providers = ("claude", "openai") if args.provider == "both" else (args.provider,)
                outcomes = asyncio.run(_predict(ctx, providers))
            predictions = []
            for provider, outcome in zip(providers, outcomes):
                if isinstance(outcome, Exception):
                    print(f"{provider} failed: {outcome}", file=sys.stderr)
                else:
                    predictions.append(outcome)
            if not predictions:
                raise SystemExit("All requested providers failed")
            _print_predictions(predictions, args.format or "text", _trace_id(span))
        finally:
            tracing.shutdown()
        return
    from .benchmark import run

    if args.command == "benchmark":
        print(json.dumps(run(args.manifest, one=args.one, dry_run=False), indent=2, sort_keys=True))
    else:
        print(json.dumps(run(args.manifest, one=False, dry_run=True), indent=2, sort_keys=True))


async def _predict(ctx, providers):
    from . import models
    from .pipeline import predict

    try:
        return await asyncio.gather(*(predict(ctx, provider) for provider in providers), return_exceptions=True)
    finally:
        await models.close()


def _trace_id(span):
    context = getattr(span, "get_span_context", lambda: None)()
    trace_id = getattr(context, "trace_id", None)
    return format(trace_id, "032x") if trace_id is not None else None
