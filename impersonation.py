"""平台管理员的临时只读代入查看。

平台管理员默认只能看运营概览。确需查看某位老师的数据时，必须填写理由、
机构同意方式和日期，并勾选已获同意。代入状态只保留 30 分钟，期间所有业务库
写入由 platform_store 的统一连接闸门拒绝。
"""

from __future__ import annotations

import html
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import streamlit as st

import cloud_store
import platform_store
import teacher_registry

DURATION_MINUTES = 30
PENDING_PAGE_KEY = "_impersonation_pending_page"
REASONS = (
    "协助排课",
    "核对课时",
    "处理成绩问题",
    "家长或学生咨询",
    "数据核对",
    "其他",
)
CONSENT_METHODS = (
    "微信文字确认",
    "电话确认",
    "书面确认",
    "当面确认",
    "其他",
)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def peek() -> dict | None:
    return platform_store.current_impersonation()


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except Exception:
        return None


def remaining_seconds(state: dict | None = None) -> int:
    current = state or peek()
    if not current:
        return 0
    expires = _parse_time(str(current.get("expires_at") or ""))
    if expires is None:
        return 0
    return max(0, int((expires - _now()).total_seconds()))


def _audit(
    action: str,
    state: dict,
    *,
    reason: str,
    detail: str = "",
) -> None:
    try:
        platform_store.record_audit(
            action,
            actor_user_id=int(state.get("actor_user_id") or 0) or None,
            actor_role="platform_admin",
            org_id=int(state.get("org_id") or 0) or None,
            target_type="teacher",
            target_id=str(state.get("teacher_slug") or ""),
            detail=detail,
            reason=reason,
        )
    except Exception as exc:
        print(f"[impersonation] 写代入审计失败：{exc}", file=sys.stderr)


def start(
    *,
    org_id: int,
    teacher_slug: str,
    reason: str,
    note: str,
    consent_method: str,
    consent_date: date | str,
    consent_confirmed: bool,
    actor_user: dict,
) -> tuple[bool, str]:
    """开始一次 30 分钟的只读代入查看。"""
    if peek() is not None:
        return False, "已经处于代入查看状态，请先结束当前查看。"
    if str(st.session_state.get("auth_actor_role") or "") != "platform_admin":
        return False, "只有平台管理员可以代入查看。"
    if actor_user.get("role") != "teacher":
        return False, "当前账号不是平台管理员。"
    if not teacher_registry.is_slug_active(teacher_slug):
        return False, "这位老师已停用或不存在。"

    member = platform_store.org_user_for_slug(teacher_slug)
    if not member or int(member.get("org_id") or 0) != int(org_id):
        return False, "这位老师不属于所选机构。"
    org = platform_store.get_organization(int(org_id))
    if org is None or org.get("status") == "停用":
        return False, "所选机构不存在或已停用。"
    entry = teacher_registry.teacher_entry(teacher_slug)
    if entry is None:
        return False, "找不到这位老师的账号。"

    clean_reason = str(reason or "").strip()
    clean_note = str(note or "").strip()
    clean_method = str(consent_method or "").strip()
    if not bool(consent_confirmed):
        return False, "请先确认已获得机构同意。"
    if clean_reason not in REASONS:
        return False, "请选择查看理由。"
    if not clean_note:
        return False, "请填写补充说明。"
    if clean_method not in CONSENT_METHODS:
        return False, "请选择机构同意方式。"
    try:
        consent_day = (
            consent_date
            if isinstance(consent_date, date)
            else date.fromisoformat(str(consent_date))
        )
    except Exception:
        return False, "同意日期格式不正确。"
    if consent_day > date.today():
        return False, "同意日期不能晚于今天。"

    slug = cloud_store.normalize_slug(teacher_slug) or cloud_store.DEFAULT_SLUG
    data_dir = cloud_store.get_data_dir(teacher_registry.base_dir(), slug)
    db = data_dir / "scores.db"
    try:
        found = cloud_store.prepare_database(db, cloud_store.db_object_path(slug))
    except Exception as exc:
        return False, f"读取老师数据库失败：{exc}"
    if cloud_store.is_cloud() and not found:
        return False, "这位老师还没有可查看的数据。"
    if not db.is_file():
        return False, "这位老师还没有可查看的数据。"

    auth_db_path = str(
        st.session_state.get("auth_db_path")
        or st.session_state.get("db_path")
        or ""
    )
    auth_db_slug = str(
        st.session_state.get("auth_db_slug")
        or cloud_store.current_slug()
        or cloud_store.DEFAULT_SLUG
    )
    if not auth_db_path:
        return False, "找不到管理员自己的账号数据库，请重新登录。"

    now = _now()
    state = {
        "actor_user_id": int(actor_user.get("id") or 0),
        "actor_username": str(actor_user.get("username") or ""),
        "org_id": int(org_id),
        "org_name": str(org.get("name") or ""),
        "teacher_slug": slug,
        "teacher_username": str(entry.get("username") or ""),
        "teacher_display_name": str(entry.get("display_name") or ""),
        "reason": clean_reason,
        "note": clean_note,
        "consent_method": clean_method,
        "consent_date": consent_day.isoformat(),
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=DURATION_MINUTES)).isoformat(),
        "auth_db_path": auth_db_path,
        "auth_db_slug": auth_db_slug,
    }
    _audit(
        "impersonate_start",
        state,
        reason=clean_reason,
        detail=(
            f"consent_method={clean_method};"
            f"consent_date={consent_day.isoformat()};"
            "note_present=1"
        ),
    )
    st.session_state["auth_db_path"] = auth_db_path
    st.session_state["auth_db_slug"] = auth_db_slug
    st.session_state[platform_store.IMPERSONATION_KEY] = state
    st.session_state["db_path"] = str(db)
    st.session_state["db_slug"] = slug
    st.session_state[PENDING_PAGE_KEY] = "首页"
    cloud_store.set_session_slug(slug)
    return True, ""


