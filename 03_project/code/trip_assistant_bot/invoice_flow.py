"""Invoice capture sessions, QR parsing, and cloud image recognition."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from expense_flow import (
    ActionSpec,
    DatabaseFeatureUnavailable,
    FlowResult,
    _book_id,
    _confirmation_actions,
    _db_function,
    _draft_result_after_edit,
    _participant_actions,
    database_contract_ready,
    database_unavailable_result,
    format_expense_draft,
    infer_category,
    normalize_participants_for_storage,
)
from privacy_redaction import redact_sensitive_identifiers, redact_structure


_LOGGER = logging.getLogger(__name__)
INVOICE_SESSION_TTL_SECONDS = max(60, int(os.getenv("INVOICE_SESSION_TTL_SECONDS", "900")))
INVOICE_MAX_IMAGE_BYTES = max(100_000, int(os.getenv("INVOICE_MAX_IMAGE_BYTES", str(8 * 1024 * 1024))))
INVOICE_OCR_MODEL = os.getenv("INVOICE_OCR_MODEL", "gpt-4.1-mini").strip()


@dataclass
class InvoiceCaptureSession:
    token: str
    line_group_id: str
    line_user_id: str
    conversation_key: str
    push_target_id: str
    payer_display_name: str
    created_at: float
    status: str = "pending"


_session_lock = threading.Lock()
_sessions: dict[str, InvoiceCaptureSession] = {}
_pending_by_user: dict[tuple[str, str], str] = {}


def _prune_sessions_locked() -> None:
    cutoff = time.time() - INVOICE_SESSION_TTL_SECONDS
    for token, session in list(_sessions.items()):
        if session.created_at <= cutoff or session.status == "consumed":
            _sessions.pop(token, None)
            key = (session.line_group_id, session.line_user_id)
            if _pending_by_user.get(key) == token:
                _pending_by_user.pop(key, None)


def create_capture_session(
    *,
    line_group_id: str,
    line_user_id: str,
    conversation_key: str,
    push_target_id: str,
    payer_display_name: str = "",
) -> InvoiceCaptureSession:
    token = uuid4().hex
    session = InvoiceCaptureSession(
        token=token,
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        conversation_key=conversation_key,
        push_target_id=push_target_id,
        payer_display_name=redact_sensitive_identifiers(payer_display_name.strip())[:40],
        created_at=time.time(),
    )
    with _session_lock:
        _prune_sessions_locked()
        previous = _pending_by_user.get((line_group_id, line_user_id))
        if previous:
            _sessions.pop(previous, None)
        _sessions[token] = session
        _pending_by_user[(line_group_id, line_user_id)] = token
    return session


def get_capture_session(token: str) -> InvoiceCaptureSession | None:
    with _session_lock:
        _prune_sessions_locked()
        return _sessions.get(token)


def claim_capture_session(token: str, line_user_id: str) -> tuple[InvoiceCaptureSession | None, str]:
    with _session_lock:
        _prune_sessions_locked()
        session = _sessions.get(token)
        if session is None:
            return None, "expired"
        if session.line_user_id != line_user_id:
            return None, "forbidden"
        if session.status != "pending":
            return None, "used"
        session.status = "processing"
        return session, ""


def claim_pending_chat_session(line_group_id: str, line_user_id: str) -> InvoiceCaptureSession | None:
    with _session_lock:
        _prune_sessions_locked()
        token = _pending_by_user.get((line_group_id, line_user_id))
        session = _sessions.get(token or "")
        if session is None or session.status != "pending":
            return None
        session.status = "processing"
        return session


def has_pending_chat_session(line_group_id: str, line_user_id: str) -> bool:
    with _session_lock:
        _prune_sessions_locked()
        token = _pending_by_user.get((line_group_id, line_user_id))
        session = _sessions.get(token or "")
        return bool(session is not None and session.status == "pending")


def mark_capture_session(session: InvoiceCaptureSession, status: str) -> None:
    with _session_lock:
        stored = _sessions.get(session.token)
        if stored:
            stored.status = status


def build_invoice_liff_url(session_token: str, request_base_url: str, *, mode: str) -> str:
    liff_id = os.getenv("LIFF_INVOICE_ID", "").strip()
    safe_mode = mode if mode in {"camera", "library", "qr"} else "camera"
    if re.fullmatch(r"\d+-[A-Za-z0-9]+", liff_id):
        return urlunsplit(
            (
                "https",
                "liff.line.me",
                f"/{liff_id}",
                urlencode({"liff_id": liff_id, "mode": safe_mode}),
                urlencode({"session_token": session_token}),
            )
        )

    endpoint = os.getenv("LIFF_INVOICE_ENDPOINT_URL", "").strip()
    if not endpoint:
        endpoint = f"{request_base_url.rstrip('/')}/liff/invoice"
    parts = urlsplit(endpoint)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key not in {"liff_id", "mode"}]
    if liff_id:
        query.append(("liff_id", liff_id))
    query.append(("mode", safe_mode))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), urlencode({"session_token": session_token})))


def invoice_database_ready() -> bool:
    return database_contract_ready(
        (
            "get_active_expense_book",
            "save_feature_draft",
            "get_feature_draft",
            "delete_feature_draft",
            "is_duplicate_invoice_import",
            "create_invoice_import",
        )
    )


def start_invoice_flow(
    *,
    line_group_id: str,
    line_user_id: str,
    conversation_key: str,
    push_target_id: str,
    request_base_url: str,
    default_payer: str = "",
) -> FlowResult:
    if not line_group_id:
        return FlowResult(True, "發票記帳目前只支援 LINE 群組。")
    if not invoice_database_ready():
        return database_unavailable_result()
    try:
        book = _db_function("get_active_expense_book")(line_group_id)
        if not isinstance(book, dict):
            return FlowResult(True, "請先輸入「開始記帳 帳本名稱」，再使用發票記帳。")
        session = create_capture_session(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            conversation_key=conversation_key,
            push_target_id=push_target_id,
            payer_display_name=default_payer,
        )
        return FlowResult(
            True,
            "請選擇發票輸入方式。發票照片只用於辨識，不會長期保存；請勿上傳身分證或信用卡。",
            actions=[
                ActionSpec("直接拍照", "uri", build_invoice_liff_url(session.token, request_base_url, mode="camera")),
                ActionSpec("從相簿選擇", "uri", build_invoice_liff_url(session.token, request_base_url, mode="library")),
                ActionSpec("掃描 QR Code", "uri", build_invoice_liff_url(session.token, request_base_url, mode="qr")),
            ],
        )
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception as exc:
        _LOGGER.exception("Invoice flow startup failed (%s)", type(exc).__name__)
        return FlowResult(True, "目前無法啟動發票記帳，請稍後再試。")


def _validate_image(image_bytes: bytes) -> tuple[bytes, str]:
    if not image_bytes or len(image_bytes) > INVOICE_MAX_IMAGE_BYTES:
        raise ValueError("image_size")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is required") from exc
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.verify()
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        if width < 200 or height < 200 or width * height > 40_000_000:
            raise ValueError("image_dimensions")
        image = image.convert("RGB")
        image.thumbnail((3000, 4000) if height >= width else (4000, 3000))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue(), "image/jpeg"


def decode_qr_payloads(image_bytes: bytes) -> list[str]:
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        return []
    with Image.open(io.BytesIO(image_bytes)) as image:
        results = zxingcpp.read_barcodes(image)
    payloads: list[str] = []
    for result in results:
        text = str(getattr(result, "text", "") or "").strip()
        if text and text not in payloads:
            payloads.append(text)
    return payloads[:4]


def _roc_date(value: str) -> str:
    if len(value) != 7 or not value.isdigit():
        return ""
    try:
        return datetime(int(value[:3]) + 1911, int(value[3:5]), int(value[5:7])).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_taiwan_invoice_qr(payloads: list[str]) -> dict[str, Any]:
    """Parse non-sensitive fixed fields from the official left QR payload."""
    left = next((item for item in payloads if len(item) >= 77 and not item.startswith("**")), "")
    if not left:
        return {}
    try:
        total_amount = int(left[29:37], 16)
    except ValueError:
        total_amount = 0
    # The first ten characters are the invoice number.  They are intentionally
    # neither returned nor logged.
    return {
        "amount": total_amount,
        "consumed_at": _roc_date(left[10:17]),
        "merchant": "",
        "items": [],
        "source": "invoice_qr",
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("invalid_ocr_json")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("invalid_ocr_json")
    return payload


def recognize_invoice_cloud(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or not INVOICE_OCR_MODEL:
        raise RuntimeError("cloud_ocr_not_configured")
    from openai import OpenAI

    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=INVOICE_OCR_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "你是台灣發票與收據辨識器。只輸出 JSON。"
                    "需要支援橫式與直式傳統發票、電子發票證明聯及長條收據。"
                    "不要輸出發票號碼、買方統編、賣方統編、隨機碼、身分證或居留證。"
                    "格式：{\"merchant\":str,\"date\":\"YYYY-MM-DD\",\"total_amount\":int,"
                    "\"category\":str,\"items\":[{\"name\":str,\"quantity\":number,"
                    "\"unit_price\":number,\"amount\":int}],\"service_fee\":int,"
                    "\"discount\":int,\"other_adjustment\":int,\"notes\":str}."
                    "無法確定的欄位使用空字串、0 或空陣列，不要猜測。"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "請辨識這張發票或收據並回傳記帳草稿。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    )
    content = response.choices[0].message.content or "{}"
    return _extract_json_object(content)


def _normalize_cloud_result(payload: dict[str, Any], qr_result: dict[str, Any]) -> dict[str, Any]:
    # Explicitly discard every invoice-number-like field before recursive redaction.
    for key in list(payload):
        if key.casefold() in {
            "invoice_number",
            "invoice_no",
            "track_number",
            "buyer_tax_id",
            "seller_tax_id",
            "random_code",
        }:
            payload.pop(key, None)
    merchant = redact_sensitive_identifiers(str(payload.get("merchant") or qr_result.get("merchant") or "").strip())[:120]
    date_value = str(payload.get("date") or qr_result.get("consumed_at") or "").strip()[:10]
    try:
        amount = int(round(float(payload.get("total_amount") or qr_result.get("amount") or 0)))
    except (TypeError, ValueError):
        amount = 0
    items: list[dict[str, Any]] = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        name = redact_sensitive_identifiers(str(raw.get("name") or "").strip())[:120]
        try:
            line_amount = int(round(float(raw.get("amount") or 0)))
        except (TypeError, ValueError):
            line_amount = 0
        if name and line_amount:
            items.append({"name": name, "amount": line_amount})
    category = redact_sensitive_identifiers(str(payload.get("category") or "").strip())[:40]
    if not category:
        category = infer_category(" ".join([merchant, *(item["name"] for item in items)]))
    def _as_int(value: Any) -> int:
        try:
            return int(round(float(value or 0)))
        except (TypeError, ValueError):
            return 0

    result = {
        "item": merchant or (items[0]["name"] if len(items) == 1 else "發票消費"),
        "amount": max(0, amount),
        "currency": "TWD",
        "participants": [],
        "consumed_at": date_value,
        "merchant": merchant,
        "category": category or "其他",
        "payer": "",
        "source": "invoice_qr" if qr_result else "invoice_ocr",
        "note": redact_sensitive_identifiers(str(payload.get("notes") or "").strip())[:500],
        "items": items[:100],
        "service_fee": _as_int(payload.get("service_fee")),
        "discount": _as_int(payload.get("discount")),
        "other_adjustment": _as_int(payload.get("other_adjustment")),
        "missing": ["分攤對象", "付款人"],
    }
    return redact_structure(result)


def recognize_invoice_image(image_bytes: bytes) -> tuple[dict[str, Any], str]:
    safe_image, mime_type = _validate_image(image_bytes)
    qr_payloads = decode_qr_payloads(safe_image)
    qr_result = parse_taiwan_invoice_qr(qr_payloads)
    cloud_result = recognize_invoice_cloud(safe_image, mime_type)
    draft = _normalize_cloud_result(cloud_result, qr_result)
    fingerprint_source = "\n".join(qr_payloads).encode("utf-8") if qr_payloads else safe_image
    fingerprint = hashlib.sha256(fingerprint_source).hexdigest()
    return draft, fingerprint


def recognize_invoice_qr_text(qr_payload: str) -> tuple[dict[str, Any], str]:
    normalized = qr_payload.strip()
    if not normalized or len(normalized) > 4096:
        raise ValueError("invalid_qr")
    qr_result = parse_taiwan_invoice_qr([normalized])
    if not qr_result:
        raise ValueError("unsupported_qr")
    draft = _normalize_cloud_result({}, qr_result)
    return draft, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _save_invoice_draft(session: InvoiceCaptureSession, draft: dict[str, Any], fingerprint: str) -> FlowResult:
    if not invoice_database_ready():
        return database_unavailable_result()
    book = _db_function("get_active_expense_book")(session.line_group_id)
    if not isinstance(book, dict):
        return FlowResult(True, "找不到進行中的行程帳本，這張發票沒有入帳。")
    if _db_function("is_duplicate_invoice_import")(book_id=_book_id(book), source_fingerprint=fingerprint):
        return FlowResult(True, "這張發票似乎已經匯入過，為避免重複記帳，本次沒有建立草稿。")
    invoice_import = _db_function("create_invoice_import")(
        book_id=_book_id(book),
        source_fingerprint=fingerprint,
        created_by=session.line_user_id,
    )
    payload = {
        **draft,
        "book_id": _book_id(book),
        "invoice_import_id": (invoice_import or {}).get("_id") if isinstance(invoice_import, dict) else invoice_import,
        "mode": "undecided",
    }
    if session.payer_display_name and not payload.get("payer"):
        payload["payer"] = session.payer_display_name
        payload["missing"] = [item for item in (payload.get("missing") or []) if item != "付款人"]
    items = payload.get("items") or []
    if len(items) <= 1:
        payload["mode"] = "merge"
    _db_function("save_feature_draft")(
        line_group_id=session.line_group_id,
        line_user_id=session.line_user_id,
        draft_type="invoice",
        payload=payload,
    )
    if len(items) <= 1:
        next_step = _draft_result_after_edit("invoice", payload)
        warning = ""
        if int(payload.get("amount") or 0) <= 0:
            warning = "\n\n⚠️ 無法可靠辨識總金額，這份草稿暫時不能確認，請取消後改用手動記帳。"
        recognition_note = (
            "只辨識到一個商品，已自動合併成一筆支出。"
            if items
            else "沒有可展開的商品明細，已自動合併成一筆支出。"
        )
        return FlowResult(
            True,
            "已完成雲端辨識。"
            + recognition_note
            + "\n\n"
            + next_step.text
            + warning,
            actions=next_step.actions,
            data={"draft": payload},
        )
    actions = [ActionSpec("合併成一筆", "postback", "invoice|mode|merge")]
    if items:
        actions.append(ActionSpec(f"展開成 {len(items)} 筆"[:20], "postback", "invoice|mode|split"))
    actions.append(ActionSpec("修改內容", "postback", "invoice|edit_prompt"))
    actions.append(ActionSpec("取消", "postback", "invoice|cancel"))
    text = "已完成雲端辨識。\n\n" + format_expense_draft(payload)
    if int(payload.get("amount") or 0) <= 0:
        text += "\n\n⚠️ 無法可靠辨識總金額，這份草稿暫時不能確認，請取消後改用手動記帳。"
    if items:
        text += f"\n\n共辨識到 {len(items)} 個商品，請選擇合併或展開。"
    else:
        text += "\n\n沒有可靠的商品明細，建議合併成一筆。"
    return FlowResult(True, text, actions=actions, data={"draft": payload})


def handle_invoice_image_bytes(
    image_bytes: bytes,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    session = claim_pending_chat_session(line_group_id, line_user_id)
    if session is None:
        return FlowResult(False)
    try:
        draft, fingerprint = recognize_invoice_image(image_bytes)
        result = _save_invoice_draft(session, draft, fingerprint)
        mark_capture_session(session, "consumed")
        return result
    except Exception as exc:
        mark_capture_session(session, "pending")
        _LOGGER.exception("Invoice chat image recognition failed (%s)", type(exc).__name__)
        return FlowResult(True, "無法辨識這張圖片，請確認發票清晰完整後重新拍攝。")


def process_liff_capture(
    session: InvoiceCaptureSession,
    *,
    image_bytes: bytes | None = None,
    qr_payload: str = "",
) -> FlowResult:
    try:
        if qr_payload:
            draft, fingerprint = recognize_invoice_qr_text(qr_payload)
        elif image_bytes is not None:
            draft, fingerprint = recognize_invoice_image(image_bytes)
        else:
            raise ValueError("missing_invoice_input")
        result = _save_invoice_draft(session, draft, fingerprint)
        mark_capture_session(session, "consumed")
        return result
    except Exception:
        mark_capture_session(session, "pending")
        raise


def _invoice_draft(line_group_id: str, line_user_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    stored = _db_function("get_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type="invoice",
    )
    if not isinstance(stored, dict):
        return None, None
    payload = stored.get("payload") if isinstance(stored.get("payload"), dict) else stored
    return stored, payload


def _invoice_expense_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the finalized expense rows required by create_expenses_from_invoice()."""
    mode = str(payload.get("mode") or "")
    participants = normalize_participants_for_storage(payload.get("participants"))
    common = {
        "currency": str(payload.get("currency") or "TWD"),
        "participants": participants,
        "consumed_at": payload.get("consumed_at"),
        "merchant": str(payload.get("merchant") or "")[:120],
        "category": str(payload.get("category") or "其他")[:40],
        "payer": payload.get("payer"),
        "source": payload.get("source"),
        "note": str(payload.get("note") or "")[:500],
    }

    if mode == "merge":
        return [
            {
                **common,
                "item": str(payload.get("item") or payload.get("merchant") or "發票消費")[:120],
                "amount": int(payload.get("amount") or 0),
            }
        ]

    if mode != "split":
        raise ValueError("invoice_mode_not_selected")

    rows: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("item") or "").strip()[:120]
        amount = int(item.get("amount") or 0)
        if name and amount:
            rows.append({**common, "item": name, "amount": amount})

    adjustments = (
        ("服務費", int(payload.get("service_fee") or 0)),
        ("折扣", -int(payload.get("discount") or 0)),
        ("其他調整", int(payload.get("other_adjustment") or 0)),
    )
    rows.extend({**common, "item": label, "amount": amount} for label, amount in adjustments if amount)

    expected_total = int(payload.get("amount") or 0)
    if not rows or sum(int(row["amount"]) for row in rows) != expected_total:
        raise ValueError("invoice_detail_total_mismatch")
    return rows


