"""Trip start/end proposal and confirmation flow."""

from __future__ import annotations

from datetime import datetime, time as datetime_time
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from expense_flow import (
    ActionSpec,
    DatabaseFeatureUnavailable,
    FlowResult,
    _book_id,
    _db_function,
    database_contract_ready,
    database_unavailable_result,
)
from privacy_redaction import redact_sensitive_identifiers, redact_structure


_DATE_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*[年/.-]\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*[月/.-]\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*日?"
    r"(?:\s*(?P<period>凌晨|早上|上午|中午|下午|晚上)?\s*"
    r"(?P<hour>2[0-3]|[01]?\d)(?:\s*[:點時]\s*(?P<minute>[0-5]?\d))?\s*分?)?"
)

_SCHEDULE_SIGNALS = (
    "設定行程時間",
    "行程時間",
    "出發",
    "回程",
    "回來",
    "到期",
    "行程從",
)

_TIMEZONE_HINTS = {
    "台灣": "Asia/Taipei",
    "臺灣": "Asia/Taipei",
    "日本": "Asia/Tokyo",
    "東京": "Asia/Tokyo",
    "北海道": "Asia/Tokyo",
    "大阪": "Asia/Tokyo",
    "韓國": "Asia/Seoul",
    "首爾": "Asia/Seoul",
}

_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _chinese_number(token: str) -> int:
    if token in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[token]
    if "十" in token:
        left, right = token.split("十", 1)
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    raise ValueError(token)


def _normalize_chinese_date_numbers(text: str) -> str:
    pattern = re.compile(r"[零一二兩三四五六七八九十]{1,3}(?=[月日點時分])")
    return pattern.sub(lambda match: str(_chinese_number(match.group(0))), text)


def _infer_timezone(text: str, default_timezone: str) -> str:
    for hint, timezone_name in _TIMEZONE_HINTS.items():
        if hint in text:
            return timezone_name
    try:
        ZoneInfo(default_timezone)
        return default_timezone
    except ZoneInfoNotFoundError:
        return "Asia/Taipei"


def _date_from_match(match: re.Match[str], *, now: datetime, timezone_name: str, is_end: bool) -> datetime:
    year = int(match.group("year") or now.year)
    month = int(match.group("month"))
    day = int(match.group("day"))
    period = match.group("period") or ""
    hour_text = match.group("hour")
    minute_text = match.group("minute")
    if hour_text is None:
        clock = datetime_time(23, 59) if is_end else datetime_time(0, 0)
    else:
        hour = int(hour_text)
        minute = int(minute_text or 0)
        if period in {"下午", "晚上"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period in {"凌晨", "早上", "上午"} and hour == 12:
            hour = 0
        clock = datetime_time(hour, minute)
    candidate = datetime(year, month, day, clock.hour, clock.minute, tzinfo=ZoneInfo(timezone_name))
    if match.group("year") is None and candidate < now.astimezone(ZoneInfo(timezone_name)):
        candidate = candidate.replace(year=year + 1)
    return candidate


def extract_schedule_candidate(
    text: str,
    *,
    now: datetime | None = None,
    default_timezone: str = "Asia/Taipei",
) -> dict[str, Any] | None:
    normalized = _normalize_chinese_date_numbers(redact_sensitive_identifiers(text.strip()))
    if not any(signal in normalized for signal in _SCHEDULE_SIGNALS):
        return None
    matches = list(_DATE_RE.finditer(normalized))
    if len(matches) < 2:
        return None
    timezone_name = _infer_timezone(normalized, default_timezone)
    current = now or datetime.now(ZoneInfo(timezone_name))
    try:
        start_at = _date_from_match(matches[0], now=current, timezone_name=timezone_name, is_end=False)
        end_at = _date_from_match(matches[1], now=current, timezone_name=timezone_name, is_end=True)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if end_at <= start_at:
        return None
    return {
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone_name,
        "source_text": normalized[:1000],
    }


def _format_candidate(candidate: dict[str, Any], book_name: str) -> str:
    start_at = candidate["start_at"]
    end_at = candidate["end_at"]
    return "\n".join(
        [
            f"是否將以下時間設為「{book_name}」的行程時間？",
            "",
            f"開始：{start_at.strftime('%Y/%m/%d %H:%M')}",
            f"結束：{end_at.strftime('%Y/%m/%d %H:%M')}",
            f"時區：{candidate['timezone']}",
        ]
    )


def handle_schedule_text(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
    now: datetime | None = None,
) -> FlowResult:
    candidate = extract_schedule_candidate(text, now=now)
    if candidate is None:
        return FlowResult(False)
    if not line_group_id:
        return FlowResult(True, "行程時間目前只支援 LINE 群組。")
    required = (
        "get_active_expense_book",
        "save_feature_draft",
        "get_feature_draft",
        "delete_feature_draft",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()
    try:
        book = _db_function("get_active_expense_book")(line_group_id)
        if not isinstance(book, dict):
            return FlowResult(True, "請先輸入「開始記帳 帳本名稱」，再設定行程時間。")
        payload = redact_structure({**candidate, "book_id": _book_id(book)})
        _db_function("save_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type="trip_schedule",
            payload=payload,
        )
        return FlowResult(
            True,
            _format_candidate(payload, str(book.get("name") or "行程")),
            actions=[
                ActionSpec("確認時間", "postback", "schedule|confirm"),
                ActionSpec("忽略", "postback", "schedule|cancel"),
            ],
        )
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception:
        return FlowResult(True, "目前無法儲存行程時間提案，請稍後再試。")


def handle_schedule_postback(
    data: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not data.startswith("schedule|"):
        return FlowResult(False)
    required = (
        "get_feature_draft",
        "delete_feature_draft",
        "update_expense_book_schedule",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()
    try:
        action = data.split("|", 1)[1]
        if action == "cancel":
            _db_function("delete_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="trip_schedule",
            )
            return FlowResult(True, "已忽略這次行程時間提案。")
        if action != "confirm":
            return FlowResult(True, "這個行程時間操作已失效。")
        draft = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type="trip_schedule",
        )
        if not isinstance(draft, dict):
            return FlowResult(True, "目前沒有等待確認的行程時間。")
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else draft
        updated = _db_function("update_expense_book_schedule")(
            book_id=payload.get("book_id"),
            start_at=payload.get("start_at"),
            end_at=payload.get("end_at"),
            timezone=payload.get("timezone"),
            updated_by=line_user_id,
        )
        _db_function("delete_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type="trip_schedule",
        )
        name = str((updated or {}).get("name") or "行程") if isinstance(updated, dict) else "行程"
        return FlowResult(True, f"已更新「{name}」的行程起訖時間，到期後會自動產生花費明細。")
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception:
        return FlowResult(True, "目前無法更新行程時間，請稍後再試。")
