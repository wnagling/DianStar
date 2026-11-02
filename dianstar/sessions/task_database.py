"""Shared private SQLite state, with serialized first-open WAL initialization."""
import sqlite3

from dianstar.platform.file_state import private_directory, private_file, state_directory
from dianstar.security.file_locks import FileLocks


def connect_tasks(directory=None):
    directory = private_directory(directory or state_directory())
    with FileLocks(directory).hold("tasks-schema"):
        path = directory / "tasks.sqlite3"
        with private_file(path):
            pass
        connection = sqlite3.connect(path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, conversation_id TEXT, created REAL, state TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, turn_id TEXT, payload BLOB NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS command_environments (conversation_id TEXT NOT NULL, "
                "repository TEXT NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL, payload BLOB, "
                "PRIMARY KEY(conversation_id,repository))")
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise
