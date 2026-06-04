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
    """將 analyze_dialogue() 的完整結果存入 summaries。

    對齊 AI decision core 所有輸出欄位：
    scenario_code / scenario_name / stage / should_intervene /
    intervention_type / reply_trigger / requires_external_search /
    intermediate_reply / suggested_reply / confidence_score /
    evidence / system_behavior / extracted_info
    """
    now = datetime.now(timezone.utc)
    extracted = result.get("extracted_info") or {}

    get_db().summaries.insert_one({
        "line_group_id": line_group_id,
        "window_start": now,
        "window_end": now,

        # ── AI 情境判斷結果 ──────────────────────────────────────
        "scenario_result": {
            "scenario_code":      result.get("scenario_code"),
            "scenario_name":      result.get("scenario_name"),
            "stage":              result.get("stage"),
            "should_intervene":   bool(result.get("should_intervene")),
            "intervention_type":  result.get("intervention_type"),
            "reply_trigger":      result.get("reply_trigger"),
            "confidence_score":   result.get("confidence_score"),
            "requires_external_search": bool(result.get("requires_external_search")),
            "intermediate_reply": result.get("intermediate_reply"),
            "suggested_reply":    result.get("suggested_reply"),
            "evidence":           result.get("evidence") or [],
            "system_behavior":    result.get("system_behavior") or [],
        },

        # ── 從 extracted_info 拉出的旅遊資訊 ─────────────────────
        "destination_city":   _first(extracted.get("location")),
        "travel_date_start":  _first(extracted.get("time")),
        "budget_min":         _first(extracted.get("budget")),
        "people_count":       _first(extracted.get("people_count")),
        "decision_state":     extracted.get("decision_state"),
        "need_type":          extracted.get("need_type"),
        "has_conflict":       bool(extracted.get("risk_info")),
        "extracted_info":     extracted,
    })


def _first(value):
    """從 list 取第一個值，非 list 直接回傳，None 回傳 None。"""
    if isinstance(value, list):
        return value[0] if value else None
    return value


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


def upsert_member(
    line_group_id: str,
    line_user_id: str,
    display_name: str = "",
) -> None:
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
