"""Anonymous group poll flow with hidden interim results."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import os
import re
import secrets
from typing import Any

from expense_flow import (
    ActionSpec,
    DatabaseFeatureUnavailable,
    FlowResult,
    _db_function,
    database_contract_ready,
    database_unavailable_result,
)
from privacy_redaction import redact_sensitive_identifiers


_POLL_PREFIXES = ("建立投票", "新增投票", "發起投票")
_END_POLL_COMMANDS = ("結束投票", "截止投票", "關閉投票")
_DEADLINE_RE = re.compile(r"(?:限時|截止)\s*(\d{1,3})\s*(分鐘|小時|天)")
_NORMAL_MINUTES = max(1, int(os.getenv("AUTO_POLL_NORMAL_MINUTES", "10")))
_URGENT_MINUTES = max(1, int(os.getenv("AUTO_POLL_URGENT_MINUTES", "3")))
_LOGGER = logging.getLogger(__name__)


def _anonymization_secret() -> str:
    value = (
        os.getenv("VOTE_ANONYMIZATION_SECRET", "").strip()
        or os.getenv("INTERNAL_TASK_SECRET", "").strip()
    )
    if len(value) < 32 or value.startswith("replace_with_"):
        return ""
    return value


def _anonymous_voter_key(*, anonymity_salt: str, line_user_id: str) -> str:
    secret = _anonymization_secret()
    if not secret:
        raise ValueError("尚未設定投票匿名化密鑰")
    message = f"{anonymity_salt}:{line_user_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def parse_poll_command(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    normalized = redact_sensitive_identifiers(text.strip())
    prefix = next((item for item in _POLL_PREFIXES if normalized.startswith(item)), None)
    if prefix is None:
        return None
    body = normalized[len(prefix) :].strip(" ：:")
    deadline_at = None
    deadline_match = _DEADLINE_RE.search(body)
    if deadline_match:
        value = max(1, int(deadline_match.group(1)))
        unit = deadline_match.group(2)
        delta = timedelta(minutes=value)
        if unit == "小時":
            delta = timedelta(hours=value)
        elif unit == "天":
            delta = timedelta(days=value)
        deadline_at = (now or datetime.now(timezone.utc)) + delta
        body = (body[: deadline_match.start()] + body[deadline_match.end() :]).strip()
    parts = [part.strip() for part in re.split(r"[｜|]", body) if part.strip()]
    if len(parts) < 3:
        return {"error": "請輸入：建立投票 問題｜選項一｜選項二（最多六個選項）。"}
    question = parts[0][:200]
    options: list[str] = []
    for item in parts[1:]:
        option = item[:80]
        if option not in options:
            options.append(option)
    if len(options) < 2:
        return {"error": "投票至少需要兩個不同的選項。"}
    if len(options) > 6:
        return {"error": "投票最多只能有六個選項。"}
    if deadline_at is None:
        deadline_at = (now or datetime.now(timezone.utc)) + timedelta(minutes=_NORMAL_MINUTES)
    return {"question": question, "options": options, "deadline_at": deadline_at}


def _poll_id(poll: dict[str, Any]) -> str:
    return str(poll.get("poll_id") or poll.get("_id") or poll.get("id") or "")


def _option_rows(poll: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, option in enumerate(poll.get("options") or [], start=1):
        if isinstance(option, dict):
            option_id = str(option.get("option_id") or option.get("id") or index)
            label = str(option.get("label") or option.get("text") or option_id)
        else:
            option_id = str(index)
            label = str(option)
        rows.append({"id": option_id, "label": label[:80]})
    return rows


def active_poll_actions(poll: dict[str, Any]) -> list[ActionSpec]:
    poll_id = _poll_id(poll)
    return [
        ActionSpec(option["label"][:20], "postback", f"vote|cast|{poll_id}|{option['id']}")
        for option in _option_rows(poll)
    ]


def _deadline_text(poll: dict[str, Any]) -> str:
    deadline = poll.get("deadline_at")
    if isinstance(deadline, datetime):
        return deadline.astimezone().strftime("%m/%d %H:%M")
    return "稍後"


def _mongo_utc_now() -> datetime:
    # db.py normalizes MongoDB datetimes to timezone-aware UTC before comparing.
    return datetime.now(timezone.utc)


def format_poll(poll: dict[str, Any], results: list[dict[str, Any]] | None = None) -> str:
    question = str(poll.get("question") or "群組投票")
    options = _option_rows(poll)
    if results is None or poll.get("status") == "active":
        lines = [f"匿名投票｜{question}", ""]
        lines.extend(f"{index}. {row['label']}" for index, row in enumerate(options, start=1))
        lines.extend(
            [
                "",
                "投票期間不公開票數，截止前可以改票。",
                f"截止時間：{_deadline_text(poll)}",
            ]
        )
        return redact_sensitive_identifiers("\n".join(lines))

    counts = {str(row.get("option_id")): int(row.get("count") or 0) for row in results}
    indexed = [
        (index, row, counts.get(row["id"], 0))
        for index, row in enumerate(options)
    ]
    ordered = sorted(indexed, key=lambda item: (-item[2], item[0]))
    lines = [f"匿名投票結果｜{question}", ""]
    previous_count: int | None = None
    previous_rank = 0
    for position, (_, row, count) in enumerate(ordered, start=1):
        rank = previous_rank if previous_count == count else position
        lines.append(f"第{rank}名：{row['label']} — {count} 票")
        previous_count = count
        previous_rank = rank

    total_votes = sum(counts.values())
    highest = ordered[0][2] if ordered else 0
    winners = [row["label"] for _, row, count in ordered if count == highest]
    lines.extend(["", f"總票數：{total_votes} 票"])
    if highest <= 0:
        lines.append("本次沒有收到任何票。")
    elif len(winners) == 1:
        lines.append(f"結果：{winners[0]} 得票最高。")
    else:
        lines.append(f"平票：{'、'.join(winners)} 並列第一名。")
    return redact_sensitive_identifiers("\n".join(lines))


def _poll_result(poll: dict[str, Any]) -> FlowResult:
    return FlowResult(
        True,
        format_poll(poll),
        actions=active_poll_actions(poll),
        data={"anonymous_poll": poll},
    )


def _create_vote_session_compatible(
    *,
    line_group_id: str,
    question: str,
    clean_options: list[str],
    deadline_at: datetime,
    created_by_key: str,
    anonymity_salt: str,
    eligible_keys: list[str],
    close_when_all_eligible: bool,
    auto_created: bool,
    discussion_fingerprint: str,
) -> dict:
    create_vote_session = _db_function("create_vote_session")
    common_args = {
        "line_group_id": line_group_id,
        "question": redact_sensitive_identifiers(question.strip())[:200],
        "deadline_at": deadline_at,
        "created_by_key": created_by_key,
        "anonymity_salt": anonymity_salt,
        "eligible_voter_keys": eligible_keys,
        "close_when_all_eligible": close_when_all_eligible,
        "auto_created": bool(auto_created),
        "discussion_fingerprint": discussion_fingerprint[:64],
    }
    try:
        return create_vote_session(
            options=[
                {"option_id": index, "label": label}
                for index, label in enumerate(clean_options, start=1)
            ],
            **common_args,
        )
    except TypeError as first_error:
        # Some teammates may still have the older db.py contract checked out.
        # Retry with plain strings so vote_flow can work across both versions.
        try:
            return create_vote_session(options=clean_options, **common_args)
        except Exception:
            raise first_error


def create_anonymous_poll(
    *,
    line_group_id: str,
    question: str,
    options: list[str],
    eligible_line_user_ids: list[str] | None = None,
    auto_created: bool = False,
    urgent: bool = False,
    deadline_at: datetime | None = None,
    discussion_fingerprint: str = "",
    created_by_line_user_id: str = "",
) -> FlowResult:
    if not line_group_id:
        return FlowResult(True, "投票只能在 LINE 群組中使用。")
    if not _anonymization_secret():
        return FlowResult(True, "投票匿名化密鑰尚未設定，請先在 .env 設定 VOTE_ANONYMIZATION_SECRET。")
    required = ("get_active_vote_session", "create_vote_session")
    if not database_contract_ready(required):
        return database_unavailable_result()
    clean_options: list[str] = []
    for value in options:
        label = redact_sensitive_identifiers(str(value).strip())[:80]
        if label and label not in clean_options:
            clean_options.append(label)
    if len(clean_options) < 2 or len(clean_options) > 6:
        return FlowResult(True, "投票需要二到六個不同的選項。")
    current = datetime.now(timezone.utc)
    if deadline_at is None:
        minutes = _URGENT_MINUTES if urgent else _NORMAL_MINUTES
        deadline_at = current + timedelta(minutes=minutes)
    anonymity_salt = secrets.token_hex(16)
    try:
        active = _db_function("get_active_vote_session")(line_group_id=line_group_id)
        if isinstance(active, dict):
            return FlowResult(False) if auto_created else FlowResult(True, "目前已有一個進行中的投票，請等它截止後再建立新的投票。")
        eligible_keys = sorted(
            {
                _anonymous_voter_key(anonymity_salt=anonymity_salt, line_user_id=user_id)
                for user_id in eligible_line_user_ids or []
                if user_id
            }
        )
        creator_key = ""
        if created_by_line_user_id:
            creator_key = _anonymous_voter_key(
                anonymity_salt=anonymity_salt,
                line_user_id=created_by_line_user_id,
            )
        poll = _create_vote_session_compatible(
            line_group_id=line_group_id,
            question=question,
            clean_options=clean_options,
            deadline_at=deadline_at,
            created_by_key=creator_key,
            anonymity_salt=anonymity_salt,
            eligible_keys=eligible_keys,
            close_when_all_eligible=bool(auto_created and len(eligible_keys) >= 2),
            auto_created=bool(auto_created),
            discussion_fingerprint=discussion_fingerprint[:64],
        )
        if not isinstance(poll, dict):
            raise ValueError("invalid poll response")
        return _poll_result(poll)
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception as exc:
        if exc.__class__.__name__ in {"DbConflictError", "DuplicateKeyError"}:
            return FlowResult(True, "目前已有一個進行中的投票，請等它截止後再建立新的投票。")
        _LOGGER.exception("Vote creation failed (%s)", type(exc).__name__)
        return FlowResult(True, "建立匿名投票時發生錯誤，請稍後再試。")


def handle_vote_text(text: str, *, line_group_id: str, line_user_id: str) -> FlowResult:
    parsed = parse_poll_command(text)
    if parsed is None:
        return FlowResult(False)
    if parsed.get("error"):
        return FlowResult(True, str(parsed["error"]))
    return create_anonymous_poll(
        line_group_id=line_group_id,
        question=str(parsed["question"]),
        options=list(parsed["options"]),
        deadline_at=parsed.get("deadline_at"),
        created_by_line_user_id=line_user_id,
    )


def handle_end_vote_text(text: str, *, line_group_id: str, line_user_id: str) -> FlowResult:
    normalized = redact_sensitive_identifiers(text.strip())
    if not any(normalized.startswith(command) for command in _END_POLL_COMMANDS):
        return FlowResult(False)
    if not line_group_id:
        return FlowResult(True, "投票只能在 LINE 群組中使用。")

    required = (
        "get_db",
        "get_active_vote_session",
        "get_vote_results",
        "mark_vote_result_announced",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()

    try:
        poll = _db_function("get_active_vote_session")(line_group_id=line_group_id)
        if not isinstance(poll, dict):
            return FlowResult(True, "目前沒有進行中的投票。")

        now = _mongo_utc_now()
        db = _db_function("get_db")()
        update_result = db.vote_sessions.update_one(
            {
                "_id": poll.get("_id"),
                "line_group_id": line_group_id,
                "status": "active",
            },
            {
                "$set": {
                    "status": "closed",
                    "closed_at": now,
                    "closed_reason": "manual",
                }
            },
        )
        if getattr(update_result, "modified_count", 0) < 1:
            return FlowResult(True, "投票已經截止或不存在，請重新確認。")

        closed_poll = dict(poll)
        closed_poll["status"] = "closed"
        closed_poll["closed_at"] = now
        closed_poll["closed_reason"] = "manual"
        poll_id = _poll_id(closed_poll)
        results = list(_db_function("get_vote_results")(poll_id=poll_id) or [])
        _db_function("mark_vote_result_announced")(
            poll_id=poll_id,
            announced_at=now,
        )
        return FlowResult(True, format_poll(closed_poll, results))
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception as exc:
        _LOGGER.exception("Vote closing failed (%s)", type(exc).__name__)
        return FlowResult(True, "結束投票時發生錯誤，請稍後再試。")


def handle_vote_postback(data: str, *, line_group_id: str, line_user_id: str) -> FlowResult:
    if not data.startswith("vote|"):
        return FlowResult(False)
    parts = data.split("|")
    if len(parts) != 4 or parts[1] != "cast":
        return FlowResult(True, "這個投票操作已失效。")
    poll_id, option_id = parts[2], parts[3]
    option_id_for_db: Any = int(option_id) if option_id.isdigit() else option_id
    required = (
        "get_vote_session",
        "cast_anonymous_vote",
        "get_vote_results",
        "mark_vote_result_announced",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()
    try:
        poll = _db_function("get_vote_session")(poll_id=poll_id, line_group_id=line_group_id)
        if not isinstance(poll, dict):
            # LINE 中的舊 Flex 卡片會永久保留。資料已清除、測試伺服器重啟，
            # 或結果早已公布時再次點擊，不要在群組中產生失效訊息。
            return FlowResult(True, "")
        if poll.get("status") != "active":
            if poll.get("result_announced_at"):
                return FlowResult(True, "")
            results = list(_db_function("get_vote_results")(poll_id=poll_id) or [])
            _db_function("mark_vote_result_announced")(
                poll_id=poll_id,
                announced_at=_mongo_utc_now(),
            )
            return FlowResult(True, format_poll(poll, results))
        valid_options = {item["id"] for item in _option_rows(poll)}
        if option_id not in valid_options:
            return FlowResult(True, "這個投票選項不存在。")
        voter_key = _anonymous_voter_key(
            anonymity_salt=str(poll.get("anonymity_salt") or ""),
            line_user_id=line_user_id,
        )
        outcome = _db_function("cast_anonymous_vote")(
            poll_id=poll_id,
            voter_key=voter_key,
            option_id=option_id_for_db,
            now=_mongo_utc_now(),
        )
        final_poll = outcome.get("poll") if isinstance(outcome, dict) else None
        if isinstance(final_poll, dict) and final_poll.get("status") != "active":
            results = list(_db_function("get_vote_results")(poll_id=poll_id) or [])
            _db_function("mark_vote_result_announced")(
                poll_id=poll_id,
                announced_at=_mongo_utc_now(),
            )
            return FlowResult(True, format_poll(final_poll, results))
        # 不公開選項內容，只讓使用者知道按鈕已成功送出。
        return FlowResult(True, "已收到你的投票，可在截止前重新選擇。")
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except ValueError as exc:
        message = str(exc)
        if "截止" in message or "結束" in message:
            return FlowResult(True, "投票已截止，結果將由 Bot 公布。")
        return FlowResult(True, "這個投票操作無法完成，請重新開啟投票卡片。")
    except Exception as exc:
        _LOGGER.exception("Vote casting failed (%s)", type(exc).__name__)
        return FlowResult(True, "投票時發生錯誤，請稍後再試。")
