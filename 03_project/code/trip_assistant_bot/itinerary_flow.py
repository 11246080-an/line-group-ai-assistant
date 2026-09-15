"""Private itinerary confirmation and post-trip sharing consent flow.

The database functions used here are intentionally loaded lazily.  This keeps the
existing application importable while the database owner implements the contract
documented in ``資料庫交接文件_行程分享與公開網站.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import inspect
import os
import re
import secrets
from typing import Any

from ai_linebot_core.app.llm_judge import LLMJudgeError, select_best_itinerary_reason
from ai_linebot_core.app.models import ItineraryDraft
from expense_flow import (
    ActionSpec,
    DatabaseFeatureUnavailable,
    FlowResult,
    _book_id,
    _db_function,
    database_contract_ready,
    database_unavailable_result,
    ensure_book_from_itinerary,
)
from location_flow import resolve_itinerary_spot_coordinates
from privacy_redaction import redact_sensitive_identifiers, redact_structure


_ITINERARY_DRAFT_TYPE = "itinerary_plan"
_RECOMMENDATION_REASON_DRAFT_TYPE = "itinerary_recommendation_reason"
_RECOMMENDATION_REASON_DB_CONTRACT = (
    "record_itinerary_recommendation_reason",
    "get_itinerary_recommendation_reasons",
    "set_published_itinerary_recommendation_reason",
)
_SHARE_DEADLINE_DAYS = max(1, int(os.getenv("ITINERARY_SHARE_DEADLINE_DAYS", "7")))


def _draft_type(draft_id: str) -> str:
    return f"{_ITINERARY_DRAFT_TYPE}:{draft_id}"


def _valid_draft_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,32}", str(value or "")))


def _accepts_keyword(function: Any, keyword: str) -> bool:
    """Allow callers to work before and after the DB owner adds optional fields."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def share_approval_required(eligible_count: int) -> int:
    """Return the at-least-half approval threshold."""
    count = max(0, int(eligible_count))
    return (count + 1) // 2 if count else 0


def evaluate_share_votes(
    eligible_voter_keys: list[str],
    consent_documents: list[dict[str, Any]],
) -> dict[str, int | str]:
    """Evaluate unique eligible decisions using an at-least-half rule."""
    eligible = {str(value) for value in eligible_voter_keys if str(value)}
    latest_decisions: dict[str, str] = {}
    for document in consent_documents:
        voter_key = str(document.get("voter_key") or "")
        decision = str(document.get("decision") or "")
        if voter_key in eligible and decision in {"approve", "decline"}:
            latest_decisions[voter_key] = decision

    approvals = sum(value == "approve" for value in latest_decisions.values())
    declines = sum(value == "decline" for value in latest_decisions.values())
    pending = max(0, len(eligible) - approvals - declines)
    required = share_approval_required(len(eligible))

    if required and approvals >= required:
        status = "approved"
    elif required and approvals + pending < required:
        status = "declined"
    else:
        status = "pending"

    return {
        "status": status,
        "eligible": len(eligible),
        "required": required,
        "approvals": approvals,
        "declines": declines,
        "pending": pending,
    }


def _anonymization_secret() -> str:
    value = (
        os.getenv("ITINERARY_SHARE_SECRET", "").strip()
        or os.getenv("VOTE_ANONYMIZATION_SECRET", "").strip()
        or os.getenv("INTERNAL_TASK_SECRET", "").strip()
    )
    if len(value) < 32 or value.startswith("replace_with_"):
        return ""
    return value


def _consent_voter_key(*, consent_salt: str, line_user_id: str) -> str:
    secret = _anonymization_secret()
    if not secret:
        raise ValueError("尚未設定行程分享匿名化密鑰")
    message = f"itinerary-share:{consent_salt}:{line_user_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _normalize_draft(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ItineraryDraft):
        draft = value
    elif isinstance(value, dict):
        draft = ItineraryDraft.from_dict(value)
    else:
        draft = None
    if draft is None:
        return None

    payload = draft.to_dict()
    spots = []
    for index, spot in enumerate(payload.get("spots") or [], start=1):
        normalized_spot = dict(spot)
        normalized_spot["sequence"] = index
        normalized_spot["spot_id"] = f"spot-{index:03d}"
        spots.append(normalized_spot)
    payload["spots"] = spots
    return redact_structure(payload)


