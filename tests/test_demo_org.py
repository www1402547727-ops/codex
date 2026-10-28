from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_store
import demo_org
import platform_store
import teacher_registry


def _test_root() -> Path:
    requested = Path(
        os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache")
    )
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="score_tracker_demo_test_"))


def _init_demo_schema(path: Path, object_path=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS students(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                class_name TEXT DEFAULT '', grade TEXT DEFAULT '',
                contact TEXT DEFAULT '', status TEXT DEFAULT '在读',
                note TEXT DEFAULT '', school TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS classes(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                grade TEXT DEFAULT '', subject TEXT DEFAULT '数学',
                total_hours REAL DEFAULT 0, note TEXT DEFAULT '',
                kind TEXT DEFAULT '班课'
            );
            CREATE TABLE IF NOT EXISTS class_members(
                class_id INTEGER, student_id INTEGER,
                UNIQUE(class_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS schedule(
                id INTEGER PRIMARY KEY, class_id INTEGER,
                weekday INTEGER, start TEXT, end TEXT
            );
            CREATE TABLE IF NOT EXISTS lessons(
                id INTEGER PRIMARY KEY, class_id INTEGER,
                lesson_date TEXT, start TEXT DEFAULT '', end TEXT DEFAULT '',
                hours REAL DEFAULT 0, content TEXT DEFAULT '',
                mastery TEXT DEFAULT '', homework TEXT DEFAULT '',
                note TEXT DEFAULT '', plan_id INTEGER, student_id INTEGER,
                target_type TEXT DEFAULT '', subject TEXT DEFAULT '',
                location TEXT DEFAULT '', location_id INTEGER,
                UNIQUE(class_id, lesson_date, start)
            );
            CREATE TABLE IF NOT EXISTS exams(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                exam_date TEXT, subject TEXT, full_score REAL DEFAULT 100,
                class_id INTEGER, category TEXT DEFAULT '',
                school TEXT DEFAULT '', grade TEXT DEFAULT '',
                paper TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS exam_categories(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                sort INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS scores(
                id INTEGER PRIMARY KEY, student_id INTEGER, exam_id INTEGER,
                score REAL, wrong_qs TEXT DEFAULT '',
                reasons TEXT DEFAULT '', knowledge TEXT DEFAULT '',
                paper TEXT DEFAULT '', sheet TEXT DEFAULT '',
                note TEXT DEFAULT '', status TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS plans(
                id INTEGER PRIMARY KEY, class_id INTEGER, plan_date TEXT,
                start TEXT, end TEXT, location TEXT DEFAULT '',
                status TEXT DEFAULT '待上课', note TEXT DEFAULT '',
                moved_from INTEGER, student_id INTEGER,
                target_type TEXT DEFAULT '', subject TEXT DEFAULT '',
                completed_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
                location_id INTEGER
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            "students": int(
                conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
            ),
            "plans": int(conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]),
            "exams": int(conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0]),
            "scores": int(conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]),
            "users": int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
        }
    finally:
        conn.close()


def _summary_db(path: Path, plans: int, exams: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE plans(id INTEGER PRIMARY KEY);
            CREATE TABLE exams(id INTEGER PRIMARY KEY);
            CREATE TABLE students(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE scores(id INTEGER PRIMARY KEY, score REAL);
            """
        )
        for index in range(plans):
            conn.execute("INSERT INTO plans(id) VALUES(?)", (index + 1,))
        for index in range(exams):
            conn.execute("INSERT INTO exams(id) VALUES(?)", (index + 1,))
        conn.execute(
            "INSERT INTO students(id, name) VALUES(1, '不能出现在摘要里的学生')"
        )
        conn.execute("INSERT INTO scores(id, score) VALUES(1, 99)")
        conn.commit()
    finally:
        conn.close()


class DemoOrganizationTests(unittest.TestCase):
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
        demo_org.configure(_init_demo_schema)
        ok, err = teacher_registry.add_teacher(
            "platform_admin",
            "平台管理员",
            "platform_admin",
            is_admin=True,
        )
        self.assertTrue(ok, err)

    def tearDown(self):
        self.case_tmp.cleanup()

    def test_reset_demo_creates_fake_data_and_random_credentials(self):
        with patch.object(
            demo_org.um,
            "generate_password",
            side_effect=[
                "teacher-pass-1",
                "student-pass-1",
                "teacher-pass-2",
                "student-pass-2",
            ],
        ):
            ok, err, first = demo_org.reset_demo("platform_admin")
            self.assertTrue(ok, err)
            self.assertEqual(first["teacher_password"], "teacher-pass-1")
            self.assertEqual(first["student_password"], "student-pass-1")

            org = demo_org.find_demo_org()
            self.assertIsNotNone(org)
            member = platform_store.org_user_for_slug("demo")
            self.assertEqual(member["org_id"], org["id"])
            self.assertEqual(member["role"], platform_store.ROLE_TEACHER)
            db = cloud_store.get_data_dir(self.teacher_root, "demo") / "scores.db"
            counts = _counts(db)
            self.assertGreaterEqual(counts["students"], 6)
            self.assertGreaterEqual(counts["plans"], 5)
            self.assertGreaterEqual(counts["exams"], 2)
            self.assertGreaterEqual(counts["scores"], 6)
            self.assertEqual(counts["users"], 2)

            ok, err, second = demo_org.reset_demo("platform_admin")
            self.assertTrue(ok, err)
            self.assertEqual(second["teacher_password"], "teacher-pass-2")
            self.assertEqual(second["student_password"], "student-pass-2")
            self.assertNotEqual(first["teacher_password"], second["teacher_password"])
            self.assertEqual(len(platform_store.list_organizations()), 1)
            self.assertEqual(_counts(db)["users"], 2)

    def test_reset_never_deletes_a_real_org_with_the_same_name(self):
        real_org, err = platform_store.create_organization("演示机构")
        self.assertIsNotNone(real_org, err)
        ok, member_err = platform_store.add_org_user(
            int(real_org), "real_teacher", platform_store.ROLE_TEACHER
        )
        self.assertTrue(ok, member_err)
        ok, teacher_err = teacher_registry.add_teacher(
            "real_teacher", "真实老师", "real_teacher"
        )
        self.assertTrue(ok, teacher_err)

        ok, message, credentials = demo_org.reset_demo("platform_admin")
        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertIsNone(credentials)
        self.assertIsNotNone(platform_store.get_organization(real_org))
        self.assertIsNotNone(teacher_registry.teacher_entry("real_teacher"))

    def test_usage_summary_hides_private_names_and_audits_export(self):
        public_org, err = platform_store.create_organization(
            "公开机构甲", public_name_ok=True
        )
        self.assertIsNotNone(public_org, err)
        private_org, err = platform_store.create_organization(
            "私密机构乙", public_name_ok=False
        )
        self.assertIsNotNone(private_org, err)
        for org_id, slug in (
            (public_org, "public_teacher"),
            (private_org, "private_teacher"),
        ):
            ok, member_err = platform_store.add_org_user(
                int(org_id), slug, platform_store.ROLE_TEACHER
            )
            self.assertTrue(ok, member_err)
            ok, teacher_err = teacher_registry.add_teacher(slug, slug, slug)
            self.assertTrue(ok, teacher_err)

        dbs = {
            "public_teacher": self.base / "public" / "scores.db",
            "private_teacher": self.base / "private" / "scores.db",
        }
        _summary_db(dbs["public_teacher"], plans=2, exams=1)
        _summary_db(dbs["private_teacher"], plans=3, exams=2)

        summary = demo_org.build_usage_summary(
            "platform_admin",
            db_resolver=lambda slug: dbs[slug],
        )
        self.assertEqual(summary["organization_count"], 2)
        self.assertEqual(summary["teacher_count"], 2)
        self.assertEqual(summary["course_count"], 5)
        self.assertEqual(summary["exam_count"], 3)
        csv_text = summary["csv"].decode("utf-8-sig")
        self.assertIn("公开机构甲", csv_text)
        self.assertNotIn("私密机构乙", csv_text)
        self.assertIn("其他未公开机构（合并统计）", csv_text)
        self.assertNotIn("不能出现在摘要里的学生", csv_text)
        self.assertNotIn("99", csv_text)

        exported = demo_org.export_usage_summary(
            "platform_admin",
            db_resolver=lambda slug: dbs[slug],
        )
        self.assertTrue(exported.startswith(b"\xef\xbb\xbf"))
        actions = [
            row["action"] for row in platform_store.list_audit(limit=100)
        ]
        self.assertIn("export", actions)

    def test_summary_sql_only_counts_operational_rows(self):
        sql = (
            demo_org._COUNT_COURSES_SQL + " " + demo_org._COUNT_EXAMS_SQL
        ).lower()
        for forbidden in (
            "student",
            "score",
            "wrong_qs",
            "knowledge",
            "paper",
            "sheet",
        ):
            self.assertNotIn(forbidden, sql)


if __name__ == "__main__":
    unittest.main()
