"""Deterministic group expense-book flow.

This module deliberately calls database functions lazily.  The existing ``db.py``
is owned by another maintainer; until that maintainer implements the contract in
``DB修改文件(記帳、投票等).md``, feature commands fail closed without breaking app import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib
import os
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from privacy_redaction import redact_sensitive_identifiers, redact_structure


class DatabaseFeatureUnavailable(RuntimeError):
    """Raised when the feature DB contract has not been implemented yet."""


@dataclass(frozen=True)
class ActionSpec:
    label: str
    kind: str
    value: str


@dataclass
class FlowResult:
    handled: bool
    text: str = ""
    actions: list[ActionSpec] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,9}(?:,\d{3})*)(?:\s*(?:元|塊|TWD|NTD|NT\$))?", re.I)
_PARTICIPANT_RE = re.compile(
    r"(?:分攤對象|分攤|參與者)(?:是|為|：|:)?\s*([^，。；;]+)", re.I
)
_PAYER_RE = re.compile(r"(?:付款人|由)(?:是|為|：|:)?\s*([^，。；;]+?)(?:付款|支付|，|。|$)")
_MERCHANT_RE = re.compile(r"(?:商家|店家)(?:是|為|：|:)?\s*([^，。；;]+)")
_EXPENSE_NO_RE = re.compile(r"#?(EXP-\d{3,})", re.I)
_EDIT_FIELD_RE = re.compile(
    r"(項目|金額|商家|分類|付款人|備註)\s*(?:是|為|：|:)?\s*"
    r"(.+?)(?=\s+(?:項目|金額|商家|分類|付款人|備註)\s*(?:是|為|：|:)?|$)"
)
_DRAFT_EDIT_LABELS = "項目|金額|分攤對象|消費日期|日期|商家|分類|付款人|備註"
_DRAFT_EDIT_FIELD_RE = re.compile(
    rf"({_DRAFT_EDIT_LABELS})\s*(?:是|為|：|:)?\s*"
    rf"(.+?)(?=(?:\s+|[，,；;]\s*)(?:{_DRAFT_EDIT_LABELS})\s*(?:是|為|：|:)?|$)"
)

_CATEGORY_HINTS = {
    "餐飲": ("早餐", "午餐", "晚餐", "餐廳", "咖啡", "飲料", "小吃", "宵夜"),
    "交通": ("車票", "租車", "計程車", "高鐵", "火車", "捷運", "公車", "油錢", "停車"),
    "住宿": ("飯店", "旅館", "民宿", "住宿"),
    "門票": ("門票", "入場", "票券"),
    "購物": ("購物", "伴手禮", "紀念品"),
}

_DB_REQUIRED_BASE = (
    "get_active_expense_book",
    "save_feature_draft",
    "get_feature_draft",
    "delete_feature_draft",
)


def get_feature_database_module() -> Any:
    module_name = (
        "in_memory_feature_db"
        if os.getenv("USE_IN_MEMORY_FEATURE_DB", "false").strip().casefold()
        in {"1", "true", "yes", "on"}
        else "db"
    )
    return importlib.import_module(module_name)


def _db_function(name: str) -> Callable[..., Any]:
    module = get_feature_database_module()
    function = getattr(module, name, None)
    if not callable(function):
        raise DatabaseFeatureUnavailable(f"db.py 尚未提供 {name}()")
    return function


def database_contract_ready(required: tuple[str, ...] = _DB_REQUIRED_BASE) -> bool:
    try:
        module = get_feature_database_module()
    except Exception:
        return False
    return all(callable(getattr(module, name, None)) for name in required)


def database_unavailable_result() -> FlowResult:
    return FlowResult(
        handled=True,
        text="這項功能的資料庫介面還在準備中，目前不會寫入任何資料。請稍後再試。",
    )


def _clean_name(value: Any, *, max_length: int = 80) -> str:
    return redact_sensitive_identifiers(str(value or "").strip())[:max_length]


def _person_name(value: Any, *, max_length: int = 40) -> str:
    if isinstance(value, dict):
        value = value.get("display_name") or value.get("name") or ""
    return _clean_name(value, max_length=max_length)


def _split_people(value: str) -> list[str]:
    normalized = re.sub(r"\s*(?:、|，|,|和|與)\s*", ",", value.strip())
    people: list[str] = []
    for item in normalized.split(","):
        name = _clean_name(item, max_length=40)
        if name and name not in people:
            people.append(name)
    return people[:50]


def _parse_participants(value: str) -> tuple[list[str], bool]:
    normalized = value.strip(" ，,。；;：:")
    if normalized in {"無", "不分攤", "不用分攤", "不需分攤"}:
        return [], True
    participants = _split_people(normalized)
    return participants, bool(participants)


def infer_category(text: str) -> str:
    for category, hints in _CATEGORY_HINTS.items():
        if any(hint in text for hint in hints):
            return category
    return "其他"


def parse_expense_command(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Parse the explicit ``記帳`` command into a safe draft."""
    normalized = redact_sensitive_identifiers(text.strip())
    if not normalized.startswith("記帳") or normalized.startswith("記帳本"):
        return None
    body = normalized[2:].strip(" ：:")
    amount_match = _AMOUNT_RE.search(body)
    if amount_match is None:
        return {"missing": ["金額"], "raw_text": normalized}

    amount = int(amount_match.group(1).replace(",", ""))
    if amount <= 0:
        return {"missing": ["金額"], "raw_text": normalized}

    item_text = body[: amount_match.start()].strip(" ，,:：")
    item = _clean_name(item_text or "未命名支出")
    participant_match = _PARTICIPANT_RE.search(body)
    participants, participants_selected = (
        _parse_participants(participant_match.group(1)) if participant_match else ([], False)
    )
    payer_match = _PAYER_RE.search(body)
    merchant_match = _MERCHANT_RE.search(body)
    occurred = now or datetime.now(timezone.utc)

    missing: list[str] = []
    if not participants_selected:
        missing.append("分攤對象")
    payer = _clean_name(payer_match.group(1), max_length=40) if payer_match else ""
    if not payer:
        missing.append("付款人")

    return {
        "item": item,
        "amount": amount,
        "currency": "TWD",
        "participants": participants,
        "consumed_at": occurred,
        "merchant": _clean_name(merchant_match.group(1)) if merchant_match else "",
        "category": infer_category(body),
        "payer": payer,
        "source": "manual",
        "note": "",
        "missing": missing,
    }