def end(reason: str = "manual", *, rerun: bool = True) -> None:
    """结束代入，恢复管理员自己的数据库和默认页面。"""
    state = peek()
    if not state:
        return
    _audit("impersonate_end", state, reason=str(reason or "manual"))
    auth_db_path = str(state.get("auth_db_path") or "")
    auth_db_slug = str(state.get("auth_db_slug") or cloud_store.DEFAULT_SLUG)
    st.session_state.pop(platform_store.IMPERSONATION_KEY, None)
    if auth_db_path:
        st.session_state["db_path"] = auth_db_path
    st.session_state["db_slug"] = auth_db_slug
    st.session_state[PENDING_PAGE_KEY] = "平台概览"
    cloud_store.set_session_slug(auth_db_slug)
    if rerun:
        st.rerun()


def ensure_active(*, rerun: bool = True) -> dict | None:
    """检查代入是否仍有效；超时或身份变化时自动结束。"""
    state = peek()
    if not state:
        return None
    if str(st.session_state.get("auth_actor_role") or "") != "platform_admin":
        end("invalid_actor", rerun=rerun)
        return None
    if remaining_seconds(state) <= 0:
        end("timeout", rerun=rerun)
        return None
    return state


def banner_text(state: dict) -> str:
    org_name = html.escape(str(state.get("org_name") or "未知机构"))
    teacher = html.escape(
        str(
            state.get("teacher_display_name")
            or state.get("teacher_username")
            or state.get("teacher_slug")
            or "未知老师"
        )
    )
    return f"正在只读查看：{org_name} / {teacher}"


def database_path(state: dict) -> Path:
    slug = cloud_store.normalize_slug(state.get("teacher_slug")) or cloud_store.DEFAULT_SLUG
    return cloud_store.get_data_dir(teacher_registry.base_dir(), slug) / "scores.db"
