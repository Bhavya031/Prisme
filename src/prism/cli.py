import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path


MANIFEST = Path(__file__).with_name("data") / "entire-file-finding-manifest.json"


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(prog="prism")
    commands = command_parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check run credentials without model calls or secret writes")
    check.add_argument("--repo", help="target repository, owner/name (optional for local setup)")
    check.add_argument("--config", type=Path, default=Path(".github/prism.json"), help="Prism JSON config")

    init = commands.add_parser("init", help="set up missing credentials and caller workflows interactively")
    init.add_argument("--repo", help="target repository, owner/name or GitHub URL")
    init.add_argument("--config", type=Path, default=Path(".github/prism.json"), help="Prism JSON config")

    predict = commands.add_parser("predict")
    predict.add_argument("issue_url")
    predict.add_argument("--repo", required=True)
    predict.add_argument("--pr-url", help="target PR to exclude from a pre-merge benchmark prediction")
    predict.add_argument("--pre-merge-sha", help="pre-merge base commit; requires --pr-url")
    predict.add_argument("--commit", help="immutable source commit for an issue prediction")
    predict.add_argument("--event", type=Path, help="GitHub event JSON for the triggering issue title and body")
    predict.add_argument("--checkout", type=Path, help="git repository containing the pinned search commit")
    predict.add_argument("--search", choices=("entire", "git"),
                         help="where candidate files come from (default: git with --checkout, else entire); with a "
                              "pinned commit, entire supplies paths only and line text is read at that commit")
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

    cfo_login = commands.add_parser("cfo-login", help="sign in to CFO.ai and save GitHub Actions secrets")
    cfo_login.add_argument("--repo", action="append", required=True, metavar="OWNER/NAME")
    cfo_login.add_argument("--client-id", help="existing public OAuth client ID; requires --port")
    cfo_login.add_argument("--port", type=int, help="registered loopback port for --client-id")
    cfo_login.add_argument("--timeout", type=int, default=300, metavar="SECONDS")

    cost = commands.add_parser("cost", help="estimated file-discovery USD per model from a saved run or a predict JSON "
                                            "output; not the cost of solving an issue (see solve-estimate)")
    cost.add_argument("--results", type=Path, required=True,
                      help="benchmark run directory, its result.json, or `prism predict --format json` output")
    cost.add_argument("--new-cases", type=int, default=10)
    cost.add_argument("--format", choices=("json", "markdown"), default="json")
    cost.add_argument("--catalog", type=Path, default=None, help="saved price catalog JSON instead of fetching it")

    policy = commands.add_parser("policy", help="recommend a tested file-discovery model and budget from a saved benchmark run")
    policy.add_argument("--results", type=Path, required=True, help="benchmark run directory or its result.json")
    policy.add_argument("--task", choices=("file-finding", "coding"), required=True)
    policy.add_argument("--complexity", choices=("simple", "complex"), required=True)
    policy.add_argument("--risk", choices=("low", "high"), required=True)
    policy.add_argument("--max-cost-usd", type=float, required=True)
    policy.add_argument("--timeout-seconds", type=float, required=True)
    policy.add_argument("--input-tokens", type=int, default=0)
    policy.add_argument("--output-tokens", type=int, default=4000)
    policy.add_argument("--retrieval-rounds-used", type=int, default=0)
    policy.add_argument("--max-retrieval-rounds", type=int, default=6)
    policy.add_argument("--issue-url")
    policy.add_argument("--repo", help="Entire repository; with --issue-url adds a ready-to-run prism predict command")
    policy.add_argument("--pr-url", help="target PR that must not appear in --context-file")
    policy.add_argument("--context-file", type=Path)
    policy.add_argument("--retrieval-file", type=Path)

    solve = commands.add_parser(
        "solve-estimate", help="estimate the tokens and USD a coding worker might spend solving an issue")
    issue = solve.add_mutually_exclusive_group(required=True)
    issue.add_argument("--issue-file", type=Path, help="text file holding the issue")
    issue.add_argument("--issue-url", help="GitHub issue URL; its title and body are fetched")
    issue.add_argument("--swebench-instance", type=Path,
                       help="SWE-bench instance JSON; only its problem statement, repo and base commit reach the estimator")
    selection = solve.add_mutually_exclusive_group(required=True)
    selection.add_argument("--prediction", type=Path, help="`prism predict --format json` output: predicted files and discovery cost")
    selection.add_argument("--files", nargs="+", help="repository paths to measure instead of a prediction")
    solve.add_argument("--provider", choices=("claude", "openai"), help="which provider's prediction to use")
    solve.add_argument("--discovery-cost-usd", type=float, default=None, help="discovery cost when --files is used")
    solve.add_argument("--commit", help="commit the files are read at (full SHA unless --checkout is given)")
    solve.add_argument("--checkout", type=Path, help="local git checkout to read the files from")
    solve.add_argument("--github-repo", help="owner/name to read the files from through the GitHub API")
    solve.add_argument("--worker-model", required=True, help="model the coding worker would use; there is no default")
    solve.add_argument("--pricing", type=Path,
                       help="JSON with model, uncached_input_per_million, cached_input_per_million, output_per_million")
    solve.add_argument("--catalog", type=Path, help="saved price catalog JSON to look the worker model up in")
    solve.add_argument("--mode", choices=("heuristic", "model-assisted"), default="heuristic")
    solve.add_argument("--assumptions", type=Path, help="JSON overriding the low/central/high assumptions")
    solve.add_argument("--glance", help="path to the Glance binary (default: PRISM_GLANCE, then `glance` on PATH)")
    solve.add_argument("--encoding", choices=("o200k_base", "cl100k_base"), default="o200k_base")
    solve.add_argument("--planner-provider", choices=("claude", "openai"), default="claude")
    solve.add_argument("--planner-max-input-tokens", type=int, default=8000,
                       help="limit on Glance's count of the issue and files sent to the planner; files are cut to fit")
    solve.add_argument("--planner-max-output-tokens", type=int, default=1500, help="output cap enforced by the provider")
    solve.add_argument("--planner-max-cost-usd", type=float, default=0.05,
                       help="refuse the call when a conservative pre-call estimate (counted request x2, plus the full "
                            "output cap) exceeds this; an estimate, not a provider-enforced cap")
    solve.add_argument("--save", type=Path, help="also save this as the pre-run estimate; never overwrites")
    solve.add_argument("--task-id", help="task name stored with --save (default: the SWE-bench instance id)")

    record = commands.add_parser("solve-record", help="record a coding-worker run against an estimate saved before it")
    record.add_argument("--estimate", type=Path, required=True, help="file written by solve-estimate --save")
    record.add_argument("--started-at", required=True, help="when the worker run started, ISO 8601 with offset")
    record.add_argument("--run-status", choices=("completed", "capped", "interrupted", "failed"), required=True)
    record.add_argument("--verified-outcome", choices=("resolved", "unresolved", "unverified"), required=True)
    record.add_argument("--worker-model", required=True)
    record.add_argument("--uncached-input", type=int, required=True)
    record.add_argument("--cached-input", type=int, default=0)
    record.add_argument("--cache-write", type=int, default=0)
    record.add_argument("--output", type=int, required=True, help="output tokens including reasoning")
    record.add_argument("--billed-usd", type=float, default=None, help="amount the provider billed, if known")
    record.add_argument("--usage-source", default="unspecified", help="where the token counts came from")
    record.add_argument("--notes")
    record.add_argument("--out", type=Path, help="write the record here; never overwrites")
    record.add_argument("--glance-records", type=Path, help="also append the run to this Glance records JSONL")
    record.add_argument("--glance", help="path to the Glance binary, for --glance-records")

    compare = commands.add_parser("solve-compare", help="counts of estimates against recorded worker runs")
    compare.add_argument("records", type=Path, nargs="+")

    gate = commands.add_parser("issue-gate", help="issue bot: refuse an event that may not start a run; print "
                                                  "note=<try-again line> when the repository is rate limited")
    gate.add_argument("--event", type=Path, required=True, help="GitHub webhook event JSON (GITHUB_EVENT_PATH)")
    gate.add_argument("--commit", help="resolved source commit for same-issue duplicate detection")
    gate.add_argument("--cooldown-minutes", type=int, default=10, help="repository estimate cooldown in minutes (default: 10)")
    comment = commands.add_parser("issue-comment", help="issue bot: the markdown comment for a finished or failed run")
    comment.add_argument("--prediction", type=Path, help="`prism predict --format json` output, if it got that far")
    comment.add_argument("--estimate", type=Path, help="`prism solve-estimate` output, if it got that far")
    comment.add_argument("--errors", type=Path, nargs="*", default=[], help="stderr of the steps, in run order")
    comment.add_argument("--run-url", help="link to the workflow run")
    comment.add_argument("--event", type=Path, help="GitHub event JSON for successful-estimate duplicate metadata")
    comment.add_argument("--commit", help="resolved source commit for successful-estimate duplicate metadata")
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
                              cost_scope=results.COST_SCOPE, cost_scope_note=results.COST_SCOPE_NOTE,
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
            if discovery := prediction.get("discovery"):
                print(f"Search source: {discovery['source']} (pinned commit: {discovery['pinned_commit'] or 'none'}; "
                      f"Entire index freshness: {discovery.get('entire_index_freshness', 'not used')})")
            print(f"Model calls: {prediction['model_calls']}")
            print(f"Estimated file-discovery cost: ${prediction['estimated_cost_usd']:.8f}")
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
    if os.environ.get("PRISM_SKIP_DOTENV"):
        return
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