def stage_generated_itinerary(
    *,
    line_group_id: str,
    line_user_id: str,
    itinerary_draft: Any,
    reply_text: str,
) -> FlowResult:
    """Save an AI-generated private draft and ask its creator to confirm it."""
    if not line_group_id:
        return FlowResult(False)
    normalized = _normalize_draft(itinerary_draft)
    if normalized is None:
        return FlowResult(False)
    if not database_contract_ready(("save_feature_draft",)):
        return database_unavailable_result()

    draft_id = secrets.token_urlsafe(9)
    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="",
        draft_type=_draft_type(draft_id),
        payload={"itinerary": normalized, "created_by": line_user_id, "draft_id": draft_id},
    )
    text = str(reply_text or "").strip()
    if text:
        text += "\n\n"
    text += "這是行程草稿。確認後才會保存為群組的私人行程。"
    return FlowResult(
        True,
        text,
        actions=[
            ActionSpec("確認行程", "postback", f"itinerary|confirm|{draft_id}"),
            ActionSpec("取消草稿", "postback", f"itinerary|cancel|{draft_id}"),
        ],
        data={
            "itinerary_draft_staged": True,
            "itinerary_draft": normalized,
            "itinerary_draft_id": draft_id,
        },
    )


def _draft_payload(
    *,
    line_group_id: str,
    line_user_id: str,
    draft_id: str = "",
) -> dict[str, Any] | None:
    if draft_id and not _valid_draft_id(draft_id):
        return None
    document = _db_function("get_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="" if draft_id else line_user_id,
        draft_type=_draft_type(draft_id) if draft_id else _ITINERARY_DRAFT_TYPE,
    )
    if not isinstance(document, dict):
        return None
    payload = document.get("payload") if isinstance(document.get("payload"), dict) else document
    return payload if isinstance(payload, dict) else None


def _cancel_draft(*, line_group_id: str, line_user_id: str, draft_id: str = "") -> FlowResult:
    if not database_contract_ready(("delete_feature_draft",)):
        return database_unavailable_result()
    payload = _draft_payload(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_id=draft_id,
    )
    if payload is None:
        return FlowResult(True, "這份行程草稿不存在或已經處理過了。")
    created_by = str(payload.get("created_by") or line_user_id)
    if draft_id and created_by and created_by != line_user_id:
        return FlowResult(True, "只有建立這份草稿的成員可以取消；同群組成員仍可按確認行程。")
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="" if draft_id else line_user_id,
        draft_type=_draft_type(draft_id) if draft_id else _ITINERARY_DRAFT_TYPE,
    )
    return FlowResult(True, "已取消這份行程草稿。")


