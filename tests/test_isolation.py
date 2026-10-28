from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import base64
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import streamlit as st

import cloud_store
import course_management as cm
import user_management as um


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXsAAAAASUVORK5CYII="
)


BASE_SCHEMA = """
CREATE TABLE students(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, class_name TEXT DEFAULT '',
    grade TEXT DEFAULT '', contact TEXT DEFAULT '', status TEXT DEFAULT '在读',
    note TEXT DEFAULT '', school TEXT DEFAULT ''
);
CREATE TABLE classes(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, grade TEXT DEFAULT '',
    subject TEXT DEFAULT '数学', total_hours REAL DEFAULT 0,
    note TEXT DEFAULT '', kind TEXT DEFAULT '班课'
);
CREATE TABLE class_members(class_id INTEGER, student_id INTEGER, UNIQUE(class_id, student_id));
CREATE TABLE schedule(id INTEGER PRIMARY KEY, class_id INTEGER, weekday INTEGER, start TEXT, end TEXT);
CREATE TABLE lessons(
    id INTEGER PRIMARY KEY, class_id INTEGER, lesson_date TEXT,
    start TEXT DEFAULT '', end TEXT DEFAULT '', hours REAL DEFAULT 0,
    content TEXT DEFAULT '', mastery TEXT DEFAULT '', homework TEXT DEFAULT '',
    note TEXT DEFAULT '', plan_id INTEGER, student_id INTEGER,
    target_type TEXT DEFAULT '', subject TEXT DEFAULT '', location TEXT DEFAULT '',
    location_id INTEGER
);
CREATE TABLE exams(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, exam_date TEXT, subject TEXT,
    full_score REAL DEFAULT 100, class_id INTEGER, category TEXT DEFAULT '',
    school TEXT DEFAULT '', grade TEXT DEFAULT '', paper TEXT DEFAULT ''
);
CREATE TABLE exam_categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort INTEGER DEFAULT 0);
CREATE TABLE scores(
    id INTEGER PRIMARY KEY, student_id INTEGER, exam_id INTEGER, score REAL,
    wrong_qs TEXT DEFAULT '', reasons TEXT DEFAULT '', knowledge TEXT DEFAULT '',
    paper TEXT DEFAULT '', sheet TEXT DEFAULT '', note TEXT DEFAULT '',
    status TEXT DEFAULT ''
);
CREATE TABLE plans(
    id INTEGER PRIMARY KEY, class_id INTEGER, plan_date TEXT, start TEXT, end TEXT,
    location TEXT DEFAULT '', status TEXT DEFAULT '待上课', note TEXT DEFAULT '',
    moved_from INTEGER, student_id INTEGER, target_type TEXT DEFAULT '',
    subject TEXT DEFAULT '', completed_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
    location_id INTEGER
);
"""


def _test_root() -> Path:
    requested = Path(os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache"))
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="score_tracker_test_"))


