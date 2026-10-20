"""Command-line entry point for DeepSeek questions and interactive chat."""

import argparse
import asyncio
import sys

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from .conversations import Conversation, list_conversation_ids
from .agent import ask_deepseek


def _answer(question: str, conversation: Conversation | None = None) -> bool:
    try:
        history = conversation.context_messages() if conversation is not None else None
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

    print(f"DeepSeek> {answer}" if conversation is not None else answer)
    if conversation is not None:
        conversation.add_exchange(question, answer)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_try",
        description="Chat with DeepSeek. Project file tools are not implemented yet.",
    )
    parser.add_argument("question", nargs="?", help="Ask once; omit to start interactive chat")
    parser.add_argument("--resume", metavar="ID", help="Resume a saved conversation")
    parser.add_argument("--list", action="store_true", help="List saved conversation IDs")
    args = parser.parse_args()

    if args.question is not None and (args.resume or args.list):
        parser.error("A one-time question cannot be combined with --resume or --list.")
    if args.resume and args.list:
        parser.error("Choose either --resume or --list.")
    if args.list:
        ids = list_conversation_ids()
        print("\n".join(ids) if ids else "暂无已保存的会话。")
        return 0
    if args.question is not None:
        return 0 if _answer(args.question) else 1

    try:
        conversation = Conversation.load(args.resume) if args.resume else Conversation.create()
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Error: Could not access local conversation files.", file=sys.stderr)
        return 1

    print(f"会话 ID：{conversation.id}")
    print("输入 /new 开始新会话，/compress 清空后续请求的上下文，/exit 退出。")
    while True:
        try:
            question = input("主包> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question == "/exit":
            break
        try:
            if question == "/new":
                conversation = Conversation.create()
                print(f"新会话 ID：{conversation.id}")
                continue
            if question == "/compress":
                conversation.compress()
                print("后续请求的上下文已清空；聊天记录仍保存在当前会话中。")
                continue
            if question.startswith("/"):
                print("未知指令。可用指令：/new、/compress、/exit。")
                continue
            if question:
                _answer(question, conversation)
        except OSError:
            print("Error: Could not save the local conversation.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
