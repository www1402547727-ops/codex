from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import string
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import cloud_store


DB_PATH = Path(__file__).parent / "data" / "scores.db"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"
ROLE_LABELS = {ROLE_TEACHER: "老师", ROLE_STUDENT: "学生"}
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_LOCK_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 5
PASSWORD_ITERATIONS = 310_000
PASSWORD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


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


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            _unb64(salt_text),
            int(iterations),
        )
        return hmac.compare_digest(digest, _unb64(digest_text))
    except Exception:
        return False


def generate_password(length: int = 10) -> str:
    raw = "".join(secrets.choice(PASSWORD_CHARS) for _ in range(length))
    return f"{raw[:5]}-{raw[5:]}"


def init_user_db(db_path: str | Path | None = None, default_teacher_password: str | None = None) -> None:
    if db_path is not None:
        configure(db_path)
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_student_unique
                ON users(student_id) WHERE student_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_users_role ON users(role, active);
            CREATE TABLE IF NOT EXISTS user_login_attempts(
                id INTEGER PRIMARY KEY,
                username TEXT COLLATE NOCASE NOT NULL,
                attempted_at REAL NOT NULL,
                success INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempt_user_time
                ON user_login_attempts(username, attempted_at);
            CREATE TABLE IF NOT EXISTS user_audit_log(
                id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                action TEXT NOT NULL,
                detail TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

        columns = {row[1] for row in conn.execute("PRAGMA table_info(one_to_one_students)")}
        if "student_id" not in columns:
            conn.execute("ALTER TABLE one_to_one_students ADD COLUMN student_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_one_to_one_student_link ON one_to_one_students(student_id)"
        )

        # Link existing one-to-one students to the general student table.
        rows = conn.execute(
            """SELECT id, name, grade FROM one_to_one_students
               WHERE student_id IS NULL ORDER BY id"""
        ).fetchall()
        for row in rows:
            student = conn.execute(
                "SELECT id FROM students WHERE name=? ORDER BY id LIMIT 1",
                (row["name"],),
            ).fetchone()
            if student:
                student_id = int(student["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO students(name, grade, status) VALUES(?,?,?)",
                    (row["name"], row["grade"] or "", "在读"),
                )
                student_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE one_to_one_students SET student_id=? WHERE id=?",
                (student_id, int(row["id"])),
            )

        teacher_count = int(
            conn.execute("SELECT COUNT(*) FROM users WHERE role=?", (ROLE_TEACHER,)).fetchone()[0]
        )
        if teacher_count == 0:
            initial_password = (
                default_teacher_password
                or os.environ.get("SCORE_TRACKER_PASSWORD")
                or "0307"
            )
            conn.execute(
                """INSERT INTO users
                   (username, password_hash, role, active, must_change_password, created_at, note)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    "teacher",
                    hash_password(initial_password),
                    ROLE_TEACHER,
                    1,
                    0,
                    _now(),
                    "首次启动自动创建",
                ),
            )
        conn.commit()
        cloud_store.save_database(DB_PATH)
    finally:
        conn.close()


def _audit(username: str, action: str, detail: str = "") -> None:
    run(
        "INSERT INTO user_audit_log(username, action, detail, created_at) VALUES(?,?,?,?)",
        (username or "", action, detail, _now()),
    )


def _record_login_attempt(username: str, success: bool) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO user_login_attempts(username, attempted_at, success) VALUES(?,?,?)",
            (username or "", time.time(), 1 if success else 0),
        )
        conn.execute(
            "DELETE FROM user_login_attempts WHERE attempted_at<?",
            (time.time() - LOGIN_WINDOW_SECONDS * 3,),
        )
        conn.commit()
    finally:
        conn.close()


def login_lock_remaining(username: str) -> int:
    now = time.time()
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) n, COALESCE(MAX(attempted_at),0) last_at
               FROM user_login_attempts
               WHERE username=? AND success=0 AND attempted_at>=?""",
            (str(username or "").strip(), now - LOGIN_WINDOW_SECONDS),
        ).fetchone()
        count, last_at = int(row["n"]), float(row["last_at"])
        if count >= LOGIN_MAX_FAILURES:
            return max(0, int(last_at + LOGIN_LOCK_SECONDS - now))
        return 0
    finally:
        conn.close()


def _clear_login_failures(username: str) -> None:
    run(
        "DELETE FROM user_login_attempts WHERE username=? AND success=0",
        (str(username or "").strip(),),
    )


def _get_user_by_username(username: str):
    df = query(
        """SELECT u.*, s.name AS student_name
           FROM users u LEFT JOIN students s ON s.id=u.student_id
           WHERE u.username=? LIMIT 1""",
        (str(username or "").strip(),),
    )
    return None if df.empty else df.iloc[0]


def _get_user(user_id: int):
    df = query(
        """SELECT u.*, s.name AS student_name
           FROM users u LEFT JOIN students s ON s.id=u.student_id
           WHERE u.id=? LIMIT 1""",
        (int(user_id),),
    )
    return None if df.empty else df.iloc[0]


def authenticate(username: str, password: str) -> tuple[dict | None, str]:
    username = str(username or "").strip()
    if not username or not password:
        return None, "请输入账号和密码"
    remaining = login_lock_remaining(username)
    if remaining:
        return None, f"登录失败次数过多，请在 {max(1, (remaining + 59) // 60)} 分钟后重试"

    row = _get_user_by_username(username)
    if row is None or not verify_password(str(password), str(row["password_hash"])):
        _record_login_attempt(username, False)
        _audit(username, "login_failed")
        time.sleep(0.5)
        remaining = login_lock_remaining(username)
        if remaining:
            return None, f"账号或密码错误。登录已暂时锁定 {max(1, (remaining + 59) // 60)} 分钟"
        return None, "账号或密码错误"
    if int(row["active"] or 0) != 1:
        _audit(username, "login_blocked_inactive")
        return None, "该账号已停用，请联系老师"

    _record_login_attempt(username, True)
    _clear_login_failures(username)
    run("UPDATE users SET last_login=? WHERE id=?", (_now(), int(row["id"])))
    _audit(username, "login_success")
    return _get_user(int(row["id"])).to_dict(), ""


def current_user():
    user_id = st.session_state.get("auth_user_id")
    if not user_id:
        return None
    row = _get_user(int(user_id))
    if row is None or int(row["active"] or 0) != 1:
        logout()
        return None
    return row.to_dict()


def logout() -> None:
    for key in ("auth_user_id", "auth_username", "auth_role", "auth_student_id", "page"):
        st.session_state.pop(key, None)
    st.rerun()


def render_login() -> None:
    st.markdown(
        """
        <style>
        .block-container {max-width: 520px; padding-top: 8vh;}
        div[data-testid="stForm"] {border:1px solid #E3E8F1;border-radius:16px;padding:24px;background:#fff;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("教务工作台")
    with st.form("login_form"):
        username = st.text_input("账号", placeholder="老师账号或学生账号")
        password = st.text_input("密码", type="password")
        ok = st.form_submit_button("登录", type="primary", use_container_width=True)
    if ok:
        user, error = authenticate(username, password)
        if user:
            st.session_state["auth_user_id"] = int(user["id"])
            st.session_state["auth_username"] = str(user["username"])
            st.session_state["auth_role"] = str(user["role"])
            st.session_state.pop("page", None)
            st.session_state["auth_student_id"] = (
                int(user["student_id"]) if pd.notna(user.get("student_id")) else None
            )
            st.rerun()
        st.error(error)

def _require_teacher():
    user = current_user()
    if user is None:
        st.stop()
    if user["role"] != ROLE_TEACHER:
        st.error("没有权限访问老师功能")
        st.stop()
    return user


def render_sidebar_identity() -> None:
    user = current_user()
    if user is None:
        st.stop()
    with st.sidebar:
        st.divider()
        st.caption(f"{ROLE_LABELS.get(user['role'], user['role'])}：{user['username']}")
        if user["role"] == ROLE_STUDENT and user.get("student_name"):
            st.caption(f"学生：{user['student_name']}")
        if st.button("退出登录", use_container_width=True):
            logout()


def page_account(force_change: bool = False) -> None:
    user = current_user()
    if user is None:
        st.stop()
    st.title("账号")
    c1, c2 = st.columns(2)
    c1.text_input("账号", value=str(user["username"]), disabled=True)
    c2.text_input("身份", value=ROLE_LABELS.get(user["role"], user["role"]), disabled=True)
    st.caption(f"最近登录：{user.get('last_login') or '—'}")

    if force_change or int(user.get("must_change_password") or 0) == 1:
        st.warning("这是初始密码，为了账号安全，请先修改密码。")

    with st.form("change_password"):
        current = st.text_input("当前密码", type="password")
        new1 = st.text_input("新密码", type="password")
        new2 = st.text_input("确认新密码", type="password")
        ok = st.form_submit_button("修改密码", type="primary", use_container_width=True)
    if ok:
        if not verify_password(current, str(user["password_hash"])):
            st.error("当前密码不正确")
        elif len(new1) < 8:
            st.error("新密码至少需要 8 位")
        elif new1 != new2:
            st.error("两次输入的新密码不一致")
        elif verify_password(new1, str(user["password_hash"])):
            st.error("新密码不能与当前密码相同")
        else:
            run(
                "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                (hash_password(new1), int(user["id"])),
            )
            _audit(str(user["username"]), "change_password")
            st.success("密码已修改，请使用新密码重新登录")
            logout()

    st.divider()
    st.subheader("关于数据权限")
    if user["role"] == ROLE_TEACHER:
        st.write("老师账号可以管理全部课程、学生、成绩和账号。")
    else:
        st.write("学生账号只能查看自己的课程与成绩，不能查看其他学生资料。")


def _create_student_account(student_id: int, username: str, password: str) -> tuple[bool, str]:
    username = username.strip()
    if not username:
        return False, "请填写账号"
    if len(username) < 3:
        return False, "账号至少需要 3 位"
    if len(password) < 8:
        return False, "密码至少需要 8 位"
    if query("SELECT id FROM students WHERE id=?", (int(student_id),)).empty:
        return False, "学生不存在"
    if not query("SELECT id FROM users WHERE student_id=?", (int(student_id),)).empty:
        return False, "这个学生已经有账号"
    if not query("SELECT id FROM users WHERE username=?", (username,)).empty:
        return False, "这个账号名已经存在"
    run(
        """INSERT INTO users
           (username, password_hash, role, student_id, active, must_change_password, created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (username, hash_password(password), ROLE_STUDENT, int(student_id), 1, 1, _now()),
    )
    _audit(username, "student_account_created", f"student_id={int(student_id)}")
    return True, ""


def page_user_admin() -> None:
    user = _require_teacher()
    st.title("账号管理")
    st.caption("学生账号只能由老师创建；学生不能自行注册或查看其他学生数据。")

    created = st.session_state.pop("account_created", None)
    reset = st.session_state.pop("account_reset", None)
    if created:
        st.success(f"学生账号已创建：{created['username']}　初始密码：{created['password']}")
        st.warning("请把初始密码发给学生，学生首次登录后必须修改密码。")
    if reset:
        st.success(f"账号 {reset['username']} 的新密码：{reset['password']}")

    students = query(
        """SELECT s.id, s.name, s.grade, s.status
           FROM students s LEFT JOIN users u ON u.student_id=s.id
           WHERE u.id IS NULL AND COALESCE(s.status,'在读')='在读'
           ORDER BY s.name"""
    )
    with st.expander("＋ 创建学生账号", expanded=False):
        if students.empty:
            st.info("没有尚未开户的在读学生。请先到「学生」页面创建学生。")
        else:
            labels = {
                int(r["id"]): f"{r['name']}" + (f" · {r['grade']}" if r["grade"] else "")
                for _, r in students.iterrows()
            }
            with st.form("create_student_user"):
                student_id = st.selectbox("绑定学生", list(labels), format_func=labels.get)
                username = st.text_input("账号", placeholder="例如：wx01 或学号")
                password = st.text_input("初始密码", placeholder="留空自动生成")
                submitted = st.form_submit_button("创建账号", type="primary")
            if submitted:
                initial = password.strip() or generate_password()
                ok, error = _create_student_account(int(student_id), username, initial)
                if ok:
                    st.session_state["account_created"] = {
                        "username": username.strip(),
                        "password": initial,
                    }
                    st.rerun()
                else:
                    st.error(error)

    st.subheader("账号列表")
    users = query(
        """SELECT u.id, u.username, u.role, u.active, u.must_change_password,
                  u.created_at, u.last_login, s.name AS student_name
           FROM users u LEFT JOIN students s ON s.id=u.student_id
           ORDER BY CASE u.role WHEN 'teacher' THEN 0 ELSE 1 END, u.username"""
    )
    if users.empty:
        st.info("还没有账号")
        return
    show = users.copy()
    show["角色"] = show["role"].map(ROLE_LABELS).fillna(show["role"])
    show["状态"] = show["active"].map({1: "启用", 0: "停用"})
    show["绑定学生"] = show["student_name"].fillna("—")
    show["首次登录改密"] = show["must_change_password"].map({1: "需要", 0: "不需要"})
    st.dataframe(
        show[["username", "角色", "绑定学生", "状态", "首次登录改密", "last_login", "created_at"]].rename(
            columns={
                "username": "账号",
                "last_login": "最后登录",
                "created_at": "创建时间",
            }
        ),
        hide_index=True,
        use_container_width=True,
    )

    student_users = users[users["role"] == ROLE_STUDENT].copy()
    if student_users.empty:
        return
    st.subheader("管理学生账号")
    labels = {
        int(r["id"]): f"{r['username']} · {r['student_name'] or '未绑定'}"
        for _, r in student_users.iterrows()
    }
    selected = st.selectbox("选择学生账号", list(labels), format_func=labels.get, key="admin_user_id")
    row = student_users[student_users["id"] == selected].iloc[0]
    c1, c2 = st.columns(2)
    if c1.button("重置密码", use_container_width=True):
        new_password = generate_password()
        run(
            "UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
            (hash_password(new_password), int(selected)),
        )
        _audit(str(user["username"]), "reset_student_password", f"user_id={int(selected)}")
        st.session_state["account_reset"] = {
            "username": str(row["username"]),
            "password": new_password,
        }
        st.rerun()
    target_active = 0 if int(row["active"] or 0) == 1 else 1
    button_text = "停用账号" if target_active == 0 else "启用账号"
    if c2.button(button_text, use_container_width=True):
        run("UPDATE users SET active=? WHERE id=?", (target_active, int(selected)))
        _audit(
            str(user["username"]),
            "enable_student_account" if target_active else "disable_student_account",
            f"user_id={int(selected)}",
        )
        st.rerun()

STATUS_ICON = {
    "待上课": "🔵",
    "已上课": "✅",
    "请假": "🟡",
    "取消": "⚫",
    "调课": "↪️",
}


def _student_courses(student_id: int) -> pd.DataFrame:
    return query(
        """SELECT p.id, p.plan_date, p.start, p.end, p.target_type, p.status,
                  COALESCE(NULLIF(p.subject,''), c.subject, os.subject, '') AS subject,
                  COALESCE(p.location,'') AS location,
                  CASE WHEN p.target_type='一对一' THEN '一对一'
                       ELSE COALESCE(c.name, '班级课') END AS target_name
           FROM plans p
           LEFT JOIN classes c ON p.target_type='班级' AND c.id=p.class_id
           LEFT JOIN one_to_one_students os ON p.target_type='一对一' AND os.id=p.student_id
           WHERE p.status!='调课'
             AND (
                 (p.target_type='班级' AND p.class_id IN (
                     SELECT class_id FROM class_members WHERE student_id=?
                 ))
                 OR
                 (p.target_type='一对一' AND p.student_id IN (
                     SELECT id FROM one_to_one_students WHERE student_id=?
                 ))
             )
           ORDER BY p.plan_date, p.start""",
        (int(student_id), int(student_id)),
    )


def _render_student_course_card(row) -> None:
    status = str(row["status"])
    with st.container(border=True):
        c1, c2 = st.columns([3, 5])
        c1.markdown(f"### {row['start']}–{row['end']}")
        c1.caption(str(row["plan_date"]))
        tag = "一对一" if row["target_type"] == "一对一" else "班级课"
        c2.markdown(f"**{row['target_name']}**")
        c2.write(f"{tag} · {row['subject'] or '未填写科目'}")
        if row["location"]:
            c2.caption(f"📍 {row['location']}")
        c2.caption(f"{STATUS_ICON.get(status, '')} {status}")


def page_student_courses() -> None:
    user = current_user()
    if user is None or user["role"] != ROLE_STUDENT or not user.get("student_id"):
        st.error("学生账号未正确绑定，请联系老师")
        st.stop()
    st.title("我的课程")
    df = _student_courses(int(user["student_id"]))
    if df.empty:
        st.info("暂时没有课程记录。")
        return
    today = str(date.today())
    scope = st.segmented_control(
        "查看范围",
        ["即将上课", "历史课程", "全部"],
        default="即将上课",
        key="student_course_scope",
    )
    if scope == "即将上课":
        df = df[df["plan_date"] >= today]
    elif scope == "历史课程":
        df = df[df["plan_date"] < today]
    if df.empty:
        st.info("这个范围内没有课程。")
        return
    if scope == "历史课程":
        df = df.sort_values(["plan_date", "start"], ascending=False)
    for _, row in df.iterrows():
        _render_student_course_card(row)


def page_student_scores() -> None:
    user = current_user()
    if user is None or user["role"] != ROLE_STUDENT or not user.get("student_id"):
        st.error("学生账号未正确绑定，请联系老师")
        st.stop()
    st.title("我的成绩")
    df = query(
        """SELECT sc.id, sc.score, sc.wrong_qs, sc.knowledge,
                  e.name AS exam_name, e.exam_date, e.subject, e.full_score
           FROM scores sc
           JOIN exams e ON e.id=sc.exam_id
           WHERE sc.student_id=?
           ORDER BY e.exam_date DESC, e.id DESC""",
        (int(user["student_id"]),),
    )
    if df.empty:
        st.info("暂时没有成绩记录。")
        return

    df["得分率%"] = (pd.to_numeric(df["score"], errors="coerce") / pd.to_numeric(df["full_score"], errors="coerce") * 100).round(1)
    st.dataframe(
        df[["exam_date", "exam_name", "subject", "score", "full_score", "得分率%"]].rename(
            columns={
                "exam_date": "日期",
                "exam_name": "考试名称",
                "subject": "科目",
                "score": "分数",
                "full_score": "满分",
            }
        ),
        hide_index=True,
        use_container_width=True,
    )
    chart = df.dropna(subset=["得分率%"]).sort_values("exam_date")
    if len(chart) >= 2:
        st.line_chart(chart.set_index("exam_date")["得分率%"])

    st.subheader("考试详情")
    for _, row in df.iterrows():
        score_text = "—" if pd.isna(row["score"]) else f"{float(row['score']):g}"
        full_text = "—" if pd.isna(row["full_score"]) else f"{float(row['full_score']):g}"
        with st.expander(f"{row['exam_date']}　{row['exam_name']}　{score_text}/{full_text}"):
            st.write(f"**科目：** {row['subject'] or '—'}")
            st.write(f"**分数：** {score_text}/{full_text}")
            st.write(f"**错题：** {row['wrong_qs'] or '—'}")
            st.write(f"**薄弱知识点：** {row['knowledge'] or '—'}")