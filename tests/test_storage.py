import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.storage import Challenge, Storage


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        path = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.storage = Storage(path)
        await self.storage.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_challenge_can_only_be_claimed_once(self) -> None:
        challenge = Challenge("abc", -100, 42, 7, 123.0)
        self.assertTrue(await self.storage.add_challenge(challenge))
        self.assertFalse(await self.storage.add_challenge(challenge))
        self.assertEqual(await self.storage.claim_challenge("abc"), challenge)
        self.assertIsNone(await self.storage.claim_challenge("abc"))

    async def test_warning_counter_can_be_reset(self) -> None:
        self.assertEqual(await self.storage.increment_warning(-100, 42), 1)
        self.assertEqual(await self.storage.increment_warning(-100, 42), 2)
        await self.storage.reset_warnings(-100, 42)
        self.assertEqual(await self.storage.increment_warning(-100, 42), 1)

    async def test_admin_warnings_persist_and_are_scoped_by_chat_and_user(self):
        counts = await asyncio.gather(*(
            self.storage.increment_admin_warning(-100, 42) for _ in range(3)
        ))
        self.assertEqual(counts, [1, 2, 3])
        reopened = Storage(str(Path(self.temp_dir.name) / "test.sqlite3"))
        await reopened.initialize()
        self.assertEqual(await reopened.increment_admin_warning(-100, 42), 4)
        self.assertEqual(await reopened.increment_admin_warning(-200, 42), 1)
        self.assertEqual(await reopened.increment_admin_warning(-100, 99), 1)
        self.assertEqual(await reopened.increment_warning(-100, 42), 1)

    async def test_username_rename_removal_and_reassignment(self):
        await self.storage.remember_user(-100, 42, "Member")
        self.assertEqual(await self.storage.find_user_id(-100, "MEMBER"), 42)
        self.assertIsNone(await self.storage.find_user_id(-200, "member"))
        await self.storage.remember_user(-100, 42, "renamed")
        self.assertIsNone(await self.storage.find_user_id(-100, "member"))
        await self.storage.remember_user(-100, 99, "renamed")
        self.assertEqual(await self.storage.find_user_id(-100, "renamed"), 99)
        await self.storage.remember_user(-100, 99, None)
        self.assertIsNone(await self.storage.find_user_id(-100, "renamed"))

    async def test_existing_database_migrates_without_resetting_state(self):
        path = str(Path(self.temp_dir.name) / "old.sqlite3")
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE warnings (chat_id INTEGER, user_id INTEGER, count INTEGER, "
                "PRIMARY KEY(chat_id, user_id)); INSERT INTO warnings VALUES (-100, 42, 2);"
            )
        finally:
            connection.close()
        storage = Storage(path)
        await storage.initialize()
        self.assertEqual(await storage.increment_warning(-100, 42), 3)
        self.assertEqual(await storage.increment_admin_warning(-100, 42), 1)
        await storage.schedule_deletion(-100, 10, 1000)
        self.assertEqual(await storage.due_deletions(1000), [(-100, 10)])

    async def test_previous_admin_warning_table_migrates_and_keeps_reason(self):
        path = str(Path(self.temp_dir.name) / "previous.sqlite3")
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "CREATE TABLE admin_warnings (chat_id INTEGER, user_id INTEGER, count INTEGER, "
                "PRIMARY KEY(chat_id, user_id)); INSERT INTO admin_warnings VALUES (-100, 42, 2);"
            )
        finally:
            connection.close()
        storage = Storage(path)
        await storage.initialize()
        await storage.initialize()
        self.assertEqual(await storage.increment_admin_warning(-100, 42, "за мат"), 3)
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("SELECT count, reason FROM admin_warnings").fetchone(), (3, "за мат"))
        finally:
            connection.close()

    async def test_mute_deadlines_and_stats_survive_restart(self):
        until = int(time.time()) + 300
        await self.storage.remember_user(-100, 42, "member")
        await self.storage.increment_admin_warning(-100, 42, "reason")
        await self.storage.set_mute(-100, 42, until)
        storage = Storage(str(Path(self.temp_dir.name) / "test.sqlite3"))
        await storage.initialize()
        self.assertEqual(await storage.active_mute(-100, 42), until)
        entry, = await storage.moderation_stats(-100)
        self.assertEqual((entry.user_id, entry.username, entry.warnings, entry.muted_until), (42, "member", 1, until))
        self.assertEqual(await storage.moderation_stats(-200), [])

    async def test_expired_mutes_are_not_active_and_permanent_mutes_remain(self):
        await self.storage.set_mute(-100, 42, time.time() - 1)
        self.assertIsNone(await self.storage.active_mute(-100, 42))
        await self.storage.set_mute(-100, 42, 0)
        self.assertEqual(await self.storage.active_mute(-100, 42), 0)
        await self.storage.clear_mute(-100, 42)
        self.assertIsNone(await self.storage.active_mute(-100, 42))


if __name__ == "__main__":
    unittest.main()