def format_expense_draft(draft: dict[str, Any]) -> str:
    consumed_at = draft.get("consumed_at")
    if isinstance(consumed_at, datetime):
        date_text = consumed_at.astimezone().strftime("%Y/%m/%d")
    else:
        date_text = str(consumed_at or "未填寫")[:20]
    participants = draft.get("participants") or []
    participants_text = "、".join(
        _clean_name(
            item.get("display_name") if isinstance(item, dict) else item,
            max_length=40,
        )
        for item in participants
    )
    if not participants_text:
        participants_text = "尚未選擇" if "分攤對象" in (draft.get("missing") or []) else "無（不分攤）"
    return "\n".join(
        [
            "請確認這筆支出：",
            "",
            f"項目：{draft.get('item') or '未填寫'}",
            f"金額：NT${int(draft.get('amount') or 0):,}",
            f"分攤對象：{participants_text}",
            f"消費日期：{date_text}",
            f"商家：{draft.get('merchant') or '未填寫'}",
            f"分類：{draft.get('category') or '其他'}",
            f"付款人：{_person_name(draft.get('payer')) or '未填寫'}",
            f"備註：{draft.get('note') or '無'}",
        ]
    )


def _participant_actions(prefix: str) -> list[ActionSpec]:
    return [
        ActionSpec("全部成員", "postback", f"{prefix}|participants|all"),
        ActionSpec("同上一筆", "postback", f"{prefix}|participants|previous"),
        ActionSpec("不分攤", "postback", f"{prefix}|participants|none"),
        ActionSpec("自訂成員", "message", "設定分攤對象 "),
        ActionSpec("修改內容", "postback", f"{prefix}|edit_prompt"),
    ]


def _payer_actions(prefix: str) -> list[ActionSpec]:
    return [
        ActionSpec("同上一筆", "postback", f"{prefix}|payer|previous"),
        ActionSpec("自訂付款人", "message", "設定付款人 "),
        ActionSpec("修改內容", "postback", f"{prefix}|edit_prompt"),
        ActionSpec("取消", "postback", f"{prefix}|cancel"),
    ]


def _confirmation_actions(draft_type: str) -> list[ActionSpec]:
    confirm_label = "確認入帳" if draft_type == "invoice" else "確認記帳"
    return [
        ActionSpec(confirm_label, "postback", f"{draft_type}|confirm"),
        ActionSpec("修改內容", "postback", f"{draft_type}|edit_prompt"),
        ActionSpec("取消", "postback", f"{draft_type}|cancel"),
    ]


def _parse_consumed_at(value: str, *, now: datetime | None = None) -> datetime | None:
    clean = value.strip(" ，,。")
    reference = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(ZoneInfo("Asia/Taipei"))
    for pattern in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(clean, pattern)
            return parsed.replace(tzinfo=ZoneInfo("Asia/Taipei"))
        except ValueError:
            pass
    for pattern in ("%m/%d", "%m-%d"):
        try:
            parsed = datetime.strptime(clean, pattern)
            return parsed.replace(year=reference.year, tzinfo=ZoneInfo("Asia/Taipei"))
        except ValueError:
            pass
    return None


