"""平台管理：创始者专用。运营概览、机构/老师账号管理、临时只读代入。

设计前提（改动前务必先读 AGENTS.md 的「多老师结构」）：
- 每位老师一份独立数据库，云端路径 database/<标识>/scores.db；
- 老师名册 teacher_registry 记录谁在用、各自用哪份库、谁是管理员；
- 本模块的跨老师操作**一律显式指定**目标库路径与云端路径
  （db_path + object_path），绝不依赖当前会话的库——
  否则会把别人的数据写到你自己那份路径上，造成数据事故。
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import cloud_store
import course_management as cm
import demo_org
import impersonation
import platform_store
import teacher_registry
import user_management as um

_init_schema = None


def configure(init_schema) -> None:
    """app.py 把建表函数挂进来，避免循环 import。"""
    global _init_schema
    _init_schema = init_schema


def _require_admin(page_label: str = "平台管理") -> str:
    user = um.current_user()
    if user is None or user.get("role") != um.ROLE_TEACHER:
        st.error(f"没有权限访问{page_label}")
        st.stop()
    slug = cloud_store.current_slug()
    entry = teacher_registry.teacher_entry(slug)
    if not entry or not bool(entry.get("is_admin")):
        st.error(f"没有权限访问{page_label}")
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


# ==================== 平台运营概览 ====================
# 平台概览只做运营数量统计，禁止在这里查询学生姓名、成绩、错题或试卷内容。
_OVERVIEW_STUDENTS_SQL = "SELECT COUNT(*) FROM students"
_OVERVIEW_PLANS_SQL = (
    "SELECT COUNT(*) FROM plans WHERE plan_date>=? AND plan_date<=?"
)
_OVERVIEW_EXAMS_SQL = "SELECT COUNT(*) FROM exams"
_OVERVIEW_LAST_LOGIN_SQL = (
    "SELECT MAX(last_login) FROM users WHERE role='teacher'"
)


def _format_bytes(size: int | float | None) -> str:
    value = max(0.0, float(size or 0))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def _max_time_text(values) -> str:
    texts = [str(value or "").strip() for value in values if str(value or "").strip()]
    return max(texts) if texts else ""


def _display_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return text
    return when.strftime("%Y-%m-%d %H:%M")


def _overview_db_counts(db: Path, since: str, today: str) -> dict:
    """只读统计一份老师库；不查询任何学生内容字段。"""
    path = Path(db)
    result = {
        "students": 0,
        "courses_30d": 0,
        "exams": 0,
        "last_login": "",
        "db_bytes": path.stat().st_size if path.is_file() else 0,
        "error": False,
    }
    if not path.is_file():
        return result
    try:
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
        )
    except Exception:
        result["error"] = True
        return result
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "students" in tables:
            result["students"] = int(
                conn.execute(_OVERVIEW_STUDENTS_SQL).fetchone()[0] or 0
            )
        if "plans" in tables:
            result["courses_30d"] = int(
                conn.execute(_OVERVIEW_PLANS_SQL, (since, today)).fetchone()[0] or 0
            )
        if "exams" in tables:
            result["exams"] = int(
                conn.execute(_OVERVIEW_EXAMS_SQL).fetchone()[0] or 0
            )
        if "users" in tables:
            result["last_login"] = str(
                conn.execute(_OVERVIEW_LAST_LOGIN_SQL).fetchone()[0] or ""
            )
    except Exception:
        result["error"] = True
    finally:
        conn.close()
    return result


def _overview_media_stats(slug: str) -> dict:
    """统计某位老师图片/PDF 数量与估算大小，不返回文件名。"""
    if cloud_store.is_cloud():
        return cloud_store.storage_prefix_stats(cloud_store.photo_storage_prefix(slug))
    photos = cloud_store.get_data_dir(teacher_registry.base_dir(), slug) / "photos"
    if not photos.is_dir():
        return {"count": 0, "bytes": 0}
    files = [path for path in photos.rglob("*") if path.is_file()]
    return {
        "count": len(files),
        "bytes": sum(path.stat().st_size for path in files),
    }


def build_platform_overview(
    *,
    db_resolver=None,
    media_resolver=None,
    today: date | None = None,
) -> list[dict]:
    """按机构汇总运营数据。

    db_resolver / media_resolver 仅用于测试时注入假数据；生产环境默认读取
    各老师自己的库和对应图片目录。返回值不包含学生姓名、成绩或试卷内容。
    """
    organizations = platform_store.list_organizations()
    if not organizations:
        return []
    db_resolver = db_resolver or _teacher_db
    media_resolver = media_resolver or _overview_media_stats
    current = today or date.today()
    since = (current - timedelta(days=29)).isoformat()
    today_text = current.isoformat()

    rows: list[dict] = []
    for org in organizations:
        org_id = int(org["id"])
        members = platform_store.list_org_users(org_id)
        total_students = 0
        total_courses = 0
        total_exams = 0
        total_db_bytes = 0
        total_media = 0
        total_media_bytes = 0
        last_logins: list[str] = []
        read_error = False
        media_error = False

        for member in members:
            slug = cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG
            try:
                db = db_resolver(slug)
                counts = _overview_db_counts(db, since, today_text)
            except Exception:
                counts = {
                    "students": 0,
                    "courses_30d": 0,
                    "exams": 0,
                    "last_login": "",
                    "db_bytes": 0,
                    "error": True,
                }
            total_students += int(counts["students"])
            total_courses += int(counts["courses_30d"])
            total_exams += int(counts["exams"])
            total_db_bytes += int(counts["db_bytes"])
            read_error = read_error or bool(counts["error"])
            if counts["last_login"]:
                last_logins.append(str(counts["last_login"]))

            try:
                media = media_resolver(slug)
                total_media += int(media.get("count") or 0)
                total_media_bytes += int(media.get("bytes") or 0)
            except Exception:
                media_error = True

        rows.append(
            {
                "org_id": org_id,
                "name": str(org.get("name") or ""),
                "kind": str(org.get("kind") or "机构"),
                "status": str(org.get("status") or ""),
                "teacher_count": len(members),
                "student_count": total_students,
                "course_count_30d": total_courses,
                "exam_count": total_exams,
                "last_login": _max_time_text(last_logins),
                "media_count": None if media_error else total_media,
                "media_bytes": total_media_bytes,
                "created_at": str(org.get("created_at") or ""),
                "read_error": read_error,
                "media_error": media_error,
                "_db_bytes": total_db_bytes,
            }
        )
    return rows


def _latest_local_backup() -> str:
    roots = [
        teacher_registry.base_dir() / "backups",
        Path(__file__).parent / "backups",
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    if not files:
        return ""
    latest = max(files, key=lambda path: path.stat().st_mtime)
    return datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


def _collect_cloud_files(prefix: str, max_depth: int = 2) -> list[dict]:
    files: list[dict] = []
    queue: list[tuple[str, int]] = [(prefix, 0)]
    while queue:
        current, depth = queue.pop(0)
        for item in cloud_store.list_storage_objects(current):
            if item.get("is_file"):
                files.append(item)
            elif depth < max_depth:
                queue.append((str(item.get("object_path") or ""), depth + 1))
    return files


def latest_backup_at() -> str:
    """返回最近一次备份时间；本地和云端都查不到时返回空字符串。"""
    if not cloud_store.is_cloud():
        return _latest_local_backup()
    try:
        roots = cloud_store.list_storage_objects("backups")
        date_folders = sorted(
            [
                item
                for item in roots
                if not item.get("is_file")
                and len(str(item.get("name") or "")) >= 10
                and str(item.get("name") or "")[:4].isdigit()
            ],
            key=lambda item: str(item.get("name") or ""),
            reverse=True,
        )
        direct_files = [item for item in roots if item.get("is_file")]
        for folder in date_folders[:14]:
            files = _collect_cloud_files(str(folder.get("object_path") or ""))
            if files:
                return _display_time(
                    _max_time_text(item.get("updated_at") for item in files)
                )
        if direct_files:
            return _display_time(
                _max_time_text(item.get("updated_at") for item in direct_files)
            )
    except Exception:
        return ""
    return ""


def _database_size_bytes(rows: list[dict]) -> int:
    total = sum(int(row.get("_db_bytes") or 0) for row in rows)
    try:
        platform_db = platform_store.platform_db_path()
        if platform_db.is_file():
            total += platform_db.stat().st_size
    except Exception:
        pass
    return total


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


def _record_platform_audit(
    action: str,
    *,
    target_type: str = "",
    target_id: str | int = "",
    detail: str = "",
    org_id: int | None = None,
) -> None:
    try:
        platform_store.record_current_audit(
            action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            org_id=org_id,
        )
    except Exception as exc:
        print(f"[admin_console] 写平台审计失败：{exc}", file=sys.stderr)


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return buffer.getvalue().encode("utf-8-sig")


def _archive_part(value: object, fallback: str = "未命名") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return text[:100] or fallback


def _teacher_photo_files(slug: str) -> list[tuple[str, bytes]]:
    """读取一位老师自己的照片/PDF；不读取其他老师目录。"""
    target = cloud_store.normalize_slug(slug) or cloud_store.DEFAULT_SLUG
    if cloud_store.is_cloud():
        if cloud_store.is_default_slug(target):
            items = cloud_store.list_storage_objects("photos")
            files = [item for item in items if item.get("is_file")]
        else:
            items = cloud_store.list_storage_objects(f"photos/{target}")
            files = [item for item in items if item.get("is_file")]
        result: list[tuple[str, bytes]] = []
        for item in files:
            name = cloud_store.safe_media_name(item.get("name"))
            object_path = str(item.get("object_path") or "").strip("/")
            data = cloud_store.download_object(object_path)
            if data is not None:
                result.append((name, data))
        return result

    photos = cloud_store.get_data_dir(
        teacher_registry.base_dir(), target
    ) / "photos"
    if not photos.is_dir():
        return []
    result = []
    for path in sorted(photos.iterdir()):
        if not path.is_file():
            continue
        name = cloud_store.safe_media_name(path.name)
        result.append((name, path.read_bytes()))
    return result


def export_organization_zip(
    org_id: int,
    *,
    actor_slug: str,
    include_photos: bool = False,
    db_resolver=None,
    photo_loader=None,
) -> bytes:
    """导出本机构全部老师的数据；调用者必须是该机构的机构管理员。"""
    org = platform_store.get_organization(int(org_id))
    if org is None:
        raise ValueError("机构不存在")
    actor = platform_store.org_user_for_slug(actor_slug)
    if (
        actor is None
        or int(actor.get("org_id") or 0) != int(org_id)
        or actor.get("role") != platform_store.ROLE_ORG_ADMIN
    ):
        raise PermissionError("只能导出自己所属机构的数据")

    members = platform_store.list_org_users(int(org_id))
    if any(int(member.get("org_id") or 0) != int(org_id) for member in members):
        raise PermissionError("机构成员数据不完整，已停止导出")
    db_resolver = db_resolver or _teacher_db
    photo_loader = photo_loader or _teacher_photo_files

    teacher_rows: list[list[object]] = []
    for member in members:
        slug = cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG
        entry = teacher_registry.teacher_entry(slug) or {}
        teacher_rows.append(
            [
                slug,
                platform_store.ROLE_LABELS.get(
                    str(member.get("role") or ""), str(member.get("role") or "")
                ),
                entry.get("username") or "",
                entry.get("display_name") or "",
                "启用" if bool(entry.get("active", True)) else "停用",
            ]
        )

    buffer = io.BytesIO()
    photo_manifest: list[list[object]] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "导出说明.txt",
            (
                "本压缩包由教务工作台导出。\n"
                "每位老师的数据库表分别保存在「老师/<标识>/」目录下，每张表一个 CSV。\n"
                "CSV 使用 UTF-8 编码，可直接用常见表格软件打开。\n"
                "如导出时勾选照片，文件保存在「照片/<标识>/」目录下。\n"
            ),
        )
        archive.writestr(
            "机构信息.csv",
            _csv_bytes(
                ["机构ID", "机构名称", "类型", "状态", "创建时间", "备注"],
                [
                    [
                        org.get("id"),
                        org.get("name"),
                        org.get("kind"),
                        org.get("status"),
                        org.get("created_at"),
                        org.get("note"),
                    ]
                ],
            ),
        )
        archive.writestr(
            "老师清单.csv",
            _csv_bytes(
                ["老师标识", "机构权限", "登录账号", "显示名", "账号状态"],
                teacher_rows,
            ),
        )

        for member in members:
            slug = cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG
            db = Path(db_resolver(slug))
            if not db.is_file():
                raise FileNotFoundError(f"找不到老师 {slug} 的数据库")
            conn = sqlite3.connect(
                db.resolve().as_uri() + "?mode=ro", uri=True, timeout=15
            )
            try:
                tables = [
                    str(row[0])
                    for row in conn.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type='table' AND name NOT LIKE 'sqlite_%'
                           ORDER BY name"""
                    ).fetchall()
                ]
                for table in tables:
                    safe_table = _archive_part(table, "table")
                    quoted = table.replace('"', '""')
                    cursor = conn.execute(f'SELECT * FROM "{quoted}"')
                    headers = [
                        str(description[0]) for description in cursor.description or []
                    ]
                    rows = [list(row) for row in cursor.fetchall()]
                    sensitive = {
                        index
                        for index, header in enumerate(headers)
                        if header.lower() in {"password", "password_hash"}
                    }
                    if sensitive:
                        rows = [
                            [
                                "[已隐藏]"
                                if index in sensitive
                                else value
                                for index, value in enumerate(row)
                            ]
                            for row in rows
                        ]
                    archive.writestr(
                        f"老师/{slug}/{safe_table}.csv",
                        _csv_bytes(headers, rows),
                    )
            finally:
                conn.close()

            if include_photos:
                for name, data in photo_loader(slug):
                    safe_name = cloud_store.safe_media_name(name)
                    archive.writestr(f"照片/{slug}/{safe_name}", data)
                    photo_manifest.append([slug, safe_name, len(data)])

        if include_photos:
            archive.writestr(
                "照片清单.csv",
                _csv_bytes(["老师标识", "文件名", "字节数"], photo_manifest),
            )

    _record_platform_audit(
        "export",
        target_type="organization",
        target_id=int(org_id),
        detail=f"teachers={len(members)};photos={1 if include_photos else 0}",
        org_id=int(org_id),
    )
    return buffer.getvalue()