def _solve_estimate(args) -> dict:
    from . import glance, github_context, solve, swebench, validation

    instance = _read_json(args.swebench_instance) if args.swebench_instance else None
    commit, github_repo = args.commit, args.github_repo
    if instance is not None:
        # Only these four fields go any further; the hidden ones are used by the leak check alone.
        task = swebench.visible_task(instance)
        issue_text, issue_source = task["problem_statement"], f"SWE-bench {task['instance_id']} problem_statement"
        commit = commit or task["base_commit"]
        if args.checkout is None:
            github_repo = github_repo or task["repo"]
    elif args.issue_file:
        issue_text, issue_source = args.issue_file.read_text(), f"file {args.issue_file}"
    else:
        match = github_context.ISSUE_URL.fullmatch(args.issue_url)
        if not match:
            raise ValueError("Expected a GitHub issue URL")
        issue = github_context.api_v2(f"repos/{match[1]}/{match[2]}/issues/{match[3]}")
        issue_text, issue_source = f"{issue['title']}\n\n{issue.get('body') or ''}", f"{args.issue_url} title and body"
    if not issue_text.strip():
        raise ValueError("The issue text is empty")
    if not commit:
        raise ValueError("--commit is required: the context is measured at one recorded commit")
    prediction = _read_json(args.prediction) if args.prediction else None
    if prediction is not None:
        paths, provider = solve.predicted_files(prediction, args.provider)
        selection = f"{provider} prediction in {args.prediction}"
        discovery = solve.discovery_cost(prediction)
    else:
        paths, selection = args.files, "--files"
        discovery = solve.discovery_cost(manual_usd=args.discovery_cost_usd)
    assumed = solve.assumptions(_read_json(args.assumptions) if args.assumptions else None)
    per_token, pricing = solve.worker_prices(args.worker_model, _read_json(args.pricing) if args.pricing else None,
                                             _read_json(args.catalog) if args.catalog else None)
    binary = glance.locate(args.glance)
    commit = glance.resolve_commit(commit, args.checkout)
    files, excluded = glance.read_files(paths, commit, checkout=args.checkout, github_repo=github_repo)
    for item in excluded:
        print(f"excluded from the measured context: {item['path']} ({item['reason']})", file=sys.stderr)
    if instance is not None:
        # Before any model call, so hidden material cannot reach the planner.
        swebench.leak_check(instance, commit=commit, files=files, texts={"issue_text": issue_text})
    print(f"measuring {len(files)} file(s) at {commit[:12]} with {binary}", file=sys.stderr)
    estimate = solve.estimate(
        issue_text=issue_text, files=files, commit=commit, worker_model=args.worker_model, per_token=per_token,
        pricing=pricing, measure=lambda text: glance.measure(text, glance=binary, encoding=args.encoding),
        assumed=assumed, discovery=discovery, mode=args.mode.replace("-", "_"),
        planner={"provider": args.planner_provider, "max_input_tokens": args.planner_max_input_tokens,
                 "max_output_tokens": args.planner_max_output_tokens, "max_cost_usd": args.planner_max_cost_usd},
        sources={"issue_source": issue_source, "file_selection": selection, "included_paths": list(files),
                 "excluded_paths": excluded,
                 "content_source": f"git objects in {args.checkout}" if args.checkout else f"GitHub API, {github_repo}"})
    if instance is not None:
        estimate["assumptions"]["leak_check"] = swebench.leak_check(
            instance, commit=commit, files=files, texts={"issue_text": issue_text, "estimate": estimate})
    if args.save:
        task_id = args.task_id or (instance or {}).get("instance_id")
        if not task_id:
            raise ValueError("--save needs --task-id")
        saved = validation.save_estimate(estimate, args.save, task_id)
        print(f"saved pre-run estimate {args.save} at {saved['saved_at']}", file=sys.stderr)
    return estimate


