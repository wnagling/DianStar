"""Private metadata only; directory contents live once in the managed vault."""
from contextlib import closing
import json
import sqlite3
import time

from dianstar.platform.file_state import private_directory, private_file, protect, state_directory
from dianstar.security.file_locks import FileLocks
from .tree import StorageError

RETENTION = 30 * 86400


class StorageJournal:
    def __init__(self, directory=None, clock=time.time):
        self.directory, self.clock = directory or state_directory(), clock

    def connect(self):
        root = private_directory(self.directory)
        with FileLocks(root).hold("storage-schema"):
            path = root / "storage.sqlite3"
            with private_file(path):
                pass
            connection = sqlite3.connect(path, timeout=10)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("CREATE TABLE IF NOT EXISTS storage_entries (id TEXT PRIMARY KEY, kind TEXT, status TEXT, "
                    "revision INTEGER, created REAL, updated REAL, expires REAL, pinned INTEGER, payload BLOB NOT NULL)")
                connection.commit()
                return connection
            except BaseException:
                connection.close()
                raise

    def save_many(self, records):
        with closing(self.connect()) as connection, connection:
            for record in records:
                record["revision"] = record.get("revision", 0) + 1
                record["updated"] = self.clock()
                encoded = protect(json.dumps(record, ensure_ascii=False, allow_nan=False).encode())
                connection.execute("INSERT INTO storage_entries VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "status=excluded.status,revision=excluded.revision,updated=excluded.updated,expires=excluded.expires,pinned=excluded.pinned,payload=excluded.payload",
                    (record["id"], record["kind"], record["status"], record["revision"], record["created"], record["updated"],
                     record.get("expires"), int(record.get("pinned", False)), encoded))

    def save(self, record):
        self.save_many([record])

    def get(self, identifier):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT payload FROM storage_entries WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise StorageError("storage_not_found", "没有找到这条本机存储记录。")
        try:
            record = json.loads(protect(row[0], decrypt=True))
            if not isinstance(record, dict) or record.get("id") != identifier:
                raise ValueError("Invalid record")
            return record
        except (ValueError, OSError):
            raise StorageError("storage_record_invalid", "存储记录损坏或无法解密，请保留数据库和副本。") from None

    def records(self, kind=None, limit=None):
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT id FROM storage_entries " + ("WHERE kind=? " if kind else "") +
                "ORDER BY created DESC" + (" LIMIT ?" if limit is not None else ""),
                (*((kind,) if kind else ()), *((limit,) if limit is not None else ()))).fetchall()
        return [self.get(row[0]) for row in rows]
