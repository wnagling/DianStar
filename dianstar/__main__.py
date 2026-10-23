# 命令行入口：处理参数、交互指令和会话切换，并展示 DeepSeek 的回答或错误。

import argparse
import asyncio
import sys

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError
from rich.console import Console
from rich.markdown import Markdown

from .conversations import Conversation, list_conversation_ids, list_conversations
from .agent import ask_deepseek


def _print_answer(answer: str, *, interactive: bool, console: Console | None = None) -> None:
    """Render model Markdown for display while keeping the saved answer unchanged."""
    output = console or Console()
    if interactive:
        output.print("DianStar> ", end="")
    output.print(Markdown(answer), soft_wrap=True)


def _answer(question: str, conversation: Conversation | None = None) -> bool:
    try:
        history = conversation.model_context_messages() if conversation is not None else None
        history_length = len(history) if history is not None else 0
        answer = asyncio.run(ask_deepseek(question, history=history))
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return False
    except APITimeoutError:
        print("错误：DeepSeek 在 60 秒内未响应。", file=sys.stderr)
        return False
    except TimeoutError:
        print("错误：DeepSeek 在 60 秒内未响应。", file=sys.stderr)
        return False
    except APIConnectionError:
        print("错误：无法连接 DeepSeek，请检查网络后重试。", file=sys.stderr)
        return False
    except APIStatusError as error:
        if error.status_code == 401:
            print("错误：DeepSeek 拒绝了 API Key（HTTP 401）。", file=sys.stderr)
        else:
            print(f"错误：DeepSeek 返回 HTTP {error.status_code}。", file=sys.stderr)
        return False
    except OpenAIError:
        print("错误：DeepSeek 请求失败。", file=sys.stderr)
        return False

    _print_answer(answer, interactive=conversation is not None)
    if conversation is not None:
        unnamed = conversation.name is None
        conversation.add_exchange(question, answer,
                                  model_turn_messages=history[history_length:] or None)
        if unnamed:
            print(f"会话名：{conversation.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dianstar",
        description="Chat with DianStar. Project file tools are not implemented yet.",
    )
    parser.add_argument("question", nargs="?", help="Ask once; omit to start interactive chat")
    parser.add_argument("--resume", metavar="ID", help="Resume a saved conversation")
    parser.add_argument("--list", action="store_true", help="List saved conversation IDs")
    parser.add_argument("--plain", action="store_true", help="Use the original text-only chat")
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
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except OSError:
        print("错误：无法访问本地会话文件。", file=sys.stderr)
        return 1

    if not args.plain:
        from .tui import DianStarApp
        DianStarApp(conversation).run()
        return 0

    print(f"会话 ID：{conversation.id}")
    print(f"当前会话：{conversation.name or '未命名'}（ID：{conversation.id}）")
    print("指令：/new、/rename 新名称、/resume 会话名、/switch、/compress、/config、/exit。")
    while True:
        try:
            question = input("主包> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question == "/exit":
            break
        try:
            if question == "/help":
                print("指令：/help、/new、/rename 新名称、/resume 会话名、/switch、/compress、/config、/exit。")
                continue
            if question == "/config":
                print("配置界面在 TUI 中使用；运行 python -m dianstar 后输入 /config。")
                continue
            if question == "/new":
                conversation = Conversation.create()
                print("已创建新会话；首次提问后自动命名。")
                continue
            if question == "/resume":
                print("输入 /resume 会话名 恢复会话；输入 /switch 按最近使用时间选择。")
                continue
            if question.startswith("/resume "):
                target = question.partition(" ")[2].strip()
                matches = [entry for entry in list_conversations() if entry.name == target]
                if len(matches) > 1:
                    print("有多个同名会话，请使用 /switch 按时间选择。")
                    continue
                try:
                    resumed = Conversation.load(matches[0].id if matches else target)
                except ValueError:
                    print("没有找到该会话，请检查名称或使用 /switch。", file=sys.stderr)
                    continue
                except OSError:
                    print("无法读取会话文件。", file=sys.stderr)
                    continue
                conversation = resumed
                print(f"已切换到会话：{conversation.name or '未命名'}")
                continue
            if question == "/rename" or question.startswith("/rename "):
                name = question.partition(" ")[2].strip()
                if not name:
                    try:
                        name = input("新会话名> ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        continue
                try:
                    conversation.rename(name)
                except ValueError as error:
                    print(f"无法重命名：{error}", file=sys.stderr)
                    continue
                print(f"会话已重命名：{conversation.name}")
                continue
            if question == "/switch":
                entries = list_conversations()
                if not entries:
                    print("暂无可恢复的会话。")
                    continue
                print("最近使用的会话：")
                for index, entry in enumerate(entries, 1):
                    current = "（当前）" if entry.id == conversation.id else ""
                    print(f"{index}. {entry.name or '未命名'}  "
                          f"{entry.updated_at:%Y-%m-%d %H:%M} {current}")
                try:
                    selection = input("选择序号（回车取消）> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    continue
                if not selection:
                    continue
                if not selection.isdecimal() or not 1 <= int(selection) <= len(entries):
                    print("序号无效，当前会话未切换。")
                    continue
                try:
                    resumed = Conversation.load(entries[int(selection) - 1].id)
                except (ValueError, OSError):
                    print("无法读取所选会话，当前会话未切换。", file=sys.stderr)
                    continue
                conversation = resumed
                print(f"已切换到会话：{conversation.name or '未命名'}")
                continue
            if question == "/compress":
                conversation.compress()
                print("后续请求的上下文已清空；聊天记录仍保存在当前会话中。")
                continue
            if question.startswith("/"):
                print("未知指令。可用指令：/new、/rename、/resume、/switch、/compress、/exit。")
                continue
            if question:
                _answer(question, conversation)
        except OSError:
            print("错误：无法保存本地会话。", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
