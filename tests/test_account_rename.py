from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import account_rename
import platform_store
import teacher_registry
import user_management as um


def _test_root() -> Path:
    requested = Path(
        os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache")
    )
    requested.mkdir(parents=True, exist_ok=True)
    return requested


def _make_teacher_db(path: Path, username: str, password: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE users(
                   id INTEGER PRIMARY KEY,
                   username TEXT,
                   password_hash TEXT,
                   role TEXT,
                   org_id INTEGER
               )"""
        )
        conn.execute("CREATE TABLE students(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE plans(id INTEGER PRIMARY KEY)")
        password_hash = um.hash_password(password)
        conn.execute(
            "INSERT INTO users(username,password_hash,role,org_id) VALUES(?,?,?,?)",
            (username, password_hash, "teacher", 7),
        )
        conn.execute("INSERT INTO students DEFAULT VALUES")
        conn.execute("INSERT INTO plans DEFAULT VALUES")
        conn.commit()
        return password_hash
    finally:
        conn.close()


def _make_platform_db(path: Path, slug: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE org_user_roles(
                   id INTEGER PRIMARY KEY,
                   org_id INTEGER,
                   slug TEXT,
                   role TEXT,
                   created_at TEXT DEFAULT ''
               )"""
        )
        conn.execute(
            "INSERT INTO org_user_roles(org_id,slug,role) VALUES(?,?,?)",
            (7, slug, "teacher"),
        )
        conn.commit()
    finally:
        conn.close()


class AccountRenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.data = Path(self.case_tmp.name) / "data"
        self.data.mkdir(parents=True)
        self.roster = {
            "teachers": [
                {
                    "username": "admin",
                    "slug": "default",
                    "is_admin": False,
                }
            ],
            "usernames": {"admin": "default"},
            "settings": {},
        }
        (self.data / "teachers.json").write_text(
            json.dumps(self.roster, ensure_ascii=False),
            encoding="utf-8",
        )
        self.hash = _make_teacher_db(
            self.data / "scores.db",
            "admin",
            "same-password",
        )
        _make_platform_db(self.data / "platform.db", "default")

    def tearDown(self):
        self.case_tmp.cleanup()

    def test_rename_preserves_password_and_data_and_removes_org(self):
        result = account_rename.apply_rename(
            self.data,
            slug="default",
            old_username="admin",
            new_username="allen",
            personal=True,
        )
        self.assertTrue(result.changed)
        roster = json.loads((self.data / "teachers.json").read_text(encoding="utf-8"))
        self.assertEqual(roster["teachers"][0]["username"], "allen")
        self.assertTrue(roster["teachers"][0]["personal"])
        self.assertEqual(roster["usernames"], {"allen": "default"})
        conn = sqlite3.connect(self.data / "scores.db")
        try:
            row = conn.execute(
                "SELECT username,password_hash,org_id FROM users WHERE id=1"
            ).fetchone()
            counts = (
                conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
            )
        finally:
            conn.close()
        self.assertEqual(row[0], "allen")
        self.assertEqual(row[1], self.hash)
        self.assertIsNone(row[2])
        self.assertEqual(counts, (1, 1))
        conn = sqlite3.connect(self.data / "platform.db")
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM org_user_roles WHERE slug='default'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_reserved_name_is_rejected_without_writes(self):
        before = (self.data / "teachers.json").read_bytes()
        for reserved in ("admin", "ROOT", "Platform", "administrator"):
            with self.subTest(reserved=reserved):
                with self.assertRaises(account_rename.AccountRenameError):
                    account_rename.apply_rename(
                        self.data,
                        slug="default",
                        old_username="admin",
                        new_username=reserved,
                        personal=True,
                    )
        self.assertEqual((self.data / "teachers.json").read_bytes(), before)

    def test_update_teacher_cannot_bypass_username_validation(self):
        teacher_registry.configure(self.data)
        with self.assertRaises(ValueError):
            teacher_registry.update_teacher("default", username="root")

    def test_reconcile_teacher_username_repairs_stale_login_name(self):
        db = self.data / "scores.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                """CREATE TABLE user_login_attempts(
                       id INTEGER PRIMARY KEY,
                       username TEXT,
                       attempted_at REAL,
                       success INTEGER
                   )"""
            )
            conn.execute(
                "INSERT INTO user_login_attempts(username,attempted_at,success) "
                "VALUES('allen',1,0)"
            )
            conn.commit()
        finally:
            conn.close()

        ok, changed, error = um.reconcile_teacher_username("allen", db)

        self.assertTrue(ok, error)
        self.assertTrue(changed)
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT username,password_hash FROM users WHERE role='teacher'"
            ).fetchone()
            attempts = conn.execute(
                "SELECT COUNT(*) FROM user_login_attempts"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(row, ("allen", self.hash))
        self.assertEqual(attempts, 0)

    def test_reconcile_teacher_username_refuses_multiple_teacher_rows(self):
        db = self.data / "scores.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO users(username,password_hash,role,org_id) "
                "VALUES('second',?, 'teacher', 7)",
                (um.hash_password("second-password"),),
            )
            conn.commit()
        finally:
            conn.close()

        ok, changed, error = um.reconcile_teacher_username("allen", db)

        self.assertFalse(ok)
        self.assertFalse(changed)
        self.assertIn("数量异常", error)
        conn = sqlite3.connect(db)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT username FROM users WHERE role='teacher'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(names, {"admin", "second"})

    def test_personal_teacher_is_not_reattached_to_default_org(self):
        result = account_rename.apply_rename(
            self.data,
            slug="default",
            old_username="admin",
            new_username="allen",
            personal=True,
        )
        platform_store.configure(self.data)
        teacher_registry.configure(self.data)
        roster = json.loads((self.data / "teachers.json").read_text(encoding="utf-8"))

        org_id = platform_store.ensure_ready(roster["teachers"])

        self.assertIsNotNone(org_id)
        self.assertEqual(platform_store.list_org_users(int(org_id)), [])
        self.assertTrue(result.changed)

    def test_platform_admin_rename_preserves_password(self):
        platform = self.data / "teachers" / "aren_platform" / "scores.db"
        platform.parent.mkdir(parents=True)
        password_hash = _make_teacher_db(
            platform,
            "aren_platform",
            "platform-password",
        )
        roster = json.loads((self.data / "teachers.json").read_text(encoding="utf-8"))
        roster["teachers"].append(
            {
                "username": "aren_platform",
                "slug": "aren_platform",
                "is_admin": True,
            }
        )
        roster["usernames"]["aren_platform"] = "aren_platform"
        (self.data / "teachers.json").write_text(
            json.dumps(roster, ensure_ascii=False),
            encoding="utf-8",
        )
        account_rename.apply_rename(
            self.data,
            slug="aren_platform",
            old_username="aren_platform",
            new_username="alen_admin",
        )
        conn = sqlite3.connect(platform)
        try:
            row = conn.execute(
                "SELECT username,password_hash FROM users WHERE id=1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "alen_admin")
        self.assertEqual(row[1], password_hash)


if __name__ == "__main__":
    unittest.main()
