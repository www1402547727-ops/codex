from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import streamlit as st

import cloud_store
import course_management as cm
import impersonation
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
        return Path(tempfile.mkdtemp(prefix="score_tracker_impersonation_test_"))


def _make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE demo(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO demo(value) VALUES('before')")
        conn.commit()
    finally:
        conn.close()


class ImpersonationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.base = Path(self.case_tmp.name)
        self.teacher_root = self.base / "teachers"
        self.teacher_root.mkdir(parents=True, exist_ok=True)
        platform_store.configure(self.base)
        teacher_registry.configure(self.teacher_root)
        cloud_store.configure("", "")
        self.org_id = platform_store.ensure_ready([])
        self.teacher_slug = "target_teacher"
        ok, err = teacher_registry.add_teacher(
            "target_teacher", "测试老师", self.teacher_slug
        )
        self.assertTrue(ok, err)
        ok, err = platform_store.add_org_user(
            self.org_id, self.teacher_slug, platform_store.ROLE_TEACHER
        )
        self.assertTrue(ok, err)
        self.target_db = (
            cloud_store.get_data_dir(self.teacher_root, self.teacher_slug)
            / "scores.db"
        )
        _make_db(self.target_db)
        self.admin_db = self.base / "admin" / "scores.db"
        _make_db(self.admin_db)

    def tearDown(self):
        self.case_tmp.cleanup()

    def _state(self) -> dict:
        return {
            "auth_actor_role": "platform_admin",
            "auth_org_id": self.org_id,
            "auth_user_id": 1,
            "auth_username": "platform_admin",
            "db_path": str(self.admin_db),
            "db_slug": "platform_admin",
            "page": "平台概览",
        }

    def _start(self, state: dict | None = None):
        state = state or self._state()
        with patch.object(st, "session_state", state):
            ok, err = impersonation.start(
                org_id=self.org_id,
                teacher_slug=self.teacher_slug,
                reason="数据核对",
                note="核对课程时间显示",
                consent_method="微信文字确认",
                consent_date=date.today(),
                consent_confirmed=True,
                actor_user={
                    "id": 1,
                    "role": "teacher",
                    "username": "platform_admin",
                },
            )
        self.assertTrue(ok, err)
        return state

    def test_start_requires_reason_consent_and_valid_org(self):
        state = self._state()
        with patch.object(st, "session_state", state):
            ok, err = impersonation.start(
                org_id=self.org_id,
                teacher_slug=self.teacher_slug,
                reason="数据核对",
                note="",
                consent_method="微信文字确认",
                consent_date=date.today(),
                consent_confirmed=True,
                actor_user={"id": 1, "role": "teacher"},
            )
            self.assertFalse(ok)
            self.assertIn("补充说明", err)
            self.assertNotIn(platform_store.IMPERSONATION_KEY, state)

        with patch.object(st, "session_state", state):
            ok, err = impersonation.start(
                org_id=self.org_id + 999,
                teacher_slug=self.teacher_slug,
                reason="数据核对",
                note="核对课程时间显示",
                consent_method="微信文字确认",
                consent_date=date.today(),
                consent_confirmed=True,
                actor_user={"id": 1, "role": "teacher"},
            )
            self.assertFalse(ok)
            self.assertIn("不属于", err)

    def test_start_requires_explicit_consent_and_known_options(self):
        state = self._state()
        base = {
            "org_id": self.org_id,
            "teacher_slug": self.teacher_slug,
            "reason": "数据核对",
            "note": "核对课程时间显示",
            "consent_method": "微信文字确认",
            "consent_date": date.today(),
            "actor_user": {"id": 1, "role": "teacher"},
        }
        with patch.object(st, "session_state", state):
            ok, err = impersonation.start(**base, consent_confirmed=False)
            self.assertFalse(ok)
            self.assertIn("机构同意", err)
            self.assertNotIn(platform_store.IMPERSONATION_KEY, state)

            ok, err = impersonation.start(
                **{**base, "reason": "自由填写的理由"},
                consent_confirmed=True,
            )
            self.assertFalse(ok)
            self.assertIn("查看理由", err)
            self.assertNotIn(platform_store.IMPERSONATION_KEY, state)

            ok, err = impersonation.start(
                **{**base, "consent_method": "口头说过"},
                consent_confirmed=True,
            )
            self.assertFalse(ok)
            self.assertIn("同意方式", err)
            self.assertNotIn(platform_store.IMPERSONATION_KEY, state)

    def test_start_sets_target_database_and_writes_safe_audit(self):
        state = self._start()
        saved = state[platform_store.IMPERSONATION_KEY]
        self.assertEqual(saved["teacher_slug"], self.teacher_slug)
        self.assertEqual(saved["org_id"], self.org_id)
        self.assertEqual(saved["db_path"] if "db_path" in saved else state["db_path"], str(self.target_db))
        self.assertEqual(state["db_slug"], self.teacher_slug)
        self.assertEqual(state[impersonation.PENDING_PAGE_KEY], "首页")
        self.assertEqual(state["page"], "平台概览")
        self.assertNotIn("学生", str(saved))
        self.assertNotIn("成绩", str(saved))

        started = datetime.fromisoformat(saved["started_at"])
        expires = datetime.fromisoformat(saved["expires_at"])
        self.assertEqual(
            int((expires - started).total_seconds()),
            impersonation.DURATION_MINUTES * 60,
        )
        records = platform_store.list_audit(
            org_id=self.org_id, actor_role="platform_admin"
        )
        self.assertEqual(records[0]["action"], "impersonate_start")
        self.assertEqual(records[0]["target_id"], self.teacher_slug)
        self.assertEqual(records[0]["reason"], "数据核对")
        self.assertIn("consent_method=微信文字确认", records[0]["detail"])
        self.assertNotIn("核对课程时间显示", records[0]["detail"])
        self.assertNotIn("学生", records[0]["detail"])

    def test_end_restores_admin_database_and_writes_end_audit(self):
        state = self._start()
        with patch.object(st, "session_state", state):
            impersonation.end("manual", rerun=False)
        self.assertNotIn(platform_store.IMPERSONATION_KEY, state)
        self.assertEqual(state["db_path"], str(self.admin_db))
        self.assertEqual(state["db_slug"], "platform_admin")
        self.assertEqual(state[impersonation.PENDING_PAGE_KEY], "平台概览")
        self.assertEqual(state["page"], "平台概览")
        records = platform_store.list_audit(
            org_id=self.org_id, actor_role="platform_admin"
        )
        self.assertEqual(
            [row["action"] for row in records[:2]],
            ["impersonate_end", "impersonate_start"],
        )

    def test_expired_impersonation_ends_as_timeout(self):
        state = self._start()
        saved = state[platform_store.IMPERSONATION_KEY]
        saved["expires_at"] = (
            datetime.now() - timedelta(seconds=1)
        ).isoformat()
        with patch.object(st, "session_state", state):
            active = impersonation.ensure_active(rerun=False)
        self.assertIsNone(active)
        self.assertNotIn(platform_store.IMPERSONATION_KEY, state)
        self.assertEqual(state["db_path"], str(self.admin_db))
        records = platform_store.list_audit(
            org_id=self.org_id, actor_role="platform_admin"
        )
        self.assertEqual(records[0]["action"], "impersonate_end")
        self.assertEqual(records[0]["reason"], "timeout")

    def test_connection_guard_blocks_writes_during_impersonation(self):
        state = self._state()
        state[platform_store.IMPERSONATION_KEY] = {
            "teacher_slug": self.teacher_slug,
            "org_id": self.org_id,
        }
        with patch.object(st, "session_state", state):
            conn = platform_store.guard_connection(
                sqlite3.connect(self.target_db), content_scope=True
            )
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM demo").fetchone()[0],
                    "before",
                )
                with self.assertRaises(PermissionError):
                    conn.execute("UPDATE demo SET value='after'")
            finally:
                conn.close()

    def test_platform_admin_without_impersonation_cannot_read_content(self):
        state = self._state()
        cm.configure(self.target_db)
        with patch.object(st, "session_state", state):
            with self.assertRaises(PermissionError):
                cm.query("SELECT value FROM demo")
            with self.assertRaises(PermissionError):
                cm.run("UPDATE demo SET value='after'")

    def test_normal_teacher_can_read_and_write_content(self):
        state = self._state()
        state["auth_actor_role"] = "teacher"
        state["db_path"] = str(self.target_db)
        state["db_slug"] = self.teacher_slug
        cm.configure(self.target_db)
        with patch.object(st, "session_state", state):
            self.assertEqual(cm.query("SELECT value FROM demo").iloc[0, 0], "before")
            cm.run("UPDATE demo SET value='after'")
            self.assertEqual(cm.query("SELECT value FROM demo").iloc[0, 0], "after")

    def test_impersonation_blocks_user_management_writes(self):
        state = self._start()
        um.configure(self.target_db)
        with patch.object(st, "session_state", state):
            with self.assertRaises(PermissionError):
                um.run("UPDATE demo SET value='after'")

    def test_cloud_database_upload_is_blocked_during_impersonation(self):
        state = self._start()
        with (
            patch.object(st, "session_state", state),
            patch.object(cloud_store, "is_cloud", return_value=True),
        ):
            with self.assertRaises(PermissionError):
                cloud_store.save_database(self.target_db, "database/test/scores.db")


if __name__ == "__main__":
    unittest.main()