def delete_organization(
    org_id: int,
    confirmation_name: str,
    *,
    actor_slug: str,
) -> tuple[bool, str]:
    """平台管理员彻底删除一个机构及其全部老师和云端数据。"""
    if platform_store.is_impersonating():
        return False, "代入查看期间不能删除机构"
    actor = teacher_registry.teacher_entry(actor_slug) or {}
    if not bool(actor.get("is_admin")):
        return False, "只有平台管理员可以删除机构"
    org = platform_store.get_organization(int(org_id))
    if org is None:
        return False, "机构不存在"
    expected = str(org.get("name") or "").strip()
    if str(confirmation_name or "").strip() != expected:
        return False, "机构名称输入不一致，已停止删除"

    members = platform_store.list_org_users(int(org_id))
    if any(int(member.get("org_id") or 0) != int(org_id) for member in members):
        return False, "机构成员数据不完整，已停止删除"
    slugs = [
        cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG
        for member in members
    ]
    protected = [
        slug
        for slug in slugs
        if bool((teacher_registry.teacher_entry(slug) or {}).get("is_admin"))
        or bool((teacher_registry.teacher_entry(slug) or {}).get("protected"))
    ]
    if protected:
        return False, "这个机构里有平台管理员或受保护教学账号，不能删除"

    deleted_objects = 0
    for slug in slugs:
        try:
            deleted_objects += cloud_store.delete_teacher_storage(slug)
            cloud_store.remove_local_teacher_data(teacher_registry.base_dir(), slug)
        except Exception as exc:
            return False, f"清理机构数据失败，请稍后重试：{exc}"

    try:
        teacher_registry.remove_teachers(slugs)
    except Exception as exc:
        return False, f"清理老师名册失败，请稍后重试：{exc}"
    if not platform_store.delete_organization_record(int(org_id)):
        return False, "机构记录已经不存在，请刷新页面确认"

    _record_platform_audit(
        "delete_organization",
        target_type="organization",
        target_id=int(org_id),
        detail=f"teachers={len(slugs)};objects={deleted_objects}",
        org_id=int(org_id),
    )
    return True, ""


