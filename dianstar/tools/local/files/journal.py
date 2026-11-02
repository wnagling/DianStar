"""Durable, private undo records. No backup bytes are returned to the model."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import uuid

from dianstar.platform.file_state import private_directory, private_file, protect, state_directory
from dianstar.security.file_locks import FileLocks, canonical
from .text import FileError


class FileJournal:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else state_directory()

    def connect(self):
        # Two projects may reach a fresh shared database at the same time.
        # SQLite's WAL-mode transition can report BUSY before its usual busy
        # timeout applies; serialize only this short initialization phase.
        with FileLocks(self.directory).hold("journal-schema"):
            return self.open_initialized()

    def open_initialized(self):
        directory = private_directory(self.directory)
        path = directory / "history.sqlite3"
        with private_file(path):
            pass
        connection = sqlite3.connect(path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise FileError("file_journal_invalid", "恢复记录版本不支持，已停止文件修改。")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS transactions (id TEXT PRIMARY KEY, root TEXT NOT NULL, "
                               "request_id TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, payload BLOB NOT NULL, summary BLOB)")
            if "summary" not in {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}:
                connection.execute("ALTER TABLE transactions ADD COLUMN summary BLOB")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def save(self, record):
        payload = protect(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
        summary = protect(json.dumps(self.summary(record), ensure_ascii=False, separators=(",", ":")).encode())
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO transactions (id,root,request_id,created,status,payload,summary) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) "
                "DO UPDATE SET status=excluded.status, payload=excluded.payload, summary=excluded.summary",
                (record["id"], record["root"], record["request_id"], record["created"], record["status"], payload, summary))

    @staticmethod
    def decode(data):
        try:
            result = json.loads(protect(data, decrypt=True))
            if not isinstance(result, dict):
                raise ValueError("Invalid journal record")
            return result
        except Exception:
            raise FileError("file_journal_invalid", "本机恢复记录损坏或无法解密，已停止恢复；请保留当前文件和恢复数据库。") from None

    @staticmethod
    def summary(record):
        return {key: record.get(key) for key in ("id", "created", "status", "conflicts", "restored_by")} | {
            "entries": [{"path": entry["path"], "after_version": entry["after"]["version"] if entry.get("after") else None,
                         "restores_version": entry.get("restores_version")} for entry in record["entries"]]}

    def summaries(self, root, *, unfinished=False, limit=100):
        with closing(self.connect()) as connection:
            query = "SELECT id,summary FROM transactions WHERE root=?"
            if unfinished:
                query += " AND status IN ('prepared','rolling_back','conflict')"
            rows = connection.execute(query + " ORDER BY created DESC LIMIT ?", (canonical(root), limit + 1)).fetchall()
        if unfinished and len(rows) > limit:
            raise FileError("file_recovery_incomplete", "未完成恢复记录过多，请先处理本机恢复记录。")
        return [self.decode(row[1]) if row[1] is not None else self.summary(self.get(root, row[0]))
                for row in rows[:limit]]

    def get(self, root, identifier):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT payload,status FROM transactions WHERE id=? AND root=?", (identifier, canonical(root))).fetchone()
        if row is None:
            raise FileError("file_history_not_found", "当前项目中没有这条文件恢复记录。")
        record = self.decode(row[0])
        if record.get("id") != identifier or record.get("root") != canonical(root) or record.get("status") != row[1]:
            raise FileError("file_journal_invalid", "恢复记录与索引不一致，未修改项目文件。")
        return record

    def create(self, root, request_id, entries, *, identifier=None):
        record = {"id": identifier or uuid.uuid4().hex, "root": canonical(root), "request_id": request_id,
                  "created": time.time(), "status": "prepared", "entries": entries, "directories": []}
        self.save(record)
        return record