def _confirm_draft(*, line_group_id: str, line_user_id: str, draft_id: str = "") -> FlowResult:
    required = (
        "get_feature_draft",
        "delete_feature_draft",
        "get_active_expense_book",
        "create_expense_book",
        "create_itinerary",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()

    payload = _draft_payload(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_id=draft_id,
    )
    itinerary = payload.get("itinerary") if isinstance(payload, dict) else None
    if not isinstance(itinerary, dict):
        return FlowResult(True, "找不到這份行程草稿，可能已確認、取消或過期。")

    created_by = str(payload.get("created_by") or line_user_id)

    resolved_spots, _coordinate_summary = resolve_itinerary_spot_coordinates(
        list(itinerary.get("spots") or []),
        region=str(itinerary.get("region") or ""),
        line_group_id=line_group_id,
    )
    itinerary = {**itinerary, "spots": resolved_spots}

    active_book = _db_function("get_active_expense_book")(line_group_id)
    had_active_book = isinstance(active_book, dict)
    if not had_active_book:
        active_book = ensure_book_from_itinerary(
            line_group_id=line_group_id,
            line_user_id=created_by or line_user_id,
            itinerary=itinerary,
        )
        if not isinstance(active_book, dict):
            active_book = _db_function("get_active_expense_book")(line_group_id)
        if not isinstance(active_book, dict):
            return FlowResult(True, "目前無法建立行程帳本，因此尚未確認行程，請稍後再試。")

    participant_user_ids = []
    for user_id in (created_by, line_user_id):
        if user_id and user_id not in participant_user_ids:
            participant_user_ids.append(user_id)
    expense_book_id = None
    if isinstance(active_book, dict):
        expense_book_id = _book_id(active_book)
        for member in active_book.get("members") or []:
            if not isinstance(member, dict) or member.get("type") != "line":
                continue
            user_id = str(member.get("line_user_id") or "")
            if user_id and user_id not in participant_user_ids:
                participant_user_ids.append(user_id)

    create_itinerary = _db_function("create_itinerary")
    create_kwargs: dict[str, Any] = dict(
        itinerary_id=f"trip_{secrets.token_urlsafe(12)}",
        line_group_id=line_group_id,
        title=str(itinerary.get("title") or "行程"),
        spots=list(itinerary.get("spots") or []),
        transport=list(itinerary.get("transport") or []),
        created_by=created_by or line_user_id,
        participant_user_ids=participant_user_ids,
        expense_book_id=expense_book_id,
        region=str(itinerary.get("region") or ""),
        summary=str(itinerary.get("summary") or ""),
        duration=str(itinerary.get("duration") or ""),
        budget={
            "currency": str(itinerary.get("currency") or "TWD"),
            "estimated_total": itinerary.get("estimated_budget"),
        },
    )
    optional_fields = {
        "itinerary_type": str(itinerary.get("type") or ""),
        "best_for": str(itinerary.get("best_for") or ""),
        "confirmed_by": line_user_id,
    }
    for key, value in optional_fields.items():
        if value and _accepts_keyword(create_itinerary, key):
            create_kwargs[key] = value
    created = create_itinerary(**create_kwargs)
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="" if draft_id else line_user_id,
        draft_type=_draft_type(draft_id) if draft_id else _ITINERARY_DRAFT_TYPE,
    )
    title = str((created or {}).get("title") or itinerary.get("title") or "行程")
    ledger_text = "已連接目前帳本" if had_active_book else "已同時建立行程帳本"
    return FlowResult(
        True,
        f"已將「{redact_sensitive_identifiers(title)}」保存為群組私人行程，{ledger_text}。",
    )


def _recommendation_reason_contract_ready() -> bool:
    return database_contract_ready(
        (
            "get_itinerary",
            "get_itinerary_share_consents",
            "save_feature_draft",
            "get_feature_draft",
            "delete_feature_draft",
            *_RECOMMENDATION_REASON_DB_CONTRACT,
        )
    )


def _eligible_voter_key(
    itinerary: dict[str, Any],
    *,
    line_user_id: str,
) -> str:
    consent_salt = str(itinerary.get("consent_salt") or "")
    if not consent_salt or not line_user_id:
        return ""
    voter_key = _consent_voter_key(
        consent_salt=consent_salt,
        line_user_id=line_user_id,
    )
    eligible_keys = {str(value) for value in itinerary.get("eligible_consent_keys") or []}
    return voter_key if voter_key in eligible_keys else ""


def _voter_approved_sharing(*, itinerary_id: str, voter_key: str) -> bool:
    latest_decision = ""
    for document in _db_function("get_itinerary_share_consents")(
        itinerary_id=itinerary_id
    ) or []:
        if isinstance(document, dict) and str(document.get("voter_key") or "") == voter_key:
            latest_decision = str(document.get("decision") or "")
    return latest_decision == "approve"


def _start_recommendation_reason(
    *,
    itinerary_id: str,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not _recommendation_reason_contract_ready():
        return database_unavailable_result()
    itinerary = _db_function("get_itinerary")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
    )
    if not isinstance(itinerary, dict):
        return FlowResult(True, "找不到這筆已分享的行程。")
    voter_key = _eligible_voter_key(itinerary, line_user_id=line_user_id)
    if not voter_key or not _voter_approved_sharing(
        itinerary_id=itinerary_id,
        voter_key=voter_key,
    ):
        return FlowResult(True, "只有同意公開這次行程的成員可以提供推薦理由。")
    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
        payload={"itinerary_id": itinerary_id},
    )
    return FlowResult(
        True,
        "請直接輸入推薦這份行程的理由（8～240 字）。AI 會比較所有成員提供的內容，選出最具體、最符合行程的一筆公開。",
    )


def _skip_recommendation_reason(*, line_group_id: str, line_user_id: str) -> FlowResult:
    if database_contract_ready(("delete_feature_draft",)):
        _db_function("delete_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
        )
    return FlowResult(True, "好的，這次不填推薦理由；網頁不會顯示空白的推薦理由區塊。")


