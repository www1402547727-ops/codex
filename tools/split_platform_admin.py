"""把旧管理员账号拆成独立平台管理员。

默认只处理本地副本。确认上线时才使用 --cloud --apply --confirm CLOUD。
云端模式只读取 teachers.json 和原管理员那一份数据库，不上传备份、不删除对象。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
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


_COUNT_TABLES = ("students", "classes", "plans", "lessons", "exams", "scores")


def _db_counts(path: str | Path) -> dict[str, int]:
    """只读数据库里的数量；不读取任何姓名、成绩或题目内容。"""
    target = Path(path)
    if not target.is_file():
        raise RuntimeError(f"数据库不存在：{target}")
    conn = sqlite3.connect(
        target.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=15,
    )
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts: dict[str, int] = {}
        for table in _COUNT_TABLES:
            if table in tables:
                counts[table] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0
                )
            else:
                counts[table] = 0
        return counts
    finally:
        conn.close()


def _db_counts_from_bytes(data: bytes) -> dict[str, int]:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".db",
        prefix="count_",
        delete=False,
    ) as handle:
        handle.write(data)
        path = Path(handle.name)
    try:
        return _db_counts(path)
    finally:
        path.unlink(missing_ok=True)


def _read_cloud_roster() -> str:
    data = _download_cloud_object_fresh(cloud_store.roster_object_path())
    if data is None:
        raise RuntimeError("云端 teachers.json 不存在，停止")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("云端 teachers.json 编码不正确，停止") from exc


def _download_cloud_object_fresh(object_path: str) -> bytes | None:
    """下载对象时绕过 Storage/CDN 的旧缓存。"""
    if not cloud_store.is_cloud():
        return None
    separator = "&" if "?" in object_path else "?"
    url = (
        cloud_store._object_url(object_path)
        + separator
        + "_st="
        + str(time.time_ns())
    )
    headers = cloud_store._headers()
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"] = "no-cache"
    try:
        response = cloud_store.requests.get(
            url,
            headers=headers,
            timeout=(15, 120),
        )
    except cloud_store.requests.RequestException as exc:
        raise cloud_store.CloudStorageError(
            f"下载云端文件失败：{type(exc).__name__}"
        ) from exc
    if response.status_code == 404:
        return None
    if not response.ok:
        raise cloud_store.CloudStorageError(
            f"下载云端文件失败：HTTP {response.status_code}"
        )
    return response.content


def _cloud_object_exists(object_path: str) -> bool:
    """用目录列表判断对象是否存在。

    Storage 对不存在的对象直接下载时可能返回 HTTP 400，而不是 404。
    列表接口能明确区分“目录里没有这个文件”和真正的读取失败。
    """
    clean = str(object_path or "").strip().strip("/")
    parent, separator, filename = clean.rpartition("/")
    if not separator or not filename:
        raise RuntimeError(f"云端对象路径不合法：{object_path}")
    rows = cloud_store.list_storage_objects(
        parent,
        page_size=100,
        max_items=1000,
    )
    return any(
        bool(row.get("is_file")) and str(row.get("object_path") or "") == clean
        for row in rows
    )


def _assert_roster_unchanged(initial_text: str, stage: str) -> None:
    latest_text = _read_cloud_roster()
    if latest_text != initial_text:
        raise RuntimeError(
            f"{stage}：云端 teachers.json 已发生变化，停止拆分，未覆盖最新版本"
        )


def _restore_cloud_roster(initial_text: str, uploaded_text: str) -> None:
    """仅在本脚本已经上传过名册时恢复；别人刚改过则绝不覆盖。"""
    current = cloud_store.download_text(cloud_store.roster_object_path())
    if current == initial_text:
        return
    if current != uploaded_text:
        raise RuntimeError(
            "云端 teachers.json 已被其他操作修改，未自动覆盖；请人工核对后回滚"
        )
    cloud_store.upload_text(cloud_store.roster_object_path(), initial_text)
    restored = cloud_store.download_text(cloud_store.roster_object_path())
    if restored != initial_text:
        raise RuntimeError("云端 teachers.json 恢复校验失败，请立即人工回滚")


def _entry_by_username(roster: dict, username: str) -> dict | None:
    target = str(username or "").strip().lower()
    for entry in roster.get("teachers", []):
        if str(entry.get("username") or "").strip().lower() == target:
            return entry
    return None


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

    roster_text = _read_cloud_roster()
    roster = json.loads(roster_text)
    old_slug = _admin_slug(roster)
    admins = [entry for entry in roster.get("teachers", []) if entry.get("is_admin")]
    old_admin = admins[0]
    old_username = str(old_admin.get("username") or "").strip()
    if old_username != "admin":
        raise RuntimeError(
            f"当前平台管理员登录名不是 admin（实际为「{old_username}」），停止"
        )
    if not cloud_store.is_default_slug(old_slug):
        raise RuntimeError(
            f"当前平台管理员不在 default 库（实际为 {old_slug}），停止"
        )

    old_object = cloud_store.db_object_path(old_slug)
    old_data = _download_cloud_object_fresh(old_object)
    if old_data is None:
        raise RuntimeError(f"云端原管理员数据库不存在：{old_object}")
    default_object = cloud_store.db_object_path(cloud_store.DEFAULT_SLUG)
    default_before = (
        old_data
        if default_object == old_object
        else _download_cloud_object_fresh(default_object)
    )
    if default_before is None:
        raise RuntimeError("云端 default 数据库不存在，停止")
    default_counts_before = _db_counts_from_bytes(default_before)

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
    result = None
    uploaded_roster_text = ""
    try:
        # 写本地 teachers.json 前，再读一次云端；任何变化都中止。
        _assert_roster_unchanged(roster_text, "准备写名册前")
        result = account_split.apply_split(
            data_dir,
            platform_username=args.username,
            password=password,
            backup_dir=backup_dir,
        )
        if not result.changed:
            print("云端名册显示账号已经拆分，本次没有上传任何文件。")
            return

        local_counts_after = _db_counts(
            data_dir / "scores.db"
            if cloud_store.is_default_slug(result.old_admin_slug)
            else data_dir / "teachers" / result.old_admin_slug / "scores.db"
        )
        if local_counts_after != default_counts_before:
            raise RuntimeError(
                "本地 default 库数量在拆分前后不一致，停止，未上传任何云端改动"
            )

        # 本地拆分完成后，再次确认云端名册没有被别人改动。
        _assert_roster_unchanged(roster_text, "上传云端前")

        new_object = cloud_store.db_object_path(result.platform_admin_slug)
        if _cloud_object_exists(new_object):
            raise RuntimeError(
                "平台管理员数据库已经存在，拒绝覆盖；本次未上传名册"
            )
        cloud_store.upload_local_file(result.platform_db, new_object)
        remote_db = _download_cloud_object_fresh(new_object)
        if (
            remote_db is None
            or hashlib.sha256(remote_db).hexdigest() != _sha256(result.platform_db)
        ):
            raise RuntimeError("平台管理员数据库上传后校验失败；名册尚未更新")

        default_after = _download_cloud_object_fresh(default_object)
        if default_after is None:
            raise RuntimeError("拆分后无法回读云端 default 数据库")
        if hashlib.sha256(default_after).hexdigest() != hashlib.sha256(
            default_before
        ).hexdigest():
            raise RuntimeError("拆分后云端 default 数据库内容发生变化，停止")
        default_counts_after = _db_counts_from_bytes(default_after)
        if default_counts_after != default_counts_before:
            raise RuntimeError("拆分后云端 default 库数量不一致，停止")

        # 上传名册前最后确认一次，防止准备期间有人改动。
        _assert_roster_unchanged(roster_text, "上传名册前")
        uploaded_roster_text = (data_dir / "teachers.json").read_text(
            encoding="utf-8"
        )
        cloud_store.upload_text(
            cloud_store.roster_object_path(), uploaded_roster_text
        )
        remote_roster = _read_cloud_roster()
        parsed = json.loads(remote_roster)
        old_entry = _entry_by_username(parsed, "admin")
        new_entry = _entry_by_username(parsed, result.platform_username)
        if old_entry is None or bool(old_entry.get("is_admin")):
            raise RuntimeError("回读名册失败：admin 的 is_admin 不是假")
        if new_entry is None or not bool(new_entry.get("is_admin")):
            raise RuntimeError(
                f"回读名册失败：{result.platform_username} 的 is_admin 不是真"
            )
        if (
            cloud_store.normalize_slug(new_entry.get("slug"))
            or cloud_store.DEFAULT_SLUG
        ) != result.platform_admin_slug:
            raise RuntimeError("回读名册失败：平台管理员库标识不一致")
        if str(parsed.get("usernames", {}).get("admin") or "") != "default":
            raise RuntimeError("回读名册失败：admin 的库映射不是 default")
        if (
            str(parsed.get("usernames", {}).get(result.platform_username) or "")
            != result.platform_admin_slug
        ):
            raise RuntimeError("回读名册失败：新平台管理员库映射不一致")

        final_default = _download_cloud_object_fresh(default_object)
        if final_default is None:
            raise RuntimeError("拆分完成后无法回读云端 default 数据库")
        final_counts = _db_counts_from_bytes(final_default)
        if final_counts != default_counts_before:
            raise RuntimeError("拆分完成后 default 库数量不一致")

        print(
            "default 库数量（拆分前）："
            + json.dumps(default_counts_before, ensure_ascii=False, sort_keys=True)
        )
        print(
            "default 库数量（拆分后）："
            + json.dumps(final_counts, ensure_ascii=False, sort_keys=True)
        )
        print(f"拆分完成：{result.old_admin_slug} -> {result.platform_admin_slug}")
        print(f"本地备份：{result.backup_dir}")
        print(f"云端新库：{new_object}")
        print("platform.db：未修改")
        print(f"登录账号：{result.platform_username}")
        print(f"一次性密码：{password}")
    except Exception as exc:
        # 只有确实上传过名册才恢复名册；绝不覆盖别人刚写入的新版本。
        if uploaded_roster_text:
            try:
                _restore_cloud_roster(roster_text, uploaded_roster_text)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"拆分失败：{exc}；云端名册回滚失败：{rollback_exc}"
                ) from exc
        if result is not None and result.changed:
            try:
                account_split.rollback_split(data_dir, result.backup_dir)
            except Exception as rollback_exc:
                raise RuntimeError(
                    f"拆分失败：{exc}；本地回滚失败：{rollback_exc}"
                ) from exc
        raise RuntimeError(f"拆分失败，已停止：{exc}") from exc


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
