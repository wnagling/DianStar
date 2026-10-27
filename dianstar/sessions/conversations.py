# 管理本地会话：保存名称、聊天记录，并支持恢复及清空模型上下文。

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dianstar.config.models import DEFAULT_MODEL as MODEL
from dianstar.config.paths import PROJECT_ROOT


CONVERSATION_DIR = PROJECT_ROOT / ".local" / "conversations"
_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def _default_name(question: str) -> str:
    """Use the first six visible characters of the first question."""
    return " ".join(question.split())[:6]


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("会话名必须是文字。")
    name = name.strip()
    if not name or len(name) > 80 or not name.isprintable():
        raise ValueError("会话名须为 1 至 80 个可显示字符。")
    return name


@dataclass(frozen=True)
class ConversationEntry:
    id: str
    name: str | None
    updated_at: datetime


@dataclass
class Conversation:
    id: str
    created_at: str
    messages: list[dict[str, str]] = field(default_factory=list)
    context_start: int = 0
    model_messages: list[dict[str, object]] = field(default_factory=list)
    model_context_start: int = 0
    name: str | None = None
    directory: Path = CONVERSATION_DIR

    @classmethod
    def create(cls, directory: Path = CONVERSATION_DIR) -> "Conversation":
        conversation = cls(
            id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(),
            directory=directory,
        )
        conversation._save(conversation.messages, conversation.context_start,
                           conversation.model_messages, conversation.model_context_start,
                           conversation.name)
        return conversation

    @classmethod
    def load(cls, conversation_id: str, directory: Path = CONVERSATION_DIR) -> "Conversation":
        if not _ID_PATTERN.fullmatch(conversation_id):
            raise ValueError("Invalid conversation ID.")
        path = directory / f"{conversation_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError("Conversation not found.") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Conversation file is invalid.") from error

        if not isinstance(data, dict):
            raise ValueError("Conversation file is invalid.")
        messages = data.get("messages")
        context_start = data.get("context_start")
        if (
            data.get("version") not in {1, 2, 3}
            or data.get("id") != conversation_id
            or not isinstance(data.get("created_at"), str)
            or not isinstance(data.get("model"), str)
            or not isinstance(messages, list)
            or not isinstance(context_start, int)
            or isinstance(context_start, bool)
            or context_start < 0
            or context_start > len(messages)
            or context_start % 2 != 0
            or len(messages) % 2 != 0
            or any(
                not isinstance(message, dict)
                or message.get("role") != ("user" if index % 2 == 0 else "assistant")
                or not isinstance(message.get("content"), str)
                or ("reasoning_content" in message and (
                    index % 2 == 0 or not isinstance(message["reasoning_content"], str)
                ))
                for index, message in enumerate(messages)
            )
        ):
            raise ValueError("Conversation file is invalid.")
        if data["version"] == 1:
            model_messages = list(messages)
            model_context_start = context_start
        else:
            model_messages = data.get("model_messages")
            model_context_start = data.get("model_context_start")
            if (
                not isinstance(model_messages, list)
                or not isinstance(model_context_start, int)
                or isinstance(model_context_start, bool)
                or not 0 <= model_context_start <= len(model_messages)
                or any(
                    not isinstance(message, dict)
                    or message.get("role") not in {"user", "assistant", "tool"}
                    or not (isinstance(message.get("content"), str)
                            or message.get("role") == "assistant" and message.get("content") is None)
                    or ("reasoning_content" in message
                        and (message["role"] != "assistant"
                             or not isinstance(message["reasoning_content"], str)))
                    for message in model_messages
                )
            ):
                raise ValueError("Conversation file is invalid.")
        if data["version"] == 3:
            if "name" not in data:
                raise ValueError("Conversation file is invalid.")
            name = data.get("name")
            if name is not None:
                try:
                    name = _validate_name(name)
                except ValueError as error:
                    raise ValueError("Conversation file is invalid.") from error
        else:
            name = _default_name(messages[0]["content"]) if messages else None
        return cls(
            id=conversation_id, created_at=data["created_at"], messages=messages,
            context_start=context_start, model_messages=model_messages,
            model_context_start=model_context_start, name=name, directory=directory,
        )

    def context_messages(self) -> list[dict[str, str]]:
        return self.messages[self.context_start :]

    def model_context_messages(self) -> list[dict[str, object]]:
        return self.model_messages[self.model_context_start :]

    def add_exchange(self, question: str, answer: str, *, reasoning_content: str | None = None,
                     model_turn_messages: list[dict[str, object]] | None = None) -> None:
        name = self.name if self.name is not None else _default_name(question)
        assistant_message = {"role": "assistant", "content": answer}
        if reasoning_content is not None:
            assistant_message["reasoning_content"] = reasoning_content
        messages = [*self.messages, {"role": "user", "content": question},
                    assistant_message]
        model_messages = [*self.model_messages, *(model_turn_messages if model_turn_messages is not None
                                                  else [{"role": "user", "content": question},
                                                        assistant_message])]
        self._save(messages, self.context_start, model_messages, self.model_context_start, name)
        self.messages = messages
        self.model_messages = model_messages
        self.name = name

    def rename(self, name: str) -> None:
        updated_name = _validate_name(name)
        self._save(self.messages, self.context_start, self.model_messages,
                   self.model_context_start, updated_name)
        self.name = updated_name

    def compress(self) -> None:
        context_start = len(self.messages)
        model_context_start = len(self.model_messages)
        self._save(self.messages, context_start, self.model_messages, model_context_start, self.name)
        self.context_start = context_start
        self.model_context_start = model_context_start

    def _save(self, messages: list[dict[str, str]], context_start: int,
              model_messages: list[dict[str, object]], model_context_start: int,
              name: str | None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.id}.json"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory,
                prefix=f".{self.id}.", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump({
                    "version": 3,
                    "id": self.id,
                    "name": name,
                    "created_at": self.created_at,
                    "model": MODEL,
                    "context_start": context_start,
                    "messages": messages,
                    "model_context_start": model_context_start,
                    "model_messages": model_messages,
                }, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def list_conversation_ids(directory: Path = CONVERSATION_DIR) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(
        (path.stem for path in directory.glob("*.json") if _ID_PATTERN.fullmatch(path.stem)),
        key=lambda conversation_id: (directory / f"{conversation_id}.json").stat().st_mtime,
        reverse=True,
    )


def list_conversations(directory: Path = CONVERSATION_DIR) -> list[ConversationEntry]:
    """List readable conversations by latest local file update, newest first."""
    entries = []
    for conversation_id in list_conversation_ids(directory):
        try:
            conversation = Conversation.load(conversation_id, directory)
            updated_at = datetime.fromtimestamp(
                (directory / f"{conversation_id}.json").stat().st_mtime,
            ).astimezone()
        except (ValueError, OSError):
            continue
        entries.append(ConversationEntry(conversation.id, conversation.name, updated_at))
    return entries
