"""Command-line entry point for DeepSeek questions and interactive chat."""

import argparse
import asyncio
import sys

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from .deepseek import ask_deepseek


def _answer(question: str, history: list[dict[str, str]] | None = None) -> bool:
    try:
        answer = asyncio.run(ask_deepseek(question, history=history))
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return False
    except APITimeoutError:
        print("Error: DeepSeek did not respond within 60 seconds.", file=sys.stderr)
        return False
    except TimeoutError:
        print("Error: DeepSeek did not respond within 60 seconds.", file=sys.stderr)
        return False
    except APIConnectionError:
        print("Error: Could not connect to DeepSeek.", file=sys.stderr)
        return False
    except APIStatusError as error:
        if error.status_code == 401:
            print("Error: DeepSeek rejected the API Key (HTTP 401).", file=sys.stderr)
        else:
            print(f"Error: DeepSeek returned HTTP {error.status_code}.", file=sys.stderr)
        return False
    except OpenAIError:
        print("Error: DeepSeek request failed.", file=sys.stderr)
        return False

    print(f"DeepSeek> {answer}" if history is not None else answer)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_try",
        description="Chat with DeepSeek. Project file tools are not implemented yet.",
    )
    parser.add_argument("question", nargs="?", help="Ask once; omit to start interactive chat")
    args = parser.parse_args()

    if args.question is not None:
        return 0 if _answer(args.question) else 1

    history: list[dict[str, str]] = []
    print("与 DeepSeek 连续对话；输入 /exit 退出，/clear 清空上下文。")
    while True:
        try:
            question = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question == "/exit":
            break
        if question == "/clear":
            history.clear()
            print("对话上下文已清空。")
            continue
        if question:
            _answer(question, history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