def _valid_recommendation_reason(value: str) -> bool:
    reason = str(value or "").strip()
    if not 8 <= len(reason) <= 240:
        return False
    if re.search(r"https?://|www\.", reason, flags=re.I):
        return False
    meaningful = re.sub(r"[\W_]+", "", reason, flags=re.UNICODE)
    return len(set(meaningful)) >= 4


def _capture_recommendation_reason(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not database_contract_ready(("get_feature_draft",)):
        return FlowResult(False)
    try:
        document = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
        )
    except Exception:
        # Ordinary conversation must continue even when the optional reason
        # collection store is temporarily unavailable.
        return FlowResult(False)
    if not isinstance(document, dict):
        return FlowResult(False)
    if not _recommendation_reason_contract_ready():
        return database_unavailable_result()

    payload = document.get("payload") if isinstance(document.get("payload"), dict) else document
    itinerary_id = str((payload or {}).get("itinerary_id") or "")
    itinerary = _db_function("get_itinerary")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
    )
    if not isinstance(itinerary, dict):
        return FlowResult(True, "這筆行程已不存在，請重新操作。")
    voter_key = _eligible_voter_key(itinerary, line_user_id=line_user_id)
    if not voter_key or not _voter_approved_sharing(
        itinerary_id=itinerary_id,
        voter_key=voter_key,
    ):
        return FlowResult(True, "只有同意公開這次行程的成員可以提供推薦理由。")

    reason = redact_sensitive_identifiers(str(text or "").strip())[:240]
    if not _valid_recommendation_reason(reason):
        return FlowResult(
            True,
            "這段理由太短、像亂碼或包含連結。請用 8～240 字說明這個行程值得推薦的具體原因。",
        )

    now = datetime.now(timezone.utc)
    _db_function("record_itinerary_recommendation_reason")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
        voter_key=voter_key,
        reason=reason,
        submitted_at=now,
    )
    candidates = list(
        _db_function("get_itinerary_recommendation_reasons")(itinerary_id=itinerary_id) or []
    )
    try:
        selected_reason = select_best_itinerary_reason(itinerary, candidates)
    except LLMJudgeError:
        selected_reason = ""

    if selected_reason:
        selected_voter_key = next(
            (
                str(candidate.get("voter_key") or "")
                for candidate in candidates
                if isinstance(candidate, dict)
                and str(candidate.get("reason") or "").strip() == selected_reason
            ),
            "",
        )
        _db_function("set_published_itinerary_recommendation_reason")(
            itinerary_id=itinerary_id,
            line_group_id=line_group_id,
            reason=selected_reason,
            selected_voter_key=selected_voter_key,
            selected_at=now,
        )

    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
    )
    if not selected_reason:
        return FlowResult(True, "已收到。AI 判斷目前候選內容尚不適合公開，因此網頁暫時不顯示推薦理由。")
    if selected_reason == reason:
        return FlowResult(True, "已收到。AI 比較候選內容後，選用這筆作為公開推薦理由。")
    return FlowResult(True, "已收到。AI 比較後保留了另一筆更符合行程內容的推薦理由。")


