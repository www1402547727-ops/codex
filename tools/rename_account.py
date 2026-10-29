"""云端账号改名的安全演练/执行工具。

默认只下载、备份、在临时副本改名并核对，不上传。
确认后才加 --apply --cloud --confirm CLOUD 执行线上改名。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from account_rename import apply_rename
from tools import split_platform_admin as cloud_tools
import cloud_store


CACHE_ROOT = PROJECT.parent / "workbuddy-cache" / "account_rename"


def _write_manifest(
    backup_dir: Path,
    *,
    slug: str,
    old_username: str,
    new_username: str,
    personal: bool,
    counts_before: dict[str, int],
    roster_text: str,
    teacher_data: bytes,
    platform_data: bytes | None,
) -> None:
    manifest = {
        "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "slug": slug,
        "old_username": old_username,
        "new_username": new_username,
        "personal": personal,
        "counts_before": counts_before,
        "files": {
            "teachers.json": hashlib.sha256(roster_text.encode("utf-8")).hexdigest(),
            "scores.db": hashlib.sha256(teacher_data).hexdigest(),
            "platform.db": (
                hashlib.sha256(platform_data).hexdigest()
                if platform_data is not None
                else ""
            ),
        },
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _object_bytes(path: str) -> bytes | None:
    return cloud_tools._download_cloud_object_fresh(path)


def _upload_and_check(
    object_path: str,
    local_path: Path,
    *,
    expected_sha: str,
) -> None:
    cloud_store.upload_local_file(local_path, object_path)
    remote = _object_bytes(object_path)
    if remote is None or hashlib.sha256(remote).hexdigest() != expected_sha:
        raise RuntimeError(f"上传后校验失败：{object_path}")


def _run(args) -> None:
    if not args.cloud:
        raise RuntimeError("目前只支持 --cloud；本地测试请使用 tests/test_account_rename.py")
    if args.apply and args.confirm != "CLOUD":
        raise RuntimeError("执行上传必须加 --confirm CLOUD")

    url, key, bucket = cloud_tools._load_cloud_config()
    cloud_store.configure(url, key, bucket)

    slug = cloud_store.normalize_slug(args.slug) or cloud_store.DEFAULT_SLUG
    old_username = str(args.old).strip()
    new_username = str(args.new).strip()
    if not old_username or not new_username:
        raise RuntimeError("旧账号和新账号都不能为空")

    roster_text = cloud_tools._read_cloud_roster()
    roster = json.loads(roster_text)
    entry = next(
        (
            item
            for item in roster.get("teachers", [])
            if (cloud_store.normalize_slug(item.get("slug")) or cloud_store.DEFAULT_SLUG)
            == slug
        ),
        None,
    )
    if entry is None:
        raise RuntimeError(f"名册里找不到数据标识：{slug}")
    if str(entry.get("username") or "").strip().lower() != old_username.lower():
        raise RuntimeError(
            f"名册登录名不是「{old_username}」，实际为「{entry.get('username')}」"
        )

    teacher_object = cloud_store.db_object_path(slug)
    platform_object = cloud_store.platform_object_path()
    roster_object = cloud_store.roster_object_path()
    teacher_data = _object_bytes(teacher_object)
    platform_data = _object_bytes(platform_object)
    if teacher_data is None:
        raise RuntimeError(f"云端老师数据库不存在：{teacher_object}")
    if platform_data is None:
        raise RuntimeError("云端平台数据库不存在，停止")
    counts_before = cloud_tools._db_counts_from_bytes(teacher_data)

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="staging_", dir=str(CACHE_ROOT)))
    data_dir = staging / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "teachers.json").write_text(roster_text, encoding="utf-8")
    teacher_db = (
        data_dir / "scores.db"
        if cloud_store.is_default_slug(slug)
        else data_dir / "teachers" / slug / "scores.db"
    )
    teacher_db.parent.mkdir(parents=True, exist_ok=True)
    teacher_db.write_bytes(teacher_data)
    (data_dir / "platform.db").write_bytes(platform_data)

    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
    else:
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_dir = CACHE_ROOT / f"backup_{slug}_{stamp}"
    if backup_dir.exists():
        raise RuntimeError(f"备份目录已存在，拒绝覆盖：{backup_dir}")

    result = apply_rename(
        data_dir,
        slug=slug,
        old_username=old_username,
        new_username=new_username,
        personal=args.personal,
        backup_dir=backup_dir,
    )
    new_roster = (data_dir / "teachers.json").read_text(encoding="utf-8")
    new_teacher_data = teacher_db.read_bytes()
    new_platform_data = (data_dir / "platform.db").read_bytes()
    counts_after = cloud_tools._db_counts_from_bytes(new_teacher_data)
    if counts_after != counts_before:
        raise RuntimeError("临时副本的业务表数量不一致，停止")
    _write_manifest(
        backup_dir,
        slug=slug,
        old_username=old_username,
        new_username=new_username,
        personal=args.personal,
        counts_before=counts_before,
        roster_text=roster_text,
        teacher_data=teacher_data,
        platform_data=platform_data,
    )

    print(f"演练完成：{old_username} -> {new_username}")
    print(f"数据标识：{slug}")
    print(f"个人账号：{str(args.personal).lower()}")
    print(f"业务数量：{json.dumps(counts_after, ensure_ascii=False, sort_keys=True)}")
    print(f"云端备份：{backup_dir}")
    print(f"老师数据库：{teacher_object}")
    print(f"平台数据库：{platform_object}")
    print("密码哈希保持：true")
    if not args.apply:
        print("当前是演练模式：没有上传任何文件。")
        return

    cloud_tools._assert_roster_unchanged(roster_text, "上传前")
    uploaded_teacher = False
    uploaded_platform = False
    uploaded_roster = False
    try:
        _upload_and_check(
            teacher_object,
            teacher_db,
            expected_sha=hashlib.sha256(new_teacher_data).hexdigest(),
        )
        uploaded_teacher = True
        if new_platform_data != platform_data:
            _upload_and_check(
                platform_object,
                data_dir / "platform.db",
                expected_sha=hashlib.sha256(new_platform_data).hexdigest(),
            )
            uploaded_platform = True
        cloud_tools._assert_roster_unchanged(roster_text, "上传名册前")
        cloud_store.upload_text(roster_object, new_roster)
        uploaded_roster = True
        if cloud_tools._read_cloud_roster() != new_roster:
            raise RuntimeError("上传后名册回读不一致")
        print("线上改名完成，已回读校验。")
    except Exception as exc:
        if uploaded_roster:
            try:
                cloud_store.upload_text(roster_object, roster_text)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"线上改名失败：{exc}；名册回滚失败：{rollback_exc}"
                ) from exc
        if uploaded_platform:
            try:
                cloud_store.upload_object(platform_object, platform_data)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"线上改名失败：{exc}；平台库回滚失败：{rollback_exc}"
                ) from exc
        if uploaded_teacher:
            try:
                cloud_store.upload_object(teacher_object, teacher_data)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"线上改名失败：{exc}；老师库回滚失败：{rollback_exc}"
                ) from exc
        raise RuntimeError(f"线上改名失败，已停止并尝试回滚：{exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--personal", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=None)
    _run(parser.parse_args())


if __name__ == "__main__":
    main()