def _draft_result_after_edit(draft_type: str, payload: dict[str, Any], *, mode_reset: bool = False) -> FlowResult:
    if not _person_name(payload.get("payer")) and "付款人" not in (payload.get("missing") or []):
        payload["missing"] = [*(payload.get("missing") or []), "付款人"]
    if "分攤對象" in (payload.get("missing") or []):
        return FlowResult(
            True,
            format_expense_draft(payload) + "\n\n請選擇分攤對象。",
            actions=_participant_actions(draft_type),
        )
    if "付款人" in (payload.get("missing") or []):
        return FlowResult(
            True,
            format_expense_draft(payload) + "\n\n請選擇或輸入付款人。",
            actions=_payer_actions(draft_type),
        )
    if draft_type == "invoice" and payload.get("mode") not in {"merge", "split"}:
        prefix = "已修改發票總額，請重新確認合併方式。\n\n" if mode_reset else ""
        actions = [ActionSpec("合併成一筆", "postback", "invoice|mode|merge")]
        if payload.get("items"):
            actions.append(ActionSpec("展開明細", "postback", "invoice|mode|split"))
        actions.append(ActionSpec("取消", "postback", "invoice|cancel"))
        return FlowResult(True, prefix + format_expense_draft(payload), actions=actions)
    return FlowResult(True, format_expense_draft(payload), actions=_confirmation_actions(draft_type))


def _set_custom_participant_waiting(line_group_id: str, line_user_id: str) -> FlowResult:
    for draft_type in ("expense", "invoice"):
        stored = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
        )
        if not isinstance(stored, dict):
            continue
        payload = dict(stored.get("payload") if isinstance(stored.get("payload"), dict) else stored)
        if "分攤對象" not in (payload.get("missing") or []):
            continue
        payload["awaiting_custom_participants"] = True
        _db_function("save_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
            payload=redact_structure(payload),
        )
        return FlowResult(True, "請直接輸入成員姓名，例如：小明、小華、Amy。若不需要分攤，也可以輸入「不分攤」。")
    return FlowResult(True, "目前沒有等待補充分攤對象的記帳草稿。")


def _set_custom_payer_waiting(line_group_id: str, line_user_id: str) -> FlowResult:
    for draft_type in ("expense", "invoice"):
        stored = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
        )
        if not isinstance(stored, dict):
            continue
        payload = dict(stored.get("payload") if isinstance(stored.get("payload"), dict) else stored)
        if "付款人" not in (payload.get("missing") or []):
            continue
        payload["awaiting_custom_payer"] = True
        _db_function("save_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
            payload=redact_structure(payload),
        )
        return FlowResult(True, "請直接輸入付款人姓名，例如：小華。")
    return FlowResult(True, "目前沒有等待補充付款人的記帳草稿。")


def _handle_custom_field_reply(
    normalized: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not line_group_id or not database_contract_ready():
        return FlowResult(False)
    for draft_type in ("expense", "invoice"):
        stored = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
        )
        if not isinstance(stored, dict):
            continue
        payload = dict(stored.get("payload") if isinstance(stored.get("payload"), dict) else stored)
        if payload.get("awaiting_custom_participants"):
            participants, selected = _parse_participants(normalized)
            if not selected:
                return FlowResult(True, "請輸入成員姓名，或輸入「不分攤」。")
            payload["participants"] = participants
            payload["missing"] = [item for item in (payload.get("missing") or []) if item != "分攤對象"]
            if not _person_name(payload.get("payer")) and "付款人" not in payload["missing"]:
                payload["missing"].append("付款人")
            payload.pop("awaiting_custom_participants", None)
        elif payload.get("awaiting_custom_payer"):
            payer = _clean_name(normalized, max_length=40)
            if not payer or payer in {"無", "不填", "不知道"}:
                return FlowResult(True, "請輸入付款人姓名，例如：小華。")
            payload["payer"] = payer
            payload["missing"] = [item for item in (payload.get("missing") or []) if item != "付款人"]
            payload.pop("awaiting_custom_payer", None)
        else:
            continue
        _db_function("save_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
            payload=redact_structure(payload),
        )
        return _draft_result_after_edit(draft_type, payload)
    return FlowResult(False)


