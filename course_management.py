from __future__ import annotations

import calendar
import html
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import cloud_store


DB_PATH = Path(__file__).parent / "data" / "scores.db"
DAY_START, DAY_END = "08:00", "20:30"
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
TARGET_CLASS = "班级"
TARGET_ONE = "一对一"
TYPE_LABELS = {TARGET_CLASS: "班级课", TARGET_ONE: "一对一"}
LABEL_TYPES = {v: k for k, v in TYPE_LABELS.items()}
STATUS_PENDING = "待上课"
STATUS_DONE = "已上课"
STATUS_LEAVE = "请假"
STATUS_CANCEL = "取消"
STATUS_MOVED = "调课"
STATUS_OPTIONS = [STATUS_PENDING, STATUS_DONE, STATUS_LEAVE, STATUS_CANCEL]
STATUS_ICON = {STATUS_PENDING: "🔵", STATUS_DONE: "✅", STATUS_LEAVE: "🟡", STATUS_CANCEL: "⚫", STATUS_MOVED: "↪️"}
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_DONE)
DISPLAY_STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_LEAVE, STATUS_CANCEL)


MOBILE_TAB_ALIAS = {"课程表": "课表", "教学对象": "教学"}


def goto_page(name: str) -> None:
    """统一切换页面，同时兼容手机底部标签栏和电脑侧边栏（只能在 on_click 回调里调用）。"""
    st.session_state["mobile_extra"] = None
    st.session_state["page"] = name
    st.session_state["mobile_tab"] = MOBILE_TAB_ALIAS.get(name, name)


def is_mobile() -> bool:
    """判断是否用手机/平板访问。电脑上想预览手机版，可在网址后面加 ?m=1。"""
    try:
        flag = str(st.query_params.get("m", "")).strip().lower()
        if flag in ("1", "true", "yes", "mobile"):
            return True
    except Exception:
        pass
    try:
        ua = str(st.context.headers.get("User-Agent", "") or "")
    except Exception:
        ua = ""
    return bool(re.search(r"iPhone|iPod|iPad|Android|Windows Phone|Mobile", ua, re.I))


class CourseValidationError(ValueError):
    pass


def configure(db_path: str | Path) -> None:
    global DB_PATH
    DB_PATH = Path(db_path)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _clean(params):
    return [p.item() if hasattr(p, "item") else p for p in params]


def run(sql: str, params=()) -> int:
    conn = _connect()
    try:
        cur = conn.execute(sql, _clean(params))
        conn.commit()
        cloud_store.save_database(DB_PATH)
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def query(sql: str, params=()) -> pd.DataFrame:
    conn = _connect()
    try:
        return pd.read_sql_query(sql, conn, params=_clean(params))
    finally:
        conn.close()


