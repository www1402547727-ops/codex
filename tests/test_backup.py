from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, call, patch

import cloud_store
import user_management as um


def _test_root() -> Path:
    requested = Path(
        os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache")
    )
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="score_tracker_backup_test_"))


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.base = Path(self.case_tmp.name)

    def tearDown(self):
        self.case_tmp.cleanup()

    def test_retention_keeps_29_days_and_deletes_40_days(self):
        today = date(2026, 10, 28)
        rows = [
            {
                "name": "2026-09-18",
                "object_path": "backups/2026-09-18",
                "is_file": False,
            },
            {
                "name": "2026-09-29",
                "object_path": "backups/2026-09-29",
                "is_file": False,
            },
            {
                "name": "not-a-date",
                "object_path": "backups/not-a-date",
                "is_file": False,
            },
            {
                "name": "platform.db",
                "object_path": "backups/platform.db",
                "is_file": True,
            },
        ]
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(
                cloud_store, "list_storage_objects", return_value=rows
            ) as listing,
            patch.object(
                cloud_store, "delete_storage_objects", return_value=1
            ) as deleting,
        ):
            stale = cloud_store.cleanup_old_backups(today=today)

        listing.assert_called_once_with("backups")
        deleting.assert_called_once_with(["backups/2026-09-18"])
        self.assertEqual(stale, ["backups/2026-09-18"])

    def test_delete_uses_supabase_bulk_prefix_endpoint(self):
        response = Mock(ok=True, status_code=200, text="")
        cloud_store.configure("https://example.supabase.co", "test-key", "bucket")
        try:
            with patch.object(
                cloud_store.requests, "delete", return_value=response
            ) as request:
                count = cloud_store.delete_storage_objects(
                    ["backups/2020-01-01", "backups/2020-01-02"]
                )
        finally:
            cloud_store.configure("", "")
        self.assertEqual(count, 2)
        args, kwargs = request.call_args
        self.assertEqual(
            args[0],
            "https://example.supabase.co/storage/v1/object/bucket",
        )
        self.assertEqual(
            kwargs["json"],
            {"prefixes": ["backups/2020-01-01", "backups/2020-01-02"]},
        )

    def test_cleanup_failure_does_not_block_database_save(self):
        db = self.base / "teacher" / "scores.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"placeholder")
        cloud_store._last_backup_date.clear()
        cloud_store._last_uploaded_digest.clear()
        try:
            with (
                patch.object(cloud_store, "is_cloud", return_value=True),
                patch.object(cloud_store, "_snapshot", return_value=b"sqlite"),
                patch.object(cloud_store, "download_object", return_value=None),
                patch.object(cloud_store, "upload_object") as uploading,
                patch.object(
                    cloud_store,
                    "cleanup_old_backups",
                    side_effect=RuntimeError("temporary"),
                ),
            ):
                cloud_store.save_database(
                    db, "database/teacher/scores.db"
                )
        finally:
            cloud_store._last_backup_date.clear()
            cloud_store._last_uploaded_digest.clear()
        uploaded_paths = [args[0] for args, _ in uploading.call_args_list]
        self.assertIn("database/teacher/scores.db", uploaded_paths)
        self.assertTrue(
            any(path.startswith("backups/") for path in uploaded_paths)
        )

    def test_real_restore_allows_login_and_content_read(self):
        live = self.base / "live" / "scores.db"
        live.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(live)
        try:
            conn.executescript(
                """
                CREATE TABLE students(
                    id INTEGER PRIMARY KEY, name TEXT, grade TEXT,
                    status TEXT DEFAULT '在读'
                );
                CREATE TABLE one_to_one_students(
                    id INTEGER PRIMARY KEY, name TEXT, grade TEXT
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        um.init_user_db(
            live,
            default_teacher_password="backup-password",
            object_path=cloud_store.db_object_path("restore_test"),
        )
        conn = sqlite3.connect(live)
        try:
            conn.execute(
                "INSERT INTO students(name, grade, status) VALUES(?,?,?)",
                ("假学生", "初二", "在读"),
            )
            conn.execute(
                """CREATE TABLE plans(
                       id INTEGER PRIMARY KEY, plan_date TEXT, status TEXT
                   )"""
            )
            conn.execute(
                "INSERT INTO plans(id, plan_date, status) VALUES(1,?,?)",
                ("2026-10-28", "待上课"),
            )
            conn.commit()
        finally:
            conn.close()

        backup = self.base / "backup" / "scores.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(cloud_store._snapshot(live))

        conn = sqlite3.connect(live)
        try:
            conn.execute("UPDATE plans SET status='已取消'")
            conn.execute("UPDATE students SET name='被改坏'")
            conn.commit()
        finally:
            conn.close()

        shutil.copy2(backup, live)
        um.configure(live)
        state = {"db_path": str(live), "db_slug": "restore_test"}
        with patch.object(um.st, "session_state", state):
            user, error = um.authenticate("teacher", "backup-password")
        self.assertIsNotNone(user, error)
        conn = sqlite3.connect(live)
        try:
            status = conn.execute(
                "SELECT status FROM plans WHERE id=1"
            ).fetchone()[0]
            name = conn.execute(
                "SELECT name FROM students WHERE id=1"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "待上课")
        self.assertEqual(name, "假学生")


if __name__ == "__main__":
    unittest.main()
