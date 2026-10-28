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

import cloud_store

ROLE_TEACHER = "teacher"
ROLE_ORG_ADMIN = "org_admin"
ROLE_LABELS = {
    ROLE_TEACHER: "老师",
    ROLE_ORG_ADMIN: "机构管理员",
}
ORG_KINDS = ("机构", "个人")
ORG_STATUSES = ("体验", "正式", "停用")
DEFAULT_ORG_NAME = "默认机构"

_lock = threading.RLock()
_base_dir: Path | None = None


def configure(base_dir: str | Path) -> None:
    global _base_dir
    _base_dir = Path(base_dir)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def platform_db_path() -> Path:
    if _base_dir is None:
        raise RuntimeError("platform_store 还没有 configure")
    return _base_dir / "platform.db"


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
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
        """
    )
    # 兼容以后可能存在的旧版本表：只加字段，不删字段。
    _ensure_column(conn, "organizations", "kind", "TEXT NOT NULL DEFAULT '机构'")


def _save() -> None:
    path = platform_db_path()
    if path.is_file():
        cloud_store.save_database(path, cloud_store.platform_object_path())


def ensure_ready(teachers: list[dict] | None = None) -> int:
    """创建平台库、默认机构，并把现有老师登记到默认机构。

    这个函数可以重复执行：已有机构不会重复创建，已有老师不会重复插入。
    返回默认机构 id。
    """
    with _lock:
        path = platform_db_path()
        try:
            cloud_store.prepare_database(path, cloud_store.platform_object_path())
        except Exception as exc:
            # 云端暂时不可用不应阻断本地老师登录；下一次保存时会再尝试同步。
            print(f"[platform_store] 读取平台库失败：{exc}", file=sys.stderr)

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


def is_org_admin(slug: str | None, org_id: int | None = None) -> bool:
    row = org_user_for_slug(slug)
    if not row or row.get("role") != ROLE_ORG_ADMIN:
        return False
    return org_id is None or int(row["org_id"]) == int(org_id)


def is_personal_org(org_id: int | None) -> bool:
    org = get_organization(org_id)
    return bool(org and org.get("kind") == "个人")
