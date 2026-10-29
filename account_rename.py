"""安全地改名册中的账号，不改密码和业务数据。

这个模块只操作一份已经准备好的本地数据目录。云端下载、上传和回滚由
`tools/rename_account.py` 负责，方便先在临时副本上验证。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cloud_store
import teacher_registry


class AccountRenameError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenameResult:
    changed: bool
    slug: str
    old_username: str
    new_username: str
    teacher_db: Path
    platform_db: Path | None
    backup_dir: Path


_COUNT_TABLES = ("students", "classes", "plans", "lessons", "exams", "scores")


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _read_roster(path: Path) -> dict:
    if not path.is_file():
        raise AccountRenameError(f"找不到老师名册：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AccountRenameError(f"老师名册读取失败：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("teachers"), list):
        raise AccountRenameError("老师名册格式不正确")
    if not isinstance(data.get("usernames"), dict):
        data["usernames"] = {}
    return data


def _write_roster(path: Path, roster: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.rename.tmp")
    temp.write_text(
        json.dumps(roster, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _db_path(data_dir: Path, slug: str) -> Path:
    target = cloud_store.normalize_slug(slug) or cloud_store.DEFAULT_SLUG
    if cloud_store.is_default_slug(target):
        return data_dir / "scores.db"
    return data_dir / "teachers" / target / "scores.db"


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        result = {}
        for table in _COUNT_TABLES:
            if table in tables:
                result[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0
                )
            else:
                result[table] = 0
        return result
    finally:
        conn.close()


def _unique_backup_dir(
    data_dir: Path,
    backup_dir: str | Path | None,
) -> Path:
    if backup_dir is not None:
        target = Path(backup_dir)
        if target.exists():
            raise AccountRenameError(f"备份目录已存在，拒绝覆盖：{target}")
        return target
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return data_dir.parent / "backups" / f"account_rename_{stamp}"


def _copy_file(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def apply_rename(
    data_dir: str | Path,
    *,
    slug: str,
    old_username: str,
    new_username: str,
    personal: bool = False,
    backup_dir: str | Path | None = None,
) -> RenameResult:
    """在本地副本中改名，保留密码哈希和业务数据。"""
    base = Path(data_dir)
    target_slug = cloud_store.normalize_slug(slug) or cloud_store.DEFAULT_SLUG
    old_name = str(old_username or "").strip()
    new_name = str(new_username or "").strip()
    if not old_name or not new_name:
        raise AccountRenameError("旧账号和新账号都不能为空")
    if teacher_registry.is_reserved_username(new_name):
        raise AccountRenameError(f"账号「{new_name}」是系统保留名称，不能使用")
    if not new_name.isascii() or any(ch.isspace() for ch in new_name):
        raise AccountRenameError("登录账号请用英文字母或数字，不要有空格")

    roster_path = base / "teachers.json"
    roster = _read_roster(roster_path)
    entry = next(
        (
            item
            for item in roster["teachers"]
            if (cloud_store.normalize_slug(item.get("slug")) or cloud_store.DEFAULT_SLUG)
            == target_slug
        ),
        None,
    )
    if entry is None:
        raise AccountRenameError(f"名册里找不到数据标识：{target_slug}")
    current = str(entry.get("username") or "").strip()
    if current.lower() != old_name.lower():
        raise AccountRenameError(
            f"名册里的登录名不是「{old_name}」，实际为「{current}」"
        )
    if new_name.lower() != old_name.lower():
        for item in roster["teachers"]:
            if (
                (cloud_store.normalize_slug(item.get("slug")) or cloud_store.DEFAULT_SLUG)
                != target_slug
                and str(item.get("username") or "").strip().lower()
                == new_name.lower()
            ):
                raise AccountRenameError(f"账号「{new_name}」已经被占用了")
        for key, value in roster["usernames"].items():
            if (
                str(key).lower() == new_name.lower()
                and (cloud_store.normalize_slug(value) or cloud_store.DEFAULT_SLUG)
                != target_slug
            ):
                raise AccountRenameError(f"账号「{new_name}」已经被占用了")

    teacher_db = _db_path(base, target_slug)
    if not teacher_db.is_file():
        raise AccountRenameError(f"找不到老师数据库：{teacher_db}")
    platform_db = base / "platform.db"

    conn = sqlite3.connect(teacher_db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute(
            """SELECT id, username, password_hash, org_id
               FROM users
               WHERE role='teacher' AND username=? COLLATE NOCASE
               ORDER BY id LIMIT 1""",
            (old_name,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AccountRenameError(f"老师数据库里找不到账号「{old_name}」")
    user_id, _, password_hash, _ = row
    counts_before = _counts(teacher_db)

    target_backup = _unique_backup_dir(base, backup_dir)
    target_backup.mkdir(parents=True, exist_ok=False)
    _copy_file(roster_path, target_backup / "teachers.json")
    _copy_file(teacher_db, target_backup / "scores.db")
    if _copy_file(platform_db, target_backup / "platform.db"):
        platform_backup = target_backup / "platform.db"
    else:
        platform_backup = None

    try:
        conn = sqlite3.connect(teacher_db)
        try:
            conn.execute(
                "UPDATE users SET username=?, org_id=? WHERE id=?",
                (new_name, None if personal else row[3], int(user_id)),
            )
            conn.commit()
        finally:
            conn.close()

        if personal and platform_db.is_file():
            conn = sqlite3.connect(platform_db)
            try:
                conn.execute(
                    "DELETE FROM org_user_roles WHERE slug=?",
                    (target_slug,),
                )
                conn.commit()
            finally:
                conn.close()

        entry["username"] = new_name
        if personal:
            entry["personal"] = True
        mapping = roster.setdefault("usernames", {})
        for key in [key for key in mapping if key.lower() == old_name.lower()]:
            mapping.pop(key, None)
        mapping[new_name] = target_slug
        _write_roster(roster_path, roster)

        if _counts(teacher_db) != counts_before:
            raise AccountRenameError("业务表数量发生变化，已停止")
        conn = sqlite3.connect(teacher_db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            after = conn.execute(
                "SELECT username, password_hash, org_id FROM users WHERE id=?",
                (int(user_id),),
            ).fetchone()
        finally:
            conn.close()
        if after is None or str(after[0]) != new_name:
            raise AccountRenameError("账号改名后回读失败")
        if str(after[1]) != str(password_hash):
            raise AccountRenameError("密码哈希发生变化，已停止")
        if personal and after[2] is not None:
            raise AccountRenameError("个人老师的机构归属没有清空，已停止")
    except Exception:
        shutil.copy2(target_backup / "teachers.json", roster_path)
        shutil.copy2(target_backup / "scores.db", teacher_db)
        if platform_backup is not None:
            shutil.copy2(platform_backup, platform_db)
        raise

    return RenameResult(
        changed=True,
        slug=target_slug,
        old_username=old_name,
        new_username=new_name,
        teacher_db=teacher_db,
        platform_db=platform_db if platform_db.is_file() else None,
        backup_dir=target_backup,
    )
