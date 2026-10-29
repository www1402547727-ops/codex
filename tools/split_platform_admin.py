"""把旧管理员账号拆成独立平台管理员。

默认只处理本地副本。确认上线时才使用 --cloud --apply --confirm CLOUD。
云端模式只读取 teachers.json 和原管理员那一份数据库，不上传备份、不删除对象。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import account_split
import cloud_store
import user_management as um


def _load_cloud_config() -> tuple[str, str, str]:
    path = PROJECT / ".streamlit" / "secrets.toml"
    if not path.is_file():
        raise RuntimeError("找不到 .streamlit/secrets.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    nested = data.get("supabase", {})
    url = str(data.get("SUPABASE_URL") or nested.get("SUPABASE_URL") or "").strip()
    key = str(
        data.get("SUPABASE_SERVICE_ROLE_KEY")
        or data.get("SUPABASE_KEY")
        or nested.get("SUPABASE_SERVICE_ROLE_KEY")
        or nested.get("SUPABASE_KEY")
        or ""
    ).strip()
    bucket = str(
        data.get("SUPABASE_BUCKET")
        or nested.get("SUPABASE_BUCKET")
        or "student-files"
    ).strip()
    if not url or not key:
        raise RuntimeError("secrets.toml 缺少 Supabase URL 或 key")
    return url, key, bucket


def _admin_slug(roster: dict) -> str:
    admins = [entry for entry in roster.get("teachers", []) if entry.get("is_admin")]
    if len(admins) != 1:
        raise RuntimeError(f"拆分前必须恰好有一个平台管理员，当前为 {len(admins)} 个")
    return cloud_store.normalize_slug(admins[0].get("slug")) or cloud_store.DEFAULT_SLUG


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_local(args, password: str) -> None:
    data_dir = PROJECT / "data"
    result = account_split.apply_split(
        data_dir,
        platform_username=args.username,
        password=password,
        backup_dir=args.backup_dir,
    )
    if not result.changed:
        print("账号已经拆分，本次没有修改任何文件。")
        return
    print(f"拆分完成：{result.old_admin_slug} -> {result.platform_admin_slug}")
    print(f"本地备份：{result.backup_dir}")
    print(f"登录账号：{result.platform_username}")
    print(f"一次性密码：{password}")


def _run_cloud(args, password: str) -> None:
    if args.confirm != "CLOUD":
        raise RuntimeError("云端模式必须加 --confirm CLOUD")
    url, key, bucket = _load_cloud_config()
    cloud_store.configure(url, key, bucket)

    roster_text = cloud_store.download_text(cloud_store.roster_object_path())
    if not roster_text:
        raise RuntimeError("云端 teachers.json 不存在，停止")
    roster = json.loads(roster_text)
    old_slug = _admin_slug(roster)
    old_object = cloud_store.db_object_path(old_slug)
    old_data = cloud_store.download_object(old_object)
    if old_data is None:
        raise RuntimeError(f"云端原管理员数据库不存在：{old_object}")

    cache_root = PROJECT.parent / "workbuddy-cache" / "platform_admin_split"
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="staging_", dir=str(cache_root)))
    data_dir = staging / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "teachers.json").write_text(roster_text, encoding="utf-8")
    old_db = (
        data_dir / "scores.db"
        if cloud_store.is_default_slug(old_slug)
        else data_dir / "teachers" / old_slug / "scores.db"
    )
    old_db.parent.mkdir(parents=True, exist_ok=True)
    old_db.write_bytes(old_data)

    backup_dir = args.backup_dir or (
        PROJECT / "backups" / f"account_split_{account_split._now().replace(':', '').replace('-', '')}"
    )
    cloud_store.configure("", "")
    try:
        result = account_split.apply_split(
            data_dir,
            platform_username=args.username,
            password=password,
            backup_dir=backup_dir,
        )
    finally:
        cloud_store.configure(url, key, bucket)

    if not result.changed:
        print("云端名册显示账号已经拆分，本次没有上传任何文件。")
        return

    new_object = cloud_store.db_object_path(result.platform_admin_slug)
    cloud_store.upload_local_file(result.platform_db, new_object)
    remote_db = cloud_store.download_object(new_object)
    if remote_db is None or hashlib.sha256(remote_db).hexdigest() != _sha256(result.platform_db):
        raise RuntimeError("平台管理员数据库上传后校验失败；名册尚未更新")

    updated_roster = (data_dir / "teachers.json").read_text(encoding="utf-8")
    cloud_store.upload_text(cloud_store.roster_object_path(), updated_roster)
    remote_roster = cloud_store.download_text(cloud_store.roster_object_path()) or ""
    parsed = json.loads(remote_roster)
    admins = [entry for entry in parsed.get("teachers", []) if entry.get("is_admin")]
    if (
        len(admins) != 1
        or str(admins[0].get("username")) != args.username
        or (cloud_store.normalize_slug(admins[0].get("slug")) or cloud_store.DEFAULT_SLUG)
        != result.platform_admin_slug
    ):
        raise RuntimeError("云端名册上传后校验失败，请立即按备份回滚")

    print(f"拆分完成：{result.old_admin_slug} -> {result.platform_admin_slug}")
    print(f"本地备份：{result.backup_dir}")
    print(f"云端新库：{new_object}")
    print("platform.db：未修改")
    print(f"登录账号：{result.platform_username}")
    print(f"一次性密码：{password}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--username", default="aren_platform")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()
    if not args.apply:
        print("这是写入操作；确认后加 --apply。")
        return
    password = um.generate_password(14)
    if args.cloud:
        _run_cloud(args, password)
    else:
        _run_local(args, password)


if __name__ == "__main__":
    main()