def _edit_pending_draft(
    normalized: str,
    *,
    line_group_id: str,
    line_user_id: str,
    now: datetime | None = None,
) -> FlowResult:
    prefixes = (
        ("修改發票草稿", "invoice"),
        ("修改記帳草稿", "expense"),
        ("修改草稿", ""),
    )
    prefix, requested_type = next((item for item in prefixes if normalized.startswith(item[0])), ("", ""))
    edit_text = normalized[len(prefix) :].strip(" ，,:：")
    if not edit_text:
        example_prefix = "修改發票草稿" if requested_type == "invoice" else "修改記帳草稿"
        return FlowResult(
            True,
            f"請在後面輸入要修改的內容，例如：{example_prefix} 金額 1800 商家 ○○餐廳。\n"
            "可修改：項目、金額、分攤對象、消費日期、商家、分類、付款人、備註。",
        )

    candidates: list[tuple[str, dict[str, Any]]] = []
    draft_types = (requested_type,) if requested_type else ("expense", "invoice")
    for draft_type in draft_types:
        stored = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=draft_type,
        )
        if isinstance(stored, dict):
            candidates.append((draft_type, stored))
    if not candidates:
        return FlowResult(True, "目前沒有可以修改的記帳草稿。")
    if len(candidates) > 1:
        return FlowResult(True, "目前同時有一般記帳與發票草稿，請輸入「修改記帳草稿」或「修改發票草稿」。")

    draft_type, stored = candidates[0]
    payload = dict(stored.get("payload") if isinstance(stored.get("payload"), dict) else stored)
    changes: dict[str, Any] = {}
    for field_match in _DRAFT_EDIT_FIELD_RE.finditer(edit_text):
        label, value = field_match.group(1), field_match.group(2).strip(" ，,；;")
        if label == "金額":
            amount_match = _AMOUNT_RE.search(value)
            if amount_match is None or int(amount_match.group(1).replace(",", "")) <= 0:
                return FlowResult(True, "金額必須是大於 0 的整數，例如：金額 1800。")
            changes["amount"] = int(amount_match.group(1).replace(",", ""))
        elif label == "分攤對象":
            participants, selected = _parse_participants(value)
            if not selected:
                return FlowResult(True, "分攤對象請輸入成員名稱，或輸入「分攤對象 不分攤」。")
            changes["participants"] = participants
        elif label in {"消費日期", "日期"}:
            consumed_at = _parse_consumed_at(value, now=now)
            if consumed_at is None:
                return FlowResult(True, "消費日期格式無法辨識，請使用 YYYY/MM/DD，例如：2026/08/10。")
            changes["consumed_at"] = consumed_at
        else:
            field_names = {
                "項目": "item",
                "商家": "merchant",
                "分類": "category",
                "付款人": "payer",
                "備註": "note",
            }
            if label == "付款人" and value in {"無", "不填", "不知道"}:
                return FlowResult(True, "付款人不能留空，請輸入付款人姓名。")
            changes[field_names[label]] = "" if value == "無" else _clean_name(
                value,
                max_length=500 if label == "備註" else 120,
            )
    if not changes:
        return FlowResult(
            True,
            "沒有找到可修改的欄位。可修改：項目、金額、分攤對象、消費日期、商家、分類、付款人、備註。",
        )

    payload.update(changes)
    if "participants" in changes:
        payload["missing"] = [item for item in (payload.get("missing") or []) if item != "分攤對象"]
    if changes.get("payer"):
        payload["missing"] = [item for item in (payload.get("missing") or []) if item != "付款人"]
    mode_reset = draft_type == "invoice" and "amount" in changes and payload.get("mode") == "split"
    if mode_reset:
        payload["mode"] = "undecided"
    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=draft_type,
        payload=redact_structure(payload),
    )
    return _draft_result_after_edit(draft_type, payload, mode_reset=mode_reset)


def _active_book_or_result(line_group_id: str) -> tuple[dict[str, Any] | None, FlowResult | None]:
    book = _db_function("get_active_expense_book")(line_group_id)
    if isinstance(book, dict):
        return book, None
    return None, FlowResult(
        handled=True,
        text="這個群組還沒有進行中的行程帳本。請輸入「開始記帳 帳本名稱」。",
    )


def _book_id(book: dict[str, Any]) -> Any:
    return book.get("_id") or book.get("id")


def _create_book(line_group_id: str, line_user_id: str, name: str) -> FlowResult:
    if not database_contract_ready(("create_expense_book",)):
        return database_unavailable_result()
    clean_name = _clean_name(name) or "未命名行程"
    book = _db_function("create_expense_book")(
        line_group_id=line_group_id,
        name=clean_name,
        created_by=line_user_id,
        members=[],
        start_at=None,
        end_at=None,
        timezone="Asia/Taipei",
    )
    return FlowResult(
        handled=True,
        text=f"已建立行程帳本「{clean_name}」。接下來可以輸入「記帳 項目 金額」。",
        data={"book": book if isinstance(book, dict) else {}},
    )


