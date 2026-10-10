import argparse


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


def main() -> None:
    parser().parse_args()
    raise NotImplementedError