def _solve(args) -> None:
    """`prism solve-estimate`, `solve-record` and `solve-compare`: JSON on stdout, problems on stderr."""
    from . import glance, validation

    try:
        if args.command == "solve-estimate":
            result = _solve_estimate(args)
        elif args.command == "solve-compare":
            result = validation.compare([_read_json(path) for path in args.records])
        else:
            if args.out and args.out.exists():
                raise ValueError(f"{args.out} already exists; run records are never overwritten")
            result = validation.record_run(
                _read_json(args.estimate), started_at=args.started_at, run_status=args.run_status,
                verified_outcome=args.verified_outcome, worker_model=args.worker_model,
                uncached_input=args.uncached_input, cached_input=args.cached_input, cache_write=args.cache_write,
                output=args.output, billed_usd=args.billed_usd, usage_source=args.usage_source, notes=args.notes)
            if args.out:
                from . import results

                # Before the Glance append, so a failed write cannot leave a Glance row that a retry would duplicate.
                args.out.parent.mkdir(parents=True, exist_ok=True)
                results.write_json(args.out, result)
            if args.glance_records:
                validation.mirror_to_glance(result, glance.locate(args.glance), args.glance_records)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"prism {args.command}: {type(error).__name__}: {error}") from None
    print(json.dumps(result, indent=2))


