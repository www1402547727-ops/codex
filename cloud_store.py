from __future__ import annotations

import hashlib
import mimetypes
import os
import re
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
# 下面这些缓存以前是单个值,多老师共用会串号;改成按「云端对象路径」记,
# 每位老师各自一份记录,互不干扰。
_prepared: set[str] = set()
_remote_db_missing: dict[str, bool] = {}
_last_backup_date: dict[str, str] = {}
_last_uploaded_digest: dict[str, str] = {}
# 当前会话该用哪份库。真正的隔离靠 st.session_state(按会话),这里只是兜底。
_session_slug = ""
_UPLOAD_RETRIES = 3   # 上传遇到网络抖动/5xx 时的重试次数

# 老数据(单老师时期)的库标识。空值和 default 都指向原来那份库,保证老数据零迁移。
DEFAULT_SLUG = "default"


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


# ==================== 每位老师一份库：标识与路径 ====================
def normalize_slug(slug: str | None) -> str:
    """把老师标识规范成只含小写字母/数字/下划线的安全字符串(会拼进路径)。"""
    text = re.sub(r"[^0-9a-zA-Z_]+", "_", str(slug or "").strip().lower()).strip("_")
    return text[:40]


def safe_media_name(filename: str) -> str:
    """只接受单个文件名，禁止目录穿越和附件列表分隔符。"""
    name = str(filename or "").strip()
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "|" in name
        or "\x00" in name
    ):
        raise ValueError("媒体文件名不合法")
    if Path(name).name != name:
        raise ValueError("媒体文件名不合法")
    return name


def is_default_slug(slug: str | None) -> bool:
    """空值或 default 都指向原来那份库，保证老数据不用搬家。"""
    return normalize_slug(slug) in ("", DEFAULT_SLUG)


def db_object_path(slug: str | None = None) -> str:
    if is_default_slug(slug):
        return "database/scores.db"
    return f"database/{normalize_slug(slug)}/scores.db"


def backup_object_path(object_path: str, day: str) -> str:
    """把 database/<标识>/scores.db 映射成 backups/<日期>/<标识>/scores.db。"""
    if object_path == "database/scores.db":
        return f"backups/{day}/scores.db"
    parts = str(object_path).split("/")
    if len(parts) == 3 and parts[0] == "database":
        return f"backups/{day}/{parts[1]}/scores.db"
    return f"backups/{day}/scores.db"


def photo_object_path(filename: str, slug: str | None = None) -> str:
    name = safe_media_name(filename)
    if is_default_slug(slug):
        return f"photos/{name}"
    return f"photos/{normalize_slug(slug)}/{name}"


def roster_object_path() -> str:
    return "database/teachers.json"


def set_session_slug(slug: str | None) -> None:
    """记录当前会话用哪份库。st.session_state 按会话隔离，多老师同时在线也不会串。"""
    global _session_slug
    _session_slug = normalize_slug(slug) or DEFAULT_SLUG
    try:
        if st is not None:
            st.session_state["db_slug"] = _session_slug
    except Exception:
        pass


def current_slug() -> str:
    try:
        if st is not None:
            value = st.session_state.get("db_slug")
            if value:
                return normalize_slug(value) or DEFAULT_SLUG
    except Exception:
        pass
    return _session_slug or DEFAULT_SLUG


def session_db_path(default: str | Path) -> Path:
    """按当前会话取数据库路径；模块级默认值只作为兜底。"""
    try:
        if st is not None:
            value = st.session_state.get("db_path")
            if value:
                return Path(value)
    except Exception:
        pass
    return Path(default)


def session_photos_dir(default_db: str | Path) -> Path:
    path = session_db_path(default_db).parent / "photos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_dir(local_dir: str | Path, slug: str | None = None) -> Path:
    """每位老师一个数据目录。default(老数据)仍然用原来的目录，不搬家。"""
    if is_cloud():
        path = Path(tempfile.gettempdir()) / "score_tracker_cloud_data"
    else:
        path = Path(local_dir)
    if not is_default_slug(slug):
        path = path / "teachers" / normalize_slug(slug)
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


def prepare_database(db_path: str | Path, object_path: str | None = None) -> bool:
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    object_path = object_path or db_object_path(current_slug())
    if not is_cloud():
        return target.exists()
    with _lock:
        if object_path in _prepared and target.exists():
            return True
        data = download_object(object_path)
        if data is None:
            _remote_db_missing[object_path] = True
            _prepared.add(object_path)
            return False
        temp = target.with_suffix(".db.download")
        temp.write_bytes(data)
        temp.replace(target)
        _remote_db_missing[object_path] = False
        _prepared.add(object_path)
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


def save_database(db_path: str | Path, object_path: str | None = None) -> None:
    """把本地数据库同步到云端。内容没变就不上传(页面每次刷新都会调用到这里)。"""
    if not is_cloud():
        return
    path = Path(db_path)
    if not path.is_file():
        return
    object_path = object_path or db_object_path(current_slug())
    with _lock:
        data = _snapshot(path)
        digest = hashlib.sha256(data).hexdigest()
        today = date.today().isoformat()
        if _last_backup_date.get(object_path) != today:
            # 当天的备份只在不存在时才写,避免应用重启后被「重启后的状态」覆盖。
            # 备份属于锦上添花:这里任何网络抖动都不应该让"保存"本身失败,所以整段兜住异常。
            try:
                backup = backup_object_path(object_path, today)
                if download_object(backup) is None:
                    upload_object(backup, data, "application/vnd.sqlite3")
                _last_backup_date[object_path] = today
            except Exception:
                pass  # 下次保存时再试
        if _last_uploaded_digest.get(object_path) != digest:
            upload_object(object_path, data, "application/vnd.sqlite3")
            _last_uploaded_digest[object_path] = digest


def download_text(object_path: str) -> str | None:
    """读取云端的小文本文件(老师名册用)。"""
    data = download_object(object_path)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def upload_text(object_path: str, text: str) -> None:
    upload_object(object_path, str(text).encode("utf-8"), "application/json")


def upload_photo(local_path: str | Path, filename: str, slug: str | None = None) -> None:
    upload_local_file(local_path, photo_object_path(filename, current_slug() if slug is None else slug))


def ensure_local_photo(filename: str, local_dir: str | Path, slug: str | None = None) -> Path:
    name = safe_media_name(filename)
    base = Path(local_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = base / name
    if target.resolve().parent != base:
        raise ValueError("媒体文件路径不合法")
    if target.exists():
        return target
    if is_cloud():
        download_file(photo_object_path(name, current_slug() if slug is None else slug), target)
    return target


def remote_database_missing(object_path: str | None = None) -> bool:
    if object_path is None:
        return any(_remote_db_missing.values())
    return bool(_remote_db_missing.get(object_path))
