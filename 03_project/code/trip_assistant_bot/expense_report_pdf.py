"""Temporary, privacy-safe PDF exports for expense reports.

The module keeps only a minimized redacted snapshot in process memory.  It does
not write PDFs, invoice images, or report metadata to MongoDB or local storage.
Download tokens are random bearer tokens and expire automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import hashlib
import io
import os
import re
import secrets
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from privacy_redaction import redact_sensitive_identifiers


EXPENSE_REPORT_PDF_TTL_SECONDS = max(
    300,
    int(os.getenv("EXPENSE_REPORT_PDF_TTL_SECONDS", "3600")),
)
EXPENSE_REPORT_PDF_MAX_SESSIONS = max(
    10,
    int(os.getenv("EXPENSE_REPORT_PDF_MAX_SESSIONS", "300")),
)


@dataclass(frozen=True)
class ExpenseReportSnapshot:
    book: dict[str, Any]
    expenses: tuple[dict[str, Any], ...]
    created_at: datetime
    expires_at_monotonic: float


_sessions: dict[str, ExpenseReportSnapshot] = {}
_sessions_lock = threading.Lock()


def _clean_text(value: Any, max_length: int = 500) -> str:
    return redact_sensitive_identifiers(str(value or "").strip())[:max_length]


def _person_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("display_name") or value.get("name") or ""
    return _clean_text(value, 80)


def _project_book(book: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _clean_text(book.get("name") or "行程", 120),
        "start_at": book.get("start_at"),
        "end_at": book.get("end_at"),
        "closed_at": book.get("closed_at"),
        "timezone": _clean_text(book.get("timezone") or "Asia/Taipei", 80),
    }


def _project_expense(expense: dict[str, Any]) -> dict[str, Any]:
    participants = [
        name
        for name in (_person_name(item) for item in expense.get("participants") or [])
        if name
    ]
    try:
        amount = int(expense.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    return {
        "expense_no": _clean_text(expense.get("expense_no") or "-", 30),
        "item": _clean_text(expense.get("item") or "未命名支出", 160),
        "amount": amount,
        "currency": _clean_text(expense.get("currency") or "TWD", 10),
        "participants": participants,
        "consumed_at": expense.get("consumed_at"),
        "merchant": _clean_text(expense.get("merchant"), 160),
        "category": _clean_text(expense.get("category") or "其他", 60),
        "payer": _person_name(expense.get("payer")),
        "source": _clean_text(expense.get("source") or "manual", 40),
        "note": _clean_text(expense.get("note"), 1000),
        "status": _clean_text(expense.get("status") or "confirmed", 20),
    }


def _session_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prune_sessions_locked(now_monotonic: float) -> None:
    for key, snapshot in list(_sessions.items()):
        if snapshot.expires_at_monotonic <= now_monotonic:
            _sessions.pop(key, None)
    if len(_sessions) < EXPENSE_REPORT_PDF_MAX_SESSIONS:
        return
    overflow = len(_sessions) - EXPENSE_REPORT_PDF_MAX_SESSIONS + 1
    oldest = sorted(_sessions, key=lambda key: _sessions[key].created_at)[:overflow]
    for key in oldest:
        _sessions.pop(key, None)


def create_expense_report_session(book: dict[str, Any], expenses: list[dict[str, Any]]) -> str:
    """Store a minimized report snapshot and return its unguessable download token."""
    token = secrets.token_urlsafe(32)
    now_monotonic = time.monotonic()
    confirmed = [
        _project_expense(expense)
        for expense in expenses
        if isinstance(expense, dict) and expense.get("status", "confirmed") == "confirmed"
    ]
    snapshot = ExpenseReportSnapshot(
        book=_project_book(book if isinstance(book, dict) else {}),
        expenses=tuple(confirmed),
        created_at=datetime.now(timezone.utc),
        expires_at_monotonic=now_monotonic + EXPENSE_REPORT_PDF_TTL_SECONDS,
    )
    with _sessions_lock:
        _prune_sessions_locked(now_monotonic)
        _sessions[_session_key(token)] = snapshot
    return token


def get_expense_report_session(token: str) -> ExpenseReportSnapshot | None:
    """Return an unexpired snapshot without consuming it, allowing download retries."""
    normalized = str(token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", normalized):
        return None
    now_monotonic = time.monotonic()
    with _sessions_lock:
        _prune_sessions_locked(now_monotonic)
        return _sessions.get(_session_key(normalized))


def _report_timezone(snapshot: ExpenseReportSnapshot) -> ZoneInfo:
    try:
        return ZoneInfo(str(snapshot.book.get("timezone") or "Asia/Taipei"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Taipei")


def _format_datetime(value: Any, snapshot: ExpenseReportSnapshot, include_time: bool = False) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        local = value.astimezone(_report_timezone(snapshot))
        return local.strftime("%Y/%m/%d %H:%M" if include_time else "%Y/%m/%d")
    return _clean_text(value, 30) or "未填寫"


def _source_label(value: Any) -> str:
    return {
        "manual": "手動記帳",
        "invoice_qr": "發票 QR Code",
        "invoice_ocr": "發票 OCR",
    }.get(str(value or ""), _clean_text(value, 40) or "手動記帳")


def build_expense_report_pdf(snapshot: ExpenseReportSnapshot) -> bytes:
    """Render a Traditional-Chinese expense report as an in-memory PDF."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    configured_font = os.getenv("EXPENSE_REPORT_PDF_FONT_PATH", "").strip()
    bundled_font_dir = os.path.join(os.path.dirname(__file__), "assets", "fonts")
    font_candidates = [
        configured_font,
        os.path.join(bundled_font_dir, "NotoSansTC-Regular.ttf"),
        r"C:\Windows\Fonts\NotoSansTC-VF.ttf",
        r"C:\Windows\Fonts\msjh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    font_name = ""
    for index, font_path in enumerate(font_candidates):
        if not font_path or not os.path.isfile(font_path):
            continue
        candidate_name = f"TripExpenseCJK{index}"
        try:
            pdfmetrics.registerFont(TTFont(candidate_name, font_path, subfontIndex=0))
            font_name = candidate_name
            break
        except Exception:
            continue
    if not font_name:
        # This fallback keeps PDF generation available, but deployments should
        # configure an embeddable TTF/TTC font so every viewer can render Chinese.
        font_name = "MSung-Light"
        try:
            pdfmetrics.getFont(font_name)
        except KeyError:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    output = io.BytesIO()
    book_name = _clean_text(snapshot.book.get("name") or "行程", 120)
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=f"{book_name}｜行程花費明細",
        author="Trip Assistant Bot",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ExpenseTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=19,
        leading=25,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17324D"),
        spaceAfter=8 * mm,
    )
    heading_style = ParagraphStyle(
        "ExpenseHeading",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=12,
        leading=17,
        textColor=colors.HexColor("#17324D"),
        spaceBefore=5 * mm,
        spaceAfter=2 * mm,
    )
    body_style = ParagraphStyle(
        "ExpenseBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#243746"),
    )
    small_style = ParagraphStyle(
        "ExpenseSmall",
        parent=body_style,
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#5D6B78"),
    )
    amount_style = ParagraphStyle(
        "ExpenseAmount",
        parent=body_style,
        alignment=TA_RIGHT,
    )

    def paragraph(value: Any, style: ParagraphStyle = body_style) -> Paragraph:
        safe = escape(_clean_text(value, 2000)).replace("\n", "<br/>")
        return Paragraph(safe or "—", style)

    confirmed = [item for item in snapshot.expenses if item.get("status") == "confirmed"]
    total = sum(int(item.get("amount") or 0) for item in confirmed)
    category_totals: dict[str, int] = {}
    for expense in confirmed:
        category = _clean_text(expense.get("category") or "其他", 60)
        category_totals[category] = category_totals.get(category, 0) + int(expense.get("amount") or 0)

    story: list[Any] = [Paragraph(escape(f"{book_name}｜行程花費明細"), title_style)]
    start_text = _format_datetime(snapshot.book.get("start_at"), snapshot)
    end_text = _format_datetime(snapshot.book.get("end_at"), snapshot)
    story.extend(
        [
            paragraph(f"行程期間：{start_text} ～ {end_text}"),
            paragraph(
                f"總筆數：{len(confirmed)} 筆　　總花費：NT${total:,}",
                heading_style,
            ),
        ]
    )

    category_rows = [[paragraph("分類"), paragraph("金額", amount_style)]]
    category_rows.extend(
        [paragraph(category), paragraph(f"NT${amount:,}", amount_style)]
        for category, amount in sorted(category_totals.items())
    )
    if len(category_rows) > 1:
        category_table = Table(category_rows, colWidths=[115 * mm, 45 * mm], repeatRows=1)
        category_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2F8")),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CAD6E0")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend([category_table, Spacer(1, 4 * mm)])

    story.append(Paragraph("支出明細", heading_style))
    for expense in confirmed:
        participants = "、".join(expense.get("participants") or []) or "無（不分攤）"
        date_text = _format_datetime(expense.get("consumed_at"), snapshot)
        header = (
            f"#{expense.get('expense_no') or '-'}｜{date_text}｜"
            f"{expense.get('item') or '未命名支出'}"
        )
        rows = [
            [
                paragraph(header, heading_style),
                "",
                paragraph(f"NT${int(expense.get('amount') or 0):,}", amount_style),
            ],
            [paragraph("分攤對象"), paragraph(participants), ""],
            [paragraph("商家"), paragraph(expense.get("merchant") or "未填寫"), ""],
            [paragraph("分類"), paragraph(expense.get("category") or "其他"), ""],
            [paragraph("付款人"), paragraph(expense.get("payer") or "未填寫"), ""],
            [paragraph("來源"), paragraph(_source_label(expense.get("source"))), ""],
        ]
        if expense.get("note"):
            rows.append([paragraph("備註"), paragraph(expense.get("note")), ""])
        table = Table(rows, colWidths=[34 * mm, 92 * mm, 34 * mm], splitByRow=1)
        detail_spans = [("SPAN", (1, row_index), (2, row_index)) for row_index in range(1, len(rows))]
        table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (1, 0)),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F8FA")),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#A8BBCB")),
                    ("LINEBELOW", (0, 1), (-1, -2), 0.2, colors.HexColor("#E2E8ED")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    *detail_spans,
                ]
            )
        )
        story.extend([table, Spacer(1, 3 * mm)])

    generated_at = snapshot.created_at.astimezone(_report_timezone(snapshot)).strftime("%Y/%m/%d %H:%M")
    story.extend(
        [
            Spacer(1, 4 * mm),
            paragraph(f"PDF 產生時間：{generated_at}　時區：{snapshot.book.get('timezone')}", small_style),
            paragraph("本報表僅列出花費紀錄，不包含欠款、每人應付金額或轉帳建議。", small_style),
        ]
    )

    def add_page_number(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 8)
        canvas.setFillColor(colors.HexColor("#6C7884"))
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"第 {doc.page} 頁")
        canvas.restoreState()

    document.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
    return output.getvalue()


def expense_report_filename(snapshot: ExpenseReportSnapshot) -> str:
    name = _clean_text(snapshot.book.get("name") or "行程", 60)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "行程"
    generated_at = snapshot.created_at.astimezone(_report_timezone(snapshot))
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S")
    return f"{name}-花費明細-{timestamp}.pdf"