def _backfill_one_to_one_students(conn: sqlite3.Connection) -> int:
    """给还没有学生档案的一对一学生，补建/关联 students 记录（幂等，不覆盖已有数据）。"""
    n = 0
    try:
        rows = conn.execute(
            "SELECT id, name, grade FROM one_to_one_students WHERE student_id IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    for oid, name, grade in rows:
        sid = conn.execute(
            "SELECT id FROM students WHERE TRIM(name)=? ORDER BY id LIMIT 1", (name,)
        ).fetchone()
        if sid:
            sid = int(sid[0])
        else:
            cur = conn.execute("INSERT INTO students(name, grade, school) VALUES(?,?,?)", (name, grade or "", ""))
            sid = int(cur.lastrowid)
        conn.execute("UPDATE one_to_one_students SET student_id=? WHERE id=?", (sid, int(oid)))
        n += 1
    return n


def init_course_db(db_path: str | Path | None = None) -> None:
    if db_path is not None:
        configure(db_path)
    conn = _connect()
    try:
        if _backfill_one_to_one_students(conn):
            conn.commit()
            cloud_store.save_database(DB_PATH)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        view_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='v_lessons'").fetchone()
        hour_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hour_transactions'"
        ).fetchone()
        location_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='teaching_locations'"
        ).fetchone()
        settlement_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='location_settlements'"
        ).fetchone()
        if version >= 5 and view_exists and hour_table_exists and location_table_exists and settlement_table_exists:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS one_to_one_students(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                grade TEXT DEFAULT '',
                subject TEXT DEFAULT '',
                total_hours REAL DEFAULT 0,
                status TEXT DEFAULT '在读',
                contact TEXT DEFAULT '',
                note TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS legacy_one_to_one_map(
                legacy_class_id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hour_transactions(
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL,
                transaction_type TEXT NOT NULL CHECK(transaction_type IN ('renewal','adjustment')),
                hours_delta REAL NOT NULL,
                occurred_at TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                created_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_hour_transactions_student
                ON hour_transactions(student_id, occurred_at, id);
            CREATE TABLE IF NOT EXISTS teaching_locations(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS location_settlements(
                id INTEGER PRIMARY KEY,
                settlement_month TEXT NOT NULL,
                location_id INTEGER NOT NULL,
                class_count INTEGER NOT NULL DEFAULT 0,
                one_count INTEGER NOT NULL DEFAULT 0,
                settled_at TEXT NOT NULL,
                note TEXT DEFAULT '',
                UNIQUE(settlement_month, location_id)
            );
            CREATE INDEX IF NOT EXISTS idx_location_settlements_month
                ON location_settlements(settlement_month, location_id);
            """
        )
        migrations = {
            "plans": {
                "student_id": "INTEGER",
                "target_type": "TEXT DEFAULT ''",
                "subject": "TEXT DEFAULT ''",
                "completed_at": "TEXT DEFAULT ''",
                "updated_at": "TEXT DEFAULT ''",
                "location_id": "INTEGER",
            },
            "lessons": {
                "student_id": "INTEGER",
                "target_type": "TEXT DEFAULT ''",
                "subject": "TEXT DEFAULT ''",
                "location": "TEXT DEFAULT ''",
                "location_id": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, column_type in columns.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        # Convert existing free-text locations into managed location options.
        for source_table in ("plans", "lessons"):
            locations = conn.execute(
                f"""SELECT DISTINCT TRIM(location) AS location FROM {source_table}
                    WHERE COALESCE(TRIM(location),'')!=''"""
            ).fetchall()
            for location_row in locations:
                name = str(location_row["location"]).strip()
                if name:
                    conn.execute(
                        "INSERT OR IGNORE INTO teaching_locations(name, active, created_at) VALUES(?,1,?)",
                        (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
            conn.execute(
                f"""UPDATE {source_table}
                    SET location_id=(SELECT id FROM teaching_locations
                                     WHERE name=TRIM({source_table}.location) LIMIT 1)
                    WHERE COALESCE(TRIM(location),'')!=''"""
            )

        conn.execute(
            """UPDATE lessons
               SET location=COALESCE(NULLIF(location,''),
                   (SELECT p.location FROM plans p WHERE p.id=lessons.plan_id), location)
               WHERE plan_id IS NOT NULL AND COALESCE(location,'')=''"""
        )
        conn.execute(
            """UPDATE lessons
               SET location_id=(SELECT p.location_id FROM plans p WHERE p.id=lessons.plan_id)
               WHERE plan_id IS NOT NULL
                 AND (location_id IS NULL OR location_id<>(SELECT p.location_id FROM plans p WHERE p.id=lessons.plan_id))"""
        )

        legacy = conn.execute(
            """SELECT id, name, grade, subject, total_hours, note
               FROM classes WHERE kind='一对一' ORDER BY id"""
        ).fetchall()
        for row in legacy:
            legacy_id = int(row["id"])
            mapped = conn.execute(
                "SELECT student_id FROM legacy_one_to_one_map WHERE legacy_class_id=?",
                (legacy_id,),
            ).fetchone()
            if mapped:
                continue
            student = conn.execute(
                "SELECT id FROM one_to_one_students WHERE name=?", (row["name"],)
            ).fetchone()
            if student:
                student_id = int(student["id"])
            else:
                cur = conn.execute(
                    """INSERT INTO one_to_one_students
                       (name, grade, subject, total_hours, status, note)
                       VALUES(?,?,?,?,?,?)""",
                    (row["name"], row["grade"] or "", row["subject"] or "",
                     float(row["total_hours"] or 0), "在读", row["note"] or ""),
                )
                student_id = int(cur.lastrowid)
            conn.execute(
                "INSERT OR REPLACE INTO legacy_one_to_one_map(legacy_class_id, student_id) VALUES(?,?)",
                (legacy_id, student_id),
            )

        conn.execute(
            """UPDATE plans
               SET student_id=(SELECT m.student_id FROM legacy_one_to_one_map m WHERE m.legacy_class_id=plans.class_id),
                   target_type='一对一'
               WHERE class_id IN (SELECT legacy_class_id FROM legacy_one_to_one_map)"""
        )
        conn.execute(
            """UPDATE lessons
               SET student_id=(SELECT m.student_id FROM legacy_one_to_one_map m WHERE m.legacy_class_id=lessons.class_id),
                   target_type='一对一'
               WHERE class_id IN (SELECT legacy_class_id FROM legacy_one_to_one_map)"""
        )
        conn.execute("UPDATE plans SET target_type=? WHERE student_id IS NOT NULL AND COALESCE(target_type,'')=''", (TARGET_ONE,))
        conn.execute("UPDATE plans SET target_type=? WHERE class_id IS NOT NULL AND COALESCE(target_type,'')=''", (TARGET_CLASS,))
        conn.execute("UPDATE lessons SET target_type=? WHERE student_id IS NOT NULL AND COALESCE(target_type,'')=''", (TARGET_ONE,))
        conn.execute("UPDATE lessons SET target_type=? WHERE class_id IS NOT NULL AND COALESCE(target_type,'')=''", (TARGET_CLASS,))
        conn.execute(
            """UPDATE plans SET subject=COALESCE(NULLIF(subject,''),
                   (SELECT CASE WHEN plans.target_type='一对一' THEN s.subject ELSE c.subject END
                    FROM classes c LEFT JOIN one_to_one_students s ON s.id=plans.student_id
                    WHERE c.id=plans.class_id), '')
               WHERE COALESCE(subject,'')=''"""
        )
        for row in conn.execute("SELECT id FROM plans WHERE target_type=? AND COALESCE(subject,'')=''", (TARGET_ONE,)).fetchall():
            conn.execute(
                "UPDATE plans SET subject=COALESCE((SELECT subject FROM one_to_one_students WHERE id=plans.student_id),'') WHERE id=?",
                (row["id"],),
            )
        conn.execute(
            """UPDATE lessons SET subject=COALESCE(NULLIF(subject,''),
                   (SELECT CASE WHEN lessons.target_type='一对一' THEN s.subject ELSE c.subject END
                    FROM classes c LEFT JOIN one_to_one_students s ON s.id=lessons.student_id
                    WHERE c.id=lessons.class_id), '')
               WHERE COALESCE(subject,'')=''"""
        )
        for row in conn.execute("SELECT id FROM lessons WHERE target_type=? AND COALESCE(subject,'')=''", (TARGET_ONE,)).fetchall():
            conn.execute(
                "UPDATE lessons SET subject=COALESCE((SELECT subject FROM one_to_one_students WHERE id=lessons.student_id),'') WHERE id=?",
                (row["id"],),
            )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_course_date ON plans(plan_date, start)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_target ON plans(target_type, class_id, student_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_course_target ON lessons(target_type, class_id, student_id, lesson_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_plan_id ON lessons(plan_id)")
        conn.executescript(
            """
            DROP VIEW IF EXISTS v_lessons;
            CREATE VIEW v_lessons AS
                SELECT
                    l.id, l.class_id, l.student_id, l.lesson_date, l.start, l.end, l.hours,
                    l.content, l.mastery, l.homework, l.note, l.plan_id, l.target_type, l.location,
                    CASE WHEN l.target_type='一对一' THEN COALESCE(s.name, c.name, '')
                         ELSE COALESCE(c.name, s.name, '') END AS cname,
                    COALESCE(NULLIF(l.subject,''), c.subject, s.subject, '') AS subject,
                    CASE WHEN l.target_type='一对一' THEN '一对一'
                         ELSE COALESCE(c.kind, '班课') END AS kind,
                    ROW_NUMBER() OVER (
                        PARTITION BY CASE WHEN l.target_type='一对一'
                                          THEN 'S' || COALESCE(l.student_id, -1)
                                          ELSE 'C' || COALESCE(l.class_id, -1) END
                        ORDER BY l.lesson_date, l.start
                    ) AS seq
                FROM lessons l
                LEFT JOIN classes c ON c.id=l.class_id
                LEFT JOIN one_to_one_students s ON s.id=l.student_id;
            """
        )
        conn.execute("PRAGMA user_version=5")
        conn.commit()
        cloud_store.save_database(DB_PATH)
    finally:
        conn.close()

def _mins(value: str) -> int:
    try:
        h, m = str(value).strip().split(":")
        h_i, m_i = int(h), int(m)
    except Exception as exc:
        raise CourseValidationError("时间格式不正确，应为 HH:MM，例如 19:30") from exc
    if not (0 <= h_i <= 23 and 0 <= m_i <= 59):
        raise CourseValidationError("时间超出有效范围")
    return h_i * 60 + m_i


def _hm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _month_bounds(value: date) -> tuple[date, date]:
    first = value.replace(day=1)
    if first.month == 12:
        nxt = date(first.year + 1, 1, 1)
    else:
        nxt = date(first.year, first.month + 1, 1)
    return first, nxt - timedelta(days=1)


def _fmt_day(value: date) -> str:
    return f"{value.month}月{value.day}日 {WD[value.weekday()]}"


def _e(value) -> str:
    return html.escape("" if pd.isna(value) else str(value))


COURSE_SQL = """
SELECT
    p.id, p.class_id, p.student_id, p.target_type, p.plan_date, p.start, p.end,
    COALESCE(NULLIF(p.subject,''), c.subject, s.subject, '') AS subject,
    COALESCE(p.location,'') AS location,
    p.location_id,
    COALESCE(p.note,'') AS note,
    p.status, p.moved_from, p.completed_at, p.updated_at,
    CASE WHEN p.target_type='一对一' THEN COALESCE(s.name, '(未指定学生)')
         ELSE COALESCE(c.name, '(未指定班级)') END AS target_name,
    CASE WHEN p.target_type='一对一' THEN COALESCE(s.total_hours, 0) ELSE 0 END AS total_hours
FROM plans p
LEFT JOIN classes c ON p.target_type='班级' AND c.id=p.class_id
LEFT JOIN one_to_one_students s ON p.target_type='一对一' AND s.id=p.student_id
"""


def _courses_between(d1: date | str, d2: date | str, include_moved: bool = False) -> pd.DataFrame:
    sql = COURSE_SQL + " WHERE p.plan_date BETWEEN ? AND ?"
    if not include_moved:
        sql += " AND p.status!='调课'"
    return query(sql + " ORDER BY p.plan_date, p.start, p.id", (str(d1), str(d2)))


def _course_by_id(course_id: int) -> pd.Series | None:
    df = query(COURSE_SQL + " WHERE p.id=?", (int(course_id),))
    return None if df.empty else df.iloc[0]


def _class_options() -> pd.DataFrame:
    return query(
        """SELECT id, name, grade, subject FROM classes
           WHERE COALESCE(kind,'班课')!='一对一' ORDER BY name"""
    )


def _one_options(active_only: bool = True) -> pd.DataFrame:
    where = " WHERE status='在读'" if active_only else ""
    return query(
        f"SELECT id, name, grade, subject, total_hours, status FROM one_to_one_students{where} ORDER BY name"
    )


def _location_options(active_only: bool = True) -> pd.DataFrame:
    where = " WHERE active=1" if active_only else ""
    return query(
        f"SELECT id, name, active, note FROM teaching_locations{where} ORDER BY name"
    )


def _validate_course(conn: sqlite3.Connection, course_id: int | None, values: dict) -> None:
    target_type = values["target_type"]
    if target_type not in (TARGET_CLASS, TARGET_ONE):
        raise CourseValidationError("课程类型无效")
    target_id = int(values["target_id"])
    table = "classes" if target_type == TARGET_CLASS else "one_to_one_students"
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (target_id,)).fetchone()
    if not exists:
        raise CourseValidationError("请选择有效的班级或一对一学生")
    _as_date(values["plan_date"])
    start, end = _mins(values["start"]), _mins(values["end"])
    if start >= end:
        raise CourseValidationError("结束时间必须晚于开始时间")
    if values["status"] not in STATUS_OPTIONS:
        raise CourseValidationError("课程状态无效")
    overlap = conn.execute(
        """SELECT start, end FROM plans
           WHERE plan_date=? AND status!='调课' AND status NOT IN ('请假','取消')
             AND id<>? AND start<? AND end>?""",
        (str(values["plan_date"]), int(course_id or -1), str(values["end"]), str(values["start"])),
    ).fetchone()
    if overlap:
        raise CourseValidationError(f"这个时间与已有课程重叠：{overlap['start']}–{overlap['end']}，请调整时间")


def _sync_attendance(conn: sqlite3.Connection, course_id: int) -> None:
    row = conn.execute(
        """SELECT id, class_id, student_id, target_type, plan_date, start, end,
                  subject, location, location_id, note, status FROM plans WHERE id=?""",
        (int(course_id),),
    ).fetchone()
    if row is None:
        return
    if row["status"] == STATUS_DONE:
        if row["target_type"] == TARGET_CLASS:
            lesson = conn.execute(
                """SELECT id FROM lessons
                   WHERE plan_id=? OR (class_id=? AND lesson_date=? AND start=?)
                   ORDER BY CASE WHEN plan_id=? THEN 0 ELSE 1 END, id LIMIT 1""",
                (int(course_id), row["class_id"], row["plan_date"], row["start"], int(course_id)),
            ).fetchone()
        else:
            lesson = conn.execute(
                """SELECT id FROM lessons
                   WHERE plan_id=? OR (target_type='一对一' AND student_id=? AND lesson_date=? AND start=?)
                   ORDER BY CASE WHEN plan_id=? THEN 0 ELSE 1 END, id LIMIT 1""",
                (int(course_id), row["student_id"], row["plan_date"], row["start"], int(course_id)),
            ).fetchone()
        if lesson:
            conn.execute(
                """UPDATE lessons SET class_id=?, student_id=?, lesson_date=?, start=?, end=?,
                       target_type=?, subject=?, location=?, location_id=?, note=? WHERE id=?""",
                (row["class_id"], row["student_id"], row["plan_date"], row["start"], row["end"],
                 row["target_type"], row["subject"] or "", row["location"] or "", row["location_id"],
                 row["note"] or "", int(lesson["id"])),
            )
        else:
            conn.execute(
                """INSERT INTO lessons
                   (class_id, student_id, lesson_date, start, end, hours, target_type,
                    subject, location, location_id, note, plan_id)
                   VALUES(?,?,?,?,?,1,?,?,?,?,?,?)""",
                (row["class_id"], row["student_id"], row["plan_date"], row["start"], row["end"],
                 row["target_type"], row["subject"] or "", row["location"] or "", row["location_id"],
                 row["note"] or "", int(course_id)),
            )
    else:
        conn.execute("DELETE FROM lessons WHERE plan_id=?", (int(course_id),))


def save_course(course_id: int | None, values: dict) -> int:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cleaned = dict(values)
        cleaned["plan_date"] = str(_as_date(cleaned["plan_date"]))
        cleaned["start"] = _hm(_mins(cleaned["start"]))
        cleaned["end"] = _hm(_mins(cleaned["end"]))
        cleaned["subject"] = str(cleaned.get("subject") or "").strip()
        location_id = int(cleaned.get("location_id") or 0)
        if location_id:
            location_row = conn.execute(
                "SELECT name FROM teaching_locations WHERE id=? AND active=1",
                (location_id,),
            ).fetchone()
            if location_row is None:
                raise CourseValidationError("授课地点无效或已经停用")
            cleaned["location"] = str(location_row["name"])
        else:
            location_id = None
            cleaned["location"] = str(cleaned.get("location") or "").strip()
        cleaned["location_id"] = location_id
        cleaned["note"] = str(cleaned.get("note") or "").strip()
        cleaned["status"] = cleaned.get("status") or STATUS_PENDING
        if cleaned["target_type"] == TARGET_CLASS:
            class_id, student_id = int(cleaned["target_id"]), None
        else:
            class_id, student_id = None, int(cleaned["target_id"])
        _validate_course(conn, course_id, {
            "target_type": cleaned["target_type"], "target_id": cleaned["target_id"],
            "plan_date": cleaned["plan_date"], "start": cleaned["start"],
            "end": cleaned["end"], "status": cleaned["status"],
        })
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if course_id is None:
            cur = conn.execute(
                """INSERT INTO plans
                   (class_id, student_id, target_type, plan_date, start, end, subject,
                    location, location_id, status, note, updated_at, completed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (class_id, student_id, cleaned["target_type"], cleaned["plan_date"],
                 cleaned["start"], cleaned["end"], cleaned["subject"], cleaned["location"],
                 cleaned["location_id"], cleaned["status"], cleaned["note"], now,
                 now if cleaned["status"] == STATUS_DONE else ""),
            )
            course_id = int(cur.lastrowid)
        else:
            old = conn.execute("SELECT status, completed_at FROM plans WHERE id=?", (int(course_id),)).fetchone()
            if old is None:
                raise CourseValidationError("课程不存在或已经被删除")
            completed_at = old["completed_at"] or ""
            if cleaned["status"] == STATUS_DONE and old["status"] != STATUS_DONE:
                completed_at = now
            elif cleaned["status"] != STATUS_DONE:
                completed_at = ""
            conn.execute(
                """UPDATE plans SET class_id=?, student_id=?, target_type=?, plan_date=?,
                       start=?, end=?, subject=?, location=?, location_id=?, status=?, note=?,
                       updated_at=?, completed_at=? WHERE id=?""",
                (class_id, student_id, cleaned["target_type"], cleaned["plan_date"],
                 cleaned["start"], cleaned["end"], cleaned["subject"], cleaned["location"],
                 cleaned["location_id"], cleaned["status"], cleaned["note"], now, completed_at,
                 int(course_id)),
            )
        _sync_attendance(conn, int(course_id))
        conn.commit()
        return int(course_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_course(course_id: int) -> bool:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM plans WHERE id=?", (int(course_id),)).fetchone()
        if row is None:
            conn.rollback()
            return False
        if row["status"] == STATUS_DONE:
            _sync_attendance(conn, int(course_id))
            conn.commit()
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE plans SET status=?, completed_at=?, updated_at=? WHERE id=?",
                     (STATUS_DONE, now, now, int(course_id)))
        _sync_attendance(conn, int(course_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def uncomplete_course(course_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE plans SET status=?, completed_at='', updated_at=? WHERE id=?",
                     (STATUS_PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(course_id)))
        conn.execute("DELETE FROM lessons WHERE plan_id=?", (int(course_id),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_course(course_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM lessons WHERE plan_id=?", (int(course_id),))
        conn.execute("DELETE FROM plans WHERE id=?", (int(course_id),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def copy_week(ws: date, weeks: int) -> int:
    end = ws + timedelta(days=6)
    rows = _courses_between(ws, end)
    rows = rows[rows["status"].isin(ACTIVE_STATUSES)]
    created = 0
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in rows.itertuples():
            for offset in range(1, int(weeks) + 1):
                new_date = _as_date(row.plan_date) + timedelta(weeks=offset)
                duplicate = conn.execute(
                    """SELECT 1 FROM plans
                       WHERE plan_date=? AND start=? AND status!='调课'
                         AND ((target_type='班级' AND class_id=?)
                              OR (target_type='一对一' AND student_id=?))""",
                    (str(new_date), row.start, row.class_id, row.student_id),
                ).fetchone()
                if duplicate:
                    continue
                conn.execute(
                    """INSERT INTO plans
                       (class_id, student_id, target_type, plan_date, start, end, subject,
                        location, location_id, status, note)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (None if pd.isna(row.class_id) else int(row.class_id), None if pd.isna(row.student_id) else int(row.student_id), row.target_type, str(new_date), row.start,
                     row.end, row.subject, row.location, None if pd.isna(row.location_id) else int(row.location_id), STATUS_PENDING, row.note),
                )
                created += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return created


def add_hour_transaction(
    student_id: int,
    transaction_type: str,
    hours_delta: float,
    occurred_at: date | str,
    note: str = "",
    created_by: int | None = None,
) -> None:
    if transaction_type not in ("renewal", "adjustment"):
        raise CourseValidationError("课时变动类型无效")
    delta = round(float(hours_delta), 2)
    if transaction_type == "renewal" and delta <= 0:
        raise CourseValidationError("续费课时必须大于 0")
    if transaction_type == "adjustment" and delta == 0:
        raise CourseValidationError("调整课时不能为 0")
    exists = query("SELECT id FROM one_to_one_students WHERE id=?", (int(student_id),))
    if exists.empty:
        raise CourseValidationError("一对一学生不存在")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run(
        """INSERT INTO hour_transactions
           (student_id, transaction_type, hours_delta, occurred_at, note, created_at, created_by)
           VALUES(?,?,?,?,?,?,?)""",
        (
            int(student_id),
            transaction_type,
            delta,
            str(_as_date(occurred_at)),
            str(note or "").strip(),
            now,
            int(created_by) if created_by else None,
        ),
    )


def update_initial_hours(student_id: int, hours: float) -> None:
    value = round(float(hours), 2)
    if value < 0:
        raise CourseValidationError("初始购买课时不能小于 0")
    row = query(
        "SELECT id FROM one_to_one_students WHERE id=?",
        (int(student_id),),
    )
    if row.empty:
        raise CourseValidationError("一对一学生不存在")
    run(
        "UPDATE one_to_one_students SET total_hours=? WHERE id=?",
        (value, int(student_id)),
    )


def one_to_one_summary() -> pd.DataFrame:
    return query(
        """SELECT s.id, s.name, s.grade, COALESCE(NULLIF(s.subject,''),'') AS subject,
                  COALESCE(s.total_hours,0) AS initial_hours,
                  COALESCE(t.renewed_hours,0) AS renewed_hours,
                  COALESCE(t.adjusted_hours,0) AS adjusted_hours,
                  COALESCE(s.total_hours,0)+COALESCE(t.renewed_hours,0)+COALESCE(t.adjusted_hours,0) AS total_hours,
                  COALESCE(u.used,0) AS used,
                  COALESCE(s.total_hours,0)+COALESCE(t.renewed_hours,0)+COALESCE(t.adjusted_hours,0)-COALESCE(u.used,0) AS remain,
                  s.status, s.contact, s.note
           FROM one_to_one_students s
           LEFT JOIN (
               SELECT student_id,
                      SUM(CASE WHEN transaction_type='renewal' THEN hours_delta ELSE 0 END) AS renewed_hours,
                      SUM(CASE WHEN transaction_type='adjustment' THEN hours_delta ELSE 0 END) AS adjusted_hours
               FROM hour_transactions
               GROUP BY student_id
           ) t ON t.student_id=s.id
           LEFT JOIN (
               SELECT student_id, COUNT(*) AS used
               FROM lessons
               WHERE target_type='一对一' AND student_id IS NOT NULL
               GROUP BY student_id
           ) u ON u.student_id=s.id
           ORDER BY s.status, s.name"""
    )


def hour_ledger(student_id: int) -> pd.DataFrame:
    student = query(
        "SELECT id, name, total_hours FROM one_to_one_students WHERE id=?",
        (int(student_id),),
    )
    if student.empty:
        return pd.DataFrame()
    frames = []
    initial = pd.DataFrame(
        [{
            "日期": "",
            "类型": "初始购买",
            "课时变化": float(student.iloc[0]["total_hours"] or 0),
            "说明": "初始购买课时",
            "排序时间": "",
        }]
    )
    frames.append(initial)
    tx = query(
        """SELECT occurred_at, transaction_type, hours_delta, note, created_at
           FROM hour_transactions WHERE student_id=?
           ORDER BY occurred_at, id""",
        (int(student_id),),
    )
    if not tx.empty:
        tx = tx.copy()
        tx["日期"] = tx["occurred_at"]
        tx["类型"] = tx["transaction_type"].map({"renewal": "续费", "adjustment": "课时调整"})
        tx["课时变化"] = pd.to_numeric(tx["hours_delta"], errors="coerce").fillna(0.0)
        tx["说明"] = tx["note"].fillna("")
        tx["排序时间"] = tx["created_at"].fillna("")
        frames.append(tx[["日期", "类型", "课时变化", "说明", "排序时间"]])
    used = query(
        """SELECT l.lesson_date, l.start, COALESCE(NULLIF(l.subject,''),'') AS subject,
                  COALESCE(l.plan_id,0) AS plan_id
           FROM lessons l
           WHERE l.target_type='一对一' AND l.student_id=?
           ORDER BY l.lesson_date, l.start""",
        (int(student_id),),
    )
    if not used.empty:
        used = used.copy()
        used["日期"] = used["lesson_date"]
        used["类型"] = "已上课"
        used["课时变化"] = -1.0
        used["说明"] = used.apply(
            lambda r: f"{r['subject'] or '课程'} {r['start'] or ''}".strip(),
            axis=1,
        )
        used["排序时间"] = used["lesson_date"].astype(str) + " " + used["start"].astype(str)
        frames.append(used[["日期", "类型", "课时变化", "说明", "排序时间"]])
    ledger = pd.concat(frames, ignore_index=True)
    ledger = ledger.sort_values(["日期", "排序时间"], ascending=[False, False])
    return ledger[["日期", "类型", "课时变化", "说明"]]

def location_month_summary(month: str) -> pd.DataFrame:
    assigned = query(
        """SELECT tl.id AS location_id, tl.name AS location_name,
                  SUM(CASE WHEN l.target_type='班级' THEN 1 ELSE 0 END) AS class_count,
                  SUM(CASE WHEN l.target_type='一对一' THEN 1 ELSE 0 END) AS one_count
           FROM teaching_locations tl
           LEFT JOIN lessons l
             ON l.location_id=tl.id AND substr(l.lesson_date,1,7)=?
           GROUP BY tl.id, tl.name
           ORDER BY tl.name""",
        (month,),
    )
    unassigned = query(
        """SELECT 0 AS location_id, '未填写地点' AS location_name,
                  SUM(CASE WHEN target_type='班级' THEN 1 ELSE 0 END) AS class_count,
                  SUM(CASE WHEN target_type='一对一' THEN 1 ELSE 0 END) AS one_count
           FROM lessons
           WHERE location_id IS NULL AND substr(lesson_date,1,7)=?""",
        (month,),
    )
    frames = []
    if not assigned.empty:
        frames.append(assigned)
    if not unassigned.empty and (int(unassigned.iloc[0]["class_count"] or 0) + int(unassigned.iloc[0]["one_count"] or 0)) > 0:
        frames.append(unassigned)
    if not frames:
        return pd.DataFrame(columns=["location_id", "location_name", "class_count", "one_count"])
    return pd.concat(frames, ignore_index=True)


def location_settlement_summary(month: str) -> pd.DataFrame:
    summary = location_month_summary(month)
    if summary.empty:
        return summary
    summary = summary.copy()
    summary["total_count"] = summary["class_count"] + summary["one_count"]
    settled = query(
        """SELECT location_id, class_count, one_count, settled_at, note
           FROM location_settlements WHERE settlement_month=?""",
        (month,),
    )
    summary = summary.merge(settled, on="location_id", how="left", suffixes=("", "_settled"))
    statuses = []
    class_delta = []
    one_delta = []
    for _, row in summary.iterrows():
        if row["location_id"] == 0 or pd.isna(row["settled_at"]):
            statuses.append("未结算")
            class_delta.append(0)
            one_delta.append(0)
            continue
        dc = int(row["class_count"] - int(row["class_count_settled"] or 0))
        do = int(row["one_count"] - int(row["one_count_settled"] or 0))
        class_delta.append(dc)
        one_delta.append(do)
        statuses.append("已结算" if dc == 0 and do == 0 else "结算后有变动")
    summary["结算状态"] = statuses
    summary["班级变化"] = class_delta
    summary["一对一变化"] = one_delta
    return summary


def mark_location_settled(month: str, location_id: int, note: str = "") -> None:
    if int(location_id) <= 0:
        raise CourseValidationError("请先为课程填写授课地点")
    summary = location_month_summary(month)
    row = summary[summary["location_id"] == int(location_id)]
    if row.empty:
        raise CourseValidationError("没有找到该地点的授课记录")
    class_count = int(row.iloc[0]["class_count"] or 0)
    one_count = int(row.iloc[0]["one_count"] or 0)
    run(
        """INSERT INTO location_settlements
           (settlement_month, location_id, class_count, one_count, settled_at, note)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(settlement_month, location_id) DO UPDATE SET
             class_count=excluded.class_count,
             one_count=excluded.one_count,
             settled_at=excluded.settled_at,
             note=excluded.note""",
        (month, int(location_id), class_count, one_count, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(note or "").strip()),
    )


def unmark_location_settled(month: str, location_id: int) -> None:
    run(
        "DELETE FROM location_settlements WHERE settlement_month=? AND location_id=?",
        (month, int(location_id)),
    )


def location_management_summary() -> pd.DataFrame:
    return query(
        """SELECT tl.id, tl.name, tl.active, tl.note, tl.created_at,
                  COUNT(l.id) AS total_lessons,
                  SUM(CASE WHEN l.target_type='班级' THEN 1 ELSE 0 END) AS class_count,
                  SUM(CASE WHEN l.target_type='一对一' THEN 1 ELSE 0 END) AS one_count
           FROM teaching_locations tl
           LEFT JOIN lessons l ON l.location_id=tl.id
           GROUP BY tl.id, tl.name, tl.active, tl.note, tl.created_at
           ORDER BY tl.active DESC, tl.name"""
    )


def class_month_summary(month: str | None = None) -> pd.DataFrame:
    if month is None:
        month = date.today().strftime("%Y-%m")
    return query(
        """SELECT c.id, c.name, c.grade, c.subject,
                  SUM(CASE WHEN substr(l.lesson_date,1,7)=? THEN 1 ELSE 0 END) AS completed
           FROM classes c
           LEFT JOIN lessons l ON l.class_id=c.id AND COALESCE(l.target_type,'班级')='班级'
           WHERE COALESCE(c.kind,'班课')!='一对一'
           GROUP BY c.id, c.name, c.grade, c.subject
           ORDER BY c.name""",
        (month,),
    )

def _class_month_detail(class_id: int, month: str) -> pd.DataFrame:
    return query(
        """SELECT lesson_date, start, end, subject, COALESCE(location,'') AS location,
                  COALESCE(content,'') AS content, COALESCE(note,'') AS note
           FROM lessons WHERE class_id=? AND substr(lesson_date,1,7)=?
           ORDER BY lesson_date, start""",
        (int(class_id), month),
    )


def _inject_css() -> None:
    """样式统一放在 ui_theme.py,由 app.py 启动时注入;这里留空函数,各页面的调用不用改。"""
    return None


def _set_dialog(course_id: int | None, default_date: date | None = None) -> None:
    st.session_state["course_dialog_id"] = course_id
    st.session_state["course_dialog_date"] = str(default_date or date.today())


def _clear_dialog_state() -> None:
    st.session_state.pop("course_dialog_id", None)
    st.session_state.pop("course_dialog_date", None)


def _close_dialog() -> None:
    st.session_state.pop("course_dialog_id", None)
    st.session_state.pop("course_dialog_date", None)
    st.rerun()


@st.dialog("课程详情", width="large", dismissible=True, on_dismiss=_clear_dialog_state)
def _course_dialog(course_id: int | None = None):
    row = None if course_id is None else _course_by_id(int(course_id))
    if course_id is not None and row is None:
        st.error("这节课已经不存在。")
        if st.button("关闭", use_container_width=True):
            _close_dialog()
        return
    default_date = date.today() if row is None else _as_date(row["plan_date"])
    current_type = TARGET_CLASS if row is None else row["target_type"]
    type_label = TYPE_LABELS.get(current_type, "班级课")
    selected_label = st.segmented_control(
        "课程类型", options=["班级课", "一对一"], default=type_label,
        key=f"dlg_type_{course_id}",
    )
    selected_label = selected_label or type_label
    target_type = LABEL_TYPES[selected_label]
    target_df = _class_options() if target_type == TARGET_CLASS else _one_options()
    if target_df.empty:
        st.warning("还没有可选的班级或一对一学生，请先到「教学对象」页面新增。")
        if st.button("去新增教学对象", type="primary", use_container_width=True,
                     on_click=lambda: goto_page("教学对象")):
            _close_dialog()
        return
    target_labels = {
        int(r["id"]): f"{r['name']}" + (f" · {r['grade']}" if str(r["grade"] or "").strip() else "")
        for _, r in target_df.iterrows()
    }
    target_ids = list(target_labels)
    current_target = None
    if row is not None and row["target_type"] == target_type:
        current_target = int(row["class_id"] if target_type == TARGET_CLASS else row["student_id"])
    if current_target not in target_ids:
        current_target = target_ids[0]

    c1, c2 = st.columns([1.2, 1])
    target_id = c1.selectbox(
        "班级 / 一对一学生", target_ids, index=target_ids.index(current_target),
        format_func=target_labels.get, key=f"dlg_target_{course_id}_{target_type}",
    )
    course_date = c2.date_input("日期", default_date, key=f"dlg_date_{course_id}")

    c3, c4 = st.columns(2)
    default_start = time(19, 0) if row is None else datetime.strptime(str(row["start"]), "%H:%M").time()
    default_end = time(20, 30) if row is None else datetime.strptime(str(row["end"]), "%H:%M").time()
    start_time = c3.time_input("开始时间", default_start, key=f"dlg_start_{course_id}")
    end_time = c4.time_input("结束时间", default_end, key=f"dlg_end_{course_id}")

    c5, c6 = st.columns(2)
    subject_default = "" if row is None else str(row["subject"] or "")
    if not subject_default and target_type == TARGET_ONE:
        selected = target_df[target_df["id"] == target_id]
        if not selected.empty:
            subject_default = str(selected.iloc[0]["subject"] or "")
    subject = c5.text_input(
        "科目", subject_default, placeholder="例如：数学、物理",
        key=f"dlg_subject_{course_id}_{target_type}_{target_id}",
    )
    locations = _location_options()
    location_labels = {0: "未填写地点"}
    if not locations.empty:
        location_labels.update({int(r["id"]): str(r["name"]) for _, r in locations.iterrows()})
    location_ids = list(location_labels)
    current_location = (
        int(row["location_id"])
        if row is not None and pd.notna(row.get("location_id"))
        else 0
    )
    if current_location not in location_ids:
        current_location = 0
    location_id = c6.selectbox(
        "授课地点",
        location_ids,
        index=location_ids.index(current_location),
        format_func=location_labels.get,
        key=f"dlg_location_{course_id}",
    )
    if len(location_ids) == 1:
        c6.caption("还没有授课地点，请先到「教学对象」页面的「授课地点」标签添加。")

    c7, c8 = st.columns([1, 2])
    status_default = STATUS_PENDING if row is None else row["status"]
    if status_default == STATUS_MOVED:
        status_default = STATUS_PENDING
    status = c7.selectbox(
        "状态", STATUS_OPTIONS,
        index=STATUS_OPTIONS.index(status_default) if status_default in STATUS_OPTIONS else 0,
        format_func=lambda x: f"{STATUS_ICON.get(x,'')} {x}", key=f"dlg_status_{course_id}",
    )
    note = c8.text_input(
        "备注", "" if row is None else str(row["note"] or ""),
        placeholder="临时调整、作业说明等", key=f"dlg_note_{course_id}",
    )
    if row is not None:
        st.caption(f"课程 ID：{int(row['id'])}　当前状态：{STATUS_ICON.get(row['status'],'')} {row['status']}")

    left, right = st.columns([2, 1])
    if left.button("保存课程", type="primary", use_container_width=True):
        values = {
            "target_type": target_type, "target_id": int(target_id),
            "plan_date": course_date, "start": start_time.strftime("%H:%M"),
            "end": end_time.strftime("%H:%M"), "subject": subject,
            "location_id": int(location_id), "status": status, "note": note,
        }
        try:
            saved_id = save_course(course_id, values)
            st.session_state["course_saved_id"] = saved_id
            _close_dialog()
        except CourseValidationError as exc:
            st.error(str(exc))
        except sqlite3.IntegrityError:
            st.error("保存失败：这节课与已有记录发生冲突。")
    if row is None:
        right.button("取消", use_container_width=True, on_click=_close_dialog)
    else:
        delete_confirm = st.checkbox("确认删除这节课及对应上课记录", key=f"dlg_delconfirm_{course_id}")
        if right.button("删除", type="secondary", disabled=not delete_confirm, use_container_width=True):
            delete_course(int(course_id))
            _close_dialog()


def _show_dialog_if_requested() -> None:
    if "course_dialog_id" in st.session_state:
        _course_dialog(st.session_state.get("course_dialog_id"))

def _course_state(status: str) -> str:
    if status == STATUS_DONE:
        return "done"
    if status in (STATUS_LEAVE, STATUS_CANCEL, STATUS_MOVED):
        return "off"
    return "pending"


def _chips_html(row, mobile: bool) -> str:
    status = str(row["status"])
    chips = [
        f"<span class='chip'>{_e(TYPE_LABELS.get(row['target_type'], row['target_type']))}</span>",
        f"<span class='chip {_course_state(status)}'>{_e(status)}</span>",
    ]
    if row["location"]:
        chips.append(f"<span class='chip'>📍 {_e(row['location'])}</span>")
    if not mobile:
        if row["target_type"] == TARGET_ONE:
            chips.append(f"<span class='chip'>初始购买 {float(row['total_hours'] or 0):g} 课时</span>")
        if status == STATUS_DONE and pd.notna(row["completed_at"]) and row["completed_at"]:
            chips.append(f"<span class='chip'>完成于 {_e(str(row['completed_at'])[:16])}</span>")
    if pd.notna(row["note"]) and row["note"]:
        chips.append(f"<span class='chip'>备注：{_e(row['note'])}</span>")
    return "<div class='chips'>" + "".join(chips) + "</div>"


def _course_item(row, key: str, show_complete: bool = True) -> None:
    """一节课 = 一张卡:左边色条是状态,第一行点一下编辑,待上课的右边有「✓ 已上课」。"""
    status = str(row["status"])
    rid = int(row["id"])
    can_complete = show_complete and status == STATUS_PENDING
    label = f"**{row['start']}** – {row['end']}　{row['target_name']} · {row['subject'] or '未填科目'}"
    with st.container(key=f"card_{_course_state(status)}_{key}_{rid}"):
        if can_complete:
            left, right = st.columns([4.5, 1.5], vertical_alignment="center")
        else:
            left, right = st.container(), None
        if left.button(label, key=f"open_{key}_{rid}", use_container_width=True):
            _set_dialog(rid)
        if right is not None and right.button(
            "✓ 已上课", key=f"done_{key}_{rid}", type="primary", use_container_width=True
        ):
            changed = complete_course(rid)
            if changed and row["target_type"] == TARGET_ONE:
                st.toast("已记录上课，自动扣除 1 课时")
            elif changed:
                st.toast("已记录上课")
            st.rerun()
        st.markdown(_chips_html(row, is_mobile()), unsafe_allow_html=True)


def _course_row(row, key: str, show_complete: bool = True) -> None:
    _course_item(row, key, show_complete)


def _course_card(row, key: str, show_complete: bool = True) -> None:
    _course_item(row, key, show_complete)


def _render_day_cards(day: date, key: str, show_empty: bool = True) -> None:
    df = _courses_between(day, day)
    if df.empty:
        if show_empty:
            st.info("这一天还没有课程。点击右上角「＋ 新增课程」即可添加。")
        return
    for _, row in df.iterrows():
        _course_card(row, key)


def _week_grid_html(ws: date, mobile: bool = False) -> str:
    days = [ws + timedelta(i) for i in range(7)]
    today_idx = (date.today() - ws).days
    courses = _courses_between(ws, days[-1])
    courses = courses[courses["status"].isin(ACTIVE_STATUSES)]
    cells: dict[tuple[int, int], tuple[str, str]] = {}
    counts = [0] * 7
    used_slots: list[int] = []
    for row in courses.itertuples():
        day_index = (_as_date(row.plan_date) - ws).days
        if 0 <= day_index < 7:
            counts[day_index] += 1
        start = _mins(row.start)
        first = start // 30 * 30
        for slot in range(first, max(first + 30, _mins(row.end)), 30):
            used_slots.append(slot)
            if slot == first:
                if mobile:
                    subject = str(row.subject).strip() if row.subject else ""
                    label = (subject or str(row.target_name))[:2]
                    text = html.escape(label) + ("✓" if row.status == STATUS_DONE else "")
                else:
                    text = (
                        f"{'👤 ' if row.target_type == TARGET_ONE else ''}"
                        f"{html.escape(str(row.target_name))} {row.start}"
                        + (" ✓" if row.status == STATUS_DONE else "")
                    )
            else:
                text = "&nbsp;"
            css = "done" if row.status == STATUS_DONE else "busy"
            cells[(day_index, slot)] = (css, text)

    lo, hi = _mins(DAY_START), _mins(DAY_END)
    if mobile and used_slots:
        lo = max(_mins(DAY_START), min(used_slots) - 30)
        hi = min(_mins(DAY_END) + 60, max(used_slots) + 90)

    out = ["<table class='wk2" + (" m" if mobile else "") + "'><tr><th style='width:46px'></th>"]
    for i, day in enumerate(days):
        cls = "today" if day == date.today() else ""
        if mobile:
            n = counts[i]
            out.append(
                f"<th class='{cls}'>{WD[i]}<br>"
                f"<span class='d'>{day.month}/{day.day}</span><br>"
                f"<span class='n'>{str(n) + '节' if n else '·'}</span></th>"
            )
        else:
            out.append(f"<th class='{cls}'>{WD[i]}<br>{day.month}/{day.day}</th>")
    out.append("</tr>")
    for slot in range(lo, hi, 30):
        out.append(f"<tr><td class='tm'>{_hm(slot) if slot % 60 == 0 else ''}</td>")
        for i in range(7):
            cell = cells.get((i, slot))
            tdy = " tdy" if i == today_idx else ""
            out.append(f"<td class='{cell[0]}{tdy}'>{cell[1]}</td>" if cell else f"<td class='free{tdy}'></td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)
def _week_list(ws: date) -> None:
    """手机版本周明细：按天分组，每节一行，默认展开，点整行编辑。"""
    days = [ws + timedelta(i) for i in range(7)]
    courses = _courses_between(days[0], days[-1])
    for idx, day in enumerate(days):
        group = courses[courses["plan_date"].map(lambda v: _as_date(v) == day)]
        active = group[group["status"].isin(ACTIVE_STATUSES)]
        is_today = day == date.today()
        if active.empty:
            st.markdown(
                f"<div class='mempty'>{WD[idx]} {day.month}/{day.day} · 无课</div>",
                unsafe_allow_html=True,
            )
            continue
        st.markdown(
            f"<div class='mday{' today' if is_today else ''}'>{WD[idx]} {day.month}/{day.day}"
            f"{' · 今天' if is_today else ''} · {len(active)}节</div>",
            unsafe_allow_html=True,
        )
        for _, row in active.iterrows():
            _course_row(row, f"mweek{idx}")
        for _, row in group[~group["status"].isin(ACTIVE_STATUSES)].iterrows():
            _course_row(row, f"mweekx{idx}", show_complete=False)
def _render_week(ws: date) -> None:
    we = ws + timedelta(days=6)
    st.caption(f"{ws.year}年{ws.month}月{ws.day}日 – {we.month}月{we.day}日")
    if is_mobile():
        st.markdown(_week_grid_html(ws, mobile=True), unsafe_allow_html=True)
        st.caption("格子里是科目，列头是当天课数；🟦待上课　⬜已上课")
        st.markdown("#### 本周课程（点课程即可编辑）")
        _week_list(ws)
    else:
        st.markdown("#### 本周课程（点击课程即可编辑）")
        st.markdown(_week_grid_html(ws), unsafe_allow_html=True)
        st.caption("按星期和时间查看；蓝色待上课，灰蓝已上课，👤 表示一对一。")
        days = [ws + timedelta(i) for i in range(7)]
        columns = st.columns(7, gap="small")
        for idx, day in enumerate(days):
            with columns[idx]:
                st.markdown(
                    f"<div class='dayhead{' today' if day == date.today() else ''}'>{WD[idx]}"
                    f"<small>{day.month}/{day.day}</small></div>",
                    unsafe_allow_html=True,
                )
                df = _courses_between(day, day)
                if df.empty:
                    st.caption("无课程")
                for _, row in df.iterrows():
                    label = f"{row['start']}\n{row['target_name']}\n{row['subject'] or '未填科目'}"
                    if st.button(label, key=f"week_{idx}_{row['id']}", use_container_width=True):
                        _set_dialog(int(row["id"]))
    with st.expander("把本周课程复制到后面几周"):
        n = st.number_input("复制到接下来几周", min_value=1, max_value=26, value=4, step=1)
        if st.button("复制本周课程"):
            count = copy_week(ws, int(n))
            st.success(f"已新增 {count} 节课")
            st.rerun()
def _month_grid_html(day_in_month: date, mobile: bool = False) -> str:
    first, last = _month_bounds(day_in_month)
    courses = _courses_between(first, last)
    grouped: dict[str, list[pd.Series]] = {}
    for _, row in courses.iterrows():
        grouped.setdefault(str(row["plan_date"]), []).append(row)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    out = ["<table class='month2" + (" m" if mobile else "") + "'><tr>"
           + "".join(f"<th>{x}</th>" for x in WD) + "</tr>"]
    for week in weeks:
        out.append("<tr>")
        for day in week:
            classes = ["outside"] if day.month != first.month else []
            if day == date.today():
                classes.append("today")
            out.append(f"<td class='{' '.join(classes)}'>")
            if day.month == first.month:
                out.append(f"<div class='daynum'>{day.day}</div>")
                items = sorted(grouped.get(str(day), []), key=lambda x: str(x["start"]))
                if mobile:
                    if items:
                        done = sum(1 for r in items if r["status"] == STATUS_DONE)
                        out.append(f"<div class='mnum'>{len(items)}节</div>")
                        if done:
                            out.append(f"<div class='mdone'>{done}✓</div>")
                else:
                    for row in items[:4]:
                        is_done = row["status"] == STATUS_DONE
                        mark = "✓" if is_done else row["start"]
                        out.append(
                            f"<span class='mini{' done' if is_done else ''}'>{_e(mark)} {_e(row['target_name'])} {_e(row['subject'] or '')}</span>"
                        )
                    if len(items) > 4:
                        out.append(f"<span class='tiny-note'>另有 {len(items)-4} 节</span>")
            out.append("</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)
def _render_month(day_in_month: date) -> None:
    first, last = _month_bounds(day_in_month)
    st.caption(f"{first.year}年{first.month}月")
    if is_mobile():
        st.markdown(_month_grid_html(day_in_month, mobile=True), unsafe_allow_html=True)
        st.caption("格子里的数字是当天课数；下面按日期列出，点课程即可编辑。")
    else:
        st.markdown(_month_grid_html(day_in_month), unsafe_allow_html=True)
        st.caption("月历用于总览；下面是按日期排列的可点击课程。")
    df = _courses_between(first, last)
    if df.empty:
        st.info("这个月还没有课程。")
        return
    for day_text, group in df.groupby("plan_date", sort=True):
        day = _as_date(day_text)
        if not is_mobile():
            st.markdown(f"#### {_fmt_day(day)}")
            for _, row in group.iterrows():
                _course_card(row, "month")
            continue
        active = group[group["status"].isin(ACTIVE_STATUSES)]
        if active.empty:
            continue
        st.markdown(
            f"<div class='mday{' today' if day == date.today() else ''}'>{_fmt_day(day)} · {len(active)}节</div>",
            unsafe_allow_html=True,
        )
        for _, row in active.iterrows():
            _course_row(row, "month")
        for _, row in group[~group["status"].isin(ACTIVE_STATUSES)].iterrows():
            _course_row(row, "monthx", show_complete=False)
def _render_today(day: date | None = None) -> None:
    day = day or date.today()
    df = _courses_between(day, day)
    active = df[df["status"].isin(ACTIVE_STATUSES)]
    done = int((active["status"] == STATUS_DONE).sum())
    pending = int((active["status"] == STATUS_PENDING).sum())
    if is_mobile():
        one_n = len(df[df["target_type"] == TARGET_ONE])
        st.markdown(
            "<div class='mstats'>"
            f"<div class='mstat'><span>今日课程</span><b>{len(active)}</b></div>"
            f"<div class='mstat'><span>已上课</span><b>{done}</b></div>"
            f"<div class='mstat'><span>待上课</span><b>{pending}</b></div>"
            f"<div class='mstat'><span>今日一对一</span><b>{one_n}</b></div>"
            "</div>",
            unsafe_allow_html=True,
        )
    else:
        cols = st.columns(4)
        cols[0].metric("今日课程", len(active))
        cols[1].metric("已上课", done)
        cols[2].metric("待上课", pending)
        cols[3].metric("今日一对一", len(df[df["target_type"] == TARGET_ONE]))
    if df.empty:
        st.info("今天没有课程。")
    else:
        for _, row in active.iterrows():
            _course_row(row, "today") if is_mobile() else _course_card(row, "today")
        other = df[~df["status"].isin(ACTIVE_STATUSES)]
        if not other.empty:
            with st.expander("请假、取消和已调课记录"):
                for _, row in other.iterrows():
                    _course_row(row, "today_other", show_complete=False) if is_mobile() else _course_card(row, "today_other", show_complete=False)
    overdue = query(
        COURSE_SQL + """ WHERE p.status='待上课' AND p.plan_date<? AND p.plan_date>=?
                        ORDER BY p.plan_date, p.start""",
        (str(day), str(day - timedelta(days=30))),
    )
    if not overdue.empty:
        st.subheader("过去 30 天尚未确认的课程")
        for _, row in overdue.iterrows():
            _course_row(row, "overdue") if is_mobile() else _course_card(row, "overdue")


def page_home() -> None:
    _inject_css()
    day = date.today()
    st.title("今天")
    st.caption(_fmt_day(day))
    if is_mobile():
        if st.button("＋ 新增课程", type="primary", use_container_width=True):
            _set_dialog(None, day)
    else:
        top = st.columns([1, 5])
        if top[0].button("＋ 新增课程", type="primary", use_container_width=True):
            _set_dialog(None, day)
    _render_today(day)
    st.divider()
    if is_mobile():
        st.button("查看完整课程表 →", use_container_width=True, on_click=lambda: goto_page("课程表"))
    else:
        st.button("查看完整课程表 →", on_click=lambda: goto_page("课程表"))
    _show_dialog_if_requested()


def page_schedule() -> None:
    _inject_css()
    st.title("课程表 / 排课管理")
    if is_mobile():
        if st.button("＋ 新增课程", type="primary", use_container_width=True):
            _set_dialog(None, st.session_state.get("schedule_date", date.today()))
        view = st.segmented_control(
            "查看范围", options=["今天", "本周", "本月"], default="本周",
            label_visibility="collapsed", key="schedule_view",
        )
        selected = st.date_input(
            "选择日期", value=date.today(), label_visibility="collapsed", key="schedule_date",
        )
    else:
        controls = st.columns([1.15, 2.4, 1.2])
        if controls[0].button("＋ 新增课程", type="primary", use_container_width=True):
            _set_dialog(None, st.session_state.get("schedule_date", date.today()))
        view = controls[1].segmented_control(
            "查看范围", options=["今天", "本周", "本月"], default="本周",
            label_visibility="collapsed", key="schedule_view",
        )
        selected = controls[2].date_input(
            "选择日期", value=date.today(), label_visibility="collapsed", key="schedule_date",
        )
    st.divider()
    if view == "今天":
        st.subheader(_fmt_day(selected))
        _render_day_cards(selected, "schedule_day")
    elif view == "本月":
        _render_month(selected)
    else:
        _render_week(_week_start(selected))
    _show_dialog_if_requested()


def page_targets() -> None:
    _inject_css()
    st.title("教学对象")
    st.caption("班级课和一对一分开管理。课程安排中的授课对象全部从这里读取，无需修改代码。")
    tab_class, tab_one, tab_location = st.tabs(["班级", "一对一学生", "授课地点"])

    with tab_class:
        with st.expander("＋ 新增班级", expanded=False):
            with st.form("course_new_class"):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("班级名称", placeholder="例如：初三 1 班")
                grade = c2.text_input("年级", placeholder="例如：初三")
                subject = c3.text_input("默认科目", placeholder="例如：数学")
                note = st.text_input("备注")
                if st.form_submit_button("创建班级", type="primary"):
                    if not name.strip():
                        st.error("请填写班级名称")
                    else:
                        try:
                            run("""INSERT INTO classes(name, grade, subject, kind, note)
                                   VALUES(?,?,?,?,?)""",
                                (name.strip(), grade.strip(), subject.strip(), "班课", note.strip()))
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("这个班级名称已经存在")
        classes = _class_options()
        month = date.today().strftime("%Y-%m")
        summary = class_month_summary(month)
        st.subheader("班级列表")
        st.dataframe(
            summary.rename(columns={"name": "班级", "grade": "年级", "subject": "科目",
                                    "completed": f"{month} 已完成节数"})
            [["班级", "年级", "科目", f"{month} 已完成节数"]],
            hide_index=True, use_container_width=True,
        )
        if not classes.empty:
            st.subheader("编辑班级")
            labels = {int(r["id"]): f"{r['name']}" + (f" · {r['grade']}" if r["grade"] else "")
                      for _, r in classes.iterrows()}
            cid = st.selectbox("选择班级", list(labels), format_func=labels.get, key="edit_class_id")
            row = query("SELECT * FROM classes WHERE id=?", (int(cid),)).iloc[0]
            with st.form(f"course_edit_class_{cid}"):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("班级名称", str(row["name"]))
                grade = c2.text_input("年级", str(row["grade"] or ""))
                subject = c3.text_input("默认科目", str(row["subject"] or ""))
                note = st.text_input("备注", str(row["note"] or ""))
                b1, b2 = st.columns(2)
                if b1.form_submit_button("保存班级信息", type="primary"):
                    try:
                        run("UPDATE classes SET name=?, grade=?, subject=?, note=? WHERE id=?",
                            (name.strip(), grade.strip(), subject.strip(), note.strip(), int(cid)))
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("这个班级名称已经存在")
                if b2.form_submit_button("删除班级"):
                    used = int(query("SELECT COUNT(*) n FROM plans WHERE class_id=?", (int(cid),)).n[0]) + int(query("SELECT COUNT(*) n FROM lessons WHERE class_id=?", (int(cid),)).n[0])
                    if int(used):
                        st.error(f"这个班级还有 {int(used)} 节课程，请先删除或调整这些课程")
                    else:
                        run("DELETE FROM classes WHERE id=?", (int(cid),))
                        st.rerun()
    with tab_one:
        with st.expander("＋ 新增一对一学生", expanded=False):
            with st.form("course_new_one"):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("学生姓名", placeholder="例如：王同学")
                grade = c2.text_input("年级", placeholder="例如：初二")
                subject = c3.text_input("默认科目", placeholder="例如：物理")
                c4, c5 = st.columns(2)
                total_hours = c4.number_input("初始购买课时", min_value=0.0, max_value=10000.0, value=0.0, step=0.5)
                contact = c5.text_input("联系方式")
                note = st.text_input("备注")
                if st.form_submit_button("创建一对一学生", type="primary"):
                    if not name.strip():
                        st.error("请填写学生姓名")
                    else:
                        try:
                            new_id = run("""INSERT INTO one_to_one_students
                                            (name, grade, subject, total_hours, status, contact, note)
                                            VALUES(?,?,?,?,?,?,?)""",
                                         (name.strip(), grade.strip(), subject.strip(), float(total_hours),
                                          "在读", contact.strip(), note.strip()))
                            # 同步建立学生档案(便于成绩录入、账号等统一使用)
                            hit = query("SELECT id FROM students WHERE TRIM(name)=? ORDER BY id LIMIT 1", (name.strip(),))
                            if hit.empty:
                                sid = run("INSERT INTO students(name, grade, school) VALUES(?,?,?)",
                                          (name.strip(), grade.strip(), ""))
                            else:
                                sid = int(hit["id"].iloc[0])
                            run("UPDATE one_to_one_students SET student_id=? WHERE id=?", (sid, new_id))
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("这个学生名称已经存在")
        summary = one_to_one_summary()
        st.subheader("一对一课时统计")
        if summary.empty:
            st.info("还没有一对一学生。")
        else:
            st.dataframe(
                summary[["name", "grade", "subject", "initial_hours", "renewed_hours", "used", "remain", "status"]].rename(
                    columns={"name": "学生", "grade": "年级", "subject": "科目",
                             "initial_hours": "初始购买", "renewed_hours": "累计续费",
                             "used": "已使用", "remain": "剩余", "status": "状态"}),
                hide_index=True, use_container_width=True,
            )
            labels = {int(r["id"]): f"{r['name']}" + (f" · {r['grade']}" if r["grade"] else "")
                      for _, r in summary.iterrows()}
            sid = st.selectbox("选择学生", list(labels), format_func=labels.get, key="edit_one_id")
            row = summary[summary["id"] == sid].iloc[0]
            with st.form(f"course_edit_one_{sid}"):
                c1, c2, c3 = st.columns(3)
                name = c1.text_input("姓名", str(row["name"]))
                grade = c2.text_input("年级", str(row["grade"] or ""))
                subject = c3.text_input("默认科目", str(row["subject"] or ""))
                c4, c5 = st.columns(2)
                hours = c4.number_input("初始购买课时", min_value=0.0, max_value=10000.0,
                                        value=float(row["initial_hours"]), step=0.5)
                status = c5.selectbox("状态", ["在读", "停课"], index=0 if row["status"] != "停课" else 1)
                contact = st.text_input("联系方式", str(row["contact"] or ""))
                note = st.text_area("备注", str(row["note"] or ""), height=70)
                b1, b2 = st.columns(2)
                if b1.form_submit_button("保存学生信息", type="primary"):
                    try:
                        run("""UPDATE one_to_one_students
                               SET name=?, grade=?, subject=?, total_hours=?, status=?, contact=?, note=?
                               WHERE id=?""",
                            (name.strip(), grade.strip(), subject.strip(), float(hours), status,
                             contact.strip(), note.strip(), int(sid)))
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("这个学生名称已经存在")
                if b2.form_submit_button("删除学生"):
                    used = int(query("SELECT COUNT(*) n FROM plans WHERE student_id=?", (int(sid),)).n[0]) + int(query("SELECT COUNT(*) n FROM lessons WHERE student_id=?", (int(sid),)).n[0]) + int(query("SELECT COUNT(*) n FROM hour_transactions WHERE student_id=?", (int(sid),)).n[0])
                    if int(used):
                        st.error(f"这个学生还有 {int(used)} 节课程，请先删除或调整这些课程")
                    else:
                        run("DELETE FROM one_to_one_students WHERE id=?", (int(sid),))
                        st.rerun()


    with tab_location:
        with st.expander("＋ 新增授课地点", expanded=False):
            with st.form("course_new_location"):
                c1, c2 = st.columns([2, 3])
                location_name = c1.text_input("地点名称", placeholder="例如：经二路、高新")
                location_note = c2.text_input("备注")
                if st.form_submit_button("创建地点", type="primary"):
                    if not location_name.strip():
                        st.error("请填写地点名称")
                    else:
                        try:
                            run(
                                """INSERT INTO teaching_locations(name, active, note, created_at)
                                   VALUES(?,1,?,?)""",
                                (
                                    location_name.strip(),
                                    location_note.strip(),
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                ),
                            )
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("这个地点已经存在")

        location_summary = location_management_summary()
        st.subheader("授课地点列表")
        if location_summary.empty:
            st.info("还没有授课地点。")
        else:
            st.dataframe(
                location_summary[["name", "class_count", "one_count", "total_lessons", "active", "note"]].rename(
                    columns={
                        "name": "地点", "class_count": "班级课节数",
                        "one_count": "一对一节数", "total_lessons": "合计节数",
                        "active": "启用", "note": "备注",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )
            labels = {int(r["id"]): str(r["name"]) for _, r in location_summary.iterrows()}
            location_id = st.selectbox("选择地点", list(labels), format_func=labels.get, key="edit_location_id")
            row = location_summary[location_summary["id"] == location_id].iloc[0]
            with st.form(f"course_edit_location_{location_id}"):
                c1, c2, c3 = st.columns([2, 2, 1])
                new_name = c1.text_input("地点名称", str(row["name"]))
                new_note = c2.text_input("备注", str(row["note"] or ""))
                active = c3.selectbox("状态", [1, 0], index=0 if int(row["active"] or 0) else 1, format_func=lambda x: "启用" if x else "停用")
                b1, b2 = st.columns(2)
                if b1.form_submit_button("保存地点信息", type="primary"):
                    try:
                        old_name = str(row["name"])
                        run(
                            "UPDATE teaching_locations SET name=?, note=?, active=? WHERE id=?",
                            (new_name.strip(), new_note.strip(), int(active), int(location_id)),
                        )
                        run("UPDATE plans SET location=? WHERE location_id=?", (new_name.strip(), int(location_id)))
                        run("UPDATE lessons SET location=? WHERE location_id=?", (new_name.strip(), int(location_id)))
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("这个地点名称已经存在")
                if b2.form_submit_button("删除地点"):
                    used = int(query("SELECT COUNT(*) n FROM plans WHERE location_id=?", (int(location_id),)).n[0]) \
                         + int(query("SELECT COUNT(*) n FROM lessons WHERE location_id=?", (int(location_id),)).n[0]) \
                         + int(query("SELECT COUNT(*) n FROM location_settlements WHERE location_id=?", (int(location_id),)).n[0])
                    if used:
                        st.error(f"这个地点还有 {used} 条关联记录，不能删除，可以改为停用")
                    else:
                        run("DELETE FROM teaching_locations WHERE id=?", (int(location_id),))
                        st.rerun()

def _clear_hour_dialog_state() -> None:
    st.session_state.pop("hour_dialog_student_id", None)


@st.dialog("课时管理", width="large", dismissible=True, on_dismiss=_clear_hour_dialog_state)
def _hour_dialog(student_id: int):
    stats = one_to_one_summary()
    row_df = stats[stats["id"] == int(student_id)]
    if row_df.empty:
        st.error("一对一学生不存在")
        if st.button("关闭", use_container_width=True):
            _clear_hour_dialog_state()
            st.rerun()
        return
    row = row_df.iloc[0]
    message = st.session_state.pop("hour_message", None)
    if message:
        st.success(message)

    st.subheader(str(row["name"]))
    metrics = st.columns(4)
    metrics[0].metric("初始购买", f"{float(row['initial_hours']):g}")
    metrics[1].metric("累计续费", f"{float(row['renewed_hours']):g}")
    metrics[2].metric("已使用", f"{float(row['used']):g}")
    metrics[3].metric("剩余", f"{float(row['remain']):g}")
    if float(row["remain"]) < 0:
        st.error("该学生课时已经超用。")
    elif float(row["remain"]) <= 3:
        st.warning("剩余课时不足，请及时续费或核对课时账目。")

    t_renew, t_adjust, t_initial, t_log = st.tabs(["新增续费", "课时调整", "修改初始购买", "课时明细"])

    with t_renew:
        with st.form(f"renew_hour_{student_id}"):
            renew_date = st.date_input("续费日期", date.today())
            renew_hours = st.number_input("本次续费课时", min_value=0.5, max_value=10000.0, value=1.0, step=0.5)
            renew_note = st.text_input("备注", placeholder="例如：秋季续费")
            submitted = st.form_submit_button("确认续费", type="primary", use_container_width=True)
        if submitted:
            try:
                add_hour_transaction(
                    int(student_id), "renewal", renew_hours, renew_date,
                    renew_note, st.session_state.get("auth_user_id"),
                )
                st.session_state["hour_message"] = f"已新增续费 {float(renew_hours):g} 课时"
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    with t_adjust:
        st.caption("正数表示增加课时，负数表示扣除、退费或纠正错误。")
        with st.form(f"adjust_hour_{student_id}"):
            adjust_date = st.date_input("调整日期", date.today())
            adjust_hours = st.number_input("课时变化", min_value=-10000.0, max_value=10000.0, value=0.0, step=0.5)
            adjust_note = st.text_input("调整原因", placeholder="例如：退费、课时校正")
            submitted = st.form_submit_button("确认调整", type="primary", use_container_width=True)
        if submitted:
            if not adjust_note.strip():
                st.error("请填写调整原因")
            else:
                try:
                    add_hour_transaction(
                        int(student_id), "adjustment", adjust_hours, adjust_date,
                        adjust_note, st.session_state.get("auth_user_id"),
                    )
                    st.session_state["hour_message"] = f"课时已调整 {float(adjust_hours):+g}"
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    with t_initial:
        st.caption("这里只修改最初购买的课时数量；后续续费请使用“新增续费”，以便保留记录。")
        with st.form(f"initial_hour_{student_id}"):
            initial_hours = st.number_input(
                "初始购买课时", min_value=0.0, max_value=10000.0,
                value=float(row["initial_hours"]), step=0.5,
            )
            submitted = st.form_submit_button("保存初始购买课时", type="primary", use_container_width=True)
        if submitted:
            try:
                update_initial_hours(int(student_id), initial_hours)
                st.session_state["hour_message"] = f"初始购买课时已改为 {float(initial_hours):g}"
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    with t_log:
        ledger = hour_ledger(int(student_id))
        if ledger.empty:
            st.info("还没有课时记录")
        else:
            st.dataframe(ledger, hide_index=True, use_container_width=True)


def _set_hour_dialog(student_id: int) -> None:
    st.session_state["hour_dialog_student_id"] = int(student_id)


def _show_hour_dialog_if_requested() -> None:
    student_id = st.session_state.get("hour_dialog_student_id")
    if student_id is not None:
        _hour_dialog(int(student_id))

def page_stats() -> None:
    _inject_css()
    st.title("授课统计")
    month = st.text_input("统计月份（YYYY-MM）", date.today().strftime("%Y-%m"))
    try:
        dt = datetime.strptime(month.strip(), "%Y-%m").date().replace(day=1)
    except ValueError:
        st.error("月份格式应为 YYYY-MM")
        return

    class_stats = class_month_summary(dt.strftime("%Y-%m"))
    total_class = int(class_stats["completed"].sum()) if not class_stats.empty else 0
    one_stats = one_to_one_summary()
    month_courses = query(
        """SELECT target_type, status, COUNT(*) n FROM plans
           WHERE substr(plan_date,1,7)=? GROUP BY target_type, status""",
        (dt.strftime("%Y-%m"),),
    )
    metrics = st.columns(4)
    metrics[0].metric("班级课本月已完成", total_class)
    metrics[1].metric("一对一累计已使用", int(one_stats["used"].sum()) if not one_stats.empty else 0)
    metrics[2].metric("本月课程记录", int(month_courses["n"].sum()) if not month_courses.empty else 0)
    metrics[3].metric("当前剩余总课时", f"{one_stats['remain'].sum():g}" if not one_stats.empty else "0")

    st.subheader(f"{dt.year}年{dt.month}月班级实际上课")
    if class_stats.empty:
        st.info("还没有班级数据。")
    else:
        st.dataframe(
            class_stats.rename(
                columns={"name": "班级", "grade": "年级", "subject": "科目", "completed": "已完成节数"}
            )[["班级", "年级", "科目", "已完成节数"]],
            hide_index=True,
            use_container_width=True,
        )

    st.subheader("按授课地点结算")
    location_stats = location_settlement_summary(dt.strftime("%Y-%m"))
    if location_stats.empty:
        st.info("本月还没有已完成课程。")
    else:
        location_message = st.session_state.pop("location_message", None)
        if location_message:
            st.success(location_message)
        display = location_stats.copy()
        display["合计"] = display["class_count"] + display["one_count"]
        st.dataframe(
            display[["location_name", "class_count", "one_count", "合计", "结算状态", "settled_at", "班级变化", "一对一变化"]].rename(
                columns={
                    "location_name": "地点",
                    "class_count": "班级课",
                    "one_count": "一对一课",
                    "settled_at": "结算时间",
                    "班级变化": "班级差异",
                    "一对一变化": "一对一差异",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        changed = display[display["结算状态"] == "结算后有变动"]
        if not changed.empty:
            st.warning("有地点在结算后发生了课程数量变化，请核对差异后重新结算。")

        manageable = display[display["location_id"] > 0]
        if not manageable.empty:
            labels = {int(r["location_id"]): str(r["location_name"]) for _, r in manageable.iterrows()}
            c1, c2 = st.columns([4, 1])
            selected_location = c1.selectbox(
                "选择地点结算", list(labels), format_func=labels.get, key="settlement_location_id"
            )
            selected_row = manageable[manageable["location_id"] == selected_location].iloc[0]
            settlement_note = c2.text_input("结算备注", key=f"settlement_note_{selected_location}")
            b1, b2 = st.columns(2)
            if b1.button("标记本月已结算", type="primary", use_container_width=True):
                try:
                    mark_location_settled(dt.strftime("%Y-%m"), int(selected_location), settlement_note)
                    st.session_state["location_message"] = f"已标记 {selected_row['location_name']} 本月为已结算"
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            if b2.button("撤销结算", disabled=pd.isna(selected_row["settled_at"]), use_container_width=True):
                unmark_location_settled(dt.strftime("%Y-%m"), int(selected_location))
                st.session_state["location_message"] = f"已撤销 {selected_row['location_name']} 的结算状态"
                st.rerun()

    st.subheader("一对一课时包")
    if one_stats.empty:
        st.info("还没有一对一学生。")
    else:
        show = one_stats.copy()
        show["共购买"] = show["initial_hours"] + show["renewed_hours"]
        show["课时状态"] = show["remain"].apply(
            lambda value: "超用" if float(value) < 0
            else "已用完" if float(value) == 0
            else "课时不足" if float(value) <= 3
            else "正常"
        )
        st.dataframe(
            show[
                ["name", "subject", "initial_hours", "renewed_hours", "共购买",
                 "adjusted_hours", "used", "remain", "课时状态"]
            ].rename(
                columns={
                    "name": "学生",
                    "subject": "科目",
                    "initial_hours": "初始购买",
                    "renewed_hours": "累计续费",
                    "adjusted_hours": "调整",
                    "used": "已使用",
                    "remain": "剩余",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        labels = {
            int(r["id"]): f"{r['name']}" + (f" · {r['subject']}" if r["subject"] else "")
            for _, r in one_stats.iterrows()
        }
        c1, c2 = st.columns([4, 1])
        selected = c1.selectbox("选择学生管理课时", list(labels), format_func=labels.get, key="hour_student_id")
        if c2.button("课时管理", type="primary", use_container_width=True):
            _set_hour_dialog(int(selected))
            st.rerun()

    with st.expander("查看本月课程明细"):
        detail = _courses_between(dt, _month_bounds(dt)[1])
        if detail.empty:
            st.info("本月还没有课程。")
        else:
            st.dataframe(
                detail[["plan_date", "start", "end", "target_name", "target_type", "subject",
                        "location", "status", "note"]].rename(
                    columns={"plan_date": "日期", "start": "开始", "end": "结束",
                             "target_name": "班级/学生", "target_type": "类型", "subject": "科目",
                             "location": "地点", "status": "状态", "note": "备注"}),
                hide_index=True, use_container_width=True,
            )

    _show_hour_dialog_if_requested()