def _saved(path: Path | None):
    """A step's JSON output, or None when the step never wrote any."""
    try:
        return _read_json(path) if path else None
    except (OSError, ValueError):
        return None


def _issue(args) -> None:
    """`prism issue-gate` and `prism issue-comment`, the two pieces the issue-bot workflow calls."""
    from . import bot, github_context

    if args.command == "issue-gate":
        if args.cooldown_minutes < 0:
            raise SystemExit("prism issue-gate: --cooldown-minutes must be nonnegative")
        try:
            note = bot.gate(_read_json(args.event), github_context.api_v2, commit=args.commit,
                            cooldown_minutes=args.cooldown_minutes)
        except PermissionError as error:
            raise SystemExit(f"prism issue-gate: not running: {error}") from None
        print(f"note={note or ''}")
        return
    estimate = _saved(args.estimate)
    if isinstance(estimate, dict):
        # A file written by --save wraps the estimate.
        estimate = estimate.get("estimate", estimate)
    if not isinstance(estimate, dict) or "solve_cost_estimate" not in estimate:
        estimate = None
    prediction = _saved(args.prediction)
    errors = "\n".join(path.read_text(errors="replace") for path in args.errors if path.is_file())
    source = bot.source_marker(_read_json(args.event), args.commit) if estimate and args.event and args.commit else None
    print(bot.render(prediction if isinstance(prediction, dict) else None, estimate, errors, args.run_url, source), end="")
    if estimate is None:
        raise SystemExit(1)