def handle_invoice_postback(
    data: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not data.startswith("invoice|"):
        return FlowResult(False)
    if not invoice_database_ready():
        return database_unavailable_result()
    try:
        parts = data.split("|")
        _, payload = _invoice_draft(line_group_id, line_user_id)
        if payload is None:
            return FlowResult(True, "這份發票草稿已過期，請重新執行發票記帳。")
        if parts[1] == "cancel":
            _db_function("delete_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="invoice",
            )
            return FlowResult(True, "已取消這次發票記帳。")
        if parts[1:3] in (["mode", "merge"], ["mode", "split"]):
            mode = parts[2]
            if mode == "split" and not payload.get("items"):
                return FlowResult(True, "這張發票沒有可展開的商品明細。")
            if mode == "split":
                item_total = sum(int(item.get("amount") or 0) for item in payload.get("items") or [])
                reconciled_total = (
                    item_total
                    + int(payload.get("service_fee") or 0)
                    - int(payload.get("discount") or 0)
                    + int(payload.get("other_adjustment") or 0)
                )
                invoice_total = int(payload.get("amount") or 0)
                if reconciled_total != invoice_total:
                    return FlowResult(
                        True,
                        "商品明細與發票總額不一致，暫時不能展開。\n"
                        f"商品與調整後合計：NT${reconciled_total:,}\n"
                        f"發票總額：NT${invoice_total:,}\n"
                        "請改用合併一筆，或取消後手動記帳。",
                    )
            payload["mode"] = mode
            _db_function("save_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="invoice",
                payload=redact_structure(payload),
            )
            if "分攤對象" in (payload.get("missing") or []):
                return FlowResult(True, "請選擇分攤對象。", actions=_participant_actions("invoice"))
            return _draft_result_after_edit("invoice", payload)
        if len(parts) == 3 and parts[1] == "participants":
            book = _db_function("get_active_expense_book")(line_group_id) or {}
            if parts[2] == "all":
                participants = book.get("members") or []
                if not participants:
                    return FlowResult(True, "帳本內還沒有可用的成員，請改用自訂成員或選擇不分攤。")
            elif parts[2] == "previous":
                if not database_contract_ready(("get_latest_expense",)):
                    return database_unavailable_result()
                latest = _db_function("get_latest_expense")(_book_id(book))
                if not isinstance(latest, dict):
                    return FlowResult(True, "目前沒有上一筆支出可以套用。")
                participants = latest.get("participants") or []
            elif parts[2] == "none":
                participants = []
            else:
                return FlowResult(True, "這個分攤對象選項已失效，請重新選擇。")
            payload["participants"] = participants
            payload["missing"] = [item for item in (payload.get("missing") or []) if item != "分攤對象"]
            if not payload.get("payer") and "付款人" not in payload["missing"]:
                payload["missing"].append("付款人")
            _db_function("save_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="invoice",
                payload=redact_structure(payload),
            )
            return _draft_result_after_edit("invoice", payload)
        if len(parts) == 3 and parts[1] == "payer":
            if parts[2] != "previous":
                return FlowResult(True, "這個付款人選項已失效，請重新選擇。")
            if not database_contract_ready(("get_latest_expense",)):
                return database_unavailable_result()
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
                draft_type="invoice",
                payload=redact_structure(payload),
            )
            return _draft_result_after_edit("invoice", payload)
        if parts[1] == "edit_prompt":
            return FlowResult(
                True,
                "請輸入要修改的內容，例如：修改發票草稿 金額 2350 商家 ○○海產店。\n"
                "可修改：項目、金額、分攤對象、消費日期、商家、分類、付款人、備註。",
            )
        if parts[1] == "confirm":
            if not database_contract_ready(("create_expenses_from_invoice", "delete_feature_draft")):
                return database_unavailable_result()
            if int(payload.get("amount") or 0) <= 0:
                return FlowResult(True, "總金額尚未正確辨識，不能確認入帳。請取消後改用手動記帳。")
            if not payload.get("payer") and "付款人" not in (payload.get("missing") or []):
                payload["missing"] = [*(payload.get("missing") or []), "付款人"]
                _db_function("save_feature_draft")(
                    line_group_id=line_group_id,
                    line_user_id=line_user_id,
                    draft_type="invoice",
                    payload=redact_structure(payload),
                )
            if payload.get("missing") or payload.get("mode") not in {"merge", "split"}:
                return _draft_result_after_edit("invoice", payload)
            expense_payload = _invoice_expense_payload(payload)
            expenses = _db_function("create_expenses_from_invoice")(
                book_id=payload.get("book_id"),
                invoice_import_id=payload.get("invoice_import_id"),
                payload=redact_structure(expense_payload),
                created_by=line_user_id,
            )
            if database_contract_ready(("add_expense_book_member",)):
                for member in expense_payload[0].get("participants") or []:
                    try:
                        _db_function("add_expense_book_member")(
                            book_id=payload.get("book_id"),
                            member=member,
                            updated_by=line_user_id,
                        )
                    except Exception as member_exc:
                        _LOGGER.warning(
                            "Confirmed invoice member sync failed (%s)",
                            type(member_exc).__name__,
                        )
            _db_function("delete_feature_draft")(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_type="invoice",
            )
            created_expenses = expenses if isinstance(expenses, list) else []
            count = len(created_expenses) if created_expenses else 1
            expense_numbers = [
                str(expense.get("expense_no") or "").strip()
                for expense in created_expenses
                if isinstance(expense, dict) and str(expense.get("expense_no") or "").strip()
            ]
            if expense_numbers:
                number_text = "、".join(expense_numbers)
                return FlowResult(
                    True,
                    f"發票記帳完成，共建立 {count} 筆支出。\n支出編號：{number_text}",
                )
            return FlowResult(True, f"發票記帳完成，共建立 {count} 筆支出。")
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except Exception as exc:
        _LOGGER.exception("Invoice postback flow failed (%s)", type(exc).__name__)
        return FlowResult(True, "發票記帳暫時無法完成這個操作，請稍後再試。")
    return FlowResult(True, "這個發票操作已失效。")