def _save_manual_draft(
    line_group_id: str,
    line_user_id: str,
    draft: dict[str, Any],
) -> FlowResult:
    book, error = _active_book_or_result(line_group_id)
    if error:
        return error
    draft = redact_structure({**draft, "book_id": _book_id(book or {})})
    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type="expense",
        payload=draft,
    )
    return _draft_result_after_edit("expense", draft)


def _confirm_expense(line_group_id: str, line_user_id: str) -> FlowResult:
    required = _DB_REQUIRED_BASE + ("create_expense",)
    if not database_contract_ready(required):
        return database_unavailable_result()
    draft = _db_function("get_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type="expense",
    )
    if not isinstance(draft, dict):
        return FlowResult(True, "目前沒有等待確認的記帳草稿。")
    payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else draft
    if not _person_name(payload.get("payer")) and "付款人" not in (payload.get("missing") or []):
        payload["missing"] = [*(payload.get("missing") or []), "付款人"]
        _db_function("save_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type="expense",
            payload=redact_structure(payload),
        )
    if payload.get("missing"):
        return _draft_result_after_edit("expense", payload)
    expense = _db_function("create_expense")(
        book_id=payload.get("book_id"),
        expense=redact_structure(payload),
        created_by=line_user_id,
    )
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type="expense",
    )
    expense_no = (expense or {}).get("expense_no") if isinstance(expense, dict) else None
    suffix = f"，支出編號為 {expense_no}" if expense_no else ""
    return FlowResult(True, f"已完成記帳{suffix}。")


def _cancel_draft(line_group_id: str, line_user_id: str) -> FlowResult:
    if not database_contract_ready():
        return database_unavailable_result()
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type="expense",
    )
    return FlowResult(True, "已取消這筆記帳草稿。")


def _source_label(value: Any) -> str:
    source = str(value or "").strip()
    return {
        "manual": "手動記帳",
        "invoice_qr": "發票 QR Code",
        "invoice_ocr": "發票 OCR",
    }.get(source, _clean_name(source, max_length=30) or "手動記帳")


def _format_report(book: dict[str, Any], expenses: list[dict[str, Any]]) -> str:
    name = _clean_name(book.get("name") or "行程")
    lines = [f"{name}｜行程花費明細", ""]
    total = 0
    category_totals: dict[str, int] = {}
    confirmed = [item for item in expenses if item.get("status", "confirmed") == "confirmed"]
    for expense in confirmed:
        amount = int(expense.get("amount") or 0)
        total += amount
        category = _clean_name(expense.get("category") or "其他", max_length=30)
        category_totals[category] = category_totals.get(category, 0) + amount
        consumed = expense.get("consumed_at")
        date_text = f"{consumed.month}/{consumed.day}" if isinstance(consumed, datetime) else str(consumed or "")[:10]
        participants = expense.get("participants") or []
        participants_text = "、".join(
            _clean_name(item.get("display_name") if isinstance(item, dict) else item, max_length=40)
            for item in participants
        ) or "無（不分攤）"
        lines.extend(
            [
                f"#{expense.get('expense_no') or '-'}｜{date_text} {_clean_name(expense.get('item'))}",
                f"金額：NT${amount:,}",
                f"分攤對象：{participants_text}",
                f"商家：{_clean_name(expense.get('merchant')) or '未填寫'}",
                f"分類：{category}",
                f"付款人：{_person_name(expense.get('payer')) or '未填寫'}",
                f"來源：{_source_label(expense.get('source'))}",
                "",
            ]
        )
    lines.extend([f"總筆數：{len(confirmed)} 筆", f"總花費：NT${total:,}", "", "分類統計："])
    lines.extend(f"{category}：NT${amount:,}" for category, amount in sorted(category_totals.items()))
    return redact_sensitive_identifiers("\n".join(lines).strip())


def build_expense_report(book: dict[str, Any], expenses: list[dict[str, Any]]) -> str:
    return _format_report(book, expenses)


def build_expense_report_result(
    book: dict[str, Any],
    expenses: list[dict[str, Any]],
) -> FlowResult:
    """Build a LINE report and carry a redacted snapshot for optional PDF export."""
    confirmed = [item for item in expenses if item.get("status", "confirmed") == "confirmed"]
    return FlowResult(
        True,
        _format_report(book, confirmed),
        data={
            "expense_report": redact_structure(
                {
                    "book": dict(book),
                    "expenses": confirmed,
                }
            )
        },
    )