def main(argv=None) -> None:
    args = parser().parse_args(argv)
    if args.command == "cfo-login":
        from .cfo_login import login
        try:
            login(args)
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f"prism cfo-login: {error}") from None
        return
    _load_env()
    if args.command == "init":
        from . import init

        try:
            passed = init.run(args.repo, args.config)
        except KeyboardInterrupt:
            raise SystemExit("prism init: Setup cancelled.") from None
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f"prism init: {error}") from None
        if not passed:
            raise SystemExit(1)
        return
    if args.command == "check":
        from . import check

        try:
            passed = check.run(args.repo, args.config, local=os.environ.get("PRISM_LOCAL_SECRETS") == "1")
        except ValueError as error:
            raise SystemExit(f"prism check: {error}") from None
        if not passed:
            raise SystemExit(1)
        return
    if args.command.startswith("issue-"):
        _issue(args)
        return
    if args.command in ("cost", "policy"):
        _report(args)
        return
    if args.command.startswith("solve-"):
        _solve(args)
        return
    if args.command == "predict":
        from . import entire, tracing
        from .pipeline import check_budget
        from .github_context import build_context

        providers = ("claude", "openai") if args.provider == "both" else (args.provider,)
        if args.pre_merge_sha and not args.pr_url:
            raise SystemExit("prism predict: --pre-merge-sha requires --pr-url")
        if args.pre_merge_sha and not args.checkout:
            raise SystemExit("prism predict: --pre-merge-sha requires --checkout for commit-scoped search")
        if args.commit and (args.pr_url or args.pre_merge_sha):
            raise SystemExit("prism predict: --commit cannot be combined with benchmark PR options")
        if args.commit and not args.checkout:
            raise SystemExit("prism predict: --commit requires --checkout for commit-scoped search")
        if args.checkout and not (args.pre_merge_sha or args.commit):
            raise SystemExit("prism predict: --checkout requires --commit or --pre-merge-sha")
        if args.search == "git" and not args.checkout:
            raise SystemExit("prism predict: --search git requires --checkout")
        issue_override = None
        if args.event:
            event = _read_json(args.event)
            issue_override = event.get("issue") if isinstance(event, dict) else None
            if (not isinstance(issue_override, dict) or
                    not isinstance(issue_override.get("title"), str) or
                    (event.get("repository") or {}).get("full_name") != args.repo.removeprefix("gh/") or
                    issue_override.get("html_url") != args.issue_url or "pull_request" in issue_override):
                raise SystemExit("prism predict: event issue does not match repository and issue URL")
        budget = {"max_cost_usd": args.max_cost_usd, "max_extra": args.max_extra_rounds,
                  "timeout_seconds": args.timeout_seconds}
        if args.max_output_tokens is not None:
            budget["max_output_tokens"] = args.max_output_tokens
        try:
            # A bad budget is refused before anything is fetched or spent.
            for provider in providers:
                check_budget(provider, **budget)
        except ValueError as error:
            raise SystemExit(f"prism predict: {error}") from None
        if (args.search or ("git" if args.checkout else "entire")) == "entire":
            try:
                entire.ensure_ready(args.repo)
            except RuntimeError as error:
                raise SystemExit(str(error)) from None
        tracing.init()
        try:
            with tracing.workflow("issue-to-files") as span:
                with tracing.step("collect-context", "TOOL"):
                    ctx = build_context(args.issue_url, entire_repo=args.repo,
                                        pr_url=args.pr_url, pre_merge_sha=args.pre_merge_sha,
                                        commit=args.commit, issue_override=issue_override,
                                        search_checkout=args.checkout)
                    ctx.search_source = args.search
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
