from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import admin_console
import cloud_store
import platform_store
import teacher_registry
import user_management as um
import streamlit as st


def _test_root() -> Path:
    requested = Path(
        os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache")
    )
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="score_tracker_platform_test_"))


def _make_user_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE one_to_one_students(
                   id INTEGER PRIMARY KEY, name TEXT, grade TEXT
               )"""
        )
        conn.commit()
    finally:
        conn.close()


def _init_fake_schema(path: Path, object_path=None) -> None:
    """只建课程初始化需要的最小表；内容全部是空表。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS students(
                id INTEGER PRIMARY KEY, name TEXT, grade TEXT, school TEXT,
                status TEXT DEFAULT '在读'
            );
            CREATE TABLE IF NOT EXISTS classes(
                id INTEGER PRIMARY KEY, name TEXT UNIQUE, grade TEXT,
                subject TEXT, total_hours REAL DEFAULT 0,
                note TEXT, kind TEXT DEFAULT '班课'
            );
            CREATE TABLE IF NOT EXISTS one_to_one_students(
                id INTEGER PRIMARY KEY, name TEXT, grade TEXT, subject TEXT,
                total_hours REAL DEFAULT 0, status TEXT DEFAULT '在读',
                contact TEXT DEFAULT '', note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS class_members(
                class_id INTEGER, student_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS plans(
                id INTEGER PRIMARY KEY, class_id INTEGER, plan_date TEXT,
                start TEXT, end TEXT, location TEXT, status TEXT, note TEXT,
                student_id INTEGER, target_type TEXT, subject TEXT,
                completed_at TEXT, updated_at TEXT, location_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS lessons(
                id INTEGER PRIMARY KEY, class_id INTEGER, lesson_date TEXT,
                start TEXT, end TEXT, hours REAL, content TEXT, mastery TEXT,
                homework TEXT, note TEXT, plan_id INTEGER, student_id INTEGER,
                target_type TEXT, subject TEXT, location TEXT, location_id INTEGER
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _init_overview_db(
    path: Path,
    *,
    students: int,
    plan_dates: list[str],
    exams: int,
    teacher_last_login: str,
    student_last_login: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE students(
                id INTEGER PRIMARY KEY, name TEXT, grade TEXT, school TEXT
            );
            CREATE TABLE class_members(class_id INTEGER, student_id INTEGER);
            CREATE TABLE one_to_one_students(
                id INTEGER PRIMARY KEY, name TEXT, student_id INTEGER
            );
            CREATE TABLE plans(
                id INTEGER PRIMARY KEY, plan_date TEXT, status TEXT
            );
            CREATE TABLE exams(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE users(
                id INTEGER PRIMARY KEY, username TEXT, role TEXT,
                last_login TEXT
            );
            """
        )
        for index in range(students):
            conn.execute(
                "INSERT INTO students(id, name, grade, school) VALUES(?,?,?,?)",
                (index + 1, f"假学生{index + 1}", "初三", "测试中学"),
            )
        if students:
            # 同一个学生同时出现在班课关系和一对一关系中，学生数仍只能算 1。
            conn.execute(
                "INSERT INTO class_members(class_id, student_id) VALUES(?,?)",
                (1, 1),
            )
            conn.execute(
                """INSERT INTO one_to_one_students(name, student_id)
                   VALUES(?,?)""",
                ("假学生1", 1),
            )
        for index, plan_date in enumerate(plan_dates):
            conn.execute(
                "INSERT INTO plans(id, plan_date, status) VALUES(?,?,?)",
                (index + 1, plan_date, "待上课"),
            )
        for index in range(exams):
            conn.execute(
                "INSERT INTO exams(id, name) VALUES(?,?)",
                (index + 1, f"测试考试{index + 1}"),
            )
        conn.execute(
            "INSERT INTO users(username, role, last_login) VALUES(?,?,?)",
            ("teacher_test", "teacher", teacher_last_login),
        )
        if student_last_login:
            conn.execute(
                "INSERT INTO users(username, role, last_login) VALUES(?,?,?)",
                ("student_test", "student", student_last_login),
            )
        conn.commit()
    finally:
        conn.close()


class PlatformStoreTests(unittest.TestCase):
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

    def tearDown(self):
        self.case_tmp.cleanup()

    def test_default_org_and_teacher_members_are_idempotent(self):
        teachers = [
            {"slug": "teacher_a"},
            {"slug": "teacher_b"},
            {"slug": "teacher_a"},
        ]
        first = platform_store.ensure_ready(teachers)
        second = platform_store.ensure_ready(teachers)

        self.assertEqual(first, second)
        self.assertEqual(len(platform_store.list_organizations()), 1)
        members = platform_store.list_org_users(first)
        self.assertEqual(len(members), 2)
        self.assertEqual(
            {member["slug"] for member in members},
            {"teacher_a", "teacher_b"},
        )

    def test_user_org_column_and_account_creation(self):
        org_id = platform_store.ensure_ready([])
        db = self.base / "user_db" / "scores.db"
        _make_user_db(db)
        um.init_user_db(
            db,
            default_teacher_password="test-password",
            object_path=cloud_store.db_object_path("user_db"),
            org_id=org_id,
        )

        conn = sqlite3.connect(db)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            self.assertIn("org_id", columns)
            teacher_org = conn.execute(
                "SELECT org_id FROM users WHERE role='teacher'"
            ).fetchone()[0]
            self.assertEqual(int(teacher_org), int(org_id))
        finally:
            conn.close()

        ok, err = um.create_account_in(
            db,
            "student_test",
            "student-password",
            um.ROLE_STUDENT,
            org_id=org_id,
        )
        self.assertTrue(ok, err)
        conn = sqlite3.connect(db)
        try:
            student_org = conn.execute(
                "SELECT org_id FROM users WHERE username='student_test'"
            ).fetchone()[0]
            self.assertEqual(int(student_org), int(org_id))
        finally:
            conn.close()

    def test_existing_user_db_gets_org_id_without_losing_rows(self):
        org_id = platform_store.ensure_ready([])
        db = self.base / "legacy_user_db" / "scores.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                """
                CREATE TABLE one_to_one_students(
                    id INTEGER PRIMARY KEY, name TEXT, grade TEXT
                );
                CREATE TABLE users(
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher','student')),
                    student_id INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT '',
                    last_login TEXT DEFAULT '',
                    note TEXT DEFAULT ''
                );
                """
            )
            conn.execute(
                """INSERT INTO users(
                       username, password_hash, role, active,
                       must_change_password, created_at, note
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    "legacy_teacher",
                    um.hash_password("legacy-password"),
                    um.ROLE_TEACHER,
                    1,
                    0,
                    "2026-01-01 00:00:00",
                    "旧账号",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        um.init_user_db(
            db,
            default_teacher_password=None,
            object_path=cloud_store.db_object_path("legacy_user_db"),
            org_id=org_id,
        )
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT username, org_id FROM users WHERE username='legacy_teacher'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row[1]), int(org_id))
        finally:
            conn.close()

    def test_org_boundary_and_org_admin_role(self):
        org_a, err_a = platform_store.create_organization("测试机构甲")
        org_b, err_b = platform_store.create_organization("测试机构乙")
        self.assertIsNotNone(org_a, err_a)
        self.assertIsNotNone(org_b, err_b)
        self.assertTrue(platform_store.add_org_user(org_a, "org_a_teacher", "org_admin")[0])
        self.assertTrue(platform_store.add_org_user(org_b, "org_b_teacher", "teacher")[0])

        self.assertEqual(platform_store.org_id_for_slug("org_a_teacher"), org_a)
        self.assertTrue(platform_store.is_org_admin("org_a_teacher", org_a))
        self.assertFalse(platform_store.is_org_admin("org_b_teacher", org_a))
        ok, _ = platform_store.add_org_user(org_b, "org_a_teacher", "teacher")
        self.assertFalse(ok)

    def test_create_organization_and_admin_account(self):
        admin_console.configure(_init_fake_schema)
        ok, err, org_id = admin_console.create_organization_with_admin(
            "测试机构戊",
            "机构",
            "org_admin_e",
            "测试管理员",
            "test-password",
        )
        self.assertTrue(ok, err)
        self.assertIsNotNone(org_id)
        member = platform_store.org_user_for_slug("org_admin_e")
        self.assertIsNotNone(member)
        self.assertEqual(member["role"], platform_store.ROLE_ORG_ADMIN)
        db = (
            cloud_store.get_data_dir(self.teacher_root, "org_admin_e")
            / "scores.db"
        )
        self.assertTrue(db.is_file())
        conn = sqlite3.connect(db)
        try:
            user_org = conn.execute(
                "SELECT org_id FROM users WHERE role='teacher'"
            ).fetchone()[0]
            self.assertEqual(int(user_org), int(org_id))
        finally:
            conn.close()
        actions = {row["action"] for row in platform_store.list_audit(org_id=org_id)}
        self.assertIn("create_organization", actions)
        self.assertIn("create_teacher_account", actions)

    def test_audit_logs_are_scoped_and_do_not_contain_content(self):
        org_a, _ = platform_store.create_organization("测试机构审计甲")
        org_b, _ = platform_store.create_organization("测试机构审计乙")
        platform_store.record_audit(
            "login_success",
            actor_user_id=101,
            actor_role="platform_admin",
            org_id=org_a,
            target_type="account",
            target_id="test_account",
            detail="",
        )
        platform_store.record_audit(
            "delete_score",
            actor_user_id=101,
            actor_role="platform_admin",
            org_id=org_b,
            target_type="score",
            target_id=42,
            detail="exam_id=7;student_id=9",
        )

        rows_a = platform_store.list_audit(org_id=org_a)
        rows_b = platform_store.list_audit(org_id=org_b)
        self.assertEqual([row["action"] for row in rows_a], ["login_success"])
        self.assertEqual([row["action"] for row in rows_b], ["delete_score"])
        self.assertNotIn("假学生", str(rows_b))
        self.assertNotIn("88", str(rows_b))

    def test_current_audit_can_be_recorded_for_target_org(self):
        org_a, _ = platform_store.create_organization("测试机构目标甲")
        org_b, _ = platform_store.create_organization("测试机构目标乙")
        state = {
            "auth_user_id": 7,
            "auth_actor_role": "platform_admin",
            "auth_org_id": org_a,
            "db_slug": "test_platform_admin",
        }
        with patch.object(st, "session_state", state):
            platform_store.record_current_audit(
                "delete_student",
                target_type="student",
                target_id=12,
                org_id=org_b,
            )

        rows_a = platform_store.list_audit(org_id=org_a)
        rows_b = platform_store.list_audit(org_id=org_b)
        self.assertEqual(rows_a, [])
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["actor_role"], "platform_admin")
        self.assertEqual(rows_b[0]["action"], "delete_student")

    def test_failed_login_is_audited_without_content(self):
        org_id = platform_store.ensure_ready([])
        db = self.base / "login_test" / "scores.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        _init_fake_schema(db)
        um.init_user_db(
            db,
            default_teacher_password="test-password",
            object_path=cloud_store.db_object_path("login_test"),
            org_id=org_id,
        )
        um.configure(db)

        user, error = um.authenticate("teacher", "wrong-password")
        self.assertIsNone(user)
        self.assertTrue(error)
        rows = platform_store.list_audit(org_id=org_id, actor_role="anonymous")
        self.assertTrue(any(row["action"] == "login_failed" for row in rows))
        self.assertTrue(any(row["detail"] == "invalid_credentials" for row in rows))
        self.assertNotIn("wrong-password", str(rows))

    def test_org_admin_cannot_disable_another_org_teacher(self):
        org_a, _ = platform_store.create_organization("测试机构丙")
        org_b, _ = platform_store.create_organization("测试机构丁")
        platform_store.add_org_user(org_a, "admin_c", "org_admin")
        platform_store.add_org_user(org_b, "teacher_d", "teacher")
        teacher_registry.add_teacher("admin_c", "", "admin_c")
        teacher_registry.add_teacher("teacher_d", "", "teacher_d")

        ok, _ = admin_console.org_set_teacher_active(
            org_a, "admin_c", "teacher_d", False
        )
        self.assertFalse(ok)
        ok, _ = admin_console.org_set_teacher_active(
            org_a, "admin_c", "admin_c", False
        )
        self.assertFalse(ok)

    def test_platform_overview_counts_are_scoped_and_contain_no_content(self):
        org_a, err_a = platform_store.create_organization("概览机构甲")
        org_b, err_b = platform_store.create_organization("概览机构乙")
        self.assertIsNotNone(org_a, err_a)
        self.assertIsNotNone(org_b, err_b)
        platform_store.add_org_user(org_a, "overview_a", "teacher")
        platform_store.add_org_user(org_b, "overview_b", "teacher")

        today = date(2026, 10, 28)
        db_a = self.base / "overview_dbs" / "overview_a" / "scores.db"
        db_b = self.base / "overview_dbs" / "overview_b" / "scores.db"
        _init_overview_db(
            db_a,
            students=1,
            plan_dates=[
                "2026-09-29",
                "2026-10-28",
                "2026-09-28",
                "2026-10-29",
            ],
            exams=3,
            teacher_last_login="2026-10-27 10:00:00",
            student_last_login="2026-10-28 23:59:00",
        )
        _init_overview_db(
            db_b,
            students=2,
            plan_dates=["2026-10-18"],
            exams=1,
            teacher_last_login="2026-10-28 09:00:00",
        )

        dbs = {
            "overview_a": db_a,
            "overview_b": db_b,
        }
        media = {
            "overview_a": {"count": 2, "bytes": 2048},
            "overview_b": {"count": 1, "bytes": 1024},
        }
        rows = admin_console.build_platform_overview(
            db_resolver=lambda slug: dbs[slug],
            media_resolver=lambda slug: media[slug],
            today=today,
        )
        by_name = {row["name"]: row for row in rows}

        row_a = by_name["概览机构甲"]
        self.assertEqual(row_a["teacher_count"], 1)
        self.assertEqual(row_a["student_count"], 1)
        self.assertEqual(row_a["course_count_30d"], 2)
        self.assertEqual(row_a["exam_count"], 3)
        self.assertEqual(row_a["last_login"], "2026-10-27 10:00:00")
        self.assertEqual(row_a["media_count"], 2)
        self.assertEqual(row_a["media_bytes"], 2048)
        self.assertFalse(row_a["read_error"])
        self.assertFalse(row_a["media_error"])

        row_b = by_name["概览机构乙"]
        self.assertEqual(row_b["student_count"], 2)
        self.assertEqual(row_b["course_count_30d"], 1)
        self.assertEqual(row_b["exam_count"], 1)
        self.assertEqual(row_b["last_login"], "2026-10-28 09:00:00")

        rendered = str(rows)
        self.assertNotIn("假学生", rendered)
        self.assertNotIn("测试考试", rendered)
        self.assertNotIn("分数", rendered)

    def test_platform_overview_survives_media_storage_failure(self):
        org_id, err = platform_store.create_organization("概览存储失败机构")
        self.assertIsNotNone(org_id, err)
        platform_store.add_org_user(org_id, "overview_media_fail", "teacher")
        db = self.base / "overview_media_fail" / "scores.db"
        _init_overview_db(
            db,
            students=1,
            plan_dates=["2026-10-28"],
            exams=1,
            teacher_last_login="2026-10-28 08:00:00",
        )

        def broken_media(_slug):
            raise RuntimeError("模拟云端目录读取失败")

        rows = admin_console.build_platform_overview(
            db_resolver=lambda _slug: db,
            media_resolver=broken_media,
            today=date(2026, 10, 28),
        )
        row = next(item for item in rows if item["name"] == "概览存储失败机构")
        self.assertEqual(row["student_count"], 1)
        self.assertEqual(row["course_count_30d"], 1)
        self.assertIsNone(row["media_count"])
        self.assertTrue(row["media_error"])

    def test_platform_overview_sql_only_reads_operational_counts(self):
        sql = " ".join(
            [
                admin_console._OVERVIEW_STUDENTS_SQL,
                admin_console._OVERVIEW_PLANS_SQL,
                admin_console._OVERVIEW_EXAMS_SQL,
                admin_console._OVERVIEW_LAST_LOGIN_SQL,
            ]
        ).lower()
        for forbidden in (
            "s.name",
            "student_name",
            "score",
            "wrong_qs",
            "knowledge",
            "paper",
            "sheet",
            "reasons",
        ):
            self.assertNotIn(forbidden, sql)

    def test_platform_overview_requires_platform_admin(self):
        teacher_registry.add_teacher("plain_teacher", "", "plain_teacher")
        with (
            patch.object(
                admin_console.um,
                "current_user",
                return_value={"id": 1, "role": um.ROLE_TEACHER},
            ),
            patch.object(cloud_store, "current_slug", return_value="plain_teacher"),
            patch.object(
                teacher_registry,
                "teacher_entry",
                return_value={"slug": "plain_teacher", "is_admin": False},
            ),
            patch.object(
                admin_console.st, "stop", side_effect=RuntimeError("stopped")
            ),
        ):
            with self.assertRaises(RuntimeError):
                admin_console._require_admin("平台概览")

        with (
            patch.object(
                admin_console.um,
                "current_user",
                return_value={"id": 1, "role": um.ROLE_TEACHER},
            ),
            patch.object(cloud_store, "current_slug", return_value="admin_slug"),
            patch.object(
                teacher_registry,
                "teacher_entry",
                return_value={"slug": "admin_slug", "is_admin": True},
            ),
        ):
            self.assertEqual(
                admin_console._require_admin("平台概览"), "admin_slug"
            )

        with (
            patch.object(
                admin_console.um,
                "current_user",
                return_value={"id": 2, "role": um.ROLE_STUDENT},
            ),
            patch.object(cloud_store, "current_slug", return_value="admin_slug"),
            patch.object(
                teacher_registry,
                "teacher_entry",
                return_value={"slug": "admin_slug", "is_admin": True},
            ),
            patch.object(
                admin_console.st, "stop", side_effect=RuntimeError("stopped")
            ),
        ):
            with self.assertRaises(RuntimeError):
                admin_console._require_admin("平台概览")

    def test_system_errors_are_readable_without_student_content(self):
        platform_store.ensure_ready([])
        platform_store.record_system_error(
            "平台概览", "TestError", "读取云端目录失败"
        )
        rows = platform_store.list_system_errors(20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["module"], "平台概览")
        self.assertEqual(rows[0]["error_type"], "TestError")
        self.assertEqual(rows[0]["detail"], "读取云端目录失败")

    def test_cloud_storage_stats_only_count_files_and_sizes(self):
        response = Mock(ok=True, status_code=200, text="")
        response.json.return_value = [
            {"name": "one.jpg", "id": "1", "metadata": {"size": 100}},
            {
                "name": "two.pdf",
                "id": "2",
                "metadata": {"contentLength": 200},
            },
            {"name": "subfolder", "id": None, "metadata": None},
        ]
        config = {
            "url": "https://example.supabase.co",
            "key": "test-key",
            "bucket": "student-files",
        }
        with (
            patch.object(cloud_store, "_config", config),
            patch.object(cloud_store.requests, "post", return_value=response) as post,
        ):
            stats = cloud_store.storage_prefix_stats("photos/test")

        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["bytes"], 300)
        self.assertFalse(stats["truncated"])
        post.assert_called_once()

    def test_latest_backup_is_empty_when_no_backup_exists(self):
        with patch.object(cloud_store, "is_cloud", return_value=False):
            self.assertEqual(admin_console.latest_backup_at(), "")


if __name__ == "__main__":
    unittest.main()