def handle_expense_text(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
    now: datetime | None = None,
    default_payer: str = "",
) -> FlowResult:
    normalized = redact_sensitive_identifiers(text.strip())
    expense_signal = normalized.startswith(
        (
            "記帳",
            "開始記帳",
            "確認記帳",
            "取消記帳",
            "設定分攤對象",
            "設定付款人",
            "修改帳本名稱",
            "修改草稿",
            "修改記帳草稿",
            "修改發票草稿",
            "修改支出",
            "取消支出",
            "查看花費",
            "產生花費明細",
            "結束行程",
            "重新開啟帳本",
        )
    )
    if not expense_signal:
        try:
            return _handle_custom_field_reply(
                normalized,
                line_group_id=line_group_id,
                line_user_id=line_user_id,
            )
        except Exception:
            return FlowResult(False)
    if not line_group_id:
        return FlowResult(True, "行程記帳目前只支援 LINE 群組。")

    try:
        if normalized.startswith("開始記帳"):
            return _create_book(line_group_id, line_user_id, normalized[len("開始記帳") :].strip())
        if normalized.startswith("修改帳本名稱"):
            new_name = _clean_name(normalized[len("修改帳本名稱") :].strip(" ：:"), max_length=120)
            if not new_name:
                return FlowResult(True, "請輸入新的帳本名稱，例如：修改帳本名稱 澎湖三天兩夜。")
            if not database_contract_ready(("get_active_expense_book", "rename_expense_book")):
                return database_unavailable_result()
            book, error = _active_book_or_result(line_group_id)
            if error:
                return error
            creator_id = str((book or {}).get("created_by") or "")
            if creator_id and creator_id != line_user_id:
                return FlowResult(True, "只有帳本建立者可以修改帳本名稱。")
            renamed = _db_function("rename_expense_book")(
                book_id=_book_id(book or {}),
                name=new_name,
                renamed_by=line_user_id,
            )
            final_name = _clean_name((renamed or {}).get("name") if isinstance(renamed, dict) else new_name)
            return FlowResult(True, f"帳本名稱已修改為「{final_name or new_name}」。")
        if normalized in {"確認記帳", "確認"}:
            return _confirm_expense(line_group_id, line_user_id)
        if normalized in {"取消記帳", "取消"}:
            return _cancel_draft(line_group_id, line_user_id)
        if normalized.startswith(("修改草稿", "修改記帳草稿", "修改發票草稿")):
            if not database_contract_ready():
                return database_unavailable_result()
            return _edit_pending_draft(
                normalized,
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                now=now,
            )
        if normalized.startswith("設定分攤對象"):
            if not database_contract_ready():
                return database_unavailable_result()
            participant_text = normalized[len("設定分攤對象") :].strip(" ：:")
            if not participant_text:
                return _set_custom_participant_waiting(line_group_id, line_user_id)
            participants, selected = _parse_participants(participant_text)
            if not selected:
                return FlowResult(True, "請輸入成員，例如：設定分攤對象 小明、小華、Amy。")
            for draft_type, confirm_data in (("expense", "expense|confirm"), ("invoice", "invoice|confirm")):
                stored = _db_function("get_feature_draft")(
                    line_group_id=line_group_id,
                    line_user_id=line_user_id,
                    draft_type=draft_type,
                )
                if not isinstance(stored, dict):
                    continue
                payload = stored.get("payload") if isinstance(stored.get("payload"), dict) else stored
                payload["participants"] = participants
                payload["missing"] = [item for item in (payload.get("missing") or []) if item != "分攤對象"]
                if not _person_name(payload.get("payer")) and "付款人" not in payload["missing"]:
                    payload["missing"].append("付款人")
                payload.pop("awaiting_custom_participants", None)
                _db_function("save_feature_draft")(
                    line_group_id=line_group_id,
                    line_user_id=line_user_id,
                    draft_type=draft_type,
                    payload=redact_structure(payload),
                )
                if draft_type == "invoice" and payload.get("mode") not in {"merge", "split"}:
                    return FlowResult(
                        True,
                        "已設定分攤對象，請再選擇發票要合併或展開。",
                        actions=[
                            ActionSpec("合併成一筆", "postback", "invoice|mode|merge"),
                            ActionSpec("展開明細", "postback", "invoice|mode|split"),
                        ],
                    )
                return _draft_result_after_edit(draft_type, payload)
            return FlowResult(True, "目前沒有等待補充分攤對象的記帳草稿。")
        if normalized.startswith("設定付款人"):
            if not database_contract_ready():
                return database_unavailable_result()
            payer_text = normalized[len("設定付款人") :].strip(" ：:")
            if not payer_text:
                return _set_custom_payer_waiting(line_group_id, line_user_id)
            payer = _clean_name(payer_text, max_length=40)
            if not payer or payer in {"無", "不填", "不知道"}:
                return FlowResult(True, "請輸入付款人，例如：設定付款人 小華。")
            for draft_type in ("expense", "invoice"):
                stored = _db_function("get_feature_draft")(
                    line_group_id=line_group_id,
                    line_user_id=line_user_id,
                    draft_type=draft_type,
                )
                if not isinstance(stored, dict):
                    continue
                payload = stored.get("payload") if isinstance(stored.get("payload"), dict) else stored
                payload["payer"] = payer
                payload["missing"] = [item for item in (payload.get("missing") or []) if item != "付款人"]
                payload.pop("awaiting_custom_payer", None)
                _db_function("save_feature_draft")(
                    line_group_id=line_group_id,
                    line_user_id=line_user_id,
                    draft_type=draft_type,
                    payload=redact_structure(payload),
                )
                return _draft_result_after_edit(draft_type, payload)
            return FlowResult(True, "目前沒有等待補充付款人的記帳草稿。")
        if normalized.startswith(("修改支出", "取消支出")):
            number_match = _EXPENSE_NO_RE.search(normalized)
            if number_match is None:
                return FlowResult(True, "請提供支出編號，例如：取消支出 EXP-001。")
            expense_no = number_match.group(1).upper()
            book, error = _active_book_or_result(line_group_id)
            if error:
                return error
            if normalized.startswith("取消支出"):
                if not database_contract_ready(("cancel_expense", "get_active_expense_book")):
                    return database_unavailable_result()
                _db_function("cancel_expense")(
                    book_id=_book_id(book or {}),
                    expense_no=expense_no,
                    cancelled_by=line_user_id,
                )
                return FlowResult(True, f"已取消支出 {expense_no}，編號不會重新使用。")

            if not database_contract_ready(("update_expense", "get_active_expense_book")):
                return database_unavailable_result()
            edit_text = normalized[number_match.end() :].strip(" ，,:：")
            changes: dict[str, Any] = {}
            field_names = {
                "項目": "item",
                "商家": "merchant",
                "分類": "category",
                "付款人": "payer",
                "備註": "note",
            }
            for field_match in _EDIT_FIELD_RE.finditer(edit_text):
                label, value = field_match.group(1), field_match.group(2).strip(" ，,")
                if label == "金額":
                    amount_match = _AMOUNT_RE.search(value)
                    if amount_match:
                        changes["amount"] = int(amount_match.group(1).replace(",", ""))
                else:
                    changes[field_names[label]] = _clean_name(value, max_length=500 if label == "備註" else 120)
            if not changes:
                return FlowResult(
                    True,
                    "請提供要修改的欄位，例如：修改支出 EXP-001 金額 1800 商家 ○○餐廳。",
                )
            updated = _db_function("update_expense")(
                book_id=_book_id(book or {}),
                expense_no=expense_no,
                changes=redact_structure(changes),
                updated_by=line_user_id,
            )
            item = _clean_name((updated or {}).get("item")) if isinstance(updated, dict) else ""
            suffix = f"（{item}）" if item else ""
            return FlowResult(True, f"已更新支出 {expense_no}{suffix}。")
        if normalized.startswith("記帳"):
            if not database_contract_ready():
                return database_unavailable_result()
            draft = parse_expense_command(normalized, now=now)
            if not draft or draft.get("missing") == ["金額"]:
                return FlowResult(True, "請提供金額，例如：記帳 晚餐 1680 元，分攤對象是小明、小華。")
            payer_name = _clean_name(default_payer, max_length=40)
            if payer_name and not draft.get("payer"):
                draft["payer"] = payer_name
                draft["missing"] = [item for item in (draft.get("missing") or []) if item != "付款人"]
            return _save_manual_draft(line_group_id, line_user_id, draft)
        if normalized in {"查看花費", "產生花費明細"}:
            if not database_contract_ready(("get_active_expense_book", "list_expenses")):
                return database_unavailable_result()
            book, error = _active_book_or_result(line_group_id)
            if error:
                return error
            expenses = _db_function("list_expenses")(_book_id(book or {}), status="confirmed") or []
            return build_expense_report_result(book or {}, list(expenses))
        if normalized == "結束行程":
            if not database_contract_ready(("get_active_expense_book",)):
                return database_unavailable_result()
            book, error = _active_book_or_result(line_group_id)
            if error:
                return error
            return FlowResult(
                True,
                f"要結束行程帳本「{_clean_name((book or {}).get('name'))}」並產生報表嗎？",
                actions=[ActionSpec("確認結束", "postback", "expense|close"), ActionSpec("取消", "postback", "expense|close_cancel")],
            )
        if normalized == "重新開啟帳本":
            if not database_contract_ready(("reopen_expense_book",)):
                return database_unavailable_result()
            book = _db_function("reopen_expense_book")(
                line_group_id=line_group_id,
                requested_by=line_user_id,
            )
            return FlowResult(True, f"已重新開啟帳本「{_clean_name((book or {}).get('name'))}」。")
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception:
        return FlowResult(True, "記帳功能暫時無法完成這個操作，請稍後再試。")
    return FlowResult(False)


