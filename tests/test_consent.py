from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import streamlit as st

import cloud_store
import platform_store
import teacher_registry
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
        teacher_registry.configure(self.base)
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
        conn = sqlite3.connect(platform_store.platform_db_path())
        try:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(teacher_notice_consents)"
                )
            }
        finally:
            conn.close()
        conn = sqlite3.connect(self.db)
        try:
            teacher_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            guardian_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(guardian_consent_records)"
                )
            }
        finally:
            conn.close()
        self.assertEqual(
            columns,
            {"username", "version", "accepted_at"},
        )
        self.assertNotIn("consent_records", teacher_tables)
        self.assertTrue(
            {
                "student_user_id",
                "student_username",
                "student_id",
                "teacher_user_id",
                "teacher_username",
                "version",
                "accepted_at",
            }.issubset(guardian_columns)
        )
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

        conn = sqlite3.connect(platform_store.platform_db_path())
        try:
            rows = conn.execute(
                "SELECT version, accepted_at FROM teacher_notice_consents WHERE username=?",
                (self.user["username"],),
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
                int(self.user["id"]), db_path=self.db, version="v3"
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
        conn = sqlite3.connect(platform_store.platform_db_path())
        try:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM teacher_notice_consents"
                ).fetchone()[0]
            )
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_teacher_without_notice_cannot_access_any_page_before_acceptance(self):
        with patch.object(st, "session_state", self._state()):
            self.assertFalse(
                um.server_side_notice_allows_page(self.user, db_path=self.db)
            )
            ok, err = um.accept_notice(
                self.user,
                db_path=self.db,
                object_path=cloud_store.db_object_path("teacher"),
            )
            self.assertTrue(ok, err)
            self.assertTrue(
                um.server_side_notice_allows_page(self.user, db_path=self.db)
            )

    def test_platform_admin_does_not_need_notice(self):
        roster = {
            "teachers": [
                {
                    "username": self.user["username"],
                    "slug": "teacher",
                    "is_admin": True,
                }
            ],
            "usernames": {self.user["username"]: "teacher"},
        }
        teacher_registry.configure(self.base)
        (self.base / "teachers.json").write_text(
            json.dumps(roster, ensure_ascii=False),
            encoding="utf-8",
        )
        state = self._state()
        with patch.object(st, "session_state", state):
            self.assertTrue(um.is_platform_admin_account(self.user))
            self.assertFalse(um.notice_required(self.user, db_path=self.db))

    def test_contact_is_live_in_platform_notice_without_version_change(self):
        self.assertEqual(platform_store.get_contact_info(), "")
        platform_store.set_contact_info("微信：zhixue-test")

        conn = sqlite3.connect(platform_store.platform_db_path())
        try:
            row = conn.execute(
                "SELECT key, value FROM platform_settings WHERE key=?",
                (platform_store.PLATFORM_CONTACT_KEY,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (platform_store.PLATFORM_CONTACT_KEY, "微信：zhixue-test"))

        version_before = um.NOTICE_VERSION
        notice = um.notice_markdown()
        self.assertIn("智学教务平台", notice)
        self.assertIn("微信：zhixue-test", notice)
        self.assertNotIn("联系方式暂未设置", notice)
        self.assertNotIn("生效日期", notice)

        platform_store.set_contact_info("电话：13800000000")
        self.assertEqual(um.NOTICE_VERSION, version_before)
        updated_notice = um.notice_markdown()
        self.assertIn("电话：13800000000", updated_notice)
        self.assertNotIn("微信：zhixue-test", updated_notice)

        platform_store.set_contact_info("")
        self.assertIn("联系方式暂未设置", um.notice_markdown())

    def test_notice_contact_escapes_markup_and_limits_length(self):
        markup = "<b>**危险**</b>"
        safe = um.safe_contact_text(markup)
        self.assertNotIn("<b>", safe)
        self.assertIn("&lt;b&gt;", safe)
        self.assertIn("\\*\\*危险\\*\\*", safe)
        self.assertNotIn("<b>", um.notice_markdown(markup))

        safe_long = um.safe_contact_text("x" * 250)
        self.assertEqual(safe_long.count("x"), platform_store.CONTACT_MAX_LENGTH)

        with self.assertRaises(ValueError):
            platform_store.set_contact_info("x" * (platform_store.CONTACT_MAX_LENGTH + 1))

    def test_student_account_creation_rejects_without_guardian_consent_backend(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO students(id,name,grade) VALUES(1,'虚构学生','初三')"
            )
            conn.commit()
        finally:
            conn.close()

        ok, error = um.create_account_in(
            self.db,
            "student_without_guardian",
            "student-password",
            um.ROLE_STUDENT,
            student_id=1,
            guardian_consent=False,
            actor_user_id=int(self.user["id"]),
            actor_username=str(self.user["username"]),
        )
        self.assertFalse(ok)
        self.assertIn("监护人", error)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM users WHERE username=?",
                    ("student_without_guardian",),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM guardian_consent_records"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

        ok, error = um.create_account_in(
            self.db,
            "student_with_guardian",
            "student-password",
            um.ROLE_STUDENT,
            student_id=1,
            guardian_consent=True,
            actor_user_id=int(self.user["id"]),
            actor_username=str(self.user["username"]),
        )
        self.assertTrue(ok, error)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                """SELECT student_username, student_id, teacher_user_id,
                          teacher_username, version
                   FROM guardian_consent_records
                   WHERE student_username=?""",
                ("student_with_guardian",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "student_with_guardian")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], int(self.user["id"]))
        self.assertEqual(row[3], self.user["username"])
        self.assertEqual(row[4], um.GUARDIAN_CONSENT_VERSION)


if __name__ == "__main__":
    unittest.main()
