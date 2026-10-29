"""把原平台管理员账号拆成独立账号。

本模块只做可重复执行的数据迁移，不负责从云端下载或上传。调用方必须先准备
一份本地数据目录，并确保 teachers.json 和原管理员数据库都在其中。

拆分结果：
- 原管理员条目保留原库、原登录名和原密码，只把 is_admin 改为 false；
- 新建一个独立平台管理员条目和一份只含账号表的数据文件；
- platform.db 不改表、不改机构成员，新平台管理员也不会被自动加入默认机构。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cloud_store
import user_management as um


class AccountSplitError(RuntimeError):
    pass


@dataclass(frozen=True)
class SplitResult:
    changed: bool
    old_admin_slug: str
    platform_admin_slug: str
    platform_username: str
    platform_db: Path
    backup_dir: Path


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _read_roster(path: Path) -> dict:
    if not path.is_file():
        raise AccountSplitError(f"找不到老师名册：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AccountSplitError(f"老师名册读取失败：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("teachers"), list):
        raise AccountSplitError("老师名册格式不正确")
    if not isinstance(data.get("usernames"), dict):
        data["usernames"] = {}
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    return data


def _write_roster(path: Path, roster: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(roster, ensure_ascii=False, indent=2)
    temp = path.with_suffix(".json.split.tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _slug_of(entry: dict) -> str:
    return cloud_store.normalize_slug(entry.get("slug")) or cloud_store.DEFAULT_SLUG


def _db_path(data_dir: Path, slug: str) -> Path:
    if cloud_store.is_default_slug(slug):
        return data_dir / "scores.db"
    return data_dir / "teachers" / cloud_store.normalize_slug(slug) / "scores.db"


def _unique_backup_dir(data_dir: Path, backup_dir: str | Path | None) -> Path:
    if backup_dir is not None:
        target = Path(backup_dir)
        if target.exists():
            raise AccountSplitError(f"备份目录已存在，拒绝覆盖：{target}")
        return target
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return data_dir.parent / "backups" / f"account_split_{stamp}"


def _copy_file(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _validate_new_account(db_path: Path, username: str, password: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT username, password_hash, role FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise AccountSplitError("平台管理员数据库必须且只能有一个账号")
    name, password_hash, role = rows[0]
    if str(name) != username or str(role) != um.ROLE_TEACHER:
        raise AccountSplitError("平台管理员账号写入校验失败")
    if not um.verify_password(password, str(password_hash)):
        raise AccountSplitError("平台管理员密码写入校验失败")


def apply_split(
    data_dir: str | Path,
    *,
    platform_username: str = "aren_platform",
    password: str,
    backup_dir: str | Path | None = None,
) -> SplitResult:
    """在本地数据目录执行账号拆分；重复执行不会重复建库或重复改名。"""
    base = Path(data_dir)
    roster_path = base / "teachers.json"
    roster = _read_roster(roster_path)
    username = str(platform_username or "").strip()
    if not username:
        raise AccountSplitError("平台管理员登录名不能为空")
    target_slug = cloud_store.normalize_slug(username)
    if not target_slug:
        raise AccountSplitError("平台管理员登录名无法生成安全的数据标识")

    admins = [entry for entry in roster["teachers"] if bool(entry.get("is_admin"))]
    if len(admins) == 1 and _slug_of(admins[0]) == target_slug:
        if str(admins[0].get("username") or "").strip().lower() == username.lower():
            platform_db = _db_path(base, target_slug)
            if not platform_db.is_file():
                raise AccountSplitError("名册显示已经拆分，但平台管理员数据库不存在")
            return SplitResult(
                changed=False,
                old_admin_slug="",
                platform_admin_slug=target_slug,
                platform_username=username,
                platform_db=platform_db,
                backup_dir=Path(backup_dir) if backup_dir is not None else Path(),
            )
    if len(admins) != 1:
        raise AccountSplitError(
            f"拆分前必须恰好有一个平台管理员，当前为 {len(admins)} 个"
        )

    old_admin = admins[0]
    old_slug = _slug_of(old_admin)
    old_db = _db_path(base, old_slug)
    if not old_db.is_file():
        raise AccountSplitError(f"原管理员数据库不存在：{old_db}")
    if len(str(password or "")) < 6:
        raise AccountSplitError("平台管理员密码至少 6 位")

    for item in roster["teachers"]:
        if str(item.get("username") or "").strip().lower() == username.lower():
            raise AccountSplitError(f"登录名「{username}」已经被占用")
        if _slug_of(item) == target_slug:
            raise AccountSplitError(f"数据标识「{target_slug}」已经被占用")
    for name, slug in roster["usernames"].items():
        if str(name).lower() == username.lower():
            raise AccountSplitError(f"登录名「{username}」已经被占用")
        if (cloud_store.normalize_slug(slug) or cloud_store.DEFAULT_SLUG) == target_slug:
            raise AccountSplitError(f"数据标识「{target_slug}」已经被占用")

    platform_db = _db_path(base, target_slug)
    if platform_db.exists():
        raise AccountSplitError(f"平台管理员数据库已经存在，拒绝覆盖：{platform_db}")

    target_backup = _unique_backup_dir(base, backup_dir)
    target_backup.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    if _copy_file(roster_path, target_backup / "teachers.json"):
        copied.append("teachers.json")
    if _copy_file(base / "platform.db", target_backup / "platform.db"):
        copied.append("platform.db")
    old_backup = target_backup / "old_admin" / "scores.db"
    if _copy_file(old_db, old_backup):
        copied.append("old_admin/scores.db")
    manifest = {
        "created_at": _now(),
        "old_admin_slug": old_slug,
        "platform_admin_slug": target_slug,
        "platform_username": username,
        "copied_files": copied,
        "platform_db_modified": False,
    }
    (target_backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    try:
        platform_db.parent.mkdir(parents=True, exist_ok=True)
        um.init_user_db(
            platform_db,
            default_teacher_password=password,
            save_cloud=False,
        )
        conn = sqlite3.connect(platform_db)
        try:
            conn.execute(
                """UPDATE users
                   SET username=?, must_change_password=0, note=?
                   WHERE role=?""",
                (username, "独立平台管理员", um.ROLE_TEACHER),
            )
            conn.commit()
        finally:
            conn.close()
        _validate_new_account(platform_db, username, password)

        old_admin["is_admin"] = False
        old_admin["protected"] = True
        note = str(old_admin.get("note") or "").strip()
        split_note = "2026-10-29 已拆分为纯老师账号"
        old_admin["note"] = f"{note}；{split_note}" if note else split_note
        roster["teachers"].append(
            {
                "username": username,
                "display_name": "平台管理员",
                "slug": target_slug,
                "active": True,
                "is_admin": True,
                "created_at": _now(),
                "note": "独立平台管理员，仅用于平台管理",
            }
        )
        roster["usernames"][username] = target_slug
        _write_roster(roster_path, roster)
    except Exception:
        # 名册尚未改动；新库若已生成也保持未登记状态，便于人工排查或回滚。
        raise

    return SplitResult(
        changed=True,
        old_admin_slug=old_slug,
        platform_admin_slug=target_slug,
        platform_username=username,
        platform_db=platform_db,
        backup_dir=target_backup,
    )


def rollback_split(
    data_dir: str | Path,
    backup_dir: str | Path,
) -> Path:
    """恢复拆分前名册，并把新平台管理员库移出使用目录（不删除）。"""
    base = Path(data_dir)
    backup = Path(backup_dir)
    manifest_path = backup / "manifest.json"
    roster_backup = backup / "teachers.json"
    if not manifest_path.is_file() or not roster_backup.is_file():
        raise AccountSplitError("备份不完整，缺少 manifest.json 或 teachers.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AccountSplitError(f"备份清单读取失败：{exc}") from exc
    new_slug = cloud_store.normalize_slug(manifest.get("platform_admin_slug"))
    if not new_slug:
        raise AccountSplitError("备份清单里的平台管理员标识不正确")

    _write_roster(base / "teachers.json", _read_roster(roster_backup))
    platform_backup = backup / "platform.db"
    if platform_backup.is_file():
        shutil.copy2(platform_backup, base / "platform.db")

    new_dir = _db_path(base, new_slug).parent
    quarantine = backup / "quarantine" / new_slug
    if new_dir.exists():
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        if quarantine.exists():
            raise AccountSplitError(f"隔离目录已经存在：{quarantine}")
        shutil.move(str(new_dir), str(quarantine))
    return quarantine
