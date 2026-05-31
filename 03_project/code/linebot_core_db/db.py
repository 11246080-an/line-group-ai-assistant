"""
MongoDB 連線模組。

使用 get_db() 取得資料庫實例，整個 process 共用同一個 MongoClient。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database

_client: MongoClient | None = None


def get_db() -> Database:
    global _client
    if _client is None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("環境變數 MONGODB_URI 未設定")
        _client = MongoClient(uri)
    db_name = os.getenv("MONGODB_DB_NAME", "linebot")
    return _client[db_name]


def ensure_indexes() -> None:
    """建立常用查詢所需的索引，應用啟動時呼叫一次。"""
    db = get_db()
    db.messages.create_index([("line_group_id", ASCENDING), ("sent_at", DESCENDING)])
    db.messages.create_index([("line_user_id", ASCENDING)])
    db.groups.create_index([("line_group_id", ASCENDING)], unique=True)
    db.summaries.create_index([("line_group_id", ASCENDING), ("window_start", DESCENDING)])
    db.itineraries.create_index([("line_group_id", ASCENDING), ("created_at", DESCENDING)])
    db.vote_sessions.create_index([("line_group_id", ASCENDING), ("status", ASCENDING)])
    db.user_preferences.create_index(
        [("line_user_id", ASCENDING), ("line_group_id", ASCENDING)],
        unique=True,
    )


# ── 訊息 ─────────────────────────────────────────────────────────

def save_message(
    line_group_id: str,
    line_user_id: str,
    message_text: str,
    display_name: str = "",
) -> None:
    get_db().messages.insert_one({
        "line_group_id": line_group_id,
        "line_user_id": line_user_id,
        "display_name": display_name,
        "message_text": message_text,
        "sent_at": datetime.now(timezone.utc),
    })


def get_recent_messages(line_group_id: str, limit: int = 15) -> list[str]:
    """取得最近 N 筆訊息文字，由舊到新排列（給 AI 當上下文用）。"""
    docs = (
        get_db().messages
        .find({"line_group_id": line_group_id}, {"message_text": 1})
        .sort("sent_at", DESCENDING)
        .limit(limit)
    )
    return [d["message_text"] for d in reversed(list(docs))]


# ── 分析結果 ──────────────────────────────────────────────────────

def save_summary(line_group_id: str, result: dict) -> None:
    """將 analyze_dialogue() 的結果存入 summaries。"""
    now = datetime.now(timezone.utc)
    get_db().summaries.insert_one({
        "line_group_id": line_group_id,
        "window_start": now,
        "window_end": now,
        "need_type": result.get("need_type"),
        "decision_state": "討論中",
        "has_conflict": False,
        "scenario_result": {
            "scenario_code": result.get("scenario_code"),
            "scenario_name": result.get("scenario_name"),
            "should_intervene": bool(result.get("should_intervene")),
            "intervention_type": result.get("intervention_type"),
            "confidence_score": result.get("confidence_score"),
            "suggested_reply": result.get("suggested_reply"),
        },
    })


# ── 群組 ──────────────────────────────────────────────────────────

def upsert_group(line_group_id: str) -> None:
    get_db().groups.update_one(
        {"line_group_id": line_group_id},
        {"$setOnInsert": {
            "line_group_id": line_group_id,
            "group_name": "",
            "created_at": datetime.now(timezone.utc),
            "members": [],
        }},
        upsert=True,
    )


def upsert_member(line_group_id: str, line_user_id: str, display_name: str = "") -> None:
    get_db().groups.update_one(
        {
            "line_group_id": line_group_id,
            "members.line_user_id": {"$ne": line_user_id},
        },
        {"$push": {"members": {
            "line_user_id": line_user_id,
            "display_name": display_name,
            "joined_at": datetime.now(timezone.utc),
        }}},
    )
