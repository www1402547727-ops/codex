"""演示机构与平台使用摘要。

演示数据全部是虚构的，可以随时重置。使用摘要只统计机构、老师、课程和
考试数量，不读取学生姓名、成绩、错题或试卷内容。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import cloud_store
import course_management as cm
import platform_store
import teacher_registry
import user_management as um

DEMO_SLUG = "demo"
DEMO_ORG_NAME = "演示机构"
DEMO_TEACHER_USERNAME = "demo_teacher"
DEMO_STUDENT_USERNAME = "demo_student"

_init_schema = None

_COUNT_COURSES_SQL = "SELECT COUNT(*) FROM plans"
_COUNT_EXAMS_SQL = "SELECT COUNT(*) FROM exams"


def configure(init_schema) -> None:
    global _init_schema
    _init_schema = init_schema


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _teacher_db(slug: str) -> Path:
    data_dir = cloud_store.get_data_dir(teacher_registry.base_dir(), slug)
    (data_dir / "photos").mkdir(parents=True, exist_ok=True)
    db = data_dir / "scores.db"
    try:
        cloud_store.prepare_database(db, cloud_store.db_object_path(slug))
    except Exception:
        pass
    return db


def _require_platform_admin(actor_slug: str) -> bool:
    return bool(
        (teacher_registry.teacher_entry(actor_slug) or {}).get("is_admin")
    )


def find_demo_org() -> dict | None:
    for org in platform_store.list_organizations():
        if str(org.get("name") or "").strip() != DEMO_ORG_NAME:
            continue
        members = platform_store.list_org_users(int(org["id"]))
        if any(
            (cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG)
            == DEMO_SLUG
            for member in members
        ):
            return org
    return None


def _seed_demo_data(db: Path, org_id: int, student_password: str) -> None:
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    now = _now()
    conn = sqlite3.connect(db)
    try:
        students = [
            ("示例学生甲", "初三", "示例学校"),
            ("示例学生乙", "初三", "示例学校"),
            ("示例学生丙", "初三", "示例学校"),
            ("示例学生丁", "初二", "示例学校"),
            ("示例学生戊", "初三", "示例学校"),
            ("示例学生己", "初二", "示例学校"),
        ]
        student_ids: dict[str, int] = {}
        for name, grade, school in students:
            cur = conn.execute(
                """INSERT INTO students(name, grade, school, status, note)
                   VALUES(?,?,?,?,?)""",
                (name, grade, school, "在读", "演示数据"),
            )
            student_ids[name] = int(cur.lastrowid)

        conn.execute(
            """INSERT INTO classes(name, grade, subject, total_hours, note, kind)
               VALUES(?,?,?,?,?,?)""",
            ("示例初三数学班", "初三", "数学", 0, "演示班级", "班课"),
        )
        math_class_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """INSERT INTO classes(name, grade, subject, total_hours, note, kind)
               VALUES(?,?,?,?,?,?)""",
            ("示例初二物理班", "初二", "物理", 0, "演示班级", "班课"),
        )
        physics_class_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for name in ("示例学生甲", "示例学生乙", "示例学生丙"):
            conn.execute(
                "INSERT INTO class_members(class_id, student_id) VALUES(?,?)",
                (math_class_id, student_ids[name]),
            )
        for name in ("示例学生丁", "示例学生己"):
            conn.execute(
                "INSERT INTO class_members(class_id, student_id) VALUES(?,?)",
                (physics_class_id, student_ids[name]),
            )

        conn.execute(
            """INSERT INTO one_to_one_students(
                   name, grade, subject, total_hours, status, contact, note, student_id
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "示例学生戊",
                "初三",
                "数学",
                20,
                "在读",
                "",
                "演示一对一",
                student_ids["示例学生戊"],
            ),
        )
        one_math_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """INSERT INTO one_to_one_students(
                   name, grade, subject, total_hours, status, contact, note, student_id
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "示例学生己",
                "初二",
                "物理",
                16,
                "在读",
                "",
                "演示一对一",
                student_ids["示例学生己"],
            ),
        )
        one_physics_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        conn.execute(
            """INSERT INTO teaching_locations(name, active, note, created_at)
               VALUES(?,?,?,?)""",
            ("示例校区A", 1, "演示地点", now),
        )
        location_a = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """INSERT INTO teaching_locations(name, active, note, created_at)
               VALUES(?,?,?,?)""",
            ("示例校区B", 1, "演示地点", now),
        )
        location_b = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        plans = [
            (
                math_class_id,
                None,
                "班级",
                str(today),
                "16:00",
                "17:30",
                "数学",
                "示例校区A",
                location_a,
                "待上课",
                "演示：今天班课",
            ),
            (
                None,
                one_math_id,
                "一对一",
                str(today),
                "19:00",
                "20:30",
                "数学",
                "示例校区B",
                location_b,
                "待上课",
                "演示：今天一对一",
            ),
            (
                physics_class_id,
                None,
                "班级",
                str(tomorrow),
                "17:00",
                "18:30",
                "物理",
                "示例校区A",
                location_a,
                "待上课",
                "演示：明天班课",
            ),
            (
                math_class_id,
                None,
                "班级",
                str(yesterday),
                "16:00",
                "17:30",
                "数学",
                "示例校区A",
                location_a,
                "已上课",
                "演示：已完成班课",
            ),
            (
                None,
                one_physics_id,
                "一对一",
                str(yesterday),
                "19:00",
                "20:00",
                "物理",
                "示例校区B",
                location_b,
                "已上课",
                "演示：已完成一对一",
            ),
        ]
        for (
            class_id,
            student_id,
            target_type,
            plan_date,
            start,
            end,
            subject,
            location,
            location_id,
            status,
            note,
        ) in plans:
            cur = conn.execute(
                """INSERT INTO plans(
                       class_id, student_id, target_type, plan_date, start, end,
                       subject, location, location_id, status, note, completed_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    class_id,
                    student_id,
                    target_type,
                    plan_date,
                    start,
                    end,
                    subject,
                    location,
                    location_id,
                    status,
                    note,
                    now if status == "已上课" else "",
                    now,
                ),
            )
            plan_id = int(cur.lastrowid)
            if status == "已上课":
                conn.execute(
                    """INSERT INTO lessons(
                           class_id, student_id, target_type, lesson_date, start, end,
                           hours, content, note, plan_id, subject, location, location_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        class_id,
                        student_id,
                        target_type,
                        plan_date,
                        start,
                        end,
                        1,
                        "演示课程已完成",
                        "演示数据",
                        plan_id,
                        subject,
                        location,
                        location_id,
                    ),
                )

        exams = [
            (
                "示例学校10月月考",
                str(today - timedelta(days=7)),
                "数学",
                120,
                "月考",
                "示例学校",
                "初三",
            ),
            (
                "示例学校10月月考",
                str(today - timedelta(days=7)),
                "物理",
                100,
                "月考",
                "示例学校",
                "初二",
            ),
        ]
        exam_ids: list[int] = []
        for name, exam_date, subject, full_score, category, school, grade in exams:
            cur = conn.execute(
                """INSERT INTO exams(
                       name, exam_date, subject, full_score, class_id, category,
                       school, grade, paper
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (name, exam_date, subject, full_score, None, category, school, grade, ""),
            )
            exam_ids.append(int(cur.lastrowid))

        math_scores = {
            "示例学生甲": 108,
            "示例学生乙": 96,
            "示例学生丙": 84,
            "示例学生戊": 112,
        }
        for index, (name, score) in enumerate(math_scores.items()):
            conn.execute(
                """INSERT INTO scores(
                       student_id, exam_id, score, wrong_qs, reasons, knowledge,
                       paper, sheet, note, status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    student_ids[name],
                    exam_ids[0],
                    score,
                    "18,22" if index % 2 == 0 else "12",
                    "计算不细心" if index % 2 == 0 else "审题不完整",
                    "二次函数" if index % 2 == 0 else "几何证明",
                    "",
                    "",
                    "演示成绩",
                    "已录入",
                ),
            )
        for name, score in {"示例学生丁": 88, "示例学生己": 91}.items():
            conn.execute(
                """INSERT INTO scores(
                       student_id, exam_id, score, wrong_qs, reasons, knowledge,
                       paper, sheet, note, status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    student_ids[name],
                    exam_ids[1],
                    score,
                    "21",
                    "公式记忆不牢",
                    "压强",
                    "",
                    "",
                    "演示成绩",
                    "已录入",
                ),
            )

        conn.execute(
            """UPDATE users
               SET username=?, must_change_password=0, active=1, note=?, org_id=?
               WHERE role='teacher'""",
            (DEMO_TEACHER_USERNAME, "演示老师账号", int(org_id)),
        )
        teacher_id = int(
            conn.execute(
                "SELECT id FROM users WHERE role='teacher' LIMIT 1"
            ).fetchone()[0]
        )
        conn.commit()
    finally:
        conn.close()

    platform_store.accept_teacher_notice(
        DEMO_TEACHER_USERNAME,
        version=um.NOTICE_VERSION,
        accepted_at=now,
    )

    ok, err = um.create_account_in(
        db,
        DEMO_STUDENT_USERNAME,
        student_password,
        um.ROLE_STUDENT,
        student_id=student_ids["示例学生甲"],
        must_change_password=False,
        note="演示学生账号",
        org_id=int(org_id),
        guardian_consent=True,
        actor_user_id=teacher_id,
        actor_username=DEMO_TEACHER_USERNAME,
    )
    if not ok:
        raise RuntimeError(err)


def reset_demo(actor_slug: str) -> tuple[bool, str, dict | None]:
    """删除并重新生成演示机构；账号密码随机生成并在本次返回。"""
    if not _require_platform_admin(actor_slug):
        return False, "只有平台管理员可以重置演示机构", None
    if platform_store.is_impersonating():
        return False, "代入查看期间不能重置演示机构", None
    if _init_schema is None:
        return False, "演示模块还没有完成初始化", None

    for username in (DEMO_TEACHER_USERNAME, DEMO_STUDENT_USERNAME):
        owner = teacher_registry.username_owner(username)
        if owner is not None and owner != DEMO_SLUG:
            return False, f"登录名 {username} 已被占用，无法生成演示账号", None

    existing = find_demo_org()
    if existing is not None:
        from admin_console import delete_organization

        ok, err = delete_organization(
            int(existing["id"]),
            DEMO_ORG_NAME,
            actor_slug=actor_slug,
            # 演示机构本来就是虚构数据，重置动作本身已由平台管理员明确触发。
            consent_records_confirmed=True,
        )
        if not ok:
            return False, err, None
    else:
        member = platform_store.org_user_for_slug(DEMO_SLUG)
        entry = teacher_registry.teacher_entry(DEMO_SLUG)
        if member is not None or entry is not None:
            return False, "演示标识 demo 已被其他数据占用，请先人工确认", None

    org_id, err = platform_store.create_organization(
        DEMO_ORG_NAME,
        kind="机构",
        status="体验",
        note="可随时重置的虚构演示数据",
        public_name_ok=False,
    )
    if org_id is None:
        return False, err, None

    teacher_password = um.generate_password()
    student_password = um.generate_password()
    object_path = cloud_store.db_object_path(DEMO_SLUG)
    db = _teacher_db(DEMO_SLUG)
    try:
        _init_schema(db, object_path)
        cm.init_course_db(db, object_path=object_path)
        um.init_user_db(
            db,
            default_teacher_password=teacher_password,
            object_path=object_path,
            org_id=int(org_id),
        )
        _seed_demo_data(db, int(org_id), student_password)
        cloud_store.save_database(db, object_path)
    except Exception as exc:
        return False, f"生成演示数据失败：{exc}", None

    ok, add_err = teacher_registry.add_teacher(
        DEMO_TEACHER_USERNAME,
        "演示老师",
        DEMO_SLUG,
        note="演示机构",
    )
    if not ok:
        return False, add_err, None
    member_ok, member_err = platform_store.add_org_user(
        int(org_id), DEMO_SLUG, platform_store.ROLE_TEACHER
    )
    if not member_ok:
        return False, member_err, None
    teacher_registry.note_usernames(DEMO_SLUG, db)
    try:
        platform_store.record_current_audit(
            "reset_demo",
            target_type="organization",
            target_id=int(org_id),
            detail="demo",
            org_id=int(org_id),
        )
    except Exception:
        pass
    return True, "", {
        "org_id": int(org_id),
        "teacher_username": DEMO_TEACHER_USERNAME,
        "teacher_password": teacher_password,
        "student_username": DEMO_STUDENT_USERNAME,
        "student_password": student_password,
    }


def _summary_csv_bytes(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["统计范围", "机构数", "老师数", "累计课程数", "考试数"])
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def build_usage_summary(
    actor_slug: str,
    *,
    db_resolver=None,
) -> dict:
    """生成不含学生内容的平台使用摘要。"""
    if not _require_platform_admin(actor_slug):
        raise PermissionError("只有平台管理员可以导出使用摘要")
    db_resolver = db_resolver or _teacher_db
    organizations = platform_store.list_organizations()
    public_rows: list[list[object]] = []
    private_orgs = 0
    private_teachers = 0
    private_courses = 0
    private_exams = 0
    total_teachers = 0
    total_courses = 0
    total_exams = 0

    for org in organizations:
        members = platform_store.list_org_users(int(org["id"]))
        org_courses = 0
        org_exams = 0
        for member in members:
            slug = cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG
            db = Path(db_resolver(slug))
            if not db.is_file():
                continue
            conn = sqlite3.connect(
                db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
            )
            try:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "plans" in tables:
                    org_courses += int(
                        conn.execute(_COUNT_COURSES_SQL).fetchone()[0] or 0
                    )
                if "exams" in tables:
                    org_exams += int(
                        conn.execute(_COUNT_EXAMS_SQL).fetchone()[0] or 0
                    )
            finally:
                conn.close()

        total_teachers += len(members)
        total_courses += org_courses
        total_exams += org_exams
        if bool(org.get("public_name_ok")):
            public_rows.append(
                [
                    str(org.get("name") or "未命名机构"),
                    1,
                    len(members),
                    org_courses,
                    org_exams,
                ]
            )
        else:
            private_orgs += 1
            private_teachers += len(members)
            private_courses += org_courses
            private_exams += org_exams

    rows = list(public_rows)
    if private_orgs:
        rows.append(
            [
                "其他未公开机构（合并统计）",
                private_orgs,
                private_teachers,
                private_courses,
                private_exams,
            ]
        )
    rows.append(
        [
            "合计",
            len(organizations),
            total_teachers,
            total_courses,
            total_exams,
        ]
    )
    return {
        "generated_at": _now(),
        "organization_count": len(organizations),
        "teacher_count": total_teachers,
        "course_count": total_courses,
        "exam_count": total_exams,
        "rows": rows,
        "csv": _summary_csv_bytes(rows),
    }


def export_usage_summary(actor_slug: str, *, db_resolver=None) -> bytes:
    summary = build_usage_summary(actor_slug, db_resolver=db_resolver)
    platform_store.record_current_audit(
        "export",
        target_type="platform_summary",
        target_id="usage",
        detail="usage_summary",
    )
    return summary["csv"]
