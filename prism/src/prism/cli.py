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
    predict.add_argument("--max-cost-usd", type=float, default=None,
                         help="stop further model calls once the estimated cost reaches this; one call can overshoot")
    predict.add_argument("--max-output-tokens", type=int, default=None, help="output cap per model call (default: 4000)")
    predict.add_argument("--max-extra-rounds", type=int, default=1, help="extra retrieval rounds after the first decision")
    predict.add_argument("--timeout-seconds", type=float, default=None, help="time limit per provider")

    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--one", action="store_true")
    benchmark.add_argument("--manifest", type=Path, default=MANIFEST)
    benchmark.add_argument("--output-dir", type=Path, default=None,
                           help="directory for run records; each run gets its own subdirectory (default: ./benchmark-runs)")
    benchmark.add_argument("--stage", default=None,
                           help="stage name for the runs.json export (default: run-<run id>)")
    commands.add_parser("dry-run").add_argument("--manifest", type=Path, default=MANIFEST)

    cost = commands.add_parser("cost", help="estimated USD per model from a saved run or a predict JSON output")
    cost.add_argument("--results", type=Path, required=True,
                      help="benchmark run directory, its result.json, or `prism predict --format json` output")
    cost.add_argument("--new-cases", type=int, default=10)
    cost.add_argument("--format", choices=("json", "markdown"), default="json")
    cost.add_argument("--catalog", type=Path, default=None, help="saved price catalog JSON instead of fetching it")

    policy = commands.add_parser("policy", help="recommend a tested model and budget from a saved benchmark run")
    policy.add_argument("--results", type=Path, required=True, help="benchmark run directory or its result.json")
    policy.add_argument("--task", choices=("file-finding", "coding"), required=True)
    policy.add_argument("--complexity", choices=("simple", "complex"), required=True)
    policy.add_argument("--risk", choices=("low", "high"), required=True)
    policy.add_argument("--max-cost-usd", type=float, required=True)
    policy.add_argument("--timeout-seconds", type=int, required=True)
    policy.add_argument("--input-tokens", type=int, default=0)
    policy.add_argument("--output-tokens", type=int, default=4000)
    policy.add_argument("--retrieval-rounds-used", type=int, default=0)
    policy.add_argument("--max-retrieval-rounds", type=int, default=6)
    policy.add_argument("--issue-url")
    policy.add_argument("--repo", help="Entire repository; with --issue-url adds a ready-to-run prism predict command")
    policy.add_argument("--pr-url", help="target PR that must not appear in --context-file")
    policy.add_argument("--context-file", type=Path)
    policy.add_argument("--retrieval-file", type=Path)
    return command_parser


def _prediction_data(value):
    return asdict(value) if is_dataclass(value) else value


def _print_predictions(predictions, output_format, trace_id=None, failed=None):
    data = {_prediction_data(prediction)["provider"]: _prediction_data(prediction)
            for prediction in predictions}
    if output_format == "json":
        from . import models, results

        # A failed provider is reported too, with what a budget stop had already spent.
        for provider, error in (failed or {}).items():
            spent = dict(getattr(error, "spent", {}))
            if "usage" in spent:
                spent["usage"] = asdict(spent["usage"])
            data[provider] = {"provider": provider, "status": "failed", "error": results.error_details(error), **spent}
        for provider, prediction in data.items():
            prediction.update(model=models._model(provider), cost_type=results.COST_TYPE, cost_note=results.COST_NOTE,
                              neatlogs_trace_id=trace_id)
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
            if (prediction.get("budget") or {}).get("over_max_cost"):
                print(f"Reached the ${prediction['budget']['max_cost_usd']:g} cost limit; one call can overshoot it.")
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


def _read_json(path: Path):
    path = Path(path)
    return json.loads((path / "result.json" if path.is_dir() else path).read_text())


def _report(args) -> None:
    """`prism cost` and `prism policy`: JSON on stdout, problems on stderr."""
    from . import cost, policy

    try:
        data = _read_json(args.results)
        if args.command == "policy":
            result = policy.recommend(
                data, args.task, args.complexity, args.risk, args.max_cost_usd, args.timeout_seconds,
                args.input_tokens, args.output_tokens, args.retrieval_rounds_used, args.max_retrieval_rounds,
                args.issue_url, args.repo, args.pr_url,
                _read_json(args.context_file) if args.context_file else None,
                _read_json(args.retrieval_file) if args.retrieval_file else None)
        else:
            if args.catalog:
                catalog, catalog_sha = _read_json(args.catalog), None
            else:
                print(f"fetching price catalog {cost.CATALOG_URL}", file=sys.stderr)
                catalog, catalog_sha = cost.load_catalog()
            result = cost.build_report(data, catalog, catalog_sha, args.new_cases)
            if args.catalog:
                result["catalog"]["url"] = str(args.catalog)
            if args.format == "markdown":
                print(cost.markdown_table(result))
                return
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        raise SystemExit(f"prism {args.command}: {type(error).__name__}: {error}") from None
    print(json.dumps(result, indent=2))


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    _load_env()
    if args.command in ("cost", "policy"):
        _report(args)
        return
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
                budget = {"max_cost_usd": args.max_cost_usd, "max_extra": args.max_extra_rounds,
                          "timeout_seconds": args.timeout_seconds}
                if args.max_output_tokens is not None:
                    budget["max_output_tokens"] = args.max_output_tokens
                outcomes = asyncio.run(_predict(ctx, providers, budget))
            predictions, failed = [], {}
            for provider, outcome in zip(providers, outcomes):
                if isinstance(outcome, Exception):
                    failed[provider] = outcome
                    print(f"{provider} failed: {outcome}", file=sys.stderr)
                    if _trace_id(span):
                        print(f"Neatlogs trace: {_trace_id(span)}", file=sys.stderr)
                else:
                    predictions.append(outcome)
            if predictions or args.format == "json":
                _print_predictions(predictions, args.format or "text", _trace_id(span), failed)
            if not predictions:
                raise SystemExit("All requested providers failed")
        finally:
            tracing.shutdown()
        return
    from .benchmark import run

    if args.command == "benchmark":
        result = run(args.manifest, one=args.one, dry_run=False, output_dir=args.output_dir, stage=args.stage)
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["status"] != "completed":
            raise SystemExit(1)
    else:
        print(json.dumps(run(args.manifest, one=False, dry_run=True), indent=2, sort_keys=True))


async def _predict(ctx, providers, budget=None):
    from . import models
    from .pipeline import predict

    try:
        return await asyncio.gather(*(predict(ctx, provider, **(budget or {})) for provider in providers),
                                    return_exceptions=True)
    finally:
        await models.close()


def _trace_id(span):
    context = getattr(span, "get_span_context", lambda: None)()
    trace_id = getattr(context, "trace_id", None)
    return format(trace_id, "032x") if trace_id is not None else None