def handle_itinerary_text(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    normalized = str(text or "").strip()
    reason_result = _capture_recommendation_reason(
        normalized,
        line_group_id=line_group_id,
        line_user_id=line_user_id,
    )
    if reason_result.handled:
        return reason_result
    if normalized == "確認行程":
        try:
            return _confirm_draft(line_group_id=line_group_id, line_user_id=line_user_id)
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法確認行程，請稍後再試。")
    if normalized in {"取消行程草稿", "放棄行程草稿"}:
        try:
            return _cancel_draft(line_group_id=line_group_id, line_user_id=line_user_id)
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法取消行程草稿，請稍後再試。")
    return FlowResult(False)


def _budget_summary(book: dict[str, Any], expenses: list[dict[str, Any]]) -> dict[str, Any]:
    confirmed = [item for item in expenses if str(item.get("status") or "confirmed") == "confirmed"]
    currencies = {str(item.get("currency") or "TWD").upper() for item in confirmed}
    members = list(book.get("members") or [])
    participant_count = len(members) or None

    if len(currencies) != 1:
        return {
            "currency": "MULTI" if currencies else "TWD",
            "actual_total": None,
            "participant_count": participant_count,
            "per_person": None,
            "category_breakdown": [],
        }

    currency = next(iter(currencies))
    actual_total = sum(int(item.get("amount") or 0) for item in confirmed)
    categories: dict[str, int] = {}
    for item in confirmed:
        category = str(item.get("category") or "其他").strip() or "其他"
        categories[category] = categories.get(category, 0) + int(item.get("amount") or 0)
    return {
        "currency": currency,
        "actual_total": actual_total,
        "participant_count": participant_count,
        "per_person": (
            round(actual_total / participant_count) if participant_count else None
        ),
        "category_breakdown": [
            {"category": category, "amount": amount}
            for category, amount in sorted(categories.items())
        ],
    }


def prepare_share_prompt_for_closed_book(
    *,
    book: dict[str, Any],
    expenses: list[dict[str, Any]],
    now: datetime | None = None,
) -> FlowResult | None:
    """Complete the linked itinerary and create one idempotent share request."""
    required = (
        "get_itinerary_by_expense_book",
        "mark_itinerary_completed",
        "create_itinerary_share_request",
    )
    if not database_contract_ready(required):
        return None

    current = now or datetime.now(timezone.utc)
    book_id = _book_id(book)
    itinerary = _db_function("get_itinerary_by_expense_book")(expense_book_id=book_id)
    if not isinstance(itinerary, dict):
        return None

    completed = _db_function("mark_itinerary_completed")(
        expense_book_id=book_id,
        completed_at=current,
        budget_summary=_budget_summary(book, expenses),
    )
    if isinstance(completed, dict):
        itinerary = completed

    user_ids = [str(value) for value in itinerary.get("participant_user_ids") or [] if str(value)]
    if not user_ids:
        for member in book.get("members") or []:
            if isinstance(member, dict) and member.get("type") == "line":
                user_id = str(member.get("line_user_id") or "")
                if user_id and user_id not in user_ids:
                    user_ids.append(user_id)
    created_by = str(itinerary.get("created_by") or "")
    if created_by and created_by not in user_ids:
        user_ids.append(created_by)
    if not user_ids:
        return None

    consent_salt = str(itinerary.get("consent_salt") or secrets.token_hex(16))
    eligible_keys = [
        _consent_voter_key(consent_salt=consent_salt, line_user_id=user_id)
        for user_id in user_ids
    ]
    request_result = _db_function("create_itinerary_share_request")(
        itinerary_id=str(itinerary.get("itinerary_id") or ""),
        line_group_id=str(itinerary.get("line_group_id") or book.get("line_group_id") or ""),
        eligible_consent_keys=eligible_keys,
        consent_salt=consent_salt,
        requested_at=current,
        deadline_at=current + timedelta(days=_SHARE_DEADLINE_DAYS),
    )
    created_now = bool((request_result or {}).get("created_now")) if isinstance(request_result, dict) else False
    if not created_now:
        return None

    request_itinerary = request_result.get("itinerary")
    if isinstance(request_itinerary, dict):
        itinerary = request_itinerary
    itinerary_id = str(itinerary.get("itinerary_id") or "")
    title = redact_sensitive_identifiers(str(itinerary.get("title") or book.get("name") or "這次行程"))
    required_count = share_approval_required(len(eligible_keys))
    return FlowResult(
        True,
        (
            f"「{title}」已經結束。\n\n"
            "是否同意將景點順序、交通方式與彙總預算匿名分享到公開行程網站？\n"
            "不會公開群組名稱、成員名稱、付款人或聊天內容。\n\n"
            f"共 {len(eligible_keys)} 位符合資格的成員，需要 {required_count} 位同意才會公開。"
        ),
        actions=[
            ActionSpec("同意匿名分享", "postback", f"itinerary_share|approve|{itinerary_id}"),
            ActionSpec("不同意分享", "postback", f"itinerary_share|decline|{itinerary_id}"),
        ],
        data={"itinerary_share_request": itinerary_id},
    )


def _handle_share_decision(
    *,
    itinerary_id: str,
    decision: str,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    required = (
        "get_itinerary",
        "record_itinerary_share_consent",
        "get_itinerary_share_consents",
        "publish_itinerary_snapshot",
        "reject_itinerary_sharing",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()

    itinerary = _db_function("get_itinerary")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
    )
    if not isinstance(itinerary, dict):
        return FlowResult(True, "找不到這筆行程，或你所在的群組不符。")

    consent_salt = str(itinerary.get("consent_salt") or "")
    voter_key = _consent_voter_key(
        consent_salt=consent_salt,
        line_user_id=line_user_id,
    )
    eligible_keys = [str(value) for value in itinerary.get("eligible_consent_keys") or []]
    if voter_key not in eligible_keys:
        return FlowResult(True, "你不在這次行程的分享同意名單中。")

    now = datetime.now(timezone.utc)
    _db_function("record_itinerary_share_consent")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
        voter_key=voter_key,
        decision=decision,
        responded_at=now,
    )
    documents = list(
        _db_function("get_itinerary_share_consents")(itinerary_id=itinerary_id) or []
    )
    outcome = evaluate_share_votes(eligible_keys, documents)

    if outcome["status"] == "approved":
        _db_function("publish_itinerary_snapshot")(
            itinerary_id=itinerary_id,
            published_at=now,
        )
        reason_actions: list[ActionSpec] = []
        reason_suffix = ""
        if _recommendation_reason_contract_ready():
            reason_actions = [
                ActionSpec("填寫推薦理由", "postback", f"itinerary_reason|write|{itinerary_id}"),
                ActionSpec("略過", "postback", f"itinerary_reason|skip|{itinerary_id}"),
            ]
            reason_suffix = "\n\n願意補充推薦理由嗎？多人填寫時會由 AI 選出最符合行程、最有參考價值的一筆。"
        return FlowResult(
            True,
            f"已達半數同意門檻（{outcome['approvals']}/{outcome['eligible']}），行程已匿名公開。{reason_suffix}",
            actions=reason_actions,
        )

    if outcome["status"] == "declined":
        _db_function("reject_itinerary_sharing")(
            itinerary_id=itinerary_id,
            line_group_id=line_group_id,
            resolved_at=now,
        )
        return FlowResult(True, "目前已不可能達到半數同意門檻，這份行程不會公開。")

    choice_text = "同意" if decision == "approve" else "不同意"
    return FlowResult(
        True,
        (
            f"已記錄你的選擇：{choice_text}。"
            f"目前 {outcome['approvals']}/{outcome['eligible']} 位同意，"
            f"需要 {outcome['required']} 位同意才會公開。"
        ),
    )


def handle_itinerary_postback(
    data: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if data == "itinerary|confirm" or data.startswith("itinerary|confirm|"):
        draft_id = data.split("|", 2)[2] if data.count("|") == 2 else ""
        try:
            return _confirm_draft(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_id=draft_id,
            )
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法確認行程，請稍後再試。")
    if data == "itinerary|cancel" or data.startswith("itinerary|cancel|"):
        draft_id = data.split("|", 2)[2] if data.count("|") == 2 else ""
        try:
            return _cancel_draft(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_id=draft_id,
            )
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法取消行程草稿，請稍後再試。")
    if data.startswith("itinerary_reason|"):
        parts = data.split("|", 2)
        if len(parts) != 3 or parts[1] not in {"write", "skip"}:
            return FlowResult(True, "這個推薦理由操作無效或已過期。")
        if parts[1] == "skip":
            return _skip_recommendation_reason(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
            )
        try:
            return _start_recommendation_reason(
                itinerary_id=parts[2],
                line_group_id=line_group_id,
                line_user_id=line_user_id,
            )
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except ValueError as exc:
            return FlowResult(True, redact_sensitive_identifiers(str(exc)))
        except Exception:
            return FlowResult(True, "目前無法開始填寫推薦理由，請稍後再試。")

    if not data.startswith("itinerary_share|"):
        return FlowResult(False)

    parts = data.split("|", 2)
    if len(parts) != 3 or parts[1] not in {"approve", "decline"}:
        return FlowResult(True, "這個分享操作無效或已過期。")
    if not line_group_id or not line_user_id:
        return FlowResult(True, "行程分享同意目前只支援 LINE 群組成員。")
    try:
        return _handle_share_decision(
            itinerary_id=parts[2],
            decision=parts[1],
            line_group_id=line_group_id,
            line_user_id=line_user_id,
        )
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except ValueError as exc:
        return FlowResult(True, redact_sensitive_identifiers(str(exc)))
    except Exception:
        return FlowResult(True, "目前無法記錄分享決定，請稍後再試。")
