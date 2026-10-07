from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

try:
    import streamlit as st
except Exception:
    st = None


_lock = threading.RLock()
_config: dict[str, str] | None = None
_prepared = False
_remote_db_missing = False
_last_backup_date = ""
_last_uploaded_digest = ""
_UPLOAD_RETRIES = 3   # 上传遇到网络抖动/5xx 时的重试次数


class CloudStorageError(RuntimeError):
    pass


def _secret(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value:
        return str(value).strip()
    if st is not None:
        try:
            if name in st.secrets:
                return str(st.secrets[name]).strip()
        except Exception:
            pass
    return default


def configure(url: str, key: str, bucket: str = "student-files") -> None:
    global _config
    url = str(url or "").strip().rstrip("/")
    key = str(key or "").strip()
    bucket = str(bucket or "student-files").strip()
    if not url or not key:
        _config = None
    else:
        _config = {"url": url, "key": key, "bucket": bucket}


def configure_from_streamlit() -> None:
    configure(
        _secret("SUPABASE_URL", ""),
        _secret("SUPABASE_SERVICE_ROLE_KEY", "") or _secret("SUPABASE_KEY", ""),
        _secret("SUPABASE_BUCKET", "student-files"),
    )


def is_cloud() -> bool:
    return _config is not None


def get_data_dir(local_dir: str | Path) -> Path:
    if is_cloud():
        path = Path(tempfile.gettempdir()) / "score_tracker_cloud_data"
    else:
        path = Path(local_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _headers(content_type: str | None = None) -> dict[str, str]:
    if not _config:
        raise CloudStorageError("云端存储尚未配置")
    headers = {
        "Authorization": f"Bearer {_config['key']}",
        "apikey": _config["key"],
        "x-upsert": "true",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _object_url(object_path: str) -> str:
    if not _config:
        raise CloudStorageError("云端存储尚未配置")
    bucket = quote(_config["bucket"], safe="")
    path = quote(str(object_path).lstrip("/"), safe="/")
    return f"{_config['url']}/storage/v1/object/{bucket}/{path}"


def download_object(object_path: str) -> bytes | None:
    if not is_cloud():
        return None
    response = requests.get(_object_url(object_path), headers=_headers(), timeout=(15, 120))
    if response.status_code == 404:
        return None
    if not response.ok:
        raise CloudStorageError(f"下载云端文件失败：HTTP {response.status_code}")
    return response.content


def upload_object(object_path: str, data: bytes, content_type: str | None = None) -> None:
    """上传到云端。遇到超时/连接错误/5xx/429 会自动重试,避免一次瞬时抖动就让保存失败甚至应用起不来。"""
    if not is_cloud():
        return
    mime = content_type or mimetypes.guess_type(str(object_path))[0] or "application/octet-stream"
    url = _object_url(object_path)
    headers = _headers(mime)
    last_error = "未知错误"
    for attempt in range(_UPLOAD_RETRIES):
        try:
            response = requests.post(url, headers=headers, data=data, timeout=(15, 180))
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.ok:
                return
            last_error = f"HTTP {response.status_code} {response.text[:200]}"
            # 4xx(除 429)是请求本身有问题,重试无意义
            if response.status_code < 500 and response.status_code != 429:
                break
        if attempt < _UPLOAD_RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))
    raise CloudStorageError(f"上传云端文件失败：{last_error}")


def upload_local_file(local_path: str | Path, object_path: str) -> None:
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    upload_object(object_path, path.read_bytes())


def download_file(object_path: str, local_path: str | Path) -> bool:
    data = download_object(object_path)
    if data is None:
        return False
    target = Path(local_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".download")
    temp.write_bytes(data)
    temp.replace(target)
    return True


def prepare_database(db_path: str | Path) -> bool:
    global _prepared, _remote_db_missing
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not is_cloud():
        return target.exists()
    with _lock:
        if _prepared and target.exists():
            return True
        data = download_object("database/scores.db")
        if data is None:
            _remote_db_missing = True
            _prepared = True
            return False
        temp = target.with_suffix(".db.download")
        temp.write_bytes(data)
        temp.replace(target)
        _remote_db_missing = False
        _prepared = True
        return True


def _snapshot(path: Path) -> bytes:
    """用 SQLite 自带的在线备份接口拿一份一致的快照。
    直接 read_bytes() 读正在被别的会话写入的 .db 文件,可能读到写了一半的内容。"""
    snap = Path(tempfile.gettempdir()) / f"snap_{os.getpid()}_{threading.get_ident()}.db"
    src = sqlite3.connect(path, timeout=15)
    try:
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
        return snap.read_bytes()
    finally:
        src.close()
        snap.unlink(missing_ok=True)


def save_database(db_path: str | Path) -> None:
    """把本地数据库同步到云端。内容没变就不上传(页面每次刷新都会调用到这里)。"""
    global _last_backup_date, _last_uploaded_digest
    if not is_cloud():
        return
    path = Path(db_path)
    if not path.is_file():
        return
    with _lock:
        data = _snapshot(path)
        digest = hashlib.sha256(data).hexdigest()
        today = date.today().isoformat()
        if today != _last_backup_date:
            # 当天的备份只在不存在时才写,避免应用重启后被「重启后的状态」覆盖。
            # 备份属于锦上添花:这里任何网络抖动都不应该让"保存"本身失败,所以整段兜住异常。
            try:
                if download_object(f"backups/{today}/scores.db") is None:
                    upload_object(f"backups/{today}/scores.db", data, "application/vnd.sqlite3")
                _last_backup_date = today
            except Exception:
                pass  # 下次保存时再试
        if digest != _last_uploaded_digest:
            upload_object("database/scores.db", data, "application/vnd.sqlite3")
            _last_uploaded_digest = digest


def upload_photo(local_path: str | Path, filename: str) -> None:
    upload_local_file(local_path, f"photos/{filename}")


def ensure_local_photo(filename: str, local_dir: str | Path) -> Path:
    target = Path(local_dir) / filename
    if target.exists():
        return target
    if is_cloud():
        download_file(f"photos/{filename}", target)
    return target


def remote_database_missing() -> bool:
    return _remote_db_missing