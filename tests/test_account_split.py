from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import streamlit as st

import account_split
import cloud_store
import platform_store
import user_management as um


def _test_root() -> Path:
    requested = Path(os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache"))
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="account_split_test_"))


def _make_old_admin_db(path: Path, password: str = "old-admin-password") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE students(
                id INTEGER PRIMARY KEY, name TEXT DEFAULT '', grade TEXT DEFAULT '',
                school TEXT DEFAULT '', status TEXT DEFAULT '在读'
            );
            CREATE TABLE plans(id INTEGER PRIMARY KEY, note TEXT DEFAULT '');
            CREATE TABLE users(
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('teacher','student')),
                org_id INTEGER,
                student_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT '',
                last_login TEXT DEFAULT '',
                note TEXT DEFAULT ''
            );
            CREATE TABLE one_to_one_students(
                id INTEGER PRIMARY KEY, name TEXT, grade TEXT, student_id INTEGER
            );
            """
        )
        password_hash = um.hash_password(password)
        conn.execute(
            """INSERT INTO users(
                   username, password_hash, role, active, must_change_password, note
               ) VALUES(?,?,?,?,?,?)""",
            ("admin", password_hash, um.ROLE_TEACHER, 1, 0, "原管理员"),
        )
        conn.execute("INSERT INTO students(name) VALUES('虚构学生')")
        conn.execute("INSERT INTO plans(note) VALUES('虚构课程')")
        conn.commit()
        return password_hash
    finally:
        conn.close()


def _roster() -> dict:
    return {
        "version": 1,
        "teachers": [
            {
                "username": "teacher",
                "display_name": "阿伦",
                "slug": "default",
                "active": True,
                "is_admin": True,
                "created_at": "2026-10-28T19:10:36",
                "note": "原有数据",
            }
        ],
        "usernames": {
            "teacher": "default",
            "admin": "default",
            "student_one": "default",
        },
        "settings": {"teachers_can_create_students": True},
    }


class AccountSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        cloud_store.configure("", "")
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.base = Path(self.case_tmp.name)
        self.data = self.base / "data"
        self.data.mkdir(parents=True)
        self.roster_path = self.data / "teachers.json"
        self.roster_path.write_text(
            json.dumps(_roster(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.old_db = self.data / "scores.db"
        self.old_hash = _make_old_admin_db(self.old_db)
        self.platform_db = self.data / "platform.db"
        self.platform_db.write_bytes(b"fake-platform-db")

    def tearDown(self):
        self.case_tmp.cleanup()

    def test_split_creates_separate_platform_admin(self):
        result = account_split.apply_split(
            self.data,
            password="fake-platform-password",
        )

        self.assertTrue(result.changed)
        roster = json.loads(self.roster_path.read_text(encoding="utf-8"))
        old_admin = next(item for item in roster["teachers"] if item["slug"] == "default")
        platform_admin = next(
            item for item in roster["teachers"] if item["slug"] == "aren_platform"
        )
        self.assertFalse(old_admin["is_admin"])
        self.assertTrue(old_admin["protected"])
        self.assertTrue(platform_admin["is_admin"])
        self.assertEqual(platform_admin["username"], "aren_platform")
        self.assertEqual(roster["usernames"]["aren_platform"], "aren_platform")
        self.assertEqual(roster["usernames"]["admin"], "default")

        conn = sqlite3.connect(result.platform_db)
        try:
            users = conn.execute(
                "SELECT username, password_hash, role FROM users"
            ).fetchall()
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0][0], "aren_platform")
        self.assertTrue(um.verify_password("fake-platform-password", users[0][1]))
        self.assertNotIn("students", tables)
        self.assertNotIn("plans", tables)

    def test_split_preserves_old_password_data_and_platform_db(self):
        platform_before = self.platform_db.read_bytes()
        old_db_before = self.old_db.read_bytes()
        result = account_split.apply_split(
            self.data,
            password="fake-platform-password",
        )

        self.assertEqual(self.old_db.read_bytes(), old_db_before)
        self.assertEqual(self.platform_db.read_bytes(), platform_before)
        conn = sqlite3.connect(self.old_db)
        try:
            row = conn.execute(
                "SELECT username, password_hash FROM users WHERE username='admin'"
            ).fetchone()
            students = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
            plans = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(row[0], "admin")
        self.assertEqual(row[1], self.old_hash)
        self.assertEqual(students, 1)
        self.assertEqual(plans, 1)
        self.assertTrue((result.backup_dir / "teachers.json").is_file())
        self.assertTrue((result.backup_dir / "old_admin" / "scores.db").is_file())
        self.assertTrue((result.backup_dir / "platform.db").is_file())

    def test_platform_admin_login_query_works_without_student_table(self):
        result = account_split.apply_split(
            self.data,
            password="fake-platform-password",
        )
        um.configure(result.platform_db)
        with patch.object(st, "session_state", {}):
            row = um._get_user_by_username("aren_platform")
        self.assertIsNotNone(row)
        self.assertEqual(row["username"], "aren_platform")
        self.assertEqual(row["student_name"], "")

    def test_platform_admin_is_not_added_to_default_org(self):
        platform_store.configure(self.base)
        org_id = platform_store.ensure_ready(
            [
                {"slug": "normal_teacher", "is_admin": False},
                {"slug": "aren_platform", "is_admin": True},
            ]
        )
        members = platform_store.list_org_users(org_id)
        self.assertEqual([item["slug"] for item in members], ["normal_teacher"])

    def test_split_is_idempotent(self):
        first = account_split.apply_split(
            self.data,
            password="fake-platform-password",
        )
        second = account_split.apply_split(
            self.data,
            password="different-but-unused-password",
            backup_dir=self.base / "should_not_be_created",
        )
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.platform_db, first.platform_db)
        self.assertFalse((self.base / "should_not_be_created").exists())

    def test_rollback_restores_roster_and_quarantines_new_db(self):
        result = account_split.apply_split(
            self.data,
            password="fake-platform-password",
        )
        quarantine = account_split.rollback_split(self.data, result.backup_dir)

        restored = json.loads(self.roster_path.read_text(encoding="utf-8"))
        self.assertTrue(restored["teachers"][0]["is_admin"])
        self.assertNotIn("aren_platform", restored["usernames"])
        self.assertFalse(result.platform_db.exists())
        self.assertTrue((quarantine / "scores.db").is_file())

    def test_old_admin_becomes_plain_teacher_in_roster(self):
        account_split.apply_split(
            self.data,
            password="fake-platform-password",
        )
        roster = json.loads(self.roster_path.read_text(encoding="utf-8"))
        old = next(item for item in roster["teachers"] if item["slug"] == "default")
        self.assertFalse(old["is_admin"])
        self.assertTrue(old["protected"])
        self.assertEqual(old["username"], "teacher")
        self.assertEqual(old["active"], True)
        self.assertIn("纯老师账号", old["note"])


if __name__ == "__main__":
    unittest.main()