def handle_expense_postback(
    data: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not data.startswith("expense|"):
        return FlowResult(False)
    if not line_group_id:
        return FlowResult(True, "行程記帳目前只支援 LINE 群組。")
    try:
        action = data.split("|", 2)[1:]
        if action == ["confirm"]:
            return _confirm_expense(line_group_id, line_user_id)
        if action == ["cancel"]:
            return _cancel_draft(line_group_id, line_user_id)
        if action == ["edit_prompt"]:
            return FlowResult(
                True,
                "請輸入要修改的內容，例如：修改記帳草稿 金額 1800 商家 ○○餐廳。\n"
                "可修改：項目、金額、分攤對象、消費日期、商家、分類、付款人、備註。",
            )
        if action == ["close_cancel"]:
            return FlowResult(True, "已取消結束行程。")
        if action == ["close"]:
            if not database_contract_ready(("get_active_expense_book", "close_expense_book", "list_expenses")):
                return database_unavailable_result()
            book, error = _active_book_or_result(line_group_id)
            if error:
                return error
            closed = _db_function("close_expense_book")(
                book_id=_book_id(book or {}),
                closed_by=line_user_id,
            )
            final_book = closed if isinstance(closed, dict) else (book or {})
            expenses = _db_function("list_expenses")(_book_id(final_book), status="confirmed") or []
            return build_expense_report_result(final_book, list(expenses))
        if len(action) == 2 and action[0] == "participants":
            if not database_contract_ready():
                return database_unavailable_result()
            draft = _db_function("get_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="expense",
            )
            if not isinstance(draft, dict):
                return FlowResult(True, "目前沒有等待補充分攤對象的草稿。")
            payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else draft
            book = _db_function("get_active_expense_book")(line_group_id) or {}
            if action[1] == "all":
                members = book.get("members") or []
                payload["participants"] = members
            elif action[1] == "previous":
                if not database_contract_ready(_DB_REQUIRED_BASE + ("get_latest_expense",)):
                    return database_unavailable_result()
                latest = _db_function("get_latest_expense")(_book_id(book))
                if not isinstance(latest, dict):
                    return FlowResult(True, "目前沒有上一筆支出可以套用。")
                payload["participants"] = latest.get("participants") or []
            elif action[1] == "none":
                payload["participants"] = []
            if action[1] == "all" and not payload.get("participants"):
                return FlowResult(True, "帳本內還沒有可用的成員，請先用自訂成員填寫。")
            payload["missing"] = [item for item in (payload.get("missing") or []) if item != "分攤對象"]
            if not _person_name(payload.get("payer")) and "付款人" not in payload["missing"]:
                payload["missing"].append("付款人")
            _db_function("save_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="expense",
                payload=redact_structure(payload),
            )
            return _draft_result_after_edit("expense", payload)
        if len(action) == 2 and action[0] == "payer":
            if not database_contract_ready(_DB_REQUIRED_BASE + ("get_latest_expense",)):
                return database_unavailable_result()
            draft = _db_function("get_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="expense",
            )
            if not isinstance(draft, dict):
                return FlowResult(True, "目前沒有等待補充付款人的草稿。")
            payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else draft
            book = _db_function("get_active_expense_book")(line_group_id) or {}
            latest = _db_function("get_latest_expense")(_book_id(book))
            payer = latest.get("payer") if isinstance(latest, dict) else None
            if not payer:
                return FlowResult(True, "上一筆支出沒有付款人可以套用，請改用自訂付款人。")
            payload["payer"] = payer
            payload["missing"] = [item for item in (payload.get("missing") or []) if item != "付款人"]
            _db_function("save_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="expense",
                payload=redact_structure(payload),
            )
            return _draft_result_after_edit("expense", payload)
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception:
        return FlowResult(True, "記帳功能暫時無法完成這個操作，請稍後再試。")
    return FlowResult(True, "這個記帳操作已失效，請重新輸入。")


def ensure_book_from_itinerary(
    *,
    line_group_id: str,
    line_user_id: str,
    itinerary: dict[str, Any],
) -> None:
    if not line_group_id or not database_contract_ready(("get_active_expense_book", "create_expense_book")):
        return
    if _db_function("get_active_expense_book")(line_group_id):
        return
    _db_function("create_expense_book")(
        line_group_id=line_group_id,
        name=_clean_name(itinerary.get("title") or "行程"),
        created_by=line_user_id,
        members=[],
        start_at=None,
        end_at=None,
        timezone="Asia/Taipei",
    )
