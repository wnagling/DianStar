# 管理本地会话：生成 ID、保存聊天记录，并支持恢复及清空模型上下文。

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .deepseek import MODEL, PROJECT_ROOT


CONVERSATION_DIR = PROJECT_ROOT / ".local" / "conversations"
_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


@dataclass
class Conversation:
    id: str
    created_at: str
    messages: list[dict[str, str]] = field(default_factory=list)
    context_start: int = 0
    directory: Path = CONVERSATION_DIR

    @classmethod
    def create(cls, directory: Path = CONVERSATION_DIR) -> "Conversation":
        conversation = cls(
            id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(),
            directory=directory,
        )
        conversation._save(conversation.messages, conversation.context_start)
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
            data.get("version") != 1
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
                for index, message in enumerate(messages)
            )
        ):
            raise ValueError("Conversation file is invalid.")
        return cls(conversation_id, data["created_at"], messages, context_start, directory)

    def context_messages(self) -> list[dict[str, str]]:
        return self.messages[self.context_start :]

    def add_exchange(self, question: str, answer: str) -> None:
        messages = [
            *self.messages,
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        self._save(messages, self.context_start)
        self.messages = messages

    def compress(self) -> None:
        context_start = len(self.messages)
        self._save(self.messages, context_start)
        self.context_start = context_start

    def _save(self, messages: list[dict[str, str]], context_start: int) -> None:
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
                    "version": 1,
                    "id": self.id,
                    "created_at": self.created_at,
                    "model": MODEL,
                    "context_start": context_start,
                    "messages": messages,
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
