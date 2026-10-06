from __future__ import annotations

import mimetypes
import os
import tempfile
import threading
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
    if not is_cloud():
        return
    mime = content_type or mimetypes.guess_type(str(object_path))[0] or "application/octet-stream"
    response = requests.post(
        _object_url(object_path),
        headers=_headers(mime),
        data=data,
        timeout=(15, 180),
    )
    if not response.ok:
        detail = response.text[:300]
        raise CloudStorageError(f"上传云端文件失败：HTTP {response.status_code} {detail}")


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


def save_database(db_path: str | Path) -> None:
    global _last_backup_date
    if not is_cloud():
        return
    path = Path(db_path)
    if not path.is_file():
        return
    with _lock:
        data = path.read_bytes()
        upload_object("database/scores.db", data, "application/vnd.sqlite3")
        today = date.today().isoformat()
        if today != _last_backup_date:
            upload_object(f"backups/{today}/scores.db", data, "application/vnd.sqlite3")
            _last_backup_date = today


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