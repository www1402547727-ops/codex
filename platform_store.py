"""平台级机构与权限数据。

老师数据仍然是一人一份 SQLite；这里只保存跨老师、跨机构需要的少量元数据：

- organizations：机构本身；
- org_user_roles：某位老师属于哪个机构，以及他是不是机构管理员。

登录路由仍然由 teacher_registry 的 teachers.json 负责。老师库里的
users.org_id 是机构归属的冗余字段，用于后续查询和审计，不改变现有
users.role 的 CHECK 约束。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import cloud_store

try:
    import streamlit as st
except Exception:
    st = None

ROLE_TEACHER = "teacher"
ROLE_ORG_ADMIN = "org_admin"
ROLE_LABELS = {
    ROLE_TEACHER: "老师",
    ROLE_ORG_ADMIN: "机构管理员",
}
ORG_KINDS = ("机构", "个人")
ORG_STATUSES = ("体验", "正式", "停用")
DEFAULT_ORG_NAME = "默认机构"
PLATFORM_CONTACT_KEY = "notice_contact"

_lock = threading.RLock()
_base_dir: Path | None = None
_recovery_blocked = False
IMPERSONATION_KEY = "impersonation"

_WRITE_KEYWORDS = {
    "insert",
    "update",
    "delete",
    "replace",
    "create",
    "alter",
    "drop",
    "vacuum",
    "attach",
    "detach",
    "reindex",
}


def configure(base_dir: str | Path) -> None:
    global _base_dir, _recovery_blocked
    _base_dir = Path(base_dir)
    _recovery_blocked = False


class PlatformStoreUnavailable(RuntimeError):
    """平台库读取失败时，禁止任何本地初始化和云端覆盖。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def platform_db_path() -> Path:
    if _base_dir is None:
        raise RuntimeError("platform_store 还没有 configure")
    if _recovery_blocked:
        raise PlatformStoreUnavailable(
            "平台库读取失败，已禁止初始化和上传，请刷新页面重试。"
        )
    return _base_dir / "platform.db"


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
    if _recovery_blocked:
        raise PlatformStoreUnavailable(
            "平台库读取失败，已禁止初始化、写入和上传，请刷新页面重试。"
        )
    target = Path(path) if path is not None else platform_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _init_schema(conn: sqlite3.Connection) -> None:
    if _recovery_blocked:
        raise PlatformStoreUnavailable(
            "平台库读取失败，已禁止初始化，请刷新页面重试。"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS organizations(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            status TEXT NOT NULL DEFAULT '体验'
                CHECK(status IN ('体验','正式','停用')),
            created_at TEXT NOT NULL DEFAULT '',
            public_name_ok INTEGER NOT NULL DEFAULT 0,
            public_name_ok_at TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '机构'
                CHECK(kind IN ('机构','个人'))
        );
        CREATE TABLE IF NOT EXISTS org_user_roles(
            id INTEGER PRIMARY KEY,
            org_id INTEGER NOT NULL,
            slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
            role TEXT NOT NULL DEFAULT 'teacher'
                CHECK(role IN ('teacher','org_admin')),
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_org_user_roles_org
            ON org_user_roles(org_id, role);
        CREATE TABLE IF NOT EXISTS audit_logs(
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            actor_user_id INTEGER,
            actor_role TEXT NOT NULL DEFAULT '',
            org_id INTEGER,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL DEFAULT '',
            target_id TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_org_time
            ON audit_logs(org_id, at);
        CREATE INDEX IF NOT EXISTS idx_audit_actor_time
            ON audit_logs(actor_role, at);
        CREATE TABLE IF NOT EXISTS teacher_notice_consents(
            username TEXT NOT NULL COLLATE NOCASE,
            version TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            PRIMARY KEY(username, version)
        );
        CREATE INDEX IF NOT EXISTS idx_teacher_notice_consents_user
            ON teacher_notice_consents(username, accepted_at);
        CREATE TABLE IF NOT EXISTS platform_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS system_errors(
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            module TEXT NOT NULL DEFAULT '',
            error_type TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_system_errors_time
            ON system_errors(at);
        """
    )
    # 兼容以后可能存在的旧版本表：只加字段，不删字段。
    _ensure_column(conn, "organizations", "kind", "TEXT NOT NULL DEFAULT '机构'")


def _save() -> None:
    if _recovery_blocked:
        raise PlatformStoreUnavailable(
            "平台库读取失败，已禁止上传，请刷新页面重试。"
        )
    path = platform_db_path()
    if path.is_file():
        cloud_store.save_database(path, cloud_store.platform_object_path())


def ensure_ready(teachers: list[dict] | None = None) -> int:
    """创建平台库、默认机构，并把现有老师登记到默认机构。

    这个函数可以重复执行：已有机构不会重复创建，已有老师不会重复插入。
    返回默认机构 id。
    """
    global _recovery_blocked
    with _lock:
        if _base_dir is None:
            raise RuntimeError("platform_store 还没有 configure")
        # 这里绕开 platform_db_path 的阻断检查，让下一次请求仍可重新尝试读取。
        path = _base_dir / "platform.db"
        try:
            cloud_store.prepare_database(path, cloud_store.platform_object_path())
        except Exception as exc:
            _recovery_blocked = True
            print(f"[platform_store] 读取平台库失败：{exc}", file=sys.stderr)
            raise PlatformStoreUnavailable(
                "平台库读取失败，已停止初始化和上传，请刷新页面重试。"
            ) from exc
        _recovery_blocked = False

        conn = _connect(path)
        try:
            _init_schema(conn)
            row = conn.execute(
                "SELECT id FROM organizations ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    """INSERT INTO organizations(
                           name, status, created_at, public_name_ok,
                           public_name_ok_at, note, kind
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        DEFAULT_ORG_NAME,
                        "正式",
                        _now(),
                        0,
                        "",
                        "系统迁移创建",
                        "机构",
                    ),
                )
                default_org_id = int(cur.lastrowid)
            else:
                default_org_id = int(row["id"])

            for item in teachers or []:
                # 平台管理员不是机构老师，不能自动登记进默认机构。
                if bool(item.get("is_admin")) or bool(item.get("personal")):
                    continue
                slug = cloud_store.normalize_slug(item.get("slug")) or cloud_store.DEFAULT_SLUG
                if not slug:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM org_user_roles WHERE slug=? LIMIT 1",
                    (slug,),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        """INSERT INTO org_user_roles(org_id, slug, role, created_at)
                           VALUES(?,?,?,?)""",
                        (default_org_id, slug, ROLE_TEACHER, _now()),
                    )
            conn.commit()
        finally:
            conn.close()

        try:
            _save()
        except Exception as exc:
            print(f"[platform_store] 保存平台库失败：{exc}", file=sys.stderr)
        return default_org_id


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ==================== 只读代入 / 内容访问闸门 ====================
def _session_get(name: str, default=None):
    if st is None:
        return default
    try:
        return st.session_state.get(name, default)
    except Exception:
        return default


def current_impersonation() -> dict | None:
    value = _session_get(IMPERSONATION_KEY)
    return value if isinstance(value, dict) else None


def is_impersonating() -> bool:
    return current_impersonation() is not None


def _first_sql_keyword(sql: str) -> str:
    text = str(sql or "").lstrip()
    while text.startswith("--"):
        newline = text.find("\n")
        if newline < 0:
            return ""
        text = text[newline + 1 :].lstrip()
    while text.startswith("/*"):
        end = text.find("*/")
        if end < 0:
            return ""
        text = text[end + 2 :].lstrip()
    return text.split(None, 1)[0].lower().strip(";") if text else ""


def is_write_sql(sql: str) -> bool:
    keyword = _first_sql_keyword(sql)
    if keyword in _WRITE_KEYWORDS:
        return True
    if keyword == "pragma":
        return "=" in str(sql)
    return False


def assert_write_allowed() -> None:
    """代入查看期间，所有业务库写入都从这里统一拒绝。"""
    if is_impersonating():
        raise PermissionError("代入查看为只读模式，不能修改任何数据。")


def assert_content_read_allowed() -> None:
    """平台管理员未代入时，不允许读取任何老师/学生内容。"""
    actor_role = str(_session_get("auth_actor_role", "") or "")
    if actor_role == "platform_admin" and not is_impersonating():
        raise PermissionError("平台管理员需要先通过「临时只读查看」进入老师数据。")


def assert_content_write_allowed() -> None:
    """内容页写入只允许普通老师；平台管理员即使代入也不能写。"""
    actor_role = str(_session_get("auth_actor_role", "") or "")
    if actor_role == "platform_admin":
        if is_impersonating():
            raise PermissionError("代入查看为只读模式，不能修改任何数据。")
        raise PermissionError("平台管理员不能直接修改老师数据。")


class GuardedConnection:
    """SQLite 连接包装器：在统一位置拦截代入期间的写语句。

    content_scope=True 时，还会阻止平台管理员未代入就读取内容数据。
    """

    def __init__(self, conn: sqlite3.Connection, *, content_scope: bool = False):
        self._conn = conn
        self._content_scope = bool(content_scope)

    def _before(self, sql: str) -> None:
        if self._content_scope:
            assert_content_read_allowed()
        if is_write_sql(sql):
            assert_write_allowed()

    def execute(self, sql: str, parameters=()):
        self._before(sql)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, parameters):
        self._before(sql)
        return self._conn.executemany(sql, parameters)

    def executescript(self, sql_script: str):
        if self._content_scope:
            assert_content_read_allowed()
        if is_impersonating():
            assert_write_allowed()
        elif is_write_sql(sql_script):
            assert_write_allowed()
        return self._conn.executescript(sql_script)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._conn.__exit__(exc_type, exc, traceback)


def guard_connection(
    conn: sqlite3.Connection, *, content_scope: bool = False
) -> GuardedConnection:
    return GuardedConnection(conn, content_scope=content_scope)


def list_organizations() -> list[dict]:
    path = platform_db_path()
    if not path.is_file():
        return []
    conn = _connect(path)
    try:
        _init_schema(conn)
        rows = conn.execute(
            """SELECT id, name, status, created_at, public_name_ok,
                      public_name_ok_at, note, kind
               FROM organizations ORDER BY id"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_organization(org_id: int | None) -> dict | None:
    if org_id is None:
        return None
    path = platform_db_path()
    if not path.is_file():
        return None
    conn = _connect(path)
    try:
        _init_schema(conn)
        row = conn.execute(
            """SELECT id, name, status, created_at, public_name_ok,
                      public_name_ok_at, note, kind
               FROM organizations WHERE id=? LIMIT 1""",
            (int(org_id),),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def default_org_id() -> int | None:
    path = platform_db_path()
    if not path.is_file():
        return None
    conn = _connect(path)
    try:
        _init_schema(conn)
        row = conn.execute(
            "SELECT id FROM organizations ORDER BY id LIMIT 1"
        ).fetchone()
        return int(row["id"]) if row else None
    finally:
        conn.close()


def create_organization(
    name: str,
    *,
    kind: str = "机构",
    status: str = "体验",
    note: str = "",
    public_name_ok: bool = False,
) -> tuple[int | None, str]:
    clean_name = str(name or "").strip()
    if not clean_name:
        return None, "请填写机构名称"
    if kind not in ORG_KINDS:
        return None, "机构类型不合法"
    if status not in ORG_STATUSES:
        return None, "机构状态不合法"
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            if conn.execute(
                "SELECT 1 FROM organizations WHERE name=? COLLATE NOCASE",
                (clean_name,),
            ).fetchone():
                return None, "这个机构名称已经存在了"
            cur = conn.execute(
                """INSERT INTO organizations(
                       name, status, created_at, public_name_ok,
                       public_name_ok_at, note, kind
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    clean_name,
                    status,
                    _now(),
                    1 if public_name_ok else 0,
                    _now() if public_name_ok else "",
                    str(note or ""),
                    kind,
                ),
            )
            conn.commit()
            org_id = int(cur.lastrowid)
        finally:
            conn.close()
        _save()
        return org_id, ""


def org_user_for_slug(slug: str | None) -> dict | None:
    target = cloud_store.normalize_slug(slug) or cloud_store.DEFAULT_SLUG
    path = platform_db_path()
    if not path.is_file():
        return None
    conn = _connect(path)
    try:
        _init_schema(conn)
        row = conn.execute(
            "SELECT id, org_id, slug, role, created_at FROM org_user_roles WHERE slug=? LIMIT 1",
            (target,),
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def org_id_for_slug(slug: str | None) -> int | None:
    row = org_user_for_slug(slug)
    return int(row["org_id"]) if row else None


def list_org_users(org_id: int) -> list[dict]:
    path = platform_db_path()
    if not path.is_file():
        return []
    conn = _connect(path)
    try:
        _init_schema(conn)
        rows = conn.execute(
            """SELECT id, org_id, slug, role, created_at
               FROM org_user_roles WHERE org_id=? ORDER BY id""",
            (int(org_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def add_org_user(org_id: int, slug: str, role: str = ROLE_TEACHER) -> tuple[bool, str]:
    target = cloud_store.normalize_slug(slug) or cloud_store.DEFAULT_SLUG
    if role not in (ROLE_TEACHER, ROLE_ORG_ADMIN):
        return False, "机构权限不合法"
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            if conn.execute(
                "SELECT 1 FROM organizations WHERE id=?", (int(org_id),)
            ).fetchone() is None:
                return False, "机构不存在"
            existing = conn.execute(
                "SELECT org_id, role FROM org_user_roles WHERE slug=? LIMIT 1",
                (target,),
            ).fetchone()
            if existing is not None:
                if int(existing["org_id"]) != int(org_id):
                    return False, "这位老师已经属于其他机构"
                conn.execute(
                    "UPDATE org_user_roles SET role=? WHERE slug=?",
                    (role, target),
                )
            else:
                conn.execute(
                    """INSERT INTO org_user_roles(org_id, slug, role, created_at)
                       VALUES(?,?,?,?)""",
                    (int(org_id), target, role, _now()),
                )
            conn.commit()
        finally:
            conn.close()
        _save()
        return True, ""


def has_accepted_teacher_notice(
    username: str,
    *,
    version: str,
) -> bool:
    """查询平台库中的老师说明同意记录。"""
    clean_username = str(username or "").strip()
    clean_version = str(version or "").strip()
    if not clean_username or not clean_version:
        return False
    path = platform_db_path()
    if not path.is_file():
        return False
    conn = _connect(path)
    try:
        _init_schema(conn)
        row = conn.execute(
            """SELECT 1 FROM teacher_notice_consents
               WHERE username=? COLLATE NOCASE AND version=?
               LIMIT 1""",
            (clean_username, clean_version),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def accept_teacher_notice(
    username: str,
    *,
    version: str,
    accepted_at: str = "",
) -> bool:
    """写入平台库中的老师说明同意记录，重复提交保持幂等。"""
    clean_username = str(username or "").strip()
    clean_version = str(version or "").strip()
    if not clean_username or not clean_version:
        return False
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            cur = conn.execute(
                """INSERT OR IGNORE INTO teacher_notice_consents(
                       username, version, accepted_at
                   ) VALUES(?,?,?)""",
                (
                    clean_username,
                    clean_version,
                    str(accepted_at or _now()),
                ),
            )
            inserted = int(cur.rowcount or 0) > 0
            conn.commit()
        finally:
            conn.close()
        if inserted:
            _save()
        return inserted


def list_teacher_notice_consents() -> list[dict]:
    """列出平台库中的老师说明同意记录，不读取老师业务数据。"""
    path = platform_db_path()
    if not path.is_file():
        return []
    conn = _connect(path)
    try:
        _init_schema(conn)
        rows = conn.execute(
            """SELECT username, version, accepted_at
               FROM teacher_notice_consents
               ORDER BY accepted_at, username"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_platform_setting(name: str, default: str = "") -> str:
    """读取平台级设置；平台库不存在时返回默认值。"""
    key = str(name or "").strip()
    if not key:
        return str(default or "")
    path = platform_db_path()
    if not path.is_file():
        return str(default or "")
    conn = _connect(path)
    try:
        _init_schema(conn)
        row = conn.execute(
            "SELECT value FROM platform_settings WHERE key=? LIMIT 1",
            (key,),
        ).fetchone()
        return str(row["value"] or "") if row is not None else str(default or "")
    finally:
        conn.close()


def set_platform_setting(name: str, value: str) -> None:
    """写入平台级设置并同步平台库。"""
    key = str(name or "").strip()
    if not key:
        raise ValueError("平台设置名不能为空")
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            conn.execute(
                """INSERT INTO platform_settings(key, value, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,
                       updated_at=excluded.updated_at""",
                (key, str(value or "").strip(), _now()),
            )
            conn.commit()
        finally:
            conn.close()
        _save()


def get_contact_info() -> str:
    """老师端体验版说明中显示的平台联系方式。"""
    return get_platform_setting(PLATFORM_CONTACT_KEY, "").strip()


def set_contact_info(value: str) -> None:
    """设置老师端体验版说明中的联系方式，不改变说明版本号。"""
    set_platform_setting(PLATFORM_CONTACT_KEY, value)


def delete_organization_record(org_id: int) -> bool:
    """删除机构及其成员关系；审计日志保留，不在这里删除。"""
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            exists = conn.execute(
                "SELECT 1 FROM organizations WHERE id=? LIMIT 1",
                (int(org_id),),
            ).fetchone()
            if exists is None:
                return False
            conn.execute(
                "DELETE FROM org_user_roles WHERE org_id=?",
                (int(org_id),),
            )
            conn.execute(
                "DELETE FROM organizations WHERE id=?",
                (int(org_id),),
            )
            conn.commit()
        finally:
            conn.close()
        _save()
        return True


def is_org_admin(slug: str | None, org_id: int | None = None) -> bool:
    row = org_user_for_slug(slug)
    if not row or row.get("role") != ROLE_ORG_ADMIN:
        return False
    return org_id is None or int(row["org_id"]) == int(org_id)


def is_personal_org(org_id: int | None) -> bool:
    org = get_organization(org_id)
    return bool(org and org.get("kind") == "个人")


# ==================== 统一审计 ====================
def current_actor() -> dict:
    """从会话读取当前操作者；没有会话时返回匿名身份。"""
    user_id = None
    role = ""
    org_id = None
    if st is not None:
        try:
            user_id = st.session_state.get("auth_user_id")
            role = str(st.session_state.get("auth_actor_role") or "")
            org_id = st.session_state.get("auth_org_id")
        except Exception:
            pass
    slug = cloud_store.current_slug()
    if org_id is None:
        org_id = org_id_for_slug(slug)
    if not role:
        member = org_user_for_slug(slug)
        if member:
            role = str(member.get("role") or ROLE_TEACHER)
        elif user_id:
            role = "teacher"
    return {
        "actor_user_id": int(user_id) if user_id else None,
        "actor_role": role or "anonymous",
        "org_id": int(org_id) if org_id is not None else None,
    }


def record_audit(
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_role: str = "",
    org_id: int | None = None,
    target_type: str = "",
    target_id: str | int = "",
    detail: str = "",
    reason: str = "",
) -> None:
    """写一条平台级审计记录。detail 只放类型、id 和原因码，不放学生内容。"""
    clean_action = str(action or "").strip()
    if not clean_action:
        return
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            conn.execute(
                """INSERT INTO audit_logs(
                       at, actor_user_id, actor_role, org_id, action,
                       target_type, target_id, detail, reason
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    _now(),
                    int(actor_user_id) if actor_user_id is not None else None,
                    str(actor_role or ""),
                    int(org_id) if org_id is not None else None,
                    clean_action,
                    str(target_type or ""),
                    str(target_id or ""),
                    str(detail or ""),
                    str(reason or ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            _save()
        except Exception as exc:
            print(f"[platform_store] 保存审计日志失败：{exc}", file=sys.stderr)


def record_current_audit(
    action: str,
    *,
    target_type: str = "",
    target_id: str | int = "",
    detail: str = "",
    reason: str = "",
    org_id: int | None = None,
) -> None:
    actor = current_actor()
    record_audit(
        action,
        actor_user_id=actor["actor_user_id"],
        actor_role=actor["actor_role"],
        # 平台管理员操作某家机构时，调用方必须显式传目标机构；
        # 不传才使用操作者自己的机构，避免日志全部落到默认机构。
        org_id=actor["org_id"] if org_id is None else int(org_id),
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        reason=reason,
    )


def list_audit(
    *,
    org_id: int | None = None,
    actor_role: str | None = None,
    limit: int = 100,
) -> list[dict]:
    path = platform_db_path()
    if not path.is_file():
        return []
    clauses = []
    params: list[object] = []
    if org_id is not None:
        clauses.append("org_id=?")
        params.append(int(org_id))
    if actor_role:
        clauses.append("actor_role=?")
        params.append(str(actor_role))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    conn = _connect(path)
    try:
        _init_schema(conn)
        rows = conn.execute(
            f"""SELECT at, actor_user_id, actor_role, org_id, action,
                       target_type, target_id, detail, reason
                FROM audit_logs{where}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ==================== 系统错误（只记技术信息） ====================
def record_system_error(
    module: str,
    error_type: str,
    detail: str = "",
    *,
    at: str | None = None,
) -> None:
    """记录平台级系统错误。

    只允许写模块、异常类型和简短技术说明，不写学生姓名、成绩或试卷内容。
    """
    with _lock:
        conn = _connect()
        try:
            _init_schema(conn)
            conn.execute(
                """INSERT INTO system_errors(at, module, error_type, detail)
                   VALUES(?,?,?,?)""",
                (
                    str(at or _now()),
                    str(module or "")[:120],
                    str(error_type or "")[:120],
                    str(detail or "")[:500],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            _save()
        except Exception as exc:
            print(f"[platform_store] 保存系统错误失败：{exc}", file=sys.stderr)


def list_system_errors(limit: int = 20) -> list[dict]:
    path = platform_db_path()
    if not path.is_file():
        return []
    conn = _connect(path)
    try:
        _init_schema(conn)
        rows = conn.execute(
            """SELECT at, module, error_type, detail
               FROM system_errors ORDER BY id DESC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
