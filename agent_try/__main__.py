"""Command-line entry point for the future read-only project agent."""

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_try",
        description="agentTry Python skeleton; project analysis is not implemented yet.",
    )
    parser.add_argument("question", nargs="?", help="Question about the project")
    args = parser.parse_args()

    if args.question is None:
        parser.print_help()
        return 0

    parser.exit(2, "Project analysis is not implemented yet.\n")


if __name__ == "__main__":
    raise SystemExit(main())
