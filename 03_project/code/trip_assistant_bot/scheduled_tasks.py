"""Idempotent scheduled closing for expense books and polls."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Callable

from expense_flow import _book_id, _db_function, build_expense_report, database_contract_ready
from vote_flow import format_poll
from weather_flow import sync_cwa_weather_daily_cache


PushCallback = Callable[[str, str], None]
ExpenseReportPushCallback = Callable[[str, dict, list[dict]], None]
_LOGGER = logging.getLogger(__name__)


def run_due_tasks(
    *,
    push_text: PushCallback,
    push_expense_report: ExpenseReportPushCallback | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> dict[str, int]:
    """Claim due records, close them once, and push their final reports."""
    current = now or datetime.now(timezone.utc)
    result = {
        "books_closed": 0,
        "polls_closed": 0,
        "weather_daily_saved": 0,
        "push_failures": 0,
    }

    try:
        weather_result = sync_cwa_weather_daily_cache(now=current)
        result["weather_daily_saved"] = int(weather_result.get("saved") or 0)
    except Exception as exc:
        _LOGGER.error("Scheduled weather sync failed (%s)", type(exc).__name__)

    book_contract = (
        "claim_due_expense_books",
        "list_expenses",
        "mark_expense_report_sent",
    )
    if database_contract_ready(book_contract):
        books = _db_function("claim_due_expense_books")(now=current, limit=limit) or []
        for book in books:
            try:
                expenses = list(_db_function("list_expenses")(_book_id(book), status="confirmed") or [])
                push_target_id = str(book.get("line_group_id") or "")
                if push_expense_report is None:
                    push_text(push_target_id, build_expense_report(book, expenses))
                else:
                    push_expense_report(push_target_id, book, expenses)
                _db_function("mark_expense_report_sent")(
                    book_id=_book_id(book),
                    sent_at=current,
                )
                result["books_closed"] += 1
            except Exception as exc:
                _LOGGER.error("Scheduled expense report failed (%s)", type(exc).__name__)
                result["push_failures"] += 1

    poll_contract = (
        "claim_due_vote_sessions",
        "get_vote_results",
        "mark_vote_result_announced",
    )
    if database_contract_ready(poll_contract):
        polls = _db_function("claim_due_vote_sessions")(now=current, limit=limit) or []
        for poll in polls:
            try:
                poll_id = str(poll.get("poll_id") or poll.get("_id") or "")
                results = list(_db_function("get_vote_results")(poll_id=poll_id) or [])
                push_target_id = str(poll.get("line_group_id") or "")
                result_text = "投票已截止。\n\n" + format_poll(poll, results)
                push_text(push_target_id, result_text)
                _db_function("mark_vote_result_announced")(
                    poll_id=poll_id,
                    announced_at=current,
                )
                result["polls_closed"] += 1
            except Exception as exc:
                _LOGGER.error("Scheduled poll result failed (%s)", type(exc).__name__)
                result["push_failures"] += 1

    return result
