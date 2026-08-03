"""
MongoDB 連線模組。

使用 get_db() 取得資料庫實例，整個 process 共用同一個 MongoClient。
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

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
    db.messages.create_index([("conversation_key", ASCENDING), ("sent_at", DESCENDING)])
    db.groups.create_index([("line_group_id", ASCENDING)], unique=True)
    db.summaries.create_index([("line_group_id", ASCENDING), ("window_start", DESCENDING)])
    db.itineraries.create_index([("line_group_id", ASCENDING), ("created_at", DESCENDING)])
    db.vote_sessions.create_index([("line_group_id", ASCENDING), ("status", ASCENDING)])
    db.user_preferences.create_index(
        [("line_user_id", ASCENDING), ("line_group_id", ASCENDING)],
        unique=True,
    )
    # 上面那個複合索引以 line_user_id 為前綴，查「整個群組」的偏好時用不到；
    # 另外建一個以 line_group_id 為前綴的索引，給 get_group_preferences() 用。
    db.user_preferences.create_index([("line_group_id", ASCENDING)])
    db.api_query_cache.create_index(
        [("query_type", ASCENDING), ("query_key", ASCENDING)],
        unique=True,
    )
    # TTL 索引：expires_at 一過期，MongoDB 會自動清掉該筆快取。
    db.api_query_cache.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)


# ── 訊息 ─────────────────────────────────────────────────────────

def save_message(
    line_group_id: str,
    line_user_id: str,
    message_text: str,
    display_name: str = "",
    conversation_key: str = "",
    message_role: str = "user",
    embedding: list[float] | None = None,
    topic_hint: str | None = None,
) -> Any:
    """
    存一筆訊息，回傳新增文件的 _id（方便呼叫端接著傳給
    get_similar_messages() 的 exclude_message_id，排除自己）。

    RAG 相關欄位（embedding / topic_hint / conversation_key / message_role）
    皆為可選：embedding 由 AI 後端算好後傳入即可，這裡只負責存放與之後的檢索。
    - conversation_key 沒給時預設沿用 line_group_id，之後如果同一群組要拆多條對話
      脈絡（例如切換話題、不同 room），由呼叫端傳入自訂的 key。
    - message_role 預設 "user"；儲存 bot 回覆時傳入 "bot"。
    """
    result = get_db().messages.insert_one({
        "line_group_id": line_group_id,
        "line_user_id": line_user_id,
        "display_name": display_name,
        "message_text": message_text,
        "sent_at": datetime.now(timezone.utc),
        "conversation_key": conversation_key or line_group_id,
        "message_role": message_role,
        "embedding": embedding,
        "topic_hint": topic_hint,
    })
    return result.inserted_id


def get_recent_messages(line_group_id: str, limit: int = 15) -> list[str]:
    """取得最近 N 筆訊息文字，由舊到新排列（給 AI 當上下文用）。"""
    docs = (
        get_db().messages
        .find({"line_group_id": line_group_id}, {"message_text": 1})
        .sort("sent_at", DESCENDING)
        .limit(limit)
    )
    return [d["message_text"] for d in reversed(list(docs))]


def _cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


def get_similar_messages(
    line_group_id: str,
    query_embedding: list[float],
    exclude_message_id: Any = None,
    limit: int = 5,
    min_score: float = 0.0,
) -> list[dict]:
    """
    在同一個 line_group_id 底下，依語意相似度找出最相關的歷史訊息，
    給後端組 RAG 用的 context_text 用。

    - 限定同一個 line_group_id
    - exclude_message_id 可傳入目前這一筆訊息的 _id，將它排除在候選之外
    - 只在已有 embedding 的訊息中比對，回傳相似度最高的前 limit 筆
      （由相似度高到低排列，每筆會多帶一個 similarity_score 欄位）

    目前用 Python 端 brute-force 算 cosine similarity，沒有依賴 Atlas
    Vector Search index，先求可用；等單一群組的訊息量變大、或 Atlas
    cluster 有開 Vector Search，再換成 $vectorSearch aggregation 即可，
    介面（輸入輸出）不需要變。
    """
    if not query_embedding:
        return []

    query: dict[str, Any] = {
        "line_group_id": line_group_id,
        "embedding": {"$ne": None},
    }
    if exclude_message_id is not None:
        query["_id"] = {"$ne": exclude_message_id}

    candidates = get_db().messages.find(
        query,
        {
            "line_group_id": 1,
            "line_user_id": 1,
            "display_name": 1,
            "message_text": 1,
            "message_role": 1,
            "topic_hint": 1,
            "conversation_key": 1,
            "embedding": 1,
            "sent_at": 1,
        },
    )

    scored: list[tuple[float, dict]] = []
    for doc in candidates:
        score = _cosine_similarity(query_embedding, doc.get("embedding") or [])
        if score >= min_score:
            scored.append((score, doc))

    scored.sort(key=lambda item: item[0], reverse=True)

    results = []
    for score, doc in scored[:limit]:
        doc["similarity_score"] = score
        results.append(doc)
    return results


# ── 外部 API 查詢結果暫存 ──────────────────────────────────────────

def save_api_query_cache(
    query_type: str,
    query_key: str,
    result: Any,
    query_params: dict | None = None,
    ttl_seconds: int = 3600,
    line_group_id: str | None = None,
) -> None:
    """
    存/更新一筆外部 API 查詢結果快取（同 query_type + query_key upsert）。

    - query_type：查詢類型，例如 "restaurant" / "weather" / "movie"
    - query_key：正規化後的查詢關鍵字或條件字串（呼叫端自行決定怎麼組，
      例如 "台中_火鍋" 或條件序列化後的字串），用來當查找快取的 key
    - query_params：原始查詢條件，方便除錯或之後重新查詢
    - result：API 回傳結果，原樣存放（dict / list 皆可）
    - ttl_seconds：快取有效秒數，預設 1 小時；到期後 get_api_query_cache
      會回傳 None，MongoDB TTL 索引也會自動清掉該筆文件
    - line_group_id：選填，記錄是哪個群組觸發了這次查詢，方便之後追蹤/除錯。
      注意這裡「不會」拿它當快取鍵：天氣、餐廳、電影場次這類外部資料本身是
      公開資訊，不同群組查同一個條件應該要能共用同一份結果，不需要重複打
      外部 API。如果某個查詢類型的結果本來就跟群組的私有資訊綁在一起、
      不能跨群組共用，處理方式是呼叫端自己把 line_group_id 編進 query_key
      裡（例如 f"{line_group_id}:{query_key}"），這樣同一個 query_type 底下
      不同群組自然就會落在不同的快取鍵，不用改這支函式。
    """
    now = datetime.now(timezone.utc)
    get_db().api_query_cache.update_one(
        {"query_type": query_type, "query_key": query_key},
        {
            "$set": {
                "query_params": query_params or {},
                "result": result,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
                "line_group_id": line_group_id,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def get_api_query_cache(query_type: str, query_key: str) -> Any | None:
    """取得快取結果；不存在或已過期回傳 None。"""
    doc = get_db().api_query_cache.find_one(
        {"query_type": query_type, "query_key": query_key}
    )
    if doc is None:
        return None

    expires_at = doc.get("expires_at")
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            return None

    return doc.get("result")


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


def get_latest_summary(line_group_id: str) -> dict | None:
    """
    取得「這個群組」最新一筆分析摘要（budget / need_type / decision_state
    等已確認條件），給 AI 組 context 時參考用。

    查詢條件只有 line_group_id，絕對不會讀到其他群組已確認的條件。
    """
    return get_db().summaries.find_one(
        {"line_group_id": line_group_id},
        sort=[("window_start", DESCENDING)],
    )


# ── 使用者 / 群組偏好 ────────────────────────────────────────────────

def upsert_user_preference(
    line_group_id: str,
    line_user_id: str,
    preference_type: str,
    preference_value: str,
) -> None:
    """
    新增或更新某位使用者在「這個群組」裡的偏好（例如飲食限制、預算）。

    務必同時帶 line_group_id + line_user_id 兩個條件去更新，確保不會誤更新到
    這位使用者在其他群組裡的偏好。同一個 preference_type 已存在時直接覆蓋
    value，不會產生重複項目。
    """
    now = datetime.now(timezone.utc)
    db = get_db()

    updated = db.user_preferences.update_one(
        {
            "line_group_id": line_group_id,
            "line_user_id": line_user_id,
            "preferences.type": preference_type,
        },
        {
            "$set": {
                "preferences.$.value": preference_value,
                "preferences.$.updated_at": now,
            }
        },
    )
    if updated.matched_count == 0:
        db.user_preferences.update_one(
            {"line_group_id": line_group_id, "line_user_id": line_user_id},
            {
                "$push": {
                    "preferences": {
                        "type": preference_type,
                        "value": preference_value,
                        "updated_at": now,
                    }
                },
                "$setOnInsert": {
                    "line_group_id": line_group_id,
                    "line_user_id": line_user_id,
                },
            },
            upsert=True,
        )


def get_user_preferences(line_group_id: str, line_user_id: str) -> list[dict]:
    """取得單一使用者在「這個群組」裡的偏好清單。嚴格限定 line_group_id + line_user_id。"""
    doc = get_db().user_preferences.find_one(
        {"line_group_id": line_group_id, "line_user_id": line_user_id}
    )
    return doc.get("preferences", []) if doc else []


def get_group_preferences(line_group_id: str) -> list[dict]:
    """
    取得「這個群組」所有成員合併後的偏好清單，給 AI 組 context_text 用。

    查詢條件只有 line_group_id，絕對不會撈到其他群組的偏好資料。
    """
    docs = get_db().user_preferences.find({"line_group_id": line_group_id})
    preferences: list[dict] = []
    for doc in docs:
        for pref in doc.get("preferences", []):
            preferences.append({
                "line_user_id": doc.get("line_user_id"),
                "type": pref.get("type"),
                "value": pref.get("value"),
                "updated_at": pref.get("updated_at"),
            })
    return preferences


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
