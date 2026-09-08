"""Private itinerary confirmation and post-trip sharing consent flow.

The database functions used here are intentionally loaded lazily.  This keeps the
existing application importable while the database owner implements the contract
documented in ``資料庫交接文件_行程分享與公開網站.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import secrets
from typing import Any

from ai_linebot_core.app.models import ItineraryDraft
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


_ITINERARY_DRAFT_TYPE = "itinerary_plan"
_SHARE_DEADLINE_DAYS = max(1, int(os.getenv("ITINERARY_SHARE_DEADLINE_DAYS", "7")))


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

    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_ITINERARY_DRAFT_TYPE,
        payload={"itinerary": normalized},
    )
    text = str(reply_text or "").strip()
    if text:
        text += "\n\n"
    text += "這是行程草稿。確認後才會保存為群組的私人行程。"
    return FlowResult(
        True,
        text,
        actions=[
            ActionSpec("確認行程", "postback", "itinerary|confirm"),
            ActionSpec("取消草稿", "postback", "itinerary|cancel"),
        ],
        data={
            "itinerary_draft_staged": True,
            "itinerary_draft": normalized,
        },
    )


def _draft_document(*, line_group_id: str, line_user_id: str) -> dict[str, Any] | None:
    document = _db_function("get_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_ITINERARY_DRAFT_TYPE,
    )
    if not isinstance(document, dict):
        return None
    payload = document.get("payload") if isinstance(document.get("payload"), dict) else document
    itinerary = payload.get("itinerary") if isinstance(payload, dict) else None
    return itinerary if isinstance(itinerary, dict) else None


def _cancel_draft(*, line_group_id: str, line_user_id: str) -> FlowResult:
    if not database_contract_ready(("delete_feature_draft",)):
        return database_unavailable_result()
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_ITINERARY_DRAFT_TYPE,
    )
    return FlowResult(True, "已取消這份行程草稿。")


def _confirm_draft(*, line_group_id: str, line_user_id: str) -> FlowResult:
    required = (
        "get_feature_draft",
        "delete_feature_draft",
        "get_active_expense_book",
        "create_itinerary",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()

    itinerary = _draft_document(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
    )
    if itinerary is None:
        return FlowResult(True, "目前沒有等待確認的行程草稿。")

    active_book = _db_function("get_active_expense_book")(line_group_id)
    participant_user_ids = [line_user_id] if line_user_id else []
    expense_book_id = None
    if isinstance(active_book, dict):
        expense_book_id = _book_id(active_book)
        for member in active_book.get("members") or []:
            if not isinstance(member, dict) or member.get("type") != "line":
                continue
            user_id = str(member.get("line_user_id") or "")
            if user_id and user_id not in participant_user_ids:
                participant_user_ids.append(user_id)

    created = _db_function("create_itinerary")(
        itinerary_id=f"trip_{secrets.token_urlsafe(12)}",
        line_group_id=line_group_id,
        title=str(itinerary.get("title") or "行程"),
        spots=list(itinerary.get("spots") or []),
        transport=list(itinerary.get("transport") or []),
        created_by=line_user_id,
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
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_ITINERARY_DRAFT_TYPE,
    )
    title = str((created or {}).get("title") or itinerary.get("title") or "行程")
    return FlowResult(True, f"已將「{redact_sensitive_identifiers(title)}」保存為群組私人行程。")


def handle_itinerary_text(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    normalized = str(text or "").strip()
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
        return FlowResult(
            True,
            f"已達半數同意門檻（{outcome['approvals']}/{outcome['eligible']}），行程已匿名公開。",
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
    if data == "itinerary|confirm":
        try:
            return _confirm_draft(line_group_id=line_group_id, line_user_id=line_user_id)
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法確認行程，請稍後再試。")
    if data == "itinerary|cancel":
        try:
            return _cancel_draft(line_group_id=line_group_id, line_user_id=line_user_id)
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法取消行程草稿，請稍後再試。")
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