def _render_platform_access_log(org_id: int) -> None:
    """机构管理员可查看平台方对本机构做过什么，但不展示学生内容。"""
    action_labels = {
        "login_success": "登录",
        "login_failed": "登录失败",
        "change_password": "修改密码",
        "create_teacher_account": "创建老师账号",
        "rename_teacher_account": "修改老师账号",
        "reset_teacher_password": "重置老师密码",
        "enable_teacher_account": "启用老师账号",
        "disable_teacher_account": "停用老师账号",
        "create_student_account": "创建学生账号",
        "delete_student_account": "删除学生账号",
        "reset_student_password": "重置学生密码",
        "enable_student_account": "启用学生账号",
        "disable_student_account": "停用学生账号",
        "delete_student": "删除学生资料",
        "delete_score": "删除成绩",
        "delete_exam": "删除考试",
        "impersonate_start": "开始代入查看",
        "impersonate_end": "结束代入查看",
        "export": "导出数据",
        "delete_organization": "删除机构",
        "consent_accepted": "同意说明页",
    }
    target_labels = {
        "account": "账号",
        "teacher_account": "老师账号",
        "student": "学生",
        "score": "成绩",
        "exam": "考试",
        "organization": "机构",
    }
    records = platform_store.list_audit(
        org_id=int(org_id),
        actor_role="platform_admin",
        limit=100,
    )
    with st.expander("平台方访问记录", expanded=False):
        st.caption(
            "只显示平台管理员对本机构账号和数据的操作，不显示学生姓名、成绩或试卷内容。"
        )
        if not records:
            st.info("暂时没有平台方访问记录。")
            return
        table = pd.DataFrame(
            [
                {
                    "时间": row.get("at") or "—",
                    "操作": action_labels.get(
                        str(row.get("action") or ""), str(row.get("action") or "—")
                    ),
                    "对象类型": target_labels.get(
                        str(row.get("target_type") or ""),
                        str(row.get("target_type") or "—"),
                    ),
                    "对象ID": str(row.get("target_id") or "—"),
                    "详情": str(row.get("detail") or "—"),
                    "原因": str(row.get("reason") or "—"),
                }
                for row in records
            ]
        )
        st.dataframe(table, hide_index=True, use_container_width=True)


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
    _record_platform_audit(
        "create_teacher_account",
        target_type="teacher_account",
        target_id=slug,
        detail=f"org_id={target_org_id}",
        org_id=target_org_id,
    )
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
    _record_platform_audit(
        "rename_teacher_account",
        target_type="teacher_account",
        target_id=slug,
        org_id=platform_store.org_id_for_slug(slug),
    )
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
    _record_platform_audit(
        "reset_teacher_password",
        target_type="teacher_account",
        target_id=slug,
        org_id=platform_store.org_id_for_slug(slug),
    )
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
    _record_platform_audit(
        "enable_teacher_account" if active else "disable_teacher_account",
        target_type="teacher_account",
        target_id=slug,
        org_id=platform_store.org_id_for_slug(slug),
    )
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
    _record_platform_audit(
        "create_organization",
        target_type="organization",
        target_id=org_id,
        detail=f"kind={kind}",
        org_id=org_id,
    )
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
    _record_platform_audit(
        "enable_teacher_account" if active else "disable_teacher_account",
        target_type="teacher_account",
        target_id=target_slug,
        detail=f"org_id={int(org_id)}",
        org_id=org_id,
    )
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
    st.caption(
        "这里只能管理本机构的老师账号和导出本机构数据，不能查看或导出其他机构。"
    )

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

    with st.expander("导出本机构全部数据", expanded=False):
        st.caption(
            "导出范围只包含本机构老师。每位老师的每张数据表会生成一个 CSV，"
            "照片和 PDF 可选。"
        )
        include_photos = st.checkbox(
            "同时导出照片和 PDF（文件可能较多，生成会慢一些）",
            key=f"org_export_photos_{org_id}",
        )
        if st.button(
            "生成机构数据压缩包",
            type="primary",
            use_container_width=True,
            key=f"org_export_button_{org_id}",
        ):
            try:
                with st.spinner("正在整理本机构数据..."):
                    data = export_organization_zip(
                        org_id,
                        actor_slug=my_slug,
                        include_photos=bool(include_photos),
                    )
                st.session_state[f"org_export_ready_{org_id}"] = {
                    "data": data,
                    "name": (
                        f"{_archive_part(org.get('name'), '机构')}"
                        f"_导出_{date.today().isoformat()}.zip"
                    ),
                }
            except Exception as exc:
                st.error(f"导出失败：{exc}")
        ready = st.session_state.get(f"org_export_ready_{org_id}")
        if isinstance(ready, dict) and ready.get("data"):
            st.success("压缩包已经生成，点下面按钮保存到电脑或手机。")
            st.download_button(
                "下载机构数据压缩包",
                data=ready["data"],
                file_name=ready["name"],
                mime="application/zip",
                use_container_width=True,
                key=f"org_export_download_{org_id}",
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
    _render_platform_access_log(org_id)
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
    _record_platform_audit(
        "create_student_account",
        target_type="account",
        target_id=name,
        detail=f"student_id={int(student_id)}",
        org_id=platform_store.org_id_for_slug(slug),
    )
    cloud_store.save_database(db, object_path)
    teacher_registry.note_usernames(slug, db)
    return True, ""


# ==================== 页面 ====================
def page_platform_overview() -> None:
    """平台管理员专用的运营概览，不展示任何学生内容。"""
    my_slug = _require_admin("平台概览")
    st.title("平台概览")
    st.caption(
        "这里只显示机构、账号数量和存储情况，不显示学生姓名、成绩或试卷内容。"
    )

    rows = build_platform_overview()
    if not rows:
        st.info("还没有机构记录。")
    else:
        total_teachers = sum(int(row["teacher_count"]) for row in rows)
        total_students = sum(int(row["student_count"]) for row in rows)
        total_courses = sum(int(row["course_count_30d"]) for row in rows)
        total_exams = sum(int(row["exam_count"]) for row in rows)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("机构", len(rows))
        m2.metric("老师", total_teachers)
        m3.metric("学生", total_students)
        m4.metric("近 30 天课程", total_courses)
        m5.metric("考试", total_exams)

        table = pd.DataFrame(
            [
                {
                    "机构": row["name"],
                    "类型": row["kind"],
                    "状态": row["status"],
                    "老师数": row["teacher_count"],
                    "学生数": row["student_count"],
                    "近30天课程": row["course_count_30d"],
                    "考试数": row["exam_count"],
                    "最近登录": _ago(row["last_login"]),
                    "照片/PDF": (
                        "暂时无法统计"
                        if row["media_count"] is None
                        else f"{row['media_count']} 个 · {_format_bytes(row['media_bytes'])}"
                    ),
                    "创建时间": row["created_at"] or "—",
                    "读取状态": (
                        "部分统计暂不可用"
                        if row["read_error"] or row["media_error"]
                        else "正常"
                    ),
                }
                for row in rows
            ]
        )
        st.dataframe(table, hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("演示机构与使用摘要")
    demo_col, summary_col = st.columns(2)
    with demo_col:
        st.markdown("**演示机构**")
        st.caption(
            "一键清空并重建一套虚构的课程、学生、成绩和账号，适合给其他机构体验。"
            "账号密码随机生成，只显示一次。"
        )
        if st.button(
            "重置演示机构",
            use_container_width=True,
            key="platform_reset_demo",
        ):
            try:
                with st.spinner("正在重建演示数据..."):
                    ok, err, credentials = demo_org.reset_demo(my_slug)
                if ok:
                    st.session_state["demo_credentials_once"] = credentials
                    st.rerun()
                else:
                    st.error(err)
            except Exception as exc:
                st.error(f"重置演示机构失败：{exc}")
        credentials = st.session_state.pop("demo_credentials_once", None)
        if isinstance(credentials, dict):
            st.success("演示机构已重建。下面的账号密码关闭或刷新后不再显示。")
            st.code(
                "老师账号：{teacher_username}\n老师密码：{teacher_password}\n"
                "学生账号：{student_username}\n学生密码：{student_password}".format(
                    **credentials
                ),
                language=None,
            )

    with summary_col:
        st.markdown("**对外使用摘要**")
        st.caption(
            "只导出机构数、老师数、累计课程数和考试数。"
            "未授权公开名称的机构会合并成一行，不显示名称。"
        )
        if st.button(
            "导出使用摘要",
            use_container_width=True,
            key="platform_export_summary",
        ):
            try:
                data = demo_org.export_usage_summary(my_slug)
                st.session_state["usage_summary_ready"] = data
            except Exception as exc:
                st.error(f"导出使用摘要失败：{exc}")
        summary_data = st.session_state.get("usage_summary_ready")
        if summary_data:
            st.download_button(
                "下载使用摘要 CSV",
                data=summary_data,
                file_name=f"平台使用摘要_{date.today().isoformat()}.csv",
                mime="text/csv",
                use_container_width=True,
                key="platform_download_summary",
            )

    st.divider()
    st.subheader("临时只读查看老师数据")
    st.caption(
        "必须填写理由、机构同意方式和日期，并勾选已获同意。进入后 30 分钟自动退出，"
        "期间不能修改课程、学生、成绩或账号。"
    )
    organizations = platform_store.list_organizations()
    org_options = {
        int(org["id"]): f"{org.get('name') or '未命名机构'}"
        + (f"（{org.get('kind') or '机构'}）" if org.get("kind") else "")
        for org in organizations
        if org.get("status") != "停用"
    }
    if not org_options:
        st.info("暂无可用机构。")
    else:
        selected_org = st.selectbox(
            "选择机构",
            list(org_options),
            format_func=org_options.get,
            key="impersonation_org",
        )
        members = platform_store.list_org_users(int(selected_org))
        member_options = {}
        for member in members:
            slug = cloud_store.normalize_slug(member.get("slug")) or cloud_store.DEFAULT_SLUG
            entry = teacher_registry.teacher_entry(slug) or {}
            if not bool(entry.get("active", True)):
                continue
            label = str(entry.get("display_name") or "").strip() or str(
                entry.get("username") or slug
            )
            member_options[slug] = label
        if not member_options:
            st.info("这个机构暂时没有可查看的老师。")
        else:
            with st.form("platform_impersonation_form"):
                selected_teacher = st.selectbox(
                    "选择老师",
                    list(member_options),
                    format_func=member_options.get,
                    key="impersonation_teacher",
                )
                reason = st.selectbox(
                    "查看理由",
                    list(impersonation.REASONS),
                    key="impersonation_reason",
                )
                note = st.text_input(
                    "补充说明",
                    placeholder="必填，例如：老师反馈课程时间显示异常，需要核对",
                    key="impersonation_note",
                )
                consent_method = st.selectbox(
                    "机构同意方式",
                    list(impersonation.CONSENT_METHODS),
                    key="impersonation_consent_method",
                )
                consent_date = st.date_input(
                    "机构同意日期",
                    value=date.today(),
                    key="impersonation_consent_date",
                )
                consent = st.checkbox(
                    "已获得机构同意",
                    key="impersonation_consent",
                )
                submitted = st.form_submit_button(
                    "开始只读查看",
                    type="primary",
                    use_container_width=True,
                )
            if submitted:
                if not consent:
                    st.error("请先确认已获得机构同意。")
                else:
                    ok, err = impersonation.start(
                        org_id=int(selected_org),
                        teacher_slug=selected_teacher,
                        reason=reason,
                        note=note,
                        consent_method=consent_method,
                        consent_date=consent_date,
                        consent_confirmed=consent,
                        actor_user=um.current_user() or {},
                    )
                    if ok:
                        st.rerun()
                    else:
                        st.error(err)

    st.divider()
    st.subheader("存储与运行")
    s1, s2 = st.columns(2)
    s1.metric("数据库大小", _format_bytes(_database_size_bytes(rows)))
    s2.metric("最近一次备份", latest_backup_at() or "暂无备份记录")

    with st.expander("最近 20 条系统错误", expanded=False):
        st.caption("系统错误只记录模块、异常类型和简短技术说明，不包含学生内容。")
        try:
            errors = platform_store.list_system_errors(20)
        except Exception:
            errors = []
        if not errors:
            st.info("暂无系统错误记录。")
        else:
            error_table = pd.DataFrame(
                [
                    {
                        "时间": row.get("at") or "—",
                        "模块": row.get("module") or "—",
                        "类型": row.get("error_type") or "—",
                        "说明": row.get("detail") or "—",
                    }
                    for row in errors
                ]
            )
            st.dataframe(error_table, hide_index=True, use_container_width=True)


def page_admin() -> None:
    my_slug = _require_admin()
    st.title("账号管理")
    st.caption("只有平台管理员能看到这一页；这里只管理机构和老师账号，不查看学生内容。")

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

    with st.expander("危险操作：删除机构及全部数据", expanded=False):
        st.warning(
            "删除后不可恢复。会同时清除该机构的老师、学生数据、课程、成绩、"
            "照片、数据库备份和登录账号。删除前请先导出备份。"
        )
        admin_slugs = {
            cloud_store.normalize_slug(item.get("slug")) or cloud_store.DEFAULT_SLUG
            for item in teacher_registry.list_teachers()
            if bool(item.get("is_admin")) or bool(item.get("protected"))
        }
        for org in organizations:
            org_id = int(org["id"])
            members = platform_store.list_org_users(org_id)
            member_slugs = {
                cloud_store.normalize_slug(item.get("slug"))
                or cloud_store.DEFAULT_SLUG
                for item in members
            }
            protected = bool(member_slugs & admin_slugs)
            with st.container(border=True):
                st.markdown(f"**{org.get('name') or '未命名机构'}**")
                if protected:
                    st.caption("包含平台管理员或受保护教学账号，禁止删除。")
                    continue
                st.caption(
                    f"将删除 {len(members)} 位老师及其全部业务数据。"
                    f"请输入机构名称「{org.get('name') or ''}」确认。"
                )
                with st.form(f"delete_org_{org_id}"):
                    confirm_name = st.text_input(
                        "输入机构名称",
                        key=f"delete_org_name_{org_id}",
                    )
                    confirmed = st.form_submit_button(
                        "永久删除这个机构",
                        type="primary",
                    )
                if confirmed:
                    ok, err = delete_organization(
                        org_id,
                        confirm_name,
                        actor_slug=my_slug,
                    )
                    if ok:
                        st.session_state["admin_org_deleted"] = str(
                            org.get("name") or ""
                        )
                        st.rerun()
                    else:
                        st.error(err)
        deleted_org = st.session_state.pop("admin_org_deleted", None)
        if deleted_org:
            st.success(f"机构「{deleted_org}」及其全部数据已删除。")

    st.divider()

    # 平台管理员是独立账号，不参与老师数量、学生汇总和老师管理列表。
    entries = [
        entry
        for entry in teacher_registry.list_teachers()
        if not bool(entry.get("is_admin"))
    ]
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

            if entry.get("is_admin"):
                st.caption("管理员账号不能停用——停用后平台就进不去了。")
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
                    placeholder="账号管理里怎么称呼他", key=f"rename_display_{slug}",
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
