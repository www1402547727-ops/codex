from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_store
import platform_store
import teacher_registry
import user_management as um


def _test_root() -> Path:
    requested = Path(os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache"))
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="recovery_guard_test_"))


def _make_user_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.execute(
            "INSERT INTO users(username, password_hash, role) VALUES(?,?,?)",
            ("admin", um.hash_password("fake-password"), um.ROLE_TEACHER),
        )
        conn.commit()
    finally:
        conn.close()


class RecoveryGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        cloud_store.configure("", "")
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.base = Path(self.case_tmp.name)
        self.teacher_root = self.base / "teacher_data"
        self.teacher_root.mkdir(parents=True, exist_ok=True)
        teacher_registry.configure(self.teacher_root)
        platform_store.configure(self.base)

    def tearDown(self):
        cloud_store.configure("", "")
        self.case_tmp.cleanup()

    def test_platform_db_read_failure_blocks_init_and_upload(self):
        with (
            patch.object(
                platform_store.cloud_store,
                "prepare_database",
                side_effect=cloud_store.CloudStorageError("network failed"),
            ),
            patch.object(platform_store, "_init_schema") as init_schema,
            patch.object(platform_store, "_save") as save,
        ):
            with self.assertRaises(platform_store.PlatformStoreUnavailable):
                platform_store.ensure_ready([])
            init_schema.assert_not_called()
            save.assert_not_called()
        self.assertFalse((self.base / "platform.db").exists())
        with self.assertRaises(platform_store.PlatformStoreUnavailable):
            platform_store.list_organizations()
        self.assertFalse((self.base / "platform.db").exists())

    def test_platform_db_404_allows_first_init(self):
        with (
            patch.object(
                platform_store.cloud_store,
                "prepare_database",
                return_value=False,
            ),
            patch.object(platform_store.cloud_store, "save_database") as save_database,
        ):
            org_id = platform_store.ensure_ready([])
        self.assertGreater(int(org_id), 0)
        self.assertTrue((self.base / "platform.db").is_file())
        save_database.assert_called_once()

    def test_teacher_db_read_failure_blocks_activation(self):
        db = self.base / "teacher" / "scores.db"
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(
                cloud_store,
                "download_object",
                side_effect=cloud_store.CloudStorageError("network failed"),
            ),
        ):
            ready, error = cloud_store.prepare_required_database(
                db,
                "database/teacher/scores.db",
            )
        self.assertFalse(ready)
        self.assertIn("无法读取", error)
        self.assertFalse(db.exists())

    def test_teacher_db_404_remains_missing(self):
        db = self.base / "teacher" / "scores.db"
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(cloud_store, "download_object", return_value=None),
        ):
            ready, error = cloud_store.prepare_required_database(
                db,
                "database/teacher/scores.db",
            )
        self.assertFalse(ready)
        self.assertIn("不存在", error)
        self.assertFalse(db.exists())

    def test_roster_read_failure_does_not_bootstrap_or_upload(self):
        default_db = self.teacher_root / "scores.db"
        _make_user_db(default_db)
        roster_path = self.teacher_root / "teachers.json"
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(
                cloud_store,
                "download_text",
                side_effect=cloud_store.CloudStorageError("network failed"),
            ),
            patch.object(teacher_registry, "bootstrap") as bootstrap,
            patch.object(cloud_store, "upload_text") as upload_text,
        ):
            with self.assertRaises(teacher_registry.RosterUnavailable):
                teacher_registry.ensure_ready(default_db)
            bootstrap.assert_not_called()
            upload_text.assert_not_called()
        self.assertFalse(roster_path.exists())

    def test_roster_invalid_json_does_not_bootstrap_or_upload(self):
        default_db = self.teacher_root / "scores.db"
        _make_user_db(default_db)
        roster_path = self.teacher_root / "teachers.json"
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(cloud_store, "download_text", return_value="{broken"),
            patch.object(teacher_registry, "bootstrap") as bootstrap,
            patch.object(cloud_store, "upload_text") as upload_text,
        ):
            with self.assertRaises(teacher_registry.RosterUnavailable):
                teacher_registry.ensure_ready(default_db)
            bootstrap.assert_not_called()
            upload_text.assert_not_called()
        self.assertFalse(roster_path.exists())

    def test_roster_configure_resets_cached_read_between_runs(self):
        default_db = self.teacher_root / "scores.db"
        _make_user_db(default_db)
        valid_roster = {
            "version": 1,
            "teachers": [
                {
                    "username": "admin",
                    "slug": "default",
                    "active": True,
                    "is_admin": True,
                }
            ],
            "usernames": {"admin": "default"},
            "settings": {},
        }
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(
                cloud_store,
                "download_text",
                side_effect=[
                    json.dumps(valid_roster),
                    cloud_store.CloudStorageError("network failed"),
                ],
            ),
            patch.object(cloud_store, "upload_text") as upload_text,
        ):
            teacher_registry.configure(self.teacher_root)
            first = teacher_registry.load()
            self.assertEqual(first["usernames"]["admin"], "default")

            teacher_registry.configure(self.teacher_root)
            with self.assertRaises(teacher_registry.RosterUnavailable):
                teacher_registry.ensure_ready(default_db)
            upload_text.assert_not_called()

    def test_roster_404_allows_first_bootstrap(self):
        default_db = self.teacher_root / "scores.db"
        _make_user_db(default_db)
        with (
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(cloud_store, "download_text", return_value=None),
            patch.object(cloud_store, "upload_text") as upload_text,
        ):
            roster = teacher_registry.ensure_ready(default_db)
        self.assertTrue(roster["teachers"][0]["is_admin"])
        self.assertTrue((self.teacher_root / "teachers.json").is_file())
        upload_text.assert_called_once()
        saved = json.loads(
            (self.teacher_root / "teachers.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["usernames"]["admin"], "default")


if __name__ == "__main__":
    unittest.main()
