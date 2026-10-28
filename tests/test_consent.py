from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import streamlit as st

import cloud_store
import platform_store
import user_management as um


def _test_root() -> Path:
    requested = Path(
        os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache")
    )
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="score_tracker_consent_test_"))


class ConsentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.base = Path(self.case_tmp.name)
        self.db = self.base / "teacher" / "scores.db"
        self.db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db)
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
        platform_store.configure(self.base)
        cloud_store.configure("", "")
        self.org_id = platform_store.ensure_ready([])
        um.init_user_db(
            self.db,
            default_teacher_password="test-password",
            object_path=cloud_store.db_object_path("teacher"),
            org_id=self.org_id,
        )
        self.user = self._teacher()

    def tearDown(self):
        self.case_tmp.cleanup()

    def _teacher(self) -> dict:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE role='teacher' LIMIT 1"
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def _state(self, *, impersonating: bool = False) -> dict:
        state = {
            "auth_user_id": int(self.user["id"]),
            "auth_username": str(self.user["username"]),
            "auth_actor_role": "teacher",
            "auth_org_id": self.org_id,
            "db_path": str(self.db),
            "db_slug": "teacher",
            "auth_db_path": str(self.db),
            "auth_db_slug": "teacher",
        }
        if impersonating:
            state[platform_store.IMPERSONATION_KEY] = {
                "teacher_slug": "teacher",
                "org_id": self.org_id,
            }
        return state

    def test_schema_and_first_login_requires_current_version(self):
        conn = sqlite3.connect(self.db)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(consent_records)")
            }
        finally:
            conn.close()
        self.assertTrue({"user_id", "version", "accepted_at"}.issubset(columns))
        with patch.object(st, "session_state", self._state()):
            self.assertTrue(um.notice_required(self.user, db_path=self.db))

    def test_acceptance_is_idempotent_and_audited_without_content(self):
        state = self._state()
        with patch.object(st, "session_state", state):
            ok, err = um.accept_notice(
                self.user,
                db_path=self.db,
                object_path=cloud_store.db_object_path("teacher"),
            )
            self.assertTrue(ok, err)
            ok, err = um.accept_notice(
                self.user,
                db_path=self.db,
                object_path=cloud_store.db_object_path("teacher"),
            )
            self.assertTrue(ok, err)
            self.assertFalse(um.notice_required(self.user, db_path=self.db))

        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT version, accepted_at FROM consent_records WHERE user_id=?",
                (int(self.user["id"]),),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], um.NOTICE_VERSION)
        self.assertTrue(rows[0][1])

        records = platform_store.list_audit(org_id=self.org_id)
        accepted = [r for r in records if r["action"] == "consent_accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["detail"], f"version={um.NOTICE_VERSION}")
        self.assertNotIn("假学生", str(accepted[0]))

    def test_new_version_requires_acceptance_again(self):
        with patch.object(st, "session_state", self._state()):
            ok, err = um.accept_notice(
                self.user,
                db_path=self.db,
                object_path=cloud_store.db_object_path("teacher"),
            )
            self.assertTrue(ok, err)
        self.assertFalse(
            um.notice_required(self.user, db_path=self.db)
        )
        self.assertTrue(
            not um.has_accepted_notice(
                int(self.user["id"]), db_path=self.db, version="v2"
            )
        )

    def test_student_does_not_need_notice(self):
        student = dict(self.user)
        student["role"] = um.ROLE_STUDENT
        self.assertFalse(um.notice_required(student, db_path=self.db))

    def test_impersonation_cannot_write_consent(self):
        state = self._state(impersonating=True)
        with patch.object(st, "session_state", state):
            ok, err = um.accept_notice(
                self.user,
                db_path=self.db,
                object_path=cloud_store.db_object_path("teacher"),
            )
        self.assertFalse(ok)
        self.assertIn("不能修改", err)
        conn = sqlite3.connect(self.db)
        try:
            count = int(
                conn.execute("SELECT COUNT(*) FROM consent_records").fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
