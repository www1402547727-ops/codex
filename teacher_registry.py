"""老师名册：记录「谁在用这个网站、他的数据放在哪一份库里」。

这是多老师结构的地基。设计要点：
- 每位老师一份独立数据库，云端路径 database/<标识>/scores.db；
- 老数据(单老师时期那份)登记成 default 标识，路径不变，**一个字节都不用搬**；
- 名册本身是一个几 KB 的小文件，登录时只读它来定位该用哪份库，
  所以别人的数据从来不会下载到当前用户的会话里；
- 名册里同时缓存「登录名 → 库标识」的对应关系(自动刷新)，
  这样学生用自己的账号登录时也能被正确路由到所属老师的库。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

import streamlit as st

import cloud_store

ROSTER_FILENAME = "teachers.json"
# 本模块自己定义，不从 cloud_store 读：
# 一旦在模块加载阶段去读别的模块的属性，只要那边文件没更新到位就会在启动时直接崩掉。
# 取值必须和 cloud_store.DEFAULT_SLUG 保持一致（有测试守着）。
DEFAULT_SLUG = "default"
DEFAULT_TEACHER_USERNAME = "teacher"
RESERVED_USERNAMES = frozenset({"admin", "root", "platform", "administrator"})

_lock = threading.RLock()
_base_dir: Path | None = None
_roster: dict | None = None
_remote_read_failed = False


def configure(base_dir: str | Path) -> None:
    """告诉名册模块数据根目录在哪(由 app.py 调用)。"""
    global _base_dir, _roster, _remote_read_failed
    # 每次页面脚本运行都从云端重新确认一次；名册只有几 KB，
    # 不能因为同一进程里留有旧缓存而掩盖本次读取失败。
    _base_dir = Path(base_dir)
    _roster = None
    _remote_read_failed = False


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def roster_path() -> Path:
    if _base_dir is None:
        raise RuntimeError("teacher_registry 还没有 configure")
    return _base_dir / ROSTER_FILENAME


def _empty_roster() -> dict:
    return {"version": 1, "teachers": [], "usernames": {}, "settings": {}}


def _normalize(roster: dict | None) -> dict:
    out = _empty_roster()
    if isinstance(roster, dict):
        if isinstance(roster.get("teachers"), list):
            out["teachers"] = [t for t in roster["teachers"] if isinstance(t, dict)]
        if isinstance(roster.get("usernames"), dict):
            out["usernames"] = {str(k): str(v) for k, v in roster["usernames"].items()}
        if isinstance(roster.get("settings"), dict):
            out["settings"] = {str(k): v for k, v in roster["settings"].items()}
    return out


# ==================== 全局开关 ====================
# 允许老师给自己的学生开账号。默认关闭：老师开不了任何账号。
SETTING_TEACHERS_CAN_CREATE_STUDENTS = "teachers_can_create_students"


def get_setting(name: str, default=False):
    return load().get("settings", {}).get(name, default)


def set_setting(name: str, value) -> None:
    with _lock:
        roster = load()
        roster.setdefault("settings", {})[name] = value
        save(roster, strict=True)


# ==================== 读写 ====================
def _read_local() -> dict | None:
    path = roster_path()
    if not path.is_file():
        return None
    try:
        return _normalize(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _read_remote() -> dict | None:
    global _remote_read_failed
    if not cloud_store.is_cloud():
        return None
    try:
        text = cloud_store.download_text(cloud_store.roster_object_path())
    except Exception as exc:
        print(f"[teacher_registry] 读取云端名册失败：{exc}", file=sys.stderr)
        _remote_read_failed = True
        raise RosterUnavailable(
            "暂时读不到云端老师名册，已停止初始化和上传，请刷新页面重试。"
        ) from exc
    if text is None:
        _remote_read_failed = False
        return None
    try:
        roster = _normalize(json.loads(text))
    except Exception as exc:
        _remote_read_failed = True
        raise RosterUnavailable(
            "云端老师名册内容无法读取，已停止初始化和上传，请刷新页面重试。"
        ) from exc
    if not _is_usable(roster):
        _remote_read_failed = True
        raise RosterUnavailable(
            "云端老师名册没有可用账号，已停止初始化和上传，请联系维护人员。"
        )
    _remote_read_failed = False
    return roster


def save(roster: dict, strict: bool = False) -> None:
    global _roster
    if cloud_store.is_cloud() and _remote_read_failed:
        raise RosterUnavailable(
            "云端老师名册读取失败，已禁止写入或上传，请刷新页面重试。"
        )
    data = _normalize(roster)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path = roster_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
    _roster = data
    if not cloud_store.is_cloud():
        return
    try:
        cloud_store.upload_text(cloud_store.roster_object_path(), text)
    except Exception as exc:
        # 名册上传失败不该拦住老师干活；但如果是管理员主动加老师，必须让他知道。
        print(f"[teacher_registry] 名册上传云端失败：{exc}", file=sys.stderr)
        if strict:
            raise


def load(force: bool = False) -> dict:
    global _roster
    with _lock:
        if _roster is not None and not force:
            return _roster
        if cloud_store.is_cloud():
            data = _read_remote()
            if data is None:
                data = _read_local()
        else:
            data = _read_local()
        if data is None:
            data = _empty_roster()
        _roster = data
        return _roster


# ==================== 首次运行：把现有数据登记成默认老师 ====================
def _read_db_accounts(db_path: str | Path) -> tuple[str, list[str]]:
    """从一份数据库里读出(第一位老师账号, 所有登录名)。只读，不改动原库。"""
    path = Path(db_path)
    if not path.is_file():
        return "", []
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except Exception:
        return "", []
    try:
        rows = conn.execute("SELECT username, role FROM users ORDER BY id").fetchall()
    except Exception:
        return "", []
    finally:
        conn.close()
    names = [str(r[0]) for r in rows if r and r[0]]
    teacher = next((str(r[0]) for r in rows if r and len(r) > 1 and r[1] == "teacher" and r[0]), "")
    return teacher, names


class RosterUnavailable(RuntimeError):
    """读不到现有账号时不允许生成名册——否则会写一份空名册上去，把所有人锁在门外。"""


def bootstrap(db_path: str | Path) -> dict:
    """首次运行：把现有这份库登记成默认老师(管理员)，并把库里的账号记进名册。"""
    teacher, names = _read_db_accounts(db_path)
    if not names:
        # 这里是安全阀：数据库还没下载下来 / 读取失败时，
        # 绝不能写一份没有账号的名册上去。宁可报错让用户重试，也不能全员被锁。
        raise RosterUnavailable(
            "暂时读不到现有数据库里的账号列表，已暂停生成名册，请刷新页面重试。"
        )
    roster = _empty_roster()
    roster["teachers"].append(
        {
            "username": teacher or names[0],
            "display_name": "",
            "slug": DEFAULT_SLUG,
            "active": True,
            "is_admin": True,
            "created_at": _now(),
            "note": "原有数据",
        }
    )
    for name in names:
        roster["usernames"][name] = DEFAULT_SLUG
    save(roster)
    return roster


def _is_usable(roster: dict | None) -> bool:
    return bool(roster) and bool(roster.get("teachers"))


def ensure_ready(db_path: str | Path) -> dict:
    """确保名册存在且可用；本地和云端都没有(或都不可用)时，按现有数据库生成一份。"""
    global _roster
    with _lock:
        candidate = load(force=True)
        if _is_usable(candidate):
            _roster = candidate
            return _roster
    return bootstrap(db_path)


# ==================== 查询 ====================
def list_teachers() -> list[dict]:
    return [t for t in load().get("teachers", []) if isinstance(t, dict)]


def teacher_entry(slug: str | None) -> dict | None:
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    for item in list_teachers():
        if (cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG) == target:
            return item
    return None


def is_slug_active(slug: str | None) -> bool:
    entry = teacher_entry(slug)
    return bool(entry.get("active", True)) if entry else False


def admin_slug() -> str | None:
    for item in list_teachers():
        if item.get("is_admin"):
            return cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG
    return None


def find_slug(username: str) -> str | None:
    """登录名 → 库标识。找不到或该老师已停用都返回 None。"""
    name = str(username or "").strip()
    if not name:
        return None
    roster = load()
    lowered = name.lower()
    for item in roster.get("teachers", []):
        if str(item.get("username") or "").strip().lower() == lowered:
            slug = cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG
            return slug if bool(item.get("active", True)) else None
    mapping = roster.get("usernames", {})
    slug = mapping.get(name)
    if slug is None:
        for key, value in mapping.items():
            if str(key).lower() == lowered:
                slug = value
                break
    if slug is None:
        return None
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    return target if is_slug_active(target) else None


def session_slug() -> str | None:
    """当前会话该用哪份库：已登录返回标识，未登录返回 None。"""
    try:
        username = st.session_state.get("auth_username")
    except Exception:
        username = None
    if not username:
        return None
    return find_slug(str(username))


def username_owner(username: str) -> str | None:
    """这个登录名当前归哪位老师；没被占用返回 None。"""
    if _base_dir is None:
        return None
    name = str(username or "").strip()
    if not name:
        return None
    lowered = name.lower()
    roster = load()
    for item in roster.get("teachers", []):
        if str(item.get("username") or "").strip().lower() == lowered:
            return cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG
    for key, value in roster.get("usernames", {}).items():
        if str(key).lower() == lowered:
            return cloud_store.normalize_slug(value) or DEFAULT_SLUG
    return None


def is_reserved_username(username: str) -> bool:
    return str(username or "").strip().lower() in RESERVED_USERNAMES


def validate_new_username(
    username: str,
    *,
    current_slug: str | None = None,
    allow_reserved: bool = False,
) -> tuple[bool, str]:
    """校验新建或改名时使用的登录名。"""
    name = str(username or "").strip()
    if not name:
        return False, "请填写登录账号"
    if is_reserved_username(name) and not allow_reserved:
        return False, f"账号「{name}」是系统保留名称，不能使用"
    if not name.isascii() or any(ch.isspace() for ch in name):
        return False, "登录账号请用英文字母或数字，不要有空格"
    owner = username_owner(name)
    current = (
        cloud_store.normalize_slug(current_slug) or DEFAULT_SLUG
        if current_slug is not None
        else None
    )
    if owner is not None and owner != current:
        return False, f"账号「{name}」已经被占用了，换一个"
    return True, ""


def note_usernames(slug: str, db_path: str | Path) -> bool:
    """把这位老师库里的登录名同步进名册，保证新开的学生账号也能登录。变了才写。

    注意：登录名在平台内必须唯一。如果某个名字已被别的老师占用，这里**不抢**，
    宁可让那一个账号登不进来（管理员改个名即可），也不能把它路由到错的库。
    """
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    _, names = _read_db_accounts(db_path)
    if not names:
        return False
    with _lock:
        roster = load()
        mapping = roster.setdefault("usernames", {})
        changed = False
        for name in names:
            current = mapping.get(name)
            if current is None:
                mapping[name] = target
                changed = True
            # current != target 时保持原样：已被别人占用，不覆盖
        for key in [k for k, v in mapping.items() if v == target and k not in names]:
            mapping.pop(key, None)
            changed = True
        if changed:
            save(roster)
        return changed


# ==================== 总后台用：开通/管理老师 ====================
def base_dir() -> Path:
    if _base_dir is None:
        raise RuntimeError("teacher_registry 还没有 configure")
    return _base_dir


def unique_slug(seed: str) -> str:
    """给新老师生成一个没被占用的库标识。"""
    base = cloud_store.normalize_slug(seed) or "teacher"
    if base == DEFAULT_SLUG:
        base = "teacher"
    taken = {
        (cloud_store.normalize_slug(t.get("slug")) or DEFAULT_SLUG)
        for t in list_teachers()
    }
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def add_teacher(
    username: str,
    display_name: str = "",
    slug: str | None = None,
    is_admin: bool = False,
    note: str = "",
    personal: bool = False,
) -> tuple[bool, str]:
    """把一位老师登记进名册（建库由调用方负责）。"""
    name = str(username or "").strip()
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    valid, error = validate_new_username(name)
    if not valid:
        return False, error
    if teacher_entry(target) is not None:
        return False, "这个数据标识已经被占用了"
    with _lock:
        roster = load()
        roster.setdefault("teachers", []).append(
            {
                "username": name,
                "display_name": str(display_name or "").strip(),
                "slug": target,
                "active": True,
                "is_admin": bool(is_admin),
                "personal": bool(personal),
                "created_at": _now(),
                "note": str(note or ""),
            }
        )
        roster.setdefault("usernames", {})[name] = target
        save(roster, strict=True)
    return True, ""


def rename_teacher(
    slug: str,
    new_username: str,
    *,
    allow_reserved: bool = False,
    **changes,
) -> None:
    """改名册里的登录名，并同步用户名映射。"""
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    name = str(new_username or "").strip()
    valid, error = validate_new_username(
        name,
        current_slug=target,
        allow_reserved=allow_reserved,
    )
    if not valid:
        raise ValueError(error)
    with _lock:
        roster = load()
        entry = next(
            (
                item
                for item in roster.get("teachers", [])
                if (cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG)
                == target
            ),
            None,
        )
        if entry is None:
            raise ValueError("找不到这位老师")
        old = str(entry.get("username") or "").strip()
        if old.lower() != name.lower():
            mapping = roster.setdefault("usernames", {})
            for key in [key for key in mapping if key.lower() == old.lower()]:
                mapping.pop(key, None)
            mapping[name] = target
        entry["username"] = name
        entry.update(changes)
        save(roster, strict=True)


def update_teacher(slug: str, **changes) -> None:
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    if "username" in changes:
        raise ValueError("修改登录账号请使用 rename_teacher()")
    with _lock:
        roster = load()
        for item in roster.get("teachers", []):
            if (cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG) == target:
                item.update(changes)
                break
        save(roster, strict=True)


def remove_teachers(slugs: list[str]) -> int:
    """从名册中彻底移除若干老师及其登录名映射，返回移除数量。"""
    targets = {
        cloud_store.normalize_slug(slug) or DEFAULT_SLUG
        for slug in (slugs or [])
        if str(slug or "").strip()
    }
    if not targets:
        return 0
    with _lock:
        roster = load()
        before = len(roster.get("teachers", []))
        roster["teachers"] = [
            item
            for item in roster.get("teachers", [])
            if (cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG)
            not in targets
        ]
        mapping = roster.get("usernames", {})
        roster["usernames"] = {
            name: slug
            for name, slug in mapping.items()
            if (cloud_store.normalize_slug(slug) or DEFAULT_SLUG) not in targets
        }
        removed = before - len(roster["teachers"])
        if removed or roster["usernames"] != mapping:
            save(roster, strict=True)
        return removed


def compute_stats(db_path: str | Path) -> dict:
    """算一位老师的汇总（学生数、本月课时等）。只读，不改动原库。"""
    path = Path(db_path)
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except Exception:
        return {}

    def count(sql: str, params=()) -> int:
        try:
            return int(conn.execute(sql, params).fetchone()[0] or 0)
        except Exception:
            return 0

    try:
        month = datetime.now().strftime("%Y-%m")
        return {
            "students": count("SELECT COUNT(*) FROM students"),
            "classes": count("SELECT COUNT(*) FROM classes"),
            "one_to_one": count("SELECT COUNT(*) FROM one_to_one_students"),
            "lessons_month": count(
                "SELECT COUNT(*) FROM lessons WHERE lesson_date LIKE ?", (month + "%",)
            ),
            "exams": count("SELECT COUNT(*) FROM exams"),
            "scores": count("SELECT COUNT(*) FROM scores"),
        }
    finally:
        conn.close()


def note_stats(slug: str, db_path: str | Path) -> bool:
    """把这位老师的汇总和"最近使用"记进名册，总后台就不必挨个下载所有人的库。"""
    target = cloud_store.normalize_slug(slug) or DEFAULT_SLUG
    stats = compute_stats(db_path)
    if not stats:
        return False
    with _lock:
        roster = load()
        for item in roster.get("teachers", []):
            if (cloud_store.normalize_slug(item.get("slug")) or DEFAULT_SLUG) == target:
                item["stats"] = stats
                item["last_active"] = _now()
                save(roster)
                return True
    return False