def _new_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(BASE_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    cm.init_course_db(path, object_path=cloud_store.db_object_path("test"))
    um.init_user_db(
        path,
        default_teacher_password="test-password",
        object_path=cloud_store.db_object_path("test"),
    )


def _seed_teacher(path: Path, suffix: str, course_id: int) -> None:
    """写入完全虚构的老师和学生数据。"""
    photos = path.parent / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / f"{suffix}_exam_paper.jpg").write_bytes(TINY_PNG)
    (photos / f"{suffix}_sheet.jpg").write_bytes(TINY_PNG)

    conn = sqlite3.connect(path)
    try:
        teacher_name = f"teacher_{suffix}"
        student_name = f"假学生{suffix}"
        conn.execute(
            "UPDATE users SET username=? WHERE role='teacher'",
            (teacher_name,),
        )
        conn.execute(
            """INSERT INTO students(name, grade, school, status)
               VALUES(?,?,?,?)""",
            (student_name, "测试年级", "测试学校", "在读"),
        )
        student_id = int(conn.execute("SELECT id FROM students").fetchone()[0])
        conn.execute(
            """INSERT INTO classes(name, grade, subject, kind)
               VALUES(?,?,?,?)""",
            (f"测试班{suffix}", "测试年级", "数学", "班课"),
        )
        class_id = int(conn.execute("SELECT id FROM classes").fetchone()[0])
        conn.execute("INSERT INTO class_members(class_id, student_id) VALUES(?,?)", (class_id, student_id))
        conn.execute(
            """INSERT INTO one_to_one_students(name, grade, subject, total_hours, status, student_id)
               VALUES(?,?,?,?,?,?)""",
            (student_name, "测试年级", "数学", 10, "在读", student_id),
        )
        one_id = int(conn.execute("SELECT id FROM one_to_one_students").fetchone()[0])
        conn.execute(
            """INSERT INTO teaching_locations(name, active, created_at)
               VALUES(?,1,'')""",
            (f"测试地点{suffix}",),
        )
        location_id = int(conn.execute("SELECT id FROM teaching_locations").fetchone()[0])
        conn.execute(
            """INSERT INTO plans(
                   id, class_id, student_id, target_type, plan_date, start, end,
                   subject, location, location_id, status, note
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                course_id, None, one_id, "一对一", "2030-01-02",
                "10:00", "11:00", "数学", f"测试地点{suffix}", location_id,
                "待上课", "测试课程",
            ),
        )
        conn.execute(
            """INSERT INTO exams(
                   id, name, exam_date, subject, full_score, school, grade, paper
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                course_id, f"测试考试{suffix}", "2030-01-03", "数学",
                100, "测试学校", "测试年级", f"{suffix}_exam_paper.jpg",
            ),
        )
        conn.execute(
            """INSERT INTO scores(
                   student_id, exam_id, score, status, paper, sheet, note
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                student_id, course_id, 88, "已录入", "",
                f"{suffix}_sheet.jpg", f"测试分析{suffix}",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    um.create_account_in(
        path,
        f"student_{suffix}",
        "student-password",
        um.ROLE_STUDENT,
        student_id=student_id,
        must_change_password=False,
    )


@contextmanager
def session(db_path: Path, slug: str):
    state = {"db_path": str(db_path), "db_slug": slug}
    with patch.object(st, "session_state", state):
        yield state


class IsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()
        cls.tmp = tempfile.TemporaryDirectory(dir=str(cls.root))
        cls.base = Path(cls.tmp.name)
        cls.db_a = cls.base / "teacher_a" / "scores.db"
        cls.db_b = cls.base / "teacher_b" / "scores.db"
        _new_db(cls.db_a)
        _new_db(cls.db_b)
        _seed_teacher(cls.db_a, "a", 1)
        _seed_teacher(cls.db_b, "b", 2)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_session_paths_do_not_follow_module_globals(self):
        # 模拟另一个会话把模块全局配置成 B，再切回 A，仍必须读取 A。
        cm.configure(self.db_b)
        um.configure(self.db_b)
        with session(self.db_b, "b"):
            cloud_store.set_session_slug("b")
        with session(self.db_a, "a"):
            self.assertEqual(um._db_path(), self.db_a)
            self.assertEqual(cm._db_path(), self.db_a)
            self.assertEqual(cloud_store.current_slug(), "a")
            self.assertEqual(cloud_store.session_db_path(self.db_b), self.db_a)
            self.assertEqual(
                cloud_store.session_photos_dir(self.db_b),
                self.db_a.parent / "photos",
            )
        with session(self.db_b, "b"):
            self.assertEqual(um._db_path(), self.db_b)
            self.assertEqual(cm._db_path(), self.db_b)
            self.assertEqual(cloud_store.current_slug(), "b")
            self.assertEqual(cloud_store.session_db_path(self.db_a), self.db_b)
            self.assertEqual(
                cloud_store.session_photos_dir(self.db_a),
                self.db_b.parent / "photos",
            )
        with session(self.db_a, "a"):
            self.assertEqual(um._db_path(), self.db_a)
            self.assertEqual(cm._db_path(), self.db_a)
            self.assertEqual(cloud_store.current_slug(), "a")

    def test_teacher_queries_only_read_current_database(self):
        with session(self.db_a, "a"):
            courses = cm._courses_between("2030-01-01", "2030-01-31")
            self.assertEqual(len(courses), 1)
            self.assertEqual(int(courses.iloc[0]["id"]), 1)
            self.assertEqual(str(courses.iloc[0]["target_name"]), "假学生a")
            self.assertIsNone(cm._course_by_id(2))
            self.assertEqual(len(cm.one_to_one_summary()), 1)
            self.assertEqual(str(cm.one_to_one_summary().iloc[0]["name"]), "假学生a")
            self.assertIsNone(um._get_user_by_username("student_b"))
            self.assertIsNone(um.authenticate("student_b", "student-password")[0])

        with session(self.db_b, "b"):
            courses = cm._courses_between("2030-01-01", "2030-01-31")
            self.assertEqual(len(courses), 1)
            self.assertEqual(int(courses.iloc[0]["id"]), 2)
            self.assertEqual(str(courses.iloc[0]["target_name"]), "假学生b")
            self.assertIsNone(cm._course_by_id(1))

    def test_direct_foreign_ids_are_rejected(self):
        with session(self.db_a, "a"):
            self.assertFalse(cm.complete_course(2))
            cm.delete_course(2)
            self.assertIsNone(cm._course_by_id(2))
            self.assertEqual(len(cm.one_to_one_summary()), 1)
        with session(self.db_b, "b"):
            self.assertIsNotNone(cm._course_by_id(2))
            self.assertEqual(str(cm._course_by_id(2)["status"]), "待上课")

    def test_student_queries_are_scoped_to_own_student(self):
        with session(self.db_a, "a"):
            courses = um._student_courses(1)
            self.assertEqual(len(courses), 1)
            self.assertEqual(int(courses.iloc[0]["id"]), 1)
            scores = um._student_score_rows(1)
            self.assertEqual(len(scores), 1)
            self.assertEqual(str(scores.iloc[0]["exam_name"]), "测试考试a")
            self.assertFalse(um._student_score_rows(999).shape[0])
        with session(self.db_b, "b"):
            scores = um._student_score_rows(1)
            self.assertEqual(str(scores.iloc[0]["exam_name"]), "测试考试b")

    def test_student_media_requires_ownership(self):
        with session(self.db_a, "a"):
            self.assertTrue(um._student_media_allowed(1, 1, "a_sheet.jpg"))
            self.assertFalse(um._student_media_allowed(1, 1, "b_sheet.jpg"))
            self.assertTrue(
                um._student_media_allowed(1, 1, "a_exam_paper.jpg", shared=True)
            )
            self.assertFalse(
                um._student_media_allowed(1, 1, "b_exam_paper.jpg", shared=True)
            )
            self.assertFalse(um._student_media_allowed(1, 1, "../b_sheet.jpg"))

    def test_media_filename_is_basename_only(self):
        for bad in ("../a_sheet.jpg", r"..\\a_sheet.jpg", "sub/a_sheet.jpg", "a|b.jpg", ""):
            with self.assertRaises(ValueError):
                cloud_store.safe_media_name(bad)
            with self.assertRaises(ValueError):
                cloud_store.photo_object_path(bad, "a")
            with self.assertRaises(ValueError):
                cloud_store.ensure_local_photo(bad, self.db_a.parent / "photos", "a")
        self.assertEqual(cloud_store.safe_media_name("a_sheet.jpg"), "a_sheet.jpg")
        self.assertEqual(
            cloud_store.photo_object_path("a_sheet.jpg", "a"),
            "photos/a/a_sheet.jpg",
        )

    def test_disabled_account_cannot_authenticate_or_remain_current(self):
        conn = sqlite3.connect(self.db_a)
        try:
            conn.execute(
                "UPDATE users SET active=0 WHERE username='student_a'"
            )
            user_id = int(
                conn.execute(
                    "SELECT id FROM users WHERE username='student_a'"
                ).fetchone()[0]
            )
            conn.commit()
        finally:
            conn.close()
        with session(self.db_a, "a"):
            user, error = um.authenticate("student_a", "student-password")
            self.assertIsNone(user)
            self.assertIn("停用", error)
            state = st.session_state
            state["auth_user_id"] = user_id
            with patch.object(st, "rerun", lambda: None):
                self.assertIsNone(um.current_user())
            self.assertNotIn("auth_user_id", state)


if __name__ == "__main__":
    unittest.main()
