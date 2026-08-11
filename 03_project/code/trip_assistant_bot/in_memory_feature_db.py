"""Process-local database adapter for LINE group feature testing only.

This module mirrors the feature contract documented in
``DB修改文件(記帳、投票等).md``.  It deliberately provides no persistence,
cross-process coordination, MongoDB indexes, or real transaction guarantees.
All data disappears when the Flask process restarts.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading
from typing import Any
from uuid import uuid4

from privacy_redaction import redact_structure


_lock = threading.RLock()
_expense_books: dict[str, dict[str, Any]] = {}
_feature_drafts: dict[tuple[str, str, str], dict[str, Any]] = {}
_expenses: dict[str, list[dict[str, Any]]] = {}
_invoice_imports: dict[str, dict[str, Any]] = {}
_invoice_import_keys: dict[tuple[str, str], str] = {}
_vote_sessions: dict[str, dict[str, Any]] = {}
_votes: dict[tuple[str, str], dict[str, Any]] = {}
_feature_events: dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _copy(value: Any) -> Any:
    return deepcopy(value)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _book(book_id: Any) -> dict[str, Any]:
    book = _expense_books.get(str(book_id or ""))
    if book is None:
        raise ValueError("找不到測試帳本")
    return book


def _active_book(book_id: Any) -> dict[str, Any]:
    book = _book(book_id)
    if book.get("status") != "active":
        raise ValueError("測試帳本目前不是進行中")
    return book


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _member(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        display_name = str(value.get("display_name") or value.get("name") or "").strip()[:80]
        line_user_id = str(value.get("line_user_id") or "").strip()
        member_type = "line" if line_user_id else str(value.get("type") or "manual")
    else:
        display_name = str(value or "").strip()[:80]
        line_user_id = ""
        member_type = "manual"
    if not display_name and not line_user_id:
        return None
    if not display_name:
        display_name = line_user_id
    result: dict[str, Any] = {"type": member_type, "display_name": display_name}
    if line_user_id:
        result["line_user_id"] = line_user_id
    return redact_structure(result)


def _member_key(member: dict[str, Any]) -> tuple[str, str]:
    line_user_id = str(member.get("line_user_id") or "").strip()
    if line_user_id:
        return "line", line_user_id
    return "manual", str(member.get("display_name") or "").strip().casefold()


def _merge_members(book: dict[str, Any], values: list[Any]) -> None:
    members = book.setdefault("members", [])
    existing = {_member_key(item) for item in members if isinstance(item, dict)}
    for value in values:
        normalized = _member(value)
        if normalized is None:
            continue
        key = _member_key(normalized)
        if key in existing:
            continue
        members.append(normalized)
        existing.add(key)


def _expense_number(number: int) -> str:
    return f"EXP-{number:03d}"


def reset_test_data() -> None:
    """Clear every process-local record. Intended for local smoke tests."""
    with _lock:
        _expense_books.clear()
        _feature_drafts.clear()
        _expenses.clear()
        _invoice_imports.clear()
        _invoice_import_keys.clear()
        _vote_sessions.clear()
        _votes.clear()
        _feature_events.clear()


# ── 帳本 ──────────────────────────────────────────────────────────────


def create_expense_book(
    *,
    line_group_id: str,
    name: str,
    created_by: str,
    members: list[Any],
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timezone: str = "Asia/Taipei",
) -> dict[str, Any]:
    with _lock:
        if any(
            item.get("line_group_id") == line_group_id and item.get("status") == "active"
            for item in _expense_books.values()
        ):
            raise ValueError("群組已有進行中的測試帳本")
        current = _now()
        book_id = _new_id("book")
        book = {
            "_id": book_id,
            "line_group_id": str(line_group_id),
            "name": str(name or "測試行程").strip()[:120],
            "members": [],
            "status": "active",
            "start_at": _aware(start_at),
            "end_at": _aware(end_at),
            "timezone": str(timezone or "Asia/Taipei"),
            "closed_at": None,
            "report_sent_at": None,
            "next_expense_number": 1,
            "created_by": str(created_by),
            "renamed_by": None,
            "renamed_at": None,
            "created_at": current,
            "updated_at": current,
        }
        _merge_members(book, list(members or []))
        _expense_books[book_id] = redact_structure(book)
        _expenses[book_id] = []
        return _copy(_expense_books[book_id])


def get_active_expense_book(line_group_id: str) -> dict[str, Any] | None:
    with _lock:
        candidates = [
            item
            for item in _expense_books.values()
            if item.get("line_group_id") == line_group_id and item.get("status") == "active"
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return _copy(candidates[0])


def add_expense_book_member(*, book_id: Any, member: Any, updated_by: str) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        _merge_members(book, [member])
        book["updated_by"] = str(updated_by)
        book["updated_at"] = _now()
        return _copy(book)


def rename_expense_book(*, book_id: Any, name: str, renamed_by: str) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        if str(book.get("created_by") or "") != str(renamed_by):
            raise PermissionError("只有測試帳本建立者可以修改名稱")
        normalized_name = str(name or "").strip()[:120]
        if not normalized_name:
            raise ValueError("帳本名稱不可為空")
        current = _now()
        book.update(
            {
                "name": normalized_name,
                "renamed_by": str(renamed_by),
                "renamed_at": current,
                "updated_at": current,
            }
        )
        return _copy(book)


def update_expense_book_schedule(
    *,
    book_id: Any,
    start_at: datetime,
    end_at: datetime,
    timezone: str,
    updated_by: str,
) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        if str(book.get("created_by") or "") != str(updated_by):
            raise PermissionError("只有測試帳本建立者可以確認時間")
        normalized_start = _aware(start_at)
        normalized_end = _aware(end_at)
        if normalized_start is None or normalized_end is None or normalized_end <= normalized_start:
            raise ValueError("行程起訖時間不正確")
        book.update(
            {
                "start_at": normalized_start,
                "end_at": normalized_end,
                "timezone": str(timezone or "Asia/Taipei"),
                "updated_by": str(updated_by),
                "updated_at": _now(),
            }
        )
        return _copy(book)


def close_expense_book(*, book_id: Any, closed_by: str) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        if str(book.get("created_by") or "") != str(closed_by):
            raise PermissionError("只有測試帳本建立者可以結束行程")
        current = _now()
        book.update({"status": "closed", "closed_at": current, "updated_at": current})
        return _copy(book)


def reopen_expense_book(*, line_group_id: str, requested_by: str) -> dict[str, Any]:
    with _lock:
        if any(
            item.get("line_group_id") == line_group_id and item.get("status") == "active"
            for item in _expense_books.values()
        ):
            raise ValueError("群組已有進行中的測試帳本")
        candidates = [item for item in _expense_books.values() if item.get("line_group_id") == line_group_id]
        if not candidates:
            raise ValueError("沒有可重新開啟的測試帳本")
        candidates.sort(key=lambda item: item.get("closed_at") or item.get("created_at"), reverse=True)
        book = candidates[0]
        if str(book.get("created_by") or "") != str(requested_by):
            raise PermissionError("只有測試帳本建立者可以重新開啟")
        book.update({"status": "active", "closed_at": None, "report_sent_at": None, "updated_at": _now()})
        return _copy(book)


def claim_due_expense_books(*, now: datetime, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        current = _aware(now) or _now()
        due = []
        for book in _expense_books.values():
            end_at = _aware(book.get("end_at"))
            if book.get("status") != "active" or end_at is None or end_at > current:
                continue
            book.update({"status": "closed", "closed_at": current, "updated_at": current})
            due.append(_copy(book))
            if len(due) >= max(1, int(limit)):
                break
        return due


def mark_expense_report_sent(*, book_id: Any, sent_at: datetime) -> None:
    with _lock:
        book = _book(book_id)
        book["report_sent_at"] = _aware(sent_at) or _now()
        book["updated_at"] = _now()


# ── 草稿 ──────────────────────────────────────────────────────────────


def save_feature_draft(
    *,
    line_group_id: str,
    line_user_id: str,
    draft_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    with _lock:
        current = _now()
        key = (str(line_group_id), str(line_user_id), str(draft_type))
        existing = _feature_drafts.get(key)
        draft = {
            "line_group_id": key[0],
            "line_user_id": key[1],
            "draft_type": key[2],
            "payload": redact_structure(_copy(payload)),
            "created_at": existing.get("created_at") if existing else current,
            "updated_at": current,
            "expires_at": current + timedelta(minutes=30),
        }
        _feature_drafts[key] = draft
        return _copy(draft)


def get_feature_draft(*, line_group_id: str, line_user_id: str, draft_type: str) -> dict[str, Any] | None:
    with _lock:
        key = (str(line_group_id), str(line_user_id), str(draft_type))
        draft = _feature_drafts.get(key)
        if draft is None:
            return None
        if draft.get("expires_at") <= _now():
            _feature_drafts.pop(key, None)
            return None
        return _copy(draft)


def delete_feature_draft(*, line_group_id: str, line_user_id: str, draft_type: str) -> None:
    with _lock:
        _feature_drafts.pop((str(line_group_id), str(line_user_id), str(draft_type)), None)


# ── 支出 ──────────────────────────────────────────────────────────────


def _new_expense(
    *,
    book: dict[str, Any],
    payload: dict[str, Any],
    created_by: str,
    expense_number: int,
    invoice_import_id: Any = None,
) -> dict[str, Any]:
    current = _now()
    expense = redact_structure(_copy(payload))
    expense.update(
        {
            "_id": _new_id("expense"),
            "book_id": book["_id"],
            "expense_no": _expense_number(expense_number),
            "currency": str(expense.get("currency") or "TWD"),
            "status": "confirmed",
            "created_by": str(created_by),
            "invoice_import_id": invoice_import_id,
            "created_at": current,
            "updated_at": current,
            "updated_by": str(created_by),
        }
    )
    expense.pop("missing", None)
    expense.pop("items", None)
    expense.pop("mode", None)
    expense.pop("service_fee", None)
    expense.pop("discount", None)
    expense.pop("other_adjustment", None)
    return expense


def create_expense(*, book_id: Any, expense: dict[str, Any], created_by: str) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        amount = int(expense.get("amount") or 0)
        if amount <= 0:
            raise ValueError("測試支出金額必須大於零")
        if not expense.get("payer"):
            raise ValueError("測試支出必須有付款人")
        number = int(book.get("next_expense_number") or 1)
        created = _new_expense(
            book=book,
            payload=expense,
            created_by=created_by,
            expense_number=number,
        )
        _expenses[book["_id"]].append(created)
        book["next_expense_number"] = number + 1
        book["updated_at"] = _now()
        _merge_members(book, list(created.get("participants") or []))
        return _copy(created)


def list_expenses(book_id: Any, *, status: str = "confirmed") -> list[dict[str, Any]]:
    with _lock:
        _book(book_id)
        rows = _expenses.get(str(book_id), [])
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return _copy(rows)


def get_latest_expense(book_id: Any) -> dict[str, Any] | None:
    rows = list_expenses(book_id, status="confirmed")
    return rows[-1] if rows else None


def update_expense(
    *,
    book_id: Any,
    expense_no: str,
    changes: dict[str, Any],
    updated_by: str,
) -> dict[str, Any]:
    allowed = {"item", "amount", "participants", "consumed_at", "merchant", "category", "payer", "note"}
    with _lock:
        book = _active_book(book_id)
        row = next(
            (item for item in _expenses.get(book["_id"], []) if item.get("expense_no") == expense_no),
            None,
        )
        if row is None or row.get("status") != "confirmed":
            raise ValueError("找不到可修改的測試支出")
        sanitized = redact_structure({key: value for key, value in changes.items() if key in allowed})
        if "amount" in sanitized and int(sanitized["amount"] or 0) <= 0:
            raise ValueError("測試支出金額必須大於零")
        row.update(sanitized)
        row.update({"updated_by": str(updated_by), "updated_at": _now()})
        if "participants" in sanitized:
            _merge_members(book, list(sanitized.get("participants") or []))
        return _copy(row)


def cancel_expense(*, book_id: Any, expense_no: str, cancelled_by: str) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        row = next(
            (item for item in _expenses.get(book["_id"], []) if item.get("expense_no") == expense_no),
            None,
        )
        if row is None:
            raise ValueError("找不到測試支出")
        row.update(
            {
                "status": "cancelled",
                "cancelled_by": str(cancelled_by),
                "updated_by": str(cancelled_by),
                "updated_at": _now(),
            }
        )
        return _copy(row)


# ── 發票匯入 ──────────────────────────────────────────────────────────


def is_duplicate_invoice_import(*, book_id: Any, source_fingerprint: str) -> bool:
    with _lock:
        import_id = _invoice_import_keys.get((str(book_id), str(source_fingerprint)))
        if not import_id:
            return False
        return _invoice_imports.get(import_id, {}).get("status") == "confirmed"


def create_invoice_import(*, book_id: Any, source_fingerprint: str, created_by: str) -> dict[str, Any]:
    with _lock:
        book = _active_book(book_id)
        key = (book["_id"], str(source_fingerprint))
        existing_id = _invoice_import_keys.get(key)
        if existing_id:
            existing = _invoice_imports[existing_id]
            if existing.get("status") == "confirmed":
                raise ValueError("測試發票已完成匯入")
            return _copy(existing)
        current = _now()
        import_id = _new_id("invoice")
        row = {
            "_id": import_id,
            "book_id": book["_id"],
            "source_fingerprint": str(source_fingerprint),
            "status": "draft",
            "expense_ids": [],
            "created_by": str(created_by),
            "created_at": current,
            "confirmed_at": None,
            "expires_at": current + timedelta(minutes=30),
        }
        _invoice_imports[import_id] = row
        _invoice_import_keys[key] = import_id
        return _copy(row)


def create_expenses_from_invoice(
    *,
    book_id: Any,
    invoice_import_id: Any,
    payload: dict[str, Any],
    created_by: str,
) -> list[dict[str, Any]]:
    with _lock:
        book = _active_book(book_id)
        import_row = _invoice_imports.get(str(invoice_import_id or ""))
        if import_row is None or import_row.get("book_id") != book["_id"]:
            raise ValueError("找不到測試發票匯入")
        if import_row.get("status") == "confirmed":
            raise ValueError("測試發票已完成匯入")

        total = int(payload.get("amount") or 0)
        if total <= 0:
            raise ValueError("發票總金額不正確")
        if not payload.get("payer"):
            raise ValueError("發票支出必須有付款人")
        common = {
            "currency": payload.get("currency") or "TWD",
            "participants": payload.get("participants") or [],
            "consumed_at": payload.get("consumed_at"),
            "merchant": payload.get("merchant") or "",
            "category": payload.get("category") or "其他",
            "payer": payload.get("payer") or "",
            "source": payload.get("source") or "invoice_ocr",
            "note": payload.get("note") or "",
        }
        expense_payloads: list[dict[str, Any]] = []
        if payload.get("mode") == "merge":
            expense_payloads.append({**common, "item": payload.get("item") or "發票消費", "amount": total})
        elif payload.get("mode") == "split":
            for item in payload.get("items") or []:
                amount = int(item.get("amount") or 0)
                if not item.get("name") or amount == 0:
                    continue
                expense_payloads.append({**common, "item": item["name"], "amount": amount})
            service_fee = int(payload.get("service_fee") or 0)
            discount = int(payload.get("discount") or 0)
            other = int(payload.get("other_adjustment") or 0)
            if service_fee:
                expense_payloads.append({**common, "item": "服務費", "amount": service_fee, "category": "其他"})
            if discount:
                expense_payloads.append({**common, "item": "折扣", "amount": -abs(discount), "category": "其他"})
            if other:
                expense_payloads.append({**common, "item": "其他調整", "amount": other, "category": "其他"})
            if sum(int(item.get("amount") or 0) for item in expense_payloads) != total:
                raise ValueError("發票明細加總與總額不一致")
        else:
            raise ValueError("尚未選擇發票合併方式")

        if not expense_payloads:
            raise ValueError("發票沒有可建立的支出")
        start_number = int(book.get("next_expense_number") or 1)
        created = [
            _new_expense(
                book=book,
                payload=item,
                created_by=created_by,
                expense_number=start_number + offset,
                invoice_import_id=import_row["_id"],
            )
            for offset, item in enumerate(expense_payloads)
        ]
        _expenses[book["_id"]].extend(created)
        book["next_expense_number"] = start_number + len(created)
        book["updated_at"] = _now()
        _merge_members(book, list(common.get("participants") or []))
        import_row.update(
            {
                "status": "confirmed",
                "expense_ids": [item["_id"] for item in created],
                "confirmed_at": _now(),
                "expires_at": None,
            }
        )
        return _copy(created)


# ── 投票 ──────────────────────────────────────────────────────────────


def create_vote_session(
    *,
    line_group_id: str,
    question: str,
    options: list[str],
    deadline_at: datetime | None,
    created_by_key: str,
    anonymity_salt: str,
    eligible_voter_keys: list[str],
    close_when_all_eligible: bool,
    auto_created: bool,
    discussion_fingerprint: str,
) -> dict[str, Any]:
    with _lock:
        if any(
            poll.get("line_group_id") == str(line_group_id)
            and poll.get("status") == "active"
            for poll in _vote_sessions.values()
        ):
            raise ValueError("群組已有進行中的投票")
        poll_id = _new_id("poll")
        poll = {
            "_id": poll_id,
            "poll_id": poll_id,
            "line_group_id": str(line_group_id),
            "question": str(question)[:200],
            "options": [
                {"option_id": str(index), "label": str(label)[:80]}
                for index, label in enumerate(options, start=1)
            ],
            "status": "active",
            "deadline_at": _aware(deadline_at),
            "created_by_key": str(created_by_key),
            "anonymity_salt": str(anonymity_salt),
            "eligible_voter_keys": sorted({str(value) for value in eligible_voter_keys if value}),
            "close_when_all_eligible": bool(close_when_all_eligible),
            "auto_created": bool(auto_created),
            "discussion_fingerprint": str(discussion_fingerprint),
            "created_at": _now(),
            "closed_at": None,
            "closed_reason": None,
            "result_announced_at": None,
        }
        _vote_sessions[poll_id] = redact_structure(poll)
        return _copy(_vote_sessions[poll_id])


def get_active_vote_session(*, line_group_id: str) -> dict[str, Any] | None:
    with _lock:
        for poll in reversed(list(_vote_sessions.values())):
            if poll.get("line_group_id") == str(line_group_id) and poll.get("status") == "active":
                return _copy(poll)
        return None


def get_vote_session(*, poll_id: str, line_group_id: str) -> dict[str, Any] | None:
    with _lock:
        poll = _vote_sessions.get(str(poll_id))
        if poll is None or poll.get("line_group_id") != line_group_id:
            return None
        return _copy(poll)


def cast_anonymous_vote(
    *,
    poll_id: str,
    voter_key: str,
    option_id: str,
    now: datetime,
) -> dict[str, Any]:
    with _lock:
        poll = _vote_sessions.get(str(poll_id))
        if poll is None or poll.get("status") != "active":
            raise ValueError("測試投票已結束")
        current = _aware(now) or _now()
        deadline = _aware(poll.get("deadline_at"))
        if deadline is not None and deadline <= current:
            raise ValueError("測試投票已截止")
        valid = {str(item.get("option_id")) for item in poll.get("options") or []}
        if str(option_id) not in valid:
            raise ValueError("測試投票選項不存在")
        key = (str(poll_id), str(voter_key))
        existing = _votes.get(key)
        row = {
            "poll_id": str(poll_id),
            "voter_key": str(voter_key),
            "option_id": str(option_id),
            "created_at": existing.get("created_at") if existing else current,
            "updated_at": current,
        }
        _votes[key] = row
        eligible = set(poll.get("eligible_voter_keys") or [])
        voted = {
            stored_voter_key
            for stored_poll_id, stored_voter_key in _votes
            if stored_poll_id == str(poll_id)
        }
        closed_now = bool(
            poll.get("close_when_all_eligible")
            and eligible
            and eligible.issubset(voted)
        )
        if closed_now:
            poll.update(
                {
                    "status": "closed",
                    "closed_at": current,
                    "closed_reason": "all_eligible_voted",
                }
            )
        return {"vote": _copy(row), "poll": _copy(poll), "closed_now": closed_now}


def get_vote_results(*, poll_id: str) -> list[dict[str, Any]]:
    with _lock:
        poll = _vote_sessions.get(str(poll_id))
        if poll is None:
            return []
        counts = {str(item.get("option_id")): 0 for item in poll.get("options") or []}
        for (stored_poll_id, _), vote in _votes.items():
            if stored_poll_id == str(poll_id):
                selected = str(vote.get("option_id"))
                counts[selected] = counts.get(selected, 0) + 1
        return [{"option_id": option_id, "count": count} for option_id, count in counts.items()]


def claim_due_vote_sessions(*, now: datetime, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        current = _aware(now) or _now()
        due = []
        for poll in _vote_sessions.values():
            deadline = _aware(poll.get("deadline_at"))
            if poll.get("status") != "active" or deadline is None or deadline > current:
                continue
            poll.update(
                {
                    "status": "closed",
                    "closed_at": current,
                    "closed_reason": "deadline",
                }
            )
            due.append(_copy(poll))
            if len(due) >= max(1, int(limit)):
                break
        return due


def mark_vote_result_announced(*, poll_id: str, announced_at: datetime) -> None:
    with _lock:
        poll = _vote_sessions.get(str(poll_id))
        if poll is None:
            raise ValueError("找不到測試投票")
        poll["result_announced_at"] = _aware(announced_at) or _now()


# ── Webhook 冪等 ──────────────────────────────────────────────────────


def claim_feature_event(
    *,
    event_id: str,
    feature: str,
    ttl_seconds: int = 604800,
) -> bool:
    del feature
    with _lock:
        current = _now()
        for stored_id, expires_at in list(_feature_events.items()):
            if expires_at <= current:
                _feature_events.pop(stored_id, None)
        if event_id in _feature_events:
            return False
        _feature_events[str(event_id)] = current + timedelta(seconds=max(60, int(ttl_seconds)))
        return True


def release_feature_event(*, event_id: str) -> None:
    with _lock:
        _feature_events.pop(str(event_id), None)
