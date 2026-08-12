"""
MongoDB 連線模組。

使用 get_db() 取得資料庫實例，整個 process 共用同一個 MongoClient。
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

_client: MongoClient | None = None


class DbConflictError(RuntimeError):
    """
    違反唯一性限制、或狀態已經被搶先變更時丟出（例如同群組已有進行中的
    帳本／投票、發票已經確認過、投票已經結束）。呼叫端應該把這個當成
    「這個操作現在不能做」的訊號，而不是系統錯誤。
    """


def get_db() -> Database:
    global _client
    if _client is None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError("環境變數 MONGODB_URI 未設定")
        _client = MongoClient(uri)
    db_name = os.getenv("MONGODB_DB_NAME", "linebot")
    return _client[db_name]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_object_id(value: Any) -> ObjectId:
    """把 book_id / poll_id 這類可能是字串或 ObjectId 的參數統一轉成 ObjectId。"""
    return value if isinstance(value, ObjectId) else ObjectId(str(value))


def ensure_indexes() -> None:
    """建立常用查詢所需的索引，應用啟動時呼叫一次。"""
    db = get_db()
    db.messages.create_index([("line_group_id", ASCENDING), ("sent_at", DESCENDING)])
    db.messages.create_index([("line_user_id", ASCENDING)])
    db.messages.create_index([("conversation_key", ASCENDING), ("sent_at", DESCENDING)])
    db.groups.create_index([("line_group_id", ASCENDING)], unique=True)
    db.summaries.create_index([("line_group_id", ASCENDING), ("window_start", DESCENDING)])
    db.itineraries.create_index([("line_group_id", ASCENDING), ("created_at", DESCENDING)])

    # vote_sessions 舊版是普通的 (line_group_id, status) 複合索引；新規格要求
    # 「同一群組最多一個 active 投票」，得改成 partial unique index。索引鍵組合
    # 沒變，只是加了 unique + partialFilterExpression，MongoDB 會把這當成選項
    # 衝突而不是自動覆蓋，所以要先偵測舊索引、視情況砍掉再建新的。
    existing_vote_indexes = db.vote_sessions.index_information()
    old_vote_status_index = existing_vote_indexes.get("line_group_id_1_status_1")
    if old_vote_status_index is not None and not old_vote_status_index.get("unique"):
        db.vote_sessions.drop_index("line_group_id_1_status_1")
    db.vote_sessions.create_index(
        [("line_group_id", ASCENDING), ("status", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "active"},
        name="uniq_active_vote_per_group",
    )
    db.vote_sessions.create_index([("status", ASCENDING), ("deadline_at", ASCENDING)])
    db.vote_sessions.create_index(
        [
            ("line_group_id", ASCENDING),
            ("discussion_fingerprint", ASCENDING),
            ("created_at", ASCENDING),
        ]
    )

    db.votes.create_index([("poll_id", ASCENDING), ("voter_key", ASCENDING)], unique=True)
    db.votes.create_index([("poll_id", ASCENDING), ("option_id", ASCENDING)])

    db.expense_books.create_index(
        [("line_group_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "active"},
        name="uniq_active_book_per_group",
    )
    db.expense_books.create_index([("status", ASCENDING), ("end_at", ASCENDING)])
    db.expense_books.create_index([("line_group_id", ASCENDING), ("created_at", DESCENDING)])

    db.expenses.create_index([("book_id", ASCENDING), ("expense_no", ASCENDING)], unique=True)
    db.expenses.create_index([("book_id", ASCENDING), ("status", ASCENDING), ("consumed_at", ASCENDING)])
    db.expenses.create_index([("invoice_import_id", ASCENDING)])

    db.feature_drafts.create_index(
        [("line_group_id", ASCENDING), ("line_user_id", ASCENDING), ("draft_type", ASCENDING)],
        unique=True,
    )
    db.feature_drafts.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

    db.invoice_imports.create_index(
        [("book_id", ASCENDING), ("source_fingerprint", ASCENDING)], unique=True
    )

    db.feature_event_dedup.create_index([("event_id", ASCENDING)], unique=True)
    db.feature_event_dedup.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

    db.user_preferences.create_index(
        [("line_user_id", ASCENDING), ("line_group_id", ASCENDING)],
        unique=True,
    )
    # 上面那個複合索引以 line_user_id 為前綴，查「整個群組」的偏好時用不到；
    # 另外建一個以 line_group_id 為前綴的索引，給 get_group_preferences() 用。
    db.user_preferences.create_index([("line_group_id", ASCENDING)])
    # 舊版唯一索引只有 query_type + query_key，現在快取要以群組隔離，
    # 唯一鍵要多加 line_group_id。先把舊索引砍掉再建新的，避免舊索引繼續
    # 擋住「不同群組、同樣 query_key」這種現在應該要允許並存的資料。
    if "query_type_1_query_key_1" in db.api_query_cache.index_information():
        db.api_query_cache.drop_index("query_type_1_query_key_1")
    db.api_query_cache.create_index(
        [
            ("query_type", ASCENDING),
            ("line_group_id", ASCENDING),
            ("query_key", ASCENDING),
        ],
        unique=True,
    )
    # TTL 索引：expires_at 一過期，MongoDB 會自動清掉該筆快取。
    db.api_query_cache.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

    db.weather_daily_cache.create_index(
        [
            ("provider", ASCENDING),
            ("county_name", ASCENDING),
            ("source_date", ASCENDING),
            ("forecast_type", ASCENDING),
        ],
        unique=True,
    )
    # 選填欄位：只有呼叫端有帶 ttl_seconds 才會設 expires_at，沒設的文件不受影響。
    db.weather_daily_cache.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)


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
    line_group_id: str,
    query_key: str,
    result: Any,
    query_params: dict | None = None,
    ttl_seconds: int = 3600,
) -> None:
    """
    存/更新一筆外部 API 查詢結果快取
    （同 query_type + line_group_id + query_key upsert）。

    群組隔離收在這裡處理：唯一鍵是 query_type + line_group_id + query_key
    三者一起比對，同樣的 query_key 在不同群組會各自存成獨立的一筆，不會
    互相覆蓋、也不會互相讀到。呼叫端（例如 location_flow.py / weather_flow.py）
    不用再自己手動把 line_group_id 拼進 query_key 字串。

    - query_type：查詢類型，例如 "restaurant" / "weather" / "movie"
    - line_group_id：這次查詢所屬的 LINE 群組 ID
    - query_key：正規化後的查詢關鍵字或條件字串（呼叫端自行決定怎麼組，
      例如 "台中_火鍋"），只放查詢條件本身就好
    - query_params：原始查詢條件，方便除錯或之後重新查詢
    - result：API 回傳結果，原樣存放（dict / list 皆可）
    - ttl_seconds：快取有效秒數，預設 1 小時；到期後 get_api_query_cache
      會回傳 None，MongoDB TTL 索引也會自動清掉該筆文件
    """
    now = datetime.now(timezone.utc)
    get_db().api_query_cache.update_one(
        {
            "query_type": query_type,
            "line_group_id": line_group_id,
            "query_key": query_key,
        },
        {
            "$set": {
                "query_params": query_params or {},
                "result": result,
                "updated_at": now,
                "expires_at": now + timedelta(seconds=ttl_seconds),
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def get_api_query_cache(query_type: str, line_group_id: str, query_key: str) -> Any | None:
    """
    取得快取結果；不存在或已過期回傳 None。

    查詢條件是 query_type + line_group_id + query_key，同一個問題在不同
    群組是各自獨立的快取，群組 A 查過的結果不會被群組 B 讀到。
    """
    doc = get_db().api_query_cache.find_one(
        {
            "query_type": query_type,
            "line_group_id": line_group_id,
            "query_key": query_key,
        }
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


# ── 天氣每日同步快取 ──────────────────────────────────────────────

def save_weather_daily_cache(
    provider: str,
    county_name: str,
    source_date: Any,
    raw_data: Any,
    forecast_type: str = "36h",
    ttl_seconds: int | None = None,
) -> None:
    """
    存/更新一筆「每日同步」的天氣資料
    （同 provider + county_name + source_date + forecast_type upsert）。

    取代「使用者一問就即時打中央氣象署 API」的做法：改成排程每天同步一次，
    寫進這個 collection，之後使用者發問時後端直接讀這裡，不用再即時呼叫外部 API。

    - provider：資料來源，例如 "cwa_weather"
    - county_name：縣市名稱，例如 "臺北市"、"宜蘭縣"
    - source_date：這筆資料是同步哪一天的；建議統一用 "YYYY-MM-DD" 字串，
      這樣查詢時格式才會一致
    - raw_data：中央氣象署回傳的原始 JSON，這裡不解析，後端自己從裡面挑
      今天/明天的內容
    - forecast_type：預報類型，先固定 "36h"
    - ttl_seconds：選填。有給的話才會設定 expires_at，供之後想清舊資料時用；
      預設不設定 —— 每日同步資料通常想留著查歷史，不像即時查詢快取那樣短命
    """
    now = datetime.now(timezone.utc)
    update_fields: dict[str, Any] = {
        "provider": provider,
        "county_name": county_name,
        "source_date": source_date,
        "forecast_type": forecast_type,
        "raw_data": raw_data,
        "updated_at": now,
    }
    if ttl_seconds is not None:
        update_fields["expires_at"] = now + timedelta(seconds=ttl_seconds)

    get_db().weather_daily_cache.update_one(
        {
            "provider": provider,
            "county_name": county_name,
            "source_date": source_date,
            "forecast_type": forecast_type,
        },
        {"$set": update_fields},
        upsert=True,
    )


def get_weather_daily_cache(
    provider: str,
    county_name: str,
    source_date: Any,
    forecast_type: str = "36h",
) -> dict | None:
    """取得某天、某縣市已同步好的天氣資料；還沒同步過就回傳 None。"""
    return get_db().weather_daily_cache.find_one(
        {
            "provider": provider,
            "county_name": county_name,
            "source_date": source_date,
            "forecast_type": forecast_type,
        }
    )


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


# ══════════════════════════════════════════════════════════════════
# 行程記帳、發票、投票（DB 修改文件：記帳、投票等）
#
# 本區塊實作 DB 修改文件要求的所有 collection 與函式。呼叫端（非 DB 模組）
# 在功能程式裡會先檢查這些函式是否存在，所以「函式名稱、參數、回傳型態」
# 都嚴格照文件的「必須提供的 Python 介面」章節實作，不要改名或改參數。
#
# 個資 / 禁止保存的資料：DB 層本身不會對傳入的 note / payload 等自由文字
# 做內容掃描或去識別化——文件裡提到的 privacy_redaction.redact_structure()
# 是另一個模組的職責，呼叫端必須在傳進這裡的函式之前就處理好，這裡只負責
# 「照 schema 存放呼叫端已經處理乾淨的資料」。
# ══════════════════════════════════════════════════════════════════


# ── 記帳：帳本 ────────────────────────────────────────────────────

def create_expense_book(
    *,
    line_group_id: str,
    name: str,
    created_by: str,
    members: list[dict],
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timezone: str = "Asia/Taipei",
) -> dict:
    """
    建立一本新帳本。同一群組同時只能有一本 status="active" 的帳本，
    由 partial unique index（uniq_active_book_per_group）在 DB 層強制保證；
    違反時丟出 DbConflictError，而不是靜默覆蓋或建立第二本。
    """
    now = _utc_now()
    doc = {
        "line_group_id": line_group_id,
        "name": name,
        "members": members,
        "status": "active",
        "start_at": start_at,
        "end_at": end_at,
        "timezone": timezone,
        "closed_at": None,
        "report_sent_at": None,
        "next_expense_number": 1,
        "created_by": created_by,
        "renamed_by": None,
        "renamed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = get_db().expense_books.insert_one(doc)
    except DuplicateKeyError as exc:
        raise DbConflictError(f"群組 {line_group_id} 已經有進行中的帳本") from exc
    doc["_id"] = result.inserted_id
    return doc


def get_active_expense_book(line_group_id: str) -> dict | None:
    return get_db().expense_books.find_one({"line_group_id": line_group_id, "status": "active"})


def add_expense_book_member(*, book_id: Any, member: dict, updated_by: str) -> dict:
    """
    新增一位成員到帳本；依 type + (line_user_id 或 display_name) 去重，
    已存在就不會重複加入。
    """
    db = get_db()
    book_oid = _as_object_id(book_id)
    book = db.expense_books.find_one({"_id": book_oid})
    if book is None:
        raise ValueError(f"找不到帳本 {book_id}")

    is_duplicate = any(
        m.get("type") == member.get("type")
        and (
            (member.get("type") == "line" and m.get("line_user_id") == member.get("line_user_id"))
            or (member.get("type") != "line" and m.get("display_name") == member.get("display_name"))
        )
        for m in book.get("members", [])
    )

    now = _utc_now()
    if not is_duplicate:
        db.expense_books.update_one(
            {"_id": book_oid},
            {"$push": {"members": member}, "$set": {"updated_at": now}},
        )
    return db.expense_books.find_one({"_id": book_oid})


def rename_expense_book(*, book_id: Any, name: str, renamed_by: str) -> dict:
    """
    重新命名帳本。只允許帳本建立者、且帳本仍為 active 時才能改名；
    名稱去除前後空白後不可為空，最長 120 字元。可以重複修改，不限制次數；
    已結束的帳本要先用 reopen_expense_book() 重新開啟才能改名。

    注意：DB 修改文件寫「帳本建立者／指定 editor」都可以改，但 expense_books
    的 schema 沒有 editors 欄位，這裡先只認 created_by。如果之後要支援多個
    editor，schema 要先加一個 editors 陣列欄位。
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("帳本名稱不可為空")
    if len(clean_name) > 120:
        raise ValueError("帳本名稱最長 120 字元")

    now = _utc_now()
    updated = get_db().expense_books.find_one_and_update(
        {"_id": _as_object_id(book_id), "status": "active", "created_by": renamed_by},
        {
            "$set": {
                "name": clean_name,
                "renamed_by": renamed_by,
                "renamed_at": now,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise PermissionError("只有帳本建立者可以在進行中時修改名稱；已結束的帳本需先重新開啟")
    return updated


def update_expense_book_schedule(
    *, book_id: Any, start_at: datetime | None, end_at: datetime | None,
    timezone: str, updated_by: str,
) -> dict:
    """更新帳本起訖時間與時區。權限限制同 rename_expense_book（見上方註解）。"""
    now = _utc_now()
    updated = get_db().expense_books.find_one_and_update(
        {"_id": _as_object_id(book_id), "status": "active", "created_by": updated_by},
        {
            "$set": {
                "start_at": start_at,
                "end_at": end_at,
                "timezone": timezone,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise PermissionError("只有帳本建立者可以在進行中時修改起訖時間")
    return updated


def close_expense_book(*, book_id: Any, closed_by: str) -> dict:
    """提前關閉帳本。只有帳本建立者可以關閉進行中的帳本。"""
    now = _utc_now()
    updated = get_db().expense_books.find_one_and_update(
        {"_id": _as_object_id(book_id), "status": "active", "created_by": closed_by},
        {"$set": {"status": "closed", "closed_at": now, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise PermissionError("只有帳本建立者可以關閉進行中的帳本")
    return updated


def reopen_expense_book(*, line_group_id: str, requested_by: str) -> dict:
    """
    重新開啟該群組「最新一本已結束」的帳本。只有該帳本的建立者可以重開。
    因為同群組同時只能有一本 active 帳本，這裡會先擋掉「已經有 active 帳本」
    的情況，find_one_and_update 失敗時也會再擋一次 race（兩個請求同時搶重開）。
    """
    db = get_db()
    now = _utc_now()

    if db.expense_books.find_one({"line_group_id": line_group_id, "status": "active"}):
        raise DbConflictError(f"群組 {line_group_id} 已經有進行中的帳本，需先關閉才能重開別本")

    latest_closed = db.expense_books.find_one(
        {"line_group_id": line_group_id, "status": "closed"},
        sort=[("closed_at", DESCENDING)],
    )
    if latest_closed is None:
        raise ValueError(f"群組 {line_group_id} 沒有已結束的帳本可以重開")
    if latest_closed.get("created_by") != requested_by:
        raise PermissionError("只有帳本建立者可以重新開啟帳本")

    try:
        updated = db.expense_books.find_one_and_update(
            {"_id": latest_closed["_id"], "status": "closed"},
            {"$set": {"status": "active", "closed_at": None, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise DbConflictError(f"群組 {line_group_id} 已經有進行中的帳本") from exc
    if updated is None:
        raise DbConflictError("重新開啟失敗，可能已被其他請求搶先處理")
    return updated


def claim_due_expense_books(*, now: datetime, limit: int = 50) -> list[dict]:
    """
    原子地把到期（end_at <= now）且仍 active 的帳本轉為 closed。逐筆用
    find_one_and_update 認領，確保多個 worker 同時跑排程時不會重複推播。
    """
    db = get_db()
    claimed: list[dict] = []
    for _ in range(limit):
        doc = db.expense_books.find_one_and_update(
            {"status": "active", "end_at": {"$ne": None, "$lte": now}},
            {"$set": {"status": "closed", "closed_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            break
        claimed.append(doc)
    return claimed


def mark_expense_report_sent(*, book_id: Any, sent_at: datetime) -> None:
    get_db().expense_books.update_one(
        {"_id": _as_object_id(book_id)},
        {"$set": {"report_sent_at": sent_at, "updated_at": sent_at}},
    )


# ── 記帳：多步驟草稿 ──────────────────────────────────────────────

_FEATURE_DRAFT_TTL = timedelta(minutes=30)


def save_feature_draft(
    *, line_group_id: str, line_user_id: str, draft_type: str, payload: dict,
) -> dict:
    """儲存/更新草稿，TTL 30 分鐘；每次呼叫都會用 upsert 刷新 expires_at。"""
    now = _utc_now()
    db = get_db()
    key = {"line_group_id": line_group_id, "line_user_id": line_user_id, "draft_type": draft_type}
    db.feature_drafts.update_one(
        key,
        {
            "$set": {"payload": payload, "updated_at": now, "expires_at": now + _FEATURE_DRAFT_TTL},
            "$setOnInsert": {**key, "created_at": now},
        },
        upsert=True,
    )
    return db.feature_drafts.find_one(key)


def get_feature_draft(
    *, line_group_id: str, line_user_id: str, draft_type: str,
) -> dict | None:
    return get_db().feature_drafts.find_one(
        {"line_group_id": line_group_id, "line_user_id": line_user_id, "draft_type": draft_type}
    )


def delete_feature_draft(
    *, line_group_id: str, line_user_id: str, draft_type: str,
) -> None:
    get_db().feature_drafts.delete_one(
        {"line_group_id": line_group_id, "line_user_id": line_user_id, "draft_type": draft_type}
    )


# ── 記帳：支出 ────────────────────────────────────────────────────

def _claim_expense_numbers(db: Database, book_oid: ObjectId, count: int) -> list[str]:
    """
    原子地從 expense_books.next_expense_number 取出連續 count 個編號
    （EXP-001 這種格式）。用 $inc 保證多個並發請求不會拿到重疊的編號；
    取消支出不會歸還編號，所以編號永遠不會重用。
    """
    updated = db.expense_books.find_one_and_update(
        {"_id": book_oid, "status": "active"},
        {"$inc": {"next_expense_number": count}},
        return_document=ReturnDocument.BEFORE,
    )
    if updated is None:
        raise ValueError(f"帳本 {book_oid} 不存在或不是進行中狀態，無法配置支出編號")
    start = updated["next_expense_number"]
    return [f"EXP-{n:03d}" for n in range(start, start + count)]


def _merge_manual_members(db: Database, book_oid: ObjectId, participants: list[dict], now: datetime) -> None:
    """分攤名單裡如果有帳本還沒收錄的人，依顯示名稱去重後加入帳本成員清單。"""
    if not participants:
        return
    book = db.expense_books.find_one({"_id": book_oid}, {"members": 1})
    if book is None:
        return

    def _key(m: dict) -> tuple:
        return (m.get("type"), m.get("line_user_id") if m.get("type") == "line" else m.get("display_name"))

    existing_keys = {_key(m) for m in book.get("members", [])}
    new_members = []
    for participant in participants:
        key = _key(participant)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        new_members.append({
            "type": participant.get("type", "manual"),
            "line_user_id": participant.get("line_user_id"),
            "display_name": participant.get("display_name", ""),
        })

    if new_members:
        db.expense_books.update_one(
            {"_id": book_oid},
            {"$push": {"members": {"$each": new_members}}, "$set": {"updated_at": now}},
        )


def create_expense(*, book_id: Any, expense: dict, created_by: str) -> dict:
    """
    新增一筆手動支出。payer 是必填欄位；participants 可以是空陣列，代表
    使用者明確選擇「不分攤」，這裡不會因為陣列是空的就拒絕建立——是否
    「尚未選擇分攤對象」由呼叫端透過草稿的 missing 欄位判斷，不是這裡的責任。
    """
    if not expense.get("payer"):
        raise ValueError("payer 為必填欄位")

    db = get_db()
    book_oid = _as_object_id(book_id)
    now = _utc_now()

    book = db.expense_books.find_one({"_id": book_oid})
    if book is None or book.get("status") != "active":
        raise ValueError(f"帳本 {book_id} 不存在或不是進行中狀態")

    expense_no = _claim_expense_numbers(db, book_oid, 1)[0]
    participants = expense.get("participants", [])

    doc = {
        "book_id": book_oid,
        "expense_no": expense_no,
        "item": expense["item"],
        "amount": int(expense["amount"]),
        "currency": expense.get("currency", "TWD"),
        "participants": participants,
        "consumed_at": expense.get("consumed_at"),
        "merchant": expense.get("merchant", ""),
        "category": expense.get("category", ""),
        "payer": expense["payer"],
        "created_by": created_by,
        "source": expense.get("source", "manual"),
        "note": expense.get("note", ""),
        "status": "confirmed",
        "invoice_import_id": expense.get("invoice_import_id"),
        "created_at": now,
        "updated_at": now,
        "updated_by": created_by,
    }

    _merge_manual_members(db, book_oid, participants, now)

    result = db.expenses.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def create_expenses_from_invoice(
    *, book_id: Any, invoice_import_id: Any, payload: list[dict], created_by: str,
) -> list[dict]:
    """
    在單一 MongoDB transaction 中完成：
    1. 確認帳本 active
    2. 確認 invoice import 尚未 confirmed
    3. 一次配置連續編號
    4. 建立所有 expenses
    5. 更新 invoice import 的 expense_ids / status / confirmed_at

    任何一步失敗都會整筆 rollback，不會只建立部分商品。

    payload 是呼叫端已經展開好的明細（含服務費、折扣等調整項），每筆至少要有
    item / amount / payer。**加總是否等於發票總額的一致性檢查，要由呼叫端在
    組出 payload 之前就先做好**——schema 裡沒有獨立的「發票總額」欄位可以在
    DB 層比對，所以這裡沒辦法再次驗證這件事。
    """
    if not payload:
        raise ValueError("payload 不可為空，至少要有一筆支出明細")

    db = get_db()
    book_oid = _as_object_id(book_id)
    invoice_oid = _as_object_id(invoice_import_id)
    now = _utc_now()
    count = len(payload)

    created: list[dict] = []

    def _run(session) -> None:
        book = db.expense_books.find_one({"_id": book_oid}, session=session)
        if book is None or book.get("status") != "active":
            raise ValueError(f"帳本 {book_id} 不存在或不是進行中狀態")

        invoice_import = db.invoice_imports.find_one({"_id": invoice_oid}, session=session)
        if invoice_import is None:
            raise ValueError(f"找不到發票匯入紀錄 {invoice_import_id}")
        if invoice_import.get("status") == "confirmed":
            raise DbConflictError(f"發票匯入紀錄 {invoice_import_id} 已經確認過，不能重複展開")

        updated_book = db.expense_books.find_one_and_update(
            {"_id": book_oid, "status": "active"},
            {"$inc": {"next_expense_number": count}},
            return_document=ReturnDocument.BEFORE,
            session=session,
        )
        if updated_book is None:
            raise ValueError(f"帳本 {book_id} 不是進行中狀態，無法配置支出編號")
        start_number = updated_book["next_expense_number"]

        expense_docs = []
        for offset, item in enumerate(payload):
            if not item.get("payer"):
                raise ValueError(f"明細第 {offset + 1} 筆缺少 payer")
            expense_docs.append({
                "book_id": book_oid,
                "expense_no": f"EXP-{start_number + offset:03d}",
                "item": item["item"],
                "amount": int(item["amount"]),
                "currency": item.get("currency", "TWD"),
                "participants": item.get("participants", []),
                "consumed_at": item.get("consumed_at"),
                "merchant": item.get("merchant", ""),
                "category": item.get("category", ""),
                "payer": item["payer"],
                "created_by": created_by,
                "source": item.get("source") if item.get("source") in ("invoice_qr", "invoice_ocr") else "invoice_qr",
                "note": item.get("note", ""),
                "status": "confirmed",
                "invoice_import_id": invoice_oid,
                "created_at": now,
                "updated_at": now,
                "updated_by": created_by,
            })

        db.expenses.insert_many(expense_docs, session=session)  # 會就地補上每筆的 _id

        db.invoice_imports.update_one(
            {"_id": invoice_oid},
            {
                "$set": {
                    "status": "confirmed",
                    "confirmed_at": now,
                    "expense_ids": [doc["_id"] for doc in expense_docs],
                }
            },
            session=session,
        )

        created.extend(expense_docs)

    with db.client.start_session() as session:
        session.with_transaction(_run)

    return created


def list_expenses(book_id: Any, *, status: str | None = "confirmed") -> list[dict]:
    """取得帳本內的支出清單，依建立時間由舊到新排列。status=None 代表不篩選狀態。"""
    query: dict[str, Any] = {"book_id": _as_object_id(book_id)}
    if status is not None:
        query["status"] = status
    return list(get_db().expenses.find(query).sort("created_at", ASCENDING))


def get_latest_expense(book_id: Any) -> dict | None:
    """
    取得帳本最新一筆已確認的支出。刻意不排除 participants 為空陣列的紀錄，
    讓「同上一筆」可以正確沿用「不分攤」這個選擇。
    """
    return get_db().expenses.find_one(
        {"book_id": _as_object_id(book_id), "status": "confirmed"},
        sort=[("created_at", DESCENDING)],
    )


def update_expense(*, book_id: Any, expense_no: str, changes: dict, updated_by: str) -> dict:
    changes = dict(changes)
    changes.pop("book_id", None)
    changes.pop("expense_no", None)
    changes.pop("_id", None)
    changes["updated_at"] = _utc_now()
    changes["updated_by"] = updated_by

    updated = get_db().expenses.find_one_and_update(
        {"book_id": _as_object_id(book_id), "expense_no": expense_no},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ValueError(f"找不到支出 {expense_no}（帳本 {book_id}）")
    return updated


def cancel_expense(*, book_id: Any, expense_no: str, cancelled_by: str) -> dict:
    now = _utc_now()
    updated = get_db().expenses.find_one_and_update(
        {"book_id": _as_object_id(book_id), "expense_no": expense_no, "status": "confirmed"},
        {"$set": {"status": "cancelled", "updated_at": now, "updated_by": cancelled_by}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ValueError(f"找不到可取消的支出 {expense_no}（帳本 {book_id}）")
    return updated


# ── 發票匯入 ──────────────────────────────────────────────────────

def is_duplicate_invoice_import(*, book_id: Any, source_fingerprint: str) -> bool:
    return get_db().invoice_imports.find_one(
        {"book_id": _as_object_id(book_id), "source_fingerprint": source_fingerprint}
    ) is not None


def create_invoice_import(*, book_id: Any, source_fingerprint: str, created_by: str) -> dict:
    """
    建立一筆發票匯入紀錄（draft 狀態）。source_fingerprint 必須是呼叫端算好
    的不可逆指紋（SHA-256／HMAC），這裡不接受、也不會存明文發票號碼。

    注意：schema 有 expires_at 欄位，但 DB 修改文件沒有定義它的到期規則或
    TTL 索引，這裡先存 None。如果之後要讓太久沒確認的 draft 自動失效，要先
    決定到期時長，再補一個 TTL 索引。
    """
    now = _utc_now()
    doc = {
        "book_id": _as_object_id(book_id),
        "source_fingerprint": source_fingerprint,
        "status": "draft",
        "expense_ids": [],
        "created_by": created_by,
        "created_at": now,
        "confirmed_at": None,
        "expires_at": None,
    }
    try:
        result = get_db().invoice_imports.insert_one(doc)
    except DuplicateKeyError as exc:
        raise DbConflictError(
            f"帳本 {book_id} 已經匯入過這張發票（source_fingerprint 重複）"
        ) from exc
    doc["_id"] = result.inserted_id
    return doc


# ── 投票（匿名） ──────────────────────────────────────────────────

def get_active_vote_session(*, line_group_id: str) -> dict | None:
    return get_db().vote_sessions.find_one({"line_group_id": line_group_id, "status": "active"})


def create_vote_session(
    *,
    line_group_id: str,
    question: str,
    options: list[dict],
    deadline_at: datetime,
    created_by_key: str,
    anonymity_salt: str,
    eligible_voter_keys: list[str],
    close_when_all_eligible: bool,
    auto_created: bool,
    discussion_fingerprint: str,
) -> dict:
    """
    建立一場投票。同一群組同時只能有一場 active 投票，由 partial unique
    index（uniq_active_vote_per_group）保證，違反時丟出 DbConflictError。

    question / options 的文字必須是呼叫端已經去識別化過的內容——DB 層不會
    對文字內容做二次檢查。created_by_key / eligible_voter_keys 必須是呼叫端
    用 HMAC 算好的 key，不可以是原始 LINE user ID；DB 層原樣存放，不做轉換。
    """
    if not (2 <= len(options) <= 6):
        raise ValueError("投票選項數必須介於 2 到 6 個之間")

    now = _utc_now()
    normalized_options = [
        {"option_id": opt.get("option_id", index + 1), "label": opt["label"]}
        for index, opt in enumerate(options)
    ]

    doc = {
        "line_group_id": line_group_id,
        "question": question,
        "options": normalized_options,
        "status": "active",
        "deadline_at": deadline_at,
        "created_by_key": created_by_key,
        "anonymity_salt": anonymity_salt,
        "eligible_voter_keys": eligible_voter_keys,
        "close_when_all_eligible": close_when_all_eligible,
        "auto_created": auto_created,
        "discussion_fingerprint": discussion_fingerprint,
        "created_at": now,
        "closed_at": None,
        "closed_reason": None,
        "result_announced_at": None,
    }
    try:
        result = get_db().vote_sessions.insert_one(doc)
    except DuplicateKeyError as exc:
        raise DbConflictError(f"群組 {line_group_id} 已經有進行中的投票") from exc
    doc["_id"] = result.inserted_id
    return doc


def get_vote_session(*, poll_id: Any, line_group_id: str) -> dict | None:
    """依 poll_id 取投票，同時強制核對 line_group_id，避免跨群組讀到別人的投票。"""
    return get_db().vote_sessions.find_one(
        {"_id": _as_object_id(poll_id), "line_group_id": line_group_id}
    )


def cast_anonymous_vote(*, poll_id: Any, voter_key: str, option_id: int, now: datetime) -> dict:
    """
    在同一個 transaction 裡完成：
    1. 再次確認 poll 仍是 active、還沒過 deadline、option_id 有效
    2. 用 (poll_id, voter_key) upsert 一筆投票；同一人改票不會增加總票數
    3. 若 close_when_all_eligible=True 且 eligible_voter_keys 全部已投票，
       把投票關閉並設定 closed_reason="all_eligible_voted"

    回傳 {"vote": vote_dict, "poll": poll_dict, "closed_now": bool}。
    投票期間 app 不會呼叫 get_vote_results，這裡也不對外提供即時票數。
    """
    db = get_db()
    poll_oid = _as_object_id(poll_id)
    outcome: dict[str, Any] = {}

    def _run(session) -> None:
        poll = db.vote_sessions.find_one({"_id": poll_oid}, session=session)
        if poll is None:
            raise ValueError(f"找不到投票 {poll_id}")
        if poll.get("status") != "active":
            raise DbConflictError("投票已經結束，無法投票")
        if poll.get("deadline_at") is not None and now >= poll["deadline_at"]:
            raise DbConflictError("投票已經超過截止時間")

        valid_option_ids = {opt["option_id"] for opt in poll.get("options", [])}
        if option_id not in valid_option_ids:
            raise ValueError(f"option_id {option_id} 不是這次投票的有效選項")

        vote = db.votes.find_one_and_update(
            {"poll_id": poll_oid, "voter_key": voter_key},
            {
                "$set": {"option_id": option_id, "updated_at": now},
                "$setOnInsert": {"poll_id": poll_oid, "voter_key": voter_key, "created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )

        updated_poll = poll
        closed_now = False
        eligible = set(poll.get("eligible_voter_keys") or [])
        if poll.get("close_when_all_eligible") and eligible:
            voted_keys = {
                v["voter_key"]
                for v in db.votes.find(
                    {"poll_id": poll_oid, "voter_key": {"$in": list(eligible)}},
                    {"voter_key": 1},
                    session=session,
                )
            }
            if eligible.issubset(voted_keys):
                closed_poll = db.vote_sessions.find_one_and_update(
                    {"_id": poll_oid, "status": "active"},
                    {"$set": {"status": "closed", "closed_at": now, "closed_reason": "all_eligible_voted"}},
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if closed_poll is not None:
                    updated_poll = closed_poll
                    closed_now = True

        outcome["vote"] = vote
        outcome["poll"] = updated_poll
        outcome["closed_now"] = closed_now

    with db.client.start_session() as session:
        session.with_transaction(_run)

    return outcome


def get_vote_results(*, poll_id: Any) -> list[dict]:
    """回傳各選項的票數統計：[{"option_id": ..., "count": ...}, ...]。"""
    pipeline = [
        {"$match": {"poll_id": _as_object_id(poll_id)}},
        {"$group": {"_id": "$option_id", "count": {"$sum": 1}}},
        {"$project": {"_id": 0, "option_id": "$_id", "count": 1}},
    ]
    return list(get_db().votes.aggregate(pipeline))


def claim_due_vote_sessions(*, now: datetime, limit: int = 50) -> list[dict]:
    """
    原子地把到期（deadline_at <= now）且仍 active 的投票關閉，設定
    closed_reason="deadline"。只認領尚未公布結果的投票，逐筆用
    find_one_and_update 認領，避免多個 worker 重複推播。
    """
    db = get_db()
    claimed: list[dict] = []
    for _ in range(limit):
        doc = db.vote_sessions.find_one_and_update(
            {
                "status": "active",
                "deadline_at": {"$ne": None, "$lte": now},
                "result_announced_at": None,
            },
            {"$set": {"status": "closed", "closed_at": now, "closed_reason": "deadline"}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            break
        claimed.append(doc)
    return claimed


def mark_vote_result_announced(*, poll_id: Any, announced_at: datetime) -> None:
    get_db().vote_sessions.update_one(
        {"_id": _as_object_id(poll_id)},
        {"$set": {"result_announced_at": announced_at}},
    )


# ── Webhook 冪等 ──────────────────────────────────────────────────

def claim_feature_event(*, event_id: str, feature: str, ttl_seconds: int = 604800) -> bool:
    """
    嘗試「認領」這個 webhook event；同一個 event_id 已經被認領過時回傳 False
    （代表是 LINE 重送，呼叫端應該直接略過，不要重複入帳/確認/關閉）。
    """
    now = _utc_now()
    try:
        get_db().feature_event_dedup.insert_one({
            "event_id": event_id,
            "feature": feature,
            "claimed_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        })
        return True
    except DuplicateKeyError:
        return False


def release_feature_event(*, event_id: str) -> None:
    """整體處理失敗、準備讓 LINE 重試時呼叫，釋放掉這個 event 的認領記錄。"""
    get_db().feature_event_dedup.delete_one({"event_id": event_id})
