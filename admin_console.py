"""总后台：创始者专用。开通老师、管理老师、查看所有老师的数据。

设计前提（改动前务必先读 AGENTS.md 的「多老师结构」）：
- 每位老师一份独立数据库，云端路径 database/<标识>/scores.db；
- 老师名册 teacher_registry 记录谁在用、各自用哪份库、谁是管理员；
- 本模块的跨老师操作**一律显式指定**目标库路径与云端路径
  （db_path + object_path），绝不依赖当前会话的库——
  否则会把别人的数据写到你自己那份路径上，造成数据事故。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import cloud_store
import course_management as cm
import platform_store
import teacher_registry
import user_management as um

_init_schema = None


def configure(init_schema) -> None:
    """app.py 把建表函数挂进来，避免循环 import。"""
    global _init_schema
    _init_schema = init_schema


def _require_admin() -> str:
    user = um.current_user()
    if user is None:
        st.stop()
    slug = cloud_store.current_slug()
    entry = teacher_registry.teacher_entry(slug)
    if not entry or not bool(entry.get("is_admin")):
        st.error("没有权限访问总后台")
        st.stop()
    return slug


def _teacher_db(slug: str) -> Path:
    """拿到某位老师的本地库路径（云端有的话先拉下来）。"""
    data_dir = cloud_store.get_data_dir(teacher_registry.base_dir(), slug)
    (data_dir / "photos").mkdir(parents=True, exist_ok=True)
    db = data_dir / "scores.db"
    try:
        cloud_store.prepare_database(db, cloud_store.db_object_path(slug))
    except Exception:
        pass  # 拉不下来就用手上这份
    return db


def _read(db: Path, sql: str, params=()) -> pd.DataFrame:
    if not db.is_file():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(db)
        try:
            return pd.read_sql_query(sql, conn, params=params)
        finally:
            conn.close()
    except Exception:
        return pd.DataFrame()


def _ago(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "还没用过"
    try:
        when = datetime.fromisoformat(text)
    except Exception:
        return text
    delta = datetime.now() - when
    if delta < timedelta(minutes=10):
        return "刚刚"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} 分钟前"
    if when.date() == datetime.now().date():
        return f"今天 {when.strftime('%H:%M')}"
    if delta < timedelta(days=2):
        return f"昨天 {when.strftime('%H:%M')}"
    return f"{int(delta.days)} 天前"


# ==================== 开通 / 管理老师 ====================
def create_teacher(
    display_name: str,
    username: str,
    password: str,
    note: str = "",
    *,
    org_id: int | None = None,
    org_role: str = "teacher",
) -> tuple[bool, str]:
    """开通一位新老师：建一份空库 + 建账号 + 登记名册。"""
    name = str(username or "").strip()
    if not name:
        return False, "请填写登录账号"
    if not name.isascii() or any(ch.isspace() for ch in name):
        return False, "登录账号请用英文字母或数字，不要有空格"
    if teacher_registry.username_owner(name) is not None:
        return False, f"账号「{name}」已经被占用了，换一个"
    pwd = str(password or "").strip()
    if len(pwd) < 6:
        return False, "初始密码至少 6 位"
    if org_role not in (platform_store.ROLE_TEACHER, platform_store.ROLE_ORG_ADMIN):
        return False, "机构权限不合法"
    target_org_id = int(org_id) if org_id is not None else platform_store.default_org_id()
    if target_org_id is None:
        return False, "找不到可用的机构"
    if platform_store.get_organization(target_org_id) is None:
        return False, "机构不存在"

    slug = teacher_registry.unique_slug(name)
    object_path = cloud_store.db_object_path(slug)
    db = _teacher_db(slug)
    if db.is_file() and teacher_registry.teacher_entry(slug) is None:
        return False, f"已经有一份数据叫「{slug}」了，换个账号名"

    try:
        _init_schema(db, object_path)
        cm.init_course_db(db, object_path=object_path)
        um.init_user_db(
            db,
            default_teacher_password=pwd,
            object_path=object_path,
            org_id=target_org_id,
        )
        conn = sqlite3.connect(db)
        try:
            # 把自动创建的 teacher 账号改成他真正的登录名，并要求首次登录改密
            conn.execute(
                """UPDATE users
                   SET username=?, must_change_password=1, note=?, org_id=?
                   WHERE role='teacher'""",
                (name, str(note or "由总后台开通"), target_org_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return False, f"给这位老师建库时出错：{exc}"

    ok, err = teacher_registry.add_teacher(name, display_name, slug)
    if not ok:
        return False, err
    member_ok, member_err = platform_store.add_org_user(
        target_org_id, slug, org_role
    )
    if not member_ok:
        return False, member_err
    cloud_store.save_database(db, object_path)
    teacher_registry.note_usernames(slug, db)
    return True, ""


def rename_teacher(
    slug: str, new_username: str, new_display_name: str | None = None
) -> tuple[bool, str]:
    """改一位老师的登录账号（可连带显示名）。

    数据库和名册必须一起改：只改一处会让人登不进来。
    任何一步失败都会把另一处**回退**，保证系统停在一个一致的状态上。
    """
    entry = teacher_registry.teacher_entry(slug)
    if entry is None:
        return False, "找不到这位老师"
    old = str(entry.get("username") or "").strip()
    new = str(new_username or "").strip()
    if not new:
        return False, "请填写登录账号"
    if not new.isascii() or any(ch.isspace() for ch in new):
        return False, "登录账号请用英文字母或数字，不要有空格"
    owner = teacher_registry.username_owner(new)
    if owner is not None and owner != slug:
        return False, f"账号「{new}」已经被占用了，换一个"
    display = (
        entry.get("display_name")
        if new_display_name is None
        else str(new_display_name).strip()
    )

    object_path = cloud_store.db_object_path(slug)
    db = _teacher_db(slug)
    if not db.is_file():
        return False, "找不到这位老师的数据"

    changed_db = False
    if new != old:
        conn = sqlite3.connect(db)
        try:
            cur = conn.execute(
                "UPDATE users SET username=? WHERE role='teacher' AND username=?", (new, old)
            )
            if cur.rowcount == 0:
                cur = conn.execute("UPDATE users SET username=? WHERE role='teacher'", (new,))
            conn.commit()
            changed_db = cur.rowcount > 0
        except Exception as exc:
            conn.rollback()
            return False, f"改数据库里的账号失败：{exc}"
        finally:
            conn.close()

    try:
        teacher_registry.update_teacher(slug, username=new, display_name=display)
    except Exception as exc:
        if changed_db:
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE users SET username=? WHERE role='teacher' AND username=?", (old, new)
                )
                conn.commit()
            finally:
                conn.close()
        return False, f"改名册失败，已把数据库的改动退回去：{exc}"

    try:
        cloud_store.save_database(db, object_path)
    except Exception as exc:
        # 云端没同步上，整体退回旧账号，避免重启后出现"两边对不上"的死结
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "UPDATE users SET username=? WHERE role='teacher' AND username=?", (old, new)
            )
            conn.commit()
        finally:
            conn.close()
        teacher_registry.update_teacher(slug, username=old, display_name=entry.get("display_name") or "")
        return False, f"同步到云端失败，已全部退回原账号，请稍后重试：{exc}"
    return True, ""


def reset_teacher_password(slug: str, password: str) -> tuple[bool, str]:
    pwd = str(password or "").strip()
    if len(pwd) < 6:
        return False, "新密码至少 6 位"
    object_path = cloud_store.db_object_path(slug)
    db = _teacher_db(slug)
    if not db.is_file():
        return False, "找不到这位老师的数据"
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash=?, must_change_password=1 WHERE role='teacher'",
            (um.hash_password(pwd),),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, "这位老师还没有老师账号"
    finally:
        conn.close()
    cloud_store.save_database(db, object_path)
    return True, ""


def set_teacher_active(slug: str, active: bool) -> tuple[bool, str]:
    """停用/启用一位老师。停用后他和他的学生都登不进来，数据不受影响。"""
    if not active:
        entry = teacher_registry.teacher_entry(slug)
        if entry is None:
            return False, "找不到这位老师"
        if bool(entry.get("is_admin")):
            # 硬性禁止：管理员被停用后就没人能进总后台了，没有别的入口能救回来
            return False, "管理员账号不能停用，否则总后台就进不去了"
    teacher_registry.update_teacher(slug, active=bool(active))
    return True, ""


def create_organization_with_admin(
    org_name: str,
    kind: str,
    admin_username: str,
    admin_display_name: str,
    password: str,
    *,
    note: str = "",
    public_name_ok: bool = False,
    status: str = "体验",
) -> tuple[bool, str, int | None]:
    """新建机构，并在同一份新库里创建第一位机构管理员。"""
    name = str(admin_username or "").strip()
    if not name:
        return False, "请填写机构管理员登录账号", None
    if not name.isascii() or any(ch.isspace() for ch in name):
        return False, "登录账号请用英文字母或数字，不要有空格", None
    if teacher_registry.username_owner(name) is not None:
        return False, f"账号「{name}」已经被占用了，换一个", None
    org_id, err = platform_store.create_organization(
        org_name,
        kind=kind,
        status=status,
        note=note,
        public_name_ok=public_name_ok,
    )
    if org_id is None:
        return False, err, None
    ok, err = create_teacher(
        admin_display_name,
        name,
        password,
        note=note or "机构管理员",
        org_id=org_id,
        org_role=platform_store.ROLE_ORG_ADMIN,
    )
    if not ok:
        return False, err, org_id
    return True, "", org_id


def _org_member(
    org_id: int,
    slug: str,
) -> tuple[dict | None, str]:
    member = platform_store.org_user_for_slug(slug)
    if not member or int(member["org_id"]) != int(org_id):
        return None, "这位老师不属于当前机构"
    entry = teacher_registry.teacher_entry(slug)
    if entry is None:
        return None, "找不到这位老师"
    return entry, ""


def org_set_teacher_active(
    org_id: int,
    actor_slug: str,
    target_slug: str,
    active: bool,
) -> tuple[bool, str]:
    """机构管理员停用/启用本机构老师，禁止跨机构和停用机构管理员。"""
    entry, err = _org_member(org_id, target_slug)
    if entry is None:
        return False, err
    member = platform_store.org_user_for_slug(target_slug) or {}
    if not active and member.get("role") == platform_store.ROLE_ORG_ADMIN:
        return False, "机构管理员账号不能在这里停用"
    if target_slug == actor_slug and not active:
        return False, "不能停用自己的账号"
    teacher_registry.update_teacher(target_slug, active=bool(active))
    return True, ""


def org_reset_teacher_password(
    org_id: int,
    target_slug: str,
    password: str,
) -> tuple[bool, str]:
    entry, err = _org_member(org_id, target_slug)
    if entry is None:
        return False, err
    return reset_teacher_password(target_slug, password)


def org_teacher_rows(org_id: int) -> list[dict]:
    rows = []
    for member in platform_store.list_org_users(org_id):
        entry = teacher_registry.teacher_entry(member.get("slug"))
        if entry is None:
            continue
        rows.append(
            {
                "slug": member["slug"],
                "role": member["role"],
                "username": str(entry.get("username") or ""),
                "display_name": str(entry.get("display_name") or ""),
                "active": bool(entry.get("active", True)),
            }
        )
    return rows


def _require_org_admin() -> tuple[str, int]:
    user = um.current_user()
    if user is None or user.get("role") != um.ROLE_TEACHER:
        st.error("没有权限访问机构管理")
        st.stop()
    slug = cloud_store.current_slug()
    member = platform_store.org_user_for_slug(slug)
    if not member or member.get("role") != platform_store.ROLE_ORG_ADMIN:
        st.error("没有权限访问机构管理")
        st.stop()
    org_id = int(member["org_id"])
    org = platform_store.get_organization(org_id)
    if org is None:
        st.error("找不到所属机构")
        st.stop()
    if org.get("kind") == "个人":
        st.error("个人机构不需要机构管理页面")
        st.stop()
    return slug, org_id


def page_org_admin() -> None:
    """机构管理员页面：只管理本机构老师账号。"""
    my_slug, org_id = _require_org_admin()
    org = platform_store.get_organization(org_id) or {}
    st.title("机构管理")
    st.caption(
        f"{org.get('name') or '未命名机构'} · {org.get('kind') or '机构'} · "
        f"{org.get('status') or '体验'}"
    )
    st.caption("这里只能管理本机构的老师账号，看不到其他机构，也看不到学生成绩。")

    created = st.session_state.pop("org_teacher_created", None)
    reset = st.session_state.pop("org_teacher_reset", None)
    if created:
        st.success(f"老师账号已创建：{created['username']}")
        st.code(
            f"登录账号：{created['username']}\n初始密码：{created['password']}",
            language=None,
        )
        st.caption("首次登录会要求修改密码。")
    if reset:
        st.success(f"账号 {reset['username']} 的密码已重置")
        st.code(
            f"登录账号：{reset['username']}\n新密码：{reset['password']}",
            language=None,
        )

    with st.expander("＋ 开通本机构老师", expanded=False):
        with st.form("org_new_teacher"):
            f1, f2, f3 = st.columns(3)
            display_name = f1.text_input(
                "老师称呼", placeholder="例如：李老师", key="org_new_display"
            )
            username = f2.text_input(
                "登录账号", placeholder="英文字母或数字", key="org_new_username"
            )
            password = f3.text_input(
                "初始密码", placeholder="留空自动生成", key="org_new_password"
            )
            submitted = st.form_submit_button("开通", type="primary")
        if submitted:
            pwd = str(password or "").strip() or um.generate_password()
            ok, err = create_teacher(
                display_name,
                username,
                pwd,
                note="由机构管理员开通",
                org_id=org_id,
                org_role=platform_store.ROLE_TEACHER,
            )
            if ok:
                st.session_state["org_teacher_created"] = {
                    "username": str(username).strip(),
                    "password": pwd,
                }
                st.rerun()
            else:
                st.error(err)

    rows = org_teacher_rows(org_id)
    st.subheader("本机构老师")
    if not rows:
        st.info("还没有老师账号。")
        return
    for item in rows:
        label = item["display_name"] or item["username"]
        state = "启用" if item["active"] else "已停用"
        role = platform_store.ROLE_LABELS.get(item["role"], item["role"])
        marks = [role]
        if item["slug"] == my_slug:
            marks.append("我")
        with st.expander(f"{label}（{item['username']}）· {state} · {'、'.join(marks)}"):
            st.caption("这里只管理老师登录账号，不会查看他的学生和成绩。")
            if item["role"] == platform_store.ROLE_ORG_ADMIN:
                st.info("机构管理员账号不能在这里停用。")
            elif item["slug"] == my_slug:
                st.info("请到「账号」页面修改自己的密码。")
            else:
                with st.form(f"org_reset_{item['slug']}"):
                    newpwd = st.text_input(
                        "重置登录密码",
                        placeholder="留空自动生成",
                        key=f"org_reset_pwd_{item['slug']}",
                    )
                    reset_submitted = st.form_submit_button("重置密码")
                if reset_submitted:
                    pwd = str(newpwd or "").strip() or um.generate_password()
                    ok, err = org_reset_teacher_password(
                        org_id, item["slug"], pwd
                    )
                    if ok:
                        st.session_state["org_teacher_reset"] = {
                            "username": item["username"],
                            "password": pwd,
                        }
                        st.rerun()
                    else:
                        st.error(err)

                target_active = not item["active"]
                button_text = "启用账号" if target_active else "停用账号"
                if st.button(
                    button_text,
                    key=f"org_toggle_{item['slug']}",
                    use_container_width=True,
                ):
                    ok, err = org_set_teacher_active(
                        org_id, my_slug, item["slug"], target_active
                    )
                    if ok:
                        st.rerun()
                    else:
                        st.error(err)


def teacher_students(slug: str) -> pd.DataFrame:
    db = _teacher_db(slug)
    return _read(
        db,
        """SELECT s.id, s.name, s.grade, s.school, s.status,
                  (SELECT u.username FROM users u WHERE u.student_id=s.id) AS 账号
           FROM students s ORDER BY s.id""",
    )


def create_student_account(
    slug: str, student_id: int, username: str, password: str
) -> tuple[bool, str]:
    """替某位老师给他的学生开账号（老师自己不能开账号）。"""
    name = str(username or "").strip()
    if not name:
        return False, "请填写账号"
    owner = teacher_registry.username_owner(name)
    if owner is not None:
        return False, f"账号「{name}」已经被占用了，换一个"
    object_path = cloud_store.db_object_path(slug)
    db = _teacher_db(slug)
    if not db.is_file():
        return False, "找不到这位老师的数据"
    ok, err = um.create_account_in(
        db, name, str(password or "").strip(), um.ROLE_STUDENT,
        student_id=int(student_id), must_change_password=True, note="由总后台开通",
        org_id=platform_store.org_id_for_slug(slug),
    )
    if not ok:
        return False, err
    cloud_store.save_database(db, object_path)
    teacher_registry.note_usernames(slug, db)
    return True, ""


# ==================== 页面 ====================
def page_admin() -> None:
    my_slug = _require_admin()
    st.title("总后台")
    st.caption("只有创始者能看到这一页；其他老师登录后看不到。")

    # ---------- 机构 ----------
    st.subheader("机构")
    organizations = platform_store.list_organizations()
    if organizations:
        for org in organizations:
            members = platform_store.list_org_users(int(org["id"]))
            admin_count = sum(
                1 for member in members
                if member.get("role") == platform_store.ROLE_ORG_ADMIN
            )
            st.markdown(
                f"**{org['name']}**　"
                f"{org.get('kind') or '机构'} · {org.get('status') or '体验'} · "
                f"{len(members)} 位老师"
                + (f" · {admin_count} 位机构管理员" if admin_count else "")
            )
    else:
        st.info("还没有机构记录。")

    with st.expander("＋ 新建机构并开通机构管理员", expanded=False):
        st.caption(
            "新机构只能由平台创建；机构管理员登录后可以自己开通本机构老师账号。"
        )
        with st.form("admin_new_org"):
            o1, o2, o3 = st.columns(3)
            org_name = o1.text_input("机构名称", placeholder="例如：XX教育")
            org_kind = o2.selectbox("机构类型", list(platform_store.ORG_KINDS))
            org_status = o3.selectbox(
                "初始状态", ["体验", "正式"], index=0
            )
            admin_display = st.text_input(
                "机构管理员称呼", placeholder="例如：王老师"
            )
            admin_username = st.text_input(
                "机构管理员登录账号", placeholder="英文字母或数字"
            )
            admin_password = st.text_input(
                "初始密码", placeholder="留空自动生成"
            )
            public_name_ok = st.checkbox("允许以后公开展示机构名称")
            org_note = st.text_area("备注", placeholder="可留空")
            submitted = st.form_submit_button("创建机构", type="primary")
        if submitted:
            pwd = str(admin_password or "").strip() or um.generate_password()
            ok, err, org_id = create_organization_with_admin(
                org_name,
                org_kind,
                admin_username,
                admin_display,
                pwd,
                note=org_note,
                public_name_ok=public_name_ok,
                status=org_status,
            )
            if ok:
                st.session_state["admin_new_org_done"] = {
                    "org_name": str(org_name).strip(),
                    "username": str(admin_username).strip(),
                    "password": pwd,
                }
                st.rerun()
            else:
                st.error(err)
        org_done = st.session_state.pop("admin_new_org_done", None)
        if org_done:
            st.success(
                f"机构「{org_done['org_name']}」和机构管理员账号已创建，"
                "请把下面这组信息发给机构管理员。"
            )
            st.code(
                f"登录账号：{org_done['username']}\n初始密码：{org_done['password']}",
                language=None,
            )
            st.caption("机构管理员首次登录会被要求修改密码。")

    st.divider()

    entries = teacher_registry.list_teachers()
    total_students = sum(int((e.get("stats") or {}).get("students") or 0) for e in entries)
    total_lessons = sum(int((e.get("stats") or {}).get("lessons_month") or 0) for e in entries)
    total_scores = sum(int((e.get("stats") or {}).get("scores") or 0) for e in entries)
    active = sum(1 for e in entries if e.get("active", True))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("老师", f"{len(entries)} 位", f"启用 {active} 位")
    c2.metric("学生", f"{total_students} 人")
    c3.metric("本月课时", f"{total_lessons} 节")
    c4.metric("成绩记录", f"{total_scores} 条")

    st.divider()

    # ---------- 全局开关 ----------
    allow = bool(
        teacher_registry.get_setting(
            teacher_registry.SETTING_TEACHERS_CAN_CREATE_STUDENTS, False
        )
    )
    new_allow = st.toggle(
        "允许老师给自己的学生开账号",
        value=allow,
        key="set_allow_students",
        help=(
            "关闭：老师完全看不到「账号管理」，开账号只能找你。"
            "打开：老师可以给自己的学生开账号、重置密码、停用启用，但没有删除入口。"
        ),
    )
    if bool(new_allow) != allow:
        teacher_registry.set_setting(
            teacher_registry.SETTING_TEACHERS_CAN_CREATE_STUDENTS, bool(new_allow)
        )
        st.rerun()
    st.caption(
        "说明：老师开账号只是往他自己那份数据里加一条记录，不会拖垮网站；"
        "真正影响访问速度的是「同时在线人数」，不是学生数量。"
    )

    st.divider()

    # ---------- 开通老师 ----------
    with st.expander("＋ 开通老师", expanded=len(entries) <= 1):
        st.caption("开通后把「登录账号 + 初始密码」发给他，他第一次登录会被要求改密码。")
        with st.form("admin_new_teacher"):
            f1, f2, f3 = st.columns(3)
            display_name = f1.text_input("老师称呼", placeholder="例如：李老师", key="admin_new_display")
            username = f2.text_input("登录账号", placeholder="英文字母或数字，例如 lilaoshi", key="admin_new_username")
            password = f3.text_input("初始密码", placeholder="留空自动生成", key="admin_new_password")
            submitted = st.form_submit_button("开通", type="primary")
        if submitted:
            pwd = str(password or "").strip() or um.generate_password()
            ok, err = create_teacher(display_name, username, pwd)
            if ok:
                st.session_state["admin_new_teacher_done"] = {
                    "username": str(username).strip(),
                    "password": pwd,
                    "display_name": str(display_name or "").strip(),
                }
                st.rerun()
            else:
                st.error(err)
        done = st.session_state.pop("admin_new_teacher_done", None)
        if done:
            st.success("开通成功，请把下面这组信息发给他（关掉就看不到了，先记下来）")
            st.code(
                f"登录账号：{done['username']}\n初始密码：{done['password']}",
                language=None,
            )

    # ---------- 老师列表 ----------
    st.subheader("老师列表")
    if not entries:
        st.info("还没有老师。点上面的「开通老师」加第一位。")
        return

    for entry in entries:
        slug = cloud_store.normalize_slug(entry.get("slug")) or cloud_store.DEFAULT_SLUG
        username = str(entry.get("username") or "")
        label = str(entry.get("display_name") or "").strip() or username
        stats = entry.get("stats") or {}
        state = "启用" if entry.get("active", True) else "已停用"
        marks = []
        if entry.get("is_admin"):
            marks.append("管理员")
        if slug == my_slug:
            marks.append("我")
        title = f"{label}（{username}）· {state}"
        if marks:
            title += " · " + "、".join(marks)

        with st.expander(title):
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("学生", f"{int(stats.get('students') or 0)} 人")
            m2.metric("班级", f"{int(stats.get('classes') or 0)} 个")
            m3.metric("本月课时", f"{int(stats.get('lessons_month') or 0)} 节")
            m4.metric("最近使用", _ago(entry.get("last_active")))
            st.caption(f"数据位置：{cloud_store.db_object_path(slug)}")

            tabs = st.tabs(["查看数据", "开学生账号", "管理"])

            with tabs[0]:
                db = _teacher_db(slug)
                if not db.is_file():
                    st.info("还没有数据（这位老师还没开始用，或者云端还没初始化）。")
                else:
                    students = _read(
                        db,
                        """SELECT s.name AS 学生, s.grade AS 年级, s.school AS 学校,
                                  s.status AS 状态,
                                  (SELECT u.username FROM users u WHERE u.student_id=s.id) AS 账号
                           FROM students s ORDER BY s.id""",
                    )
                    st.markdown(f"**学生名单（{len(students)} 人）**")
                    if students.empty:
                        st.caption("暂无学生")
                    else:
                        st.dataframe(students, use_container_width=True, hide_index=True)

                    c1, c2 = st.columns(2)
                    with c1:
                        classes = _read(
                            db,
                            """SELECT c.name AS 班级, c.grade AS 年级,
                                      CASE WHEN c.kind='一对一' THEN '一对一' ELSE '班课' END AS 类型
                               FROM classes c ORDER BY c.id""",
                        )
                        st.markdown(f"**班级（{len(classes)} 个）**")
                        st.dataframe(classes, use_container_width=True, hide_index=True)
                    with c2:
                        exams = _read(
                            db,
                            """SELECT e.name AS 考试, e.exam_date AS 日期,
                                      COUNT(s.id) AS 成绩条数
                               FROM exams e LEFT JOIN scores s ON s.exam_id=e.id
                               GROUP BY e.id ORDER BY e.exam_date DESC LIMIT 20""",
                        )
                        st.markdown(f"**考试（{len(exams)} 场）**")
                        st.dataframe(exams, use_container_width=True, hide_index=True)
                    st.caption("这里只能看，不能改。要改某位老师的数据，请用他自己的账号登录。")

            with tabs[1]:
                st.caption("老师自己不能开账号，所以学生的账号由你在这里开。")
                students = teacher_students(slug)
                if students.empty:
                    st.info("这位老师还没有学生。让他在自己的「学生」页添加学生后，你再来开账号。")
                else:
                    options = {
                        int(row["id"]): f"{row['name']}"
                        + (f"（{row['grade']}）" if str(row["grade"] or "").strip() else "")
                        + ("· 已有账号 " + str(row["账号"]) if str(row["账号"] or "").strip() else "")
                        for _, row in students.iterrows()
                    }
                    with st.form(f"admin_new_student_{slug}"):
                        sid = st.selectbox(
                            "选择学生", list(options), format_func=options.get,
                            key=f"pick_{slug}",
                        )
                        s1, s2 = st.columns(2)
                        uname = s1.text_input("账号", placeholder="例如 wx01 或学号", key=f"stu_uname_{slug}")
                        upwd = s2.text_input("初始密码", placeholder="留空自动生成", key=f"stu_pwd_{slug}")
                        sub = st.form_submit_button("给他开账号", type="primary")
                    if sub:
                        pwd = str(upwd or "").strip() or um.generate_password()
                        ok, err = create_student_account(slug, int(sid), uname, pwd)
                        if ok:
                            st.session_state[f"admin_new_student_done_{slug}"] = {
                                "username": str(uname).strip(), "password": pwd,
                            }
                            st.rerun()
                        else:
                            st.error(err)
                    made = st.session_state.pop(f"admin_new_student_done_{slug}", None)
                    if made:
                        st.success("开好了，把下面这组发给他（关掉就看不到了）")
                        st.code(
                            f"登录账号：{made['username']}\n初始密码：{made['password']}",
                            language=None,
                        )

            with tabs[2]:
                if entry.get("is_admin"):
                    st.caption("管理员账号不能停用——停用后总后台就进不去了。")
                else:
                    is_active = bool(entry.get("active", True))
                    if is_active:
                        if st.button("停用这位老师", key=f"off_{slug}"):
                            ok, err = set_teacher_active(slug, False)
                            if ok:
                                st.success("已停用。他和他的学生都登不进来了，数据都还在。")
                                st.rerun()
                            else:
                                st.error(err)
                    else:
                        if st.button("重新启用", key=f"on_{slug}"):
                            ok, err = set_teacher_active(slug, True)
                            if ok:
                                st.success("已启用。")
                                st.rerun()
                            else:
                                st.error(err)
                st.divider()
                with st.form(f"admin_rename_{slug}"):
                    st.caption("改登录账号或显示名（数据库和名册一起改，改完用新账号登录，密码不变）")
                    rn = st.text_input(
                        "登录账号", value=str(entry.get("username") or ""), key=f"rename_user_{slug}"
                    )
                    rd = st.text_input(
                        "显示名", value=str(entry.get("display_name") or ""),
                        placeholder="总后台里怎么称呼他", key=f"rename_display_{slug}",
                    )
                    nsub = st.form_submit_button("保存账号")
                if nsub:
                    ok, err = rename_teacher(slug, rn, rd)
                    if ok:
                        st.session_state[f"admin_rename_done_{slug}"] = str(rn).strip()
                        if slug == my_slug:
                            # 改的是自己：同步会话里的登录名，避免被踢回登录页
                            st.session_state["auth_username"] = str(rn).strip()
                        st.rerun()
                    else:
                        st.error(err)
                renamed = st.session_state.pop(f"admin_rename_done_{slug}", None)
                if renamed:
                    st.success(
                        f"已改好。以后用「{renamed}」登录（密码不变）。"
                        "如果是你自己的账号，下次登录请用新账号。"
                    )

                st.divider()
                with st.form(f"admin_reset_{slug}"):
                    newpwd = st.text_input(
                        "重置他的登录密码", placeholder="留空自动生成", key=f"reset_pwd_{slug}"
                    )
                    rsub = st.form_submit_button("重置密码")
                if rsub:
                    pwd = str(newpwd or "").strip() or um.generate_password()
                    ok, err = reset_teacher_password(slug, pwd)
                    if ok:
                        st.session_state[f"admin_reset_done_{slug}"] = pwd
                        st.rerun()
                    else:
                        st.error(err)
                reset_done = st.session_state.pop(f"admin_reset_done_{slug}", None)
                if reset_done:
                    st.success("已重置，请把这组信息发给他（关掉就看不到了）")
                    st.code(
                        f"登录账号：{entry.get('username')}\n新密码：{reset_done}",
                        language=None,
                    )
                st.caption("提示：这里只提供停用，不提供删除——删数据不可逆，需要时再单独处理。")
