from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Challenge:
    token: str
    chat_id: int
    user_id: int
    answer: int
    deadline: float
    message_id: int | None = None


class Storage:
    """Small SQLite repository; all operations are serialized in-process."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        parent = Path(self._path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS challenges (
                        token TEXT PRIMARY KEY,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        answer INTEGER NOT NULL,
                        deadline REAL NOT NULL,
                        message_id INTEGER,
                        UNIQUE(chat_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS warnings (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        count INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS admin_warnings (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        count INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS chat_users (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        username TEXT,
                        PRIMARY KEY(chat_id, user_id),
                        UNIQUE(chat_id, username)
                    );
                    CREATE TABLE IF NOT EXISTS pending_deletions (
                        chat_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        delete_at REAL NOT NULL,
                        PRIMARY KEY(chat_id, message_id)
                    );
                    """
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def add_challenge(self, challenge: Challenge) -> bool:
        async with self._lock:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO challenges
                        (token, chat_id, user_id, answer, deadline, message_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        challenge.token,
                        challenge.chat_id,
                        challenge.user_id,
                        challenge.answer,
                        challenge.deadline,
                        challenge.message_id,
                    ),
                )
                return cursor.rowcount == 1

    async def set_challenge_message(self, token: str, message_id: int) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE challenges SET message_id = ? WHERE token = ?",
                    (message_id, token),
                )

    async def get_challenge(self, token: str) -> Challenge | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM challenges WHERE token = ?", (token,)
                ).fetchone()
        return _challenge_from_row(row) if row else None

    async def claim_challenge(self, token: str) -> Challenge | None:
        """Atomically return and remove a challenge so it is resolved once."""
        async with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM challenges WHERE token = ?", (token,)
                ).fetchone()
                if row is not None:
                    connection.execute(
                        "DELETE FROM challenges WHERE token = ?", (token,)
                    )
                connection.commit()
        return _challenge_from_row(row) if row else None

    async def remove_user_challenge(
        self, chat_id: int, user_id: int
    ) -> Challenge | None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM challenges WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        "DELETE FROM challenges WHERE chat_id = ? AND user_id = ?",
                        (chat_id, user_id),
                    )
                connection.commit()
        return _challenge_from_row(row) if row else None

    async def list_challenges(self) -> list[Challenge]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute("SELECT * FROM challenges").fetchall()
        return [_challenge_from_row(row) for row in rows]

    async def increment_warning(self, chat_id: int, user_id: int) -> int:
        async with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO warnings (chat_id, user_id, count) VALUES (?, ?, 1)
                    ON CONFLICT(chat_id, user_id)
                    DO UPDATE SET count = count + 1
                    """,
                    (chat_id, user_id),
                )
                row = connection.execute(
                    "SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                ).fetchone()
                connection.commit()
        return int(row["count"])

    async def reset_warnings(self, chat_id: int, user_id: int) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM warnings WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                )

    async def increment_admin_warning(self, chat_id: int, user_id: int) -> int:
        async with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO admin_warnings (chat_id, user_id, count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1
                    """,
                    (chat_id, user_id),
                )
                row = connection.execute(
                    "SELECT count FROM admin_warnings WHERE chat_id = ? AND user_id = ?",
                    (chat_id, user_id),
                ).fetchone()
        return int(row["count"])

    async def remember_user(
        self, chat_id: int, user_id: int, username: str | None
    ) -> None:
        username = username.casefold() if username else None
        async with self._lock:
            with self._connect() as connection:
                # Telegram usernames can be renamed and reassigned to another user.
                connection.execute(
                    "UPDATE chat_users SET username = NULL "
                    "WHERE chat_id = ? AND username = ? AND user_id != ?",
                    (chat_id, username, user_id),
                )
                connection.execute(
                    """
                    INSERT INTO chat_users (chat_id, user_id, username) VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET username = excluded.username
                    """,
                    (chat_id, user_id, username),
                )

    async def find_user_id(self, chat_id: int, username: str) -> int | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT user_id FROM chat_users WHERE chat_id = ? AND username = ?",
                    (chat_id, username.casefold()),
                ).fetchone()
        return int(row["user_id"]) if row else None

    async def schedule_deletion(
        self, chat_id: int, message_id: int, delete_at: float
    ) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO pending_deletions (chat_id, message_id, delete_at)
                    VALUES (?, ?, ?)
                    """,
                    (chat_id, message_id, delete_at),
                )

    async def due_deletions(self, now: float) -> list[tuple[int, int]]:
        async with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT chat_id, message_id FROM pending_deletions "
                    "WHERE delete_at <= ? ORDER BY delete_at LIMIT 100",
                    (now,),
                ).fetchall()
        return [(int(row["chat_id"]), int(row["message_id"])) for row in rows]

    async def remove_deletion(self, chat_id: int, message_id: int) -> None:
        async with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM pending_deletions WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                )


def _challenge_from_row(row: sqlite3.Row) -> Challenge:
    return Challenge(
        token=str(row["token"]),
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        answer=int(row["answer"]),
        deadline=float(row["deadline"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
    )

