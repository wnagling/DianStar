"""Conversation/project environment records; never credentials or execution grants."""
from contextlib import closing
import json
import sqlite3
import time

from dianstar.platform.file_state import protect, state_directory
from dianstar.platform.command_environment import profile_values
from dianstar.security.file_locks import canonical
from .task_database import connect_tasks


class EnvironmentProfileError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class EnvironmentStore:
    def __init__(self, conversation_id, directory=None):
        self.conversation_id = conversation_id
        self.directory = directory or state_directory()

    def scope(self, root):
        identifier = self.conversation_id
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 128
                or any(ord(char) < 32 for char in identifier)):
            raise EnvironmentProfileError("conversation_required", "当前调用没有有效会话标识，不能保存或复用会话环境；仍可传本次命令环境。")
        return identifier, canonical(root)

    def load(self, root):
        scope = self.scope(root)
        try:
            with closing(connect_tasks(self.directory)) as connection:
                row = connection.execute("SELECT revision,payload FROM command_environments WHERE conversation_id=? AND repository=?", scope).fetchone()
            if row is None:
                return {"revision": 0, "environment": {}, "path_prepend": [], "configured": False}
            revision, payload = row
            values = json.loads(protect(payload, decrypt=True)) if payload is not None else {"environment": {}, "path_prepend": []}
            if not isinstance(values, dict) or set(values) != {"environment", "path_prepend"} or type(revision) is not int or revision < 1:
                raise ValueError("Invalid environment record")
            values = profile_values(values["environment"], values["path_prepend"], check_directories=False)
            return {**values, "revision": revision, "configured": payload is not None}
        except (ValueError, OSError, sqlite3.Error):
            raise EnvironmentProfileError("project_environment_unavailable", "本机会话环境记录无法读取或解密；未执行命令，请保留数据库并排查。") from None

    def replace(self, root, expected_revision, values):
        scope = self.scope(root)
        if values is not None:
            values = profile_values(values["environment"], values["path_prepend"], check_directories=False)
        payload = protect(json.dumps(values, ensure_ascii=False, allow_nan=False).encode()) if values is not None else None
        try:
            with closing(connect_tasks(self.directory)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT revision FROM command_environments WHERE conversation_id=? AND repository=?", scope).fetchone()
                current = row[0] if row else 0
                if type(expected_revision) is not int or expected_revision != current:
                    raise EnvironmentProfileError("project_environment_changed", "会话环境已变化；请重新查询当前版本，再决定是否修改。")
                revision = current + 1
                connection.execute("INSERT INTO command_environments VALUES (?,?,?,?,?) ON CONFLICT(conversation_id,repository) "
                    "DO UPDATE SET revision=excluded.revision,updated=excluded.updated,payload=excluded.payload",
                    (*scope, revision, time.time(), payload))
            return revision
        except (OSError, sqlite3.Error):
            raise EnvironmentProfileError("project_environment_unavailable", "会话环境保存失败；未确认保存成功，请重新查询。") from None
