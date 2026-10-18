"""Command-line entry point for a single DeepSeek question."""

import argparse
import sys

from openai import OpenAIError

from .deepseek import ask_deepseek


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_try",
        description="Ask DeepSeek one question (project file tools are not implemented yet).",
    )
    parser.add_argument("question", nargs="?", help="Question about the project")
    args = parser.parse_args()

    if args.question is None:
        parser.print_help()
        return 0

    try:
        answer = ask_deepseek(args.question)
    except (ValueError, RuntimeError, OpenAIError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
