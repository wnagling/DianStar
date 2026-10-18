"""Command-line entry point for a single DeepSeek question."""

import argparse
import asyncio
import sys

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from .deepseek import ask_deepseek


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_try",
        description="Ask DeepSeek one question. Project file tools are not implemented yet.",
    )
    parser.add_argument("question", nargs="?", help="Question about the project")
    args = parser.parse_args()

    if args.question is None:
        parser.print_help()
        return 0

    try:
        answer = asyncio.run(ask_deepseek(args.question))
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except APITimeoutError:
        print("Error: DeepSeek did not respond within 60 seconds.", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Error: DeepSeek did not respond within 60 seconds.", file=sys.stderr)
        return 1
    except APIConnectionError:
        print("Error: Could not connect to DeepSeek.", file=sys.stderr)
        return 1
    except APIStatusError as error:
        if error.status_code == 401:
            print("Error: DeepSeek rejected the API Key (HTTP 401).", file=sys.stderr)
        else:
            print(f"Error: DeepSeek returned HTTP {error.status_code}.", file=sys.stderr)
        return 1
    except OpenAIError:
        print("Error: DeepSeek request failed.", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
