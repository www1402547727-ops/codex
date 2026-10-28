from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import admin_console
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


if __name__ == "__main__":
    unittest.main()
