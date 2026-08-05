from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory
import requests as http_requests

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
    URIAction,
)
from linebot.v3.webhooks import (
    BeaconEvent,
    LocationMessageContent,
    MessageEvent,
    TextMessageContent,
)

from ai_linebot_core.app.engine import analyze_dialogue
from ai_linebot_core.app.line_import import (
    LineImportError,
    build_itinerary_context,
    build_itinerary_followup_reply,
    build_itinerary_import_reply,
    build_spot_import_reply,
    create_signed_line_import_token,
    create_focus_spot_from_import,
    create_placeholder_itinerary_from_spot,
    extract_line_import_command,
    find_itinerary_spot,
    normalize_itinerary_payload,
    normalize_spot_payload,
)
from db import (
    ensure_indexes,
    get_similar_messages,
    save_message,
    save_summary,
    upsert_group,
    upsert_member,
)
from location_flow import (
    build_liff_url,
    claim_recommendation_session,
    clear_recent_location_context,
    create_recommendation_session,
    finalize_session_result,
    get_recent_beacon_context,
    get_recent_location_context,
    get_recommendation_session,
    mark_session_failed,
    register_beacon_event,
    run_location_recommendation,
    run_text_location_recommendation,
    save_recent_location_context,
)
from weather_flow import run_weather_recommendation
from route_optimization import build_optimized_route_reply, should_optimize_route

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = max(
    1024,
    int(os.getenv("MAX_REQUEST_BODY_BYTES", str(1024 * 1024))),
)
ENABLE_VERBOSE_DEBUG = os.getenv("ENABLE_VERBOSE_DEBUG", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _debug_print(message: str) -> None:
    if ENABLE_VERBOSE_DEBUG:
        print(message, flush=True)

try:
    ensure_indexes()
    print("MongoDB 索引建立完成")
except Exception:
    print("MongoDB 連線失敗，繼續啟動（無持久化）。")

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

# 這幾個參數控制 Bot 的對話視窗、最低介入信心，以及重複回覆抑制。
EXTERNAL_SEARCH_DELAY_SECONDS = float(os.getenv("EXTERNAL_SEARCH_DELAY_SECONDS", "0"))
CONVERSATION_WINDOW_SIZE = max(1, int(os.getenv("CONVERSATION_WINDOW_SIZE", "10")))
MIN_INTERVENTION_CONFIDENCE = float(os.getenv("MIN_INTERVENTION_CONFIDENCE", "0.8"))
MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY = max(
    1,
    int(os.getenv("MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY", "4")),
)
RAG_RETRIEVAL_LIMIT = max(1, int(os.getenv("RAG_RETRIEVAL_LIMIT", "3")))
RAG_MIN_SIMILARITY_SCORE = float(os.getenv("RAG_MIN_SIMILARITY_SCORE", "0.78"))
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_TOPIC_JUDGE_MODEL = os.getenv("OPENAI_TOPIC_JUDGE_MODEL", "gpt-4.1-mini")
OPENAI_LOCATION_JUDGE_MODEL = os.getenv(
    "OPENAI_LOCATION_JUDGE_MODEL",
    OPENAI_TOPIC_JUDGE_MODEL,
)
TOPIC_SWITCH_SIMILARITY_THRESHOLD = float(
    os.getenv("TOPIC_SWITCH_SIMILARITY_THRESHOLD", "0.72")
)
SEMANTIC_DUPLICATE_SIMILARITY_THRESHOLD = float(
    os.getenv("SEMANTIC_DUPLICATE_SIMILARITY_THRESHOLD", "0.88")
)
TOPIC_JUDGE_HISTORY_LIMIT = max(1, int(os.getenv("TOPIC_JUDGE_HISTORY_LIMIT", "6")))
DEFAULT_FINAL_REPLY = "我先整理一個方向給大家參考。"
LIFF_LOCATION_DIR = os.path.join(app.root_path, "liff_app")
LIFF_MAX_JSON_BYTES = max(1024, int(os.getenv("LIFF_MAX_JSON_BYTES", "8192")))
LIFF_MAX_ACCURACY_METERS = max(
    0.0,
    float(os.getenv("LIFF_MAX_ACCURACY_METERS", "100000")),
)
LIFF_AUTH_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("LIFF_AUTH_TIMEOUT_SECONDS", "5")),
)
LIFF_RATE_LIMIT_WINDOW_SECONDS = max(
    1,
    int(os.getenv("LIFF_RATE_LIMIT_WINDOW_SECONDS", "60")),
)
LIFF_RATE_LIMIT_PER_IP = max(1, int(os.getenv("LIFF_RATE_LIMIT_PER_IP", "20")))
LIFF_RATE_LIMIT_PER_USER = max(1, int(os.getenv("LIFF_RATE_LIMIT_PER_USER", "5")))
LINE_IMPORT_MAX_JSON_BYTES = max(
    512,
    int(os.getenv("LINE_IMPORT_MAX_JSON_BYTES", "2048")),
)
LINE_IMPORT_RATE_LIMIT_PER_IP = max(
    1,
    int(os.getenv("LINE_IMPORT_RATE_LIMIT_PER_IP", "30")),
)
WEBHOOK_DEDUP_TTL_SECONDS = max(
    60,
    int(os.getenv("WEBHOOK_DEDUP_TTL_SECONDS", str(24 * 60 * 60))),
)
CONVERSATION_STATE_TTL_SECONDS = max(
    60,
    int(os.getenv("CONVERSATION_STATE_TTL_SECONDS", str(6 * 60 * 60))),
)
LIFF_ALLOWED_MAP_HOSTS = tuple(
    host.strip().casefold()
    for host in os.getenv(
        "LIFF_ALLOWED_MAP_HOSTS",
        "google.com,maps.google.com,maps.app.goo.gl",
    ).split(",")
    if host.strip()
)


def _log_failure(operation: str, exc: BaseException) -> None:
    """Log an operation failure without leaking request data or exception text."""
    app.logger.error("%s failed (%s)", operation, type(exc).__name__)


def _expected_liff_channel_id() -> str:
    configured_channel_id = os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip()
    if configured_channel_id:
        return configured_channel_id

    liff_id_prefix = os.getenv("LIFF_ID", "").strip().split("-", 1)[0]
    return liff_id_prefix if liff_id_prefix.isdigit() else ""


def _extract_bearer_token() -> str:
    authorization = request.headers.get("Authorization", "").strip()
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.casefold() == "bearer":
        normalized_token = token.strip()
        if 1 <= len(normalized_token) <= 4096:
            return normalized_token
    return ""


def _verify_liff_access_token(access_token: str) -> str | None:
    """Validate a LIFF access token and return its LINE user ID."""
    expected_channel_id = _expected_liff_channel_id()
    if not expected_channel_id:
        app.logger.error("LIFF authentication is unavailable: channel ID is not configured")
        return None

    try:
        verification_response = http_requests.get(
            "https://api.line.me/oauth2/v2.1/verify",
            params={"access_token": access_token},
            timeout=LIFF_AUTH_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if verification_response.status_code != 200:
            return None
        verification = verification_response.json()
        if not isinstance(verification, dict):
            return None
        if str(verification.get("client_id") or "") != expected_channel_id:
            return None
        if int(verification.get("expires_in") or 0) <= 0:
            return None

        profile_response = http_requests.get(
            "https://api.line.me/v2/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=LIFF_AUTH_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        if profile_response.status_code != 200:
            return None
        profile = profile_response.json()
        if not isinstance(profile, dict):
            return None
        line_user_id = str(profile.get("userId") or "").strip()
        return line_user_id or None
    except (http_requests.RequestException, TypeError, ValueError) as exc:
        _log_failure("LIFF token verification", exc)
        return None


_rate_limit_lock = threading.Lock()
_rate_limit_buckets: dict[tuple[str, str], deque[float]] = {}
_webhook_dedup_lock = threading.Lock()
_processed_webhook_events: dict[str, float] = {}


def _client_ip_address() -> str:
    if os.getenv("TRUST_PROXY_IP_HEADERS", "").strip().casefold() in {"1", "true", "yes"}:
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        if forwarded:
            return forwarded[:128]
    return (request.remote_addr or "unknown")[:128]


def _is_rate_limited(scope: str, identity: str, limit: int) -> bool:
    now = time.monotonic()
    cutoff = now - LIFF_RATE_LIMIT_WINDOW_SECONDS
    bucket_key = (scope, identity)

    with _rate_limit_lock:
        for key, bucket in list(_rate_limit_buckets.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                _rate_limit_buckets.pop(key, None)

        bucket = _rate_limit_buckets.setdefault(bucket_key, deque())
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        return False


def _claim_webhook_event(event_id: str) -> bool:
    now = time.monotonic()
    cutoff = now - WEBHOOK_DEDUP_TTL_SECONDS
    with _webhook_dedup_lock:
        for stored_id, processed_at in list(_processed_webhook_events.items()):
            if processed_at <= cutoff:
                _processed_webhook_events.pop(stored_id, None)
        if event_id in _processed_webhook_events:
            return False
        _processed_webhook_events[event_id] = now
        return True


def _release_webhook_event(event_id: str) -> None:
    with _webhook_dedup_lock:
        _processed_webhook_events.pop(event_id, None)


def _sanitize_maps_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2048:
        return ""
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not hostname or parsed.username or parsed.password:
        return ""
    if not any(hostname == host or hostname.endswith(f".{host}") for host in LIFF_ALLOWED_MAP_HOSTS):
        return ""
    return candidate


def _public_liff_result(result: dict[str, Any], *, synced_to_group: bool) -> dict[str, Any]:
    raw_results = result.get("results") or []
    if not isinstance(raw_results, list):
        raw_results = []
    public_results: list[dict[str, Any]] = []
    for raw_item in raw_results[:20]:
        if not isinstance(raw_item, dict):
            continue
        item: dict[str, Any] = {}
        for field_name, max_length in (
            ("name", 200),
            ("subtitle", 500),
            ("description", 2000),
            ("address", 500),
        ):
            value = str(raw_item.get(field_name) or "").strip()
            if value:
                item[field_name] = value[:max_length]
        try:
            distance_km = float(raw_item.get("distance_km"))
            if math.isfinite(distance_km) and distance_km >= 0:
                item["distance_km"] = distance_km
        except (TypeError, ValueError):
            pass
        maps_url = _sanitize_maps_url(raw_item.get("maps_url"))
        if maps_url:
            item["maps_url"] = maps_url
        public_results.append(item)

    return {
        "group_message": str(result.get("group_message") or "").strip()[:5000],
        "results": public_results,
        "location_source": "liff",
        "synced_to_group": synced_to_group,
    }


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.path.startswith(("/liff/", "/api/liff/")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if request.path == "/liff/location":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://static.line-scdn.net; "
            "connect-src 'self' https://api.line.me https://*.line.me; "
            "img-src 'self' data: https:; "
            "style-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
    return response


@dataclass
class ConversationState:
    # 保存群組最近訊息，讓 AI 能看到短期上下文。
    history: deque[str] = field(
        default_factory=lambda: deque(maxlen=CONVERSATION_WINDOW_SIZE)
    )
    # 只保存能明確辨識的主題；模糊訊息不會覆蓋它。
    current_topic: str = ""
    user_message_count: int = 0
    # 保存 Bot 上一次回覆，用來避免短時間重複講相近內容。
    last_reply_text: str = ""
    last_scenario_code: str = ""
    last_reply_message_count: int = 0
    # 保存網站匯入的行程與目前焦點景點。
    imported_itinerary: dict[str, Any] | None = None
    focused_spot: dict[str, Any] | None = None
    last_accessed_at: float = field(default_factory=time.monotonic)


conversation_states: dict[str, ConversationState] = {}
conversation_lock = threading.Lock()


def _prune_conversation_states_locked() -> None:
    cutoff = time.monotonic() - CONVERSATION_STATE_TTL_SECONDS
    for key, state in list(conversation_states.items()):
        if state.last_accessed_at <= cutoff:
            conversation_states.pop(key, None)


def _get_or_create_state(conversation_key: str) -> ConversationState:
    _prune_conversation_states_locked()
    state = conversation_states.get(conversation_key)
    if state is None:
        state = ConversationState()
        conversation_states[conversation_key] = state
    state.last_accessed_at = time.monotonic()
    return state


def _note_user_message(conversation_key: str, text: str) -> None:
    # 這裡會把匯入行程這類系統轉換出的訊息也記進 history。
    normalized_text = text.strip()
    if not normalized_text:
        return

    with conversation_lock:
        state = _get_or_create_state(conversation_key)
        state.history.append(normalized_text)
        state.user_message_count += 1


def _store_imported_itinerary(
    conversation_key: str,
    itinerary: dict[str, Any],
    focused_spot: dict[str, Any] | None,
) -> None:
    with conversation_lock:
        state = _get_or_create_state(conversation_key)
        state.imported_itinerary = itinerary
        state.focused_spot = focused_spot


def _get_imported_itinerary_state(
    conversation_key: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with conversation_lock:
        _prune_conversation_states_locked()
        state = conversation_states.get(conversation_key)
        if state is None:
            return None, None
        state.last_accessed_at = time.monotonic()
        return state.imported_itinerary, state.focused_spot


def _reset_conversation_state(conversation_key: str) -> None:
    with conversation_lock:
        conversation_states.pop(conversation_key, None)
    clear_recent_location_context(conversation_key)


def _reply_text(line_bot_api: MessagingApi, reply_token: str, text: str) -> None:
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)],
        )
    )


def _reply_text_and_mark(
    event: MessageEvent,
    conversation_key: str,
    scenario_code: str,
    text: str,
) -> None:
    normalized_text = text.strip()
    if not normalized_text:
        return

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        _reply_text(line_bot_api, event.reply_token, normalized_text)

    _mark_reply_sent(conversation_key, scenario_code, normalized_text)


def _reply_text_if_allowed(
    event: MessageEvent,
    conversation_key: str,
    scenario_code: str,
    text: str,
) -> None:
    normalized_text = text.strip()
    if not normalized_text:
        return

    current_user_message_count = _get_user_message_count(conversation_key)
    if _should_suppress_duplicate_reply(
        conversation_key,
        scenario_code,
        normalized_text,
        current_user_message_count,
    ):
        return

    _reply_text_and_mark(event, conversation_key, scenario_code, normalized_text)


def _push_text(push_target_id: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=push_target_id,
                messages=[TextMessage(text=text)],
            ),
            x_line_retry_key=str(uuid4()),
        )


def _reply_message_object(
    line_bot_api: MessagingApi,
    reply_token: str,
    message: TextMessage,
) -> None:
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[message],
        )
    )


def _is_location_recommendation_request(
    text: str,
    analysis_result: dict[str, Any] | None = None,
) -> bool:
    normalized_text = text.strip()
    if not normalized_text:
        return False

    result = _call_small_json_model(
        model=OPENAI_LOCATION_JUDGE_MODEL,
        purpose="Location routing judge",
        system_prompt=(
            "你是 LINE 群組助理的定位需求判斷器。"
            "請判斷這則訊息是否『一定需要使用者目前的即時位置』才能繼續處理。"
            "只有像『用我現在的位置推薦附近餐廳、景點、路線』這類需求才算 true。"
            "若只是一般問餐廳、一般問行程、提到某個已知地點，或還在初步討論，都應該是 false。"
            "只輸出 JSON，格式必須是 {\"needs_current_location\": true/false}。"
        ),
        user_prompt=json.dumps(
            {
                "user_text": normalized_text,
                "analysis_result": analysis_result or {},
            },
            ensure_ascii=False,
        ),
    )
    if isinstance(result, dict) and "needs_current_location" in result:
        return bool(result.get("needs_current_location"))

    return False


def _has_explicit_current_location_signal(analysis_result: dict[str, Any]) -> bool:
    extracted_info = analysis_result.get("extracted_info") or {}
    locations = extracted_info.get("location") or []
    if not isinstance(locations, list):
        locations = [locations]

    normalized_locations = {
        str(item).strip()
        for item in locations
        if str(item).strip()
    }
    return bool(
        normalized_locations
        & {"目前位置", "現在位置", "當前位置", "我的位置", "我附近", "這裡附近"}
    )


def _should_route_to_location_flow(
    user_text: str,
    analysis_result: dict[str, Any],
) -> bool:
    if not bool(analysis_result.get("requires_external_search")):
        return False

    if not bool(analysis_result.get("should_intervene")):
        return False

    reply_trigger = str(analysis_result.get("reply_trigger") or "").strip()
    if reply_trigger not in {"functional_question", "explicit_request"}:
        return False

    if not _has_explicit_current_location_signal(analysis_result):
        return False

    return _is_location_recommendation_request(user_text, analysis_result)


def _extract_text_location_query_payload(
    user_text: str,
    analysis_result: dict[str, Any],
) -> dict[str, Any] | None:
    if _has_weather_request_signal(user_text, analysis_result):
        return None

    if not bool(analysis_result.get("requires_external_search")):
        return None

    if not bool(analysis_result.get("should_intervene")):
        return None

    extracted_info = analysis_result.get("extracted_info") or {}
    locations = extracted_info.get("location") or []
    constraints = extracted_info.get("constraints") or []
    activity_types = extracted_info.get("activity_types") or []

    if not isinstance(locations, list):
        locations = [locations]
    if not isinstance(constraints, list):
        constraints = [constraints]
    if not isinstance(activity_types, list):
        activity_types = [activity_types]

    normalized_locations = [
        str(item).strip()
        for item in locations
        if str(item).strip()
    ]
    if not normalized_locations:
        return None

    location_text = normalized_locations[0]
    if location_text in {"目前位置", "現在位置", "當前位置"}:
        return None
    if location_text == "附近":
        return None

    return {
        "query_text": user_text.strip(),
        "location_text": location_text,
        "constraints": [str(item).strip() for item in constraints if str(item).strip()],
        "activity_types": [str(item).strip() for item in activity_types if str(item).strip()],
    }


def _has_weather_request_signal(
    user_text: str,
    analysis_result: dict[str, Any],
) -> bool:
    scenario_code = str(analysis_result.get("scenario_code") or "").strip()
    if scenario_code == "劇本十七":
        return True

    extracted_info = analysis_result.get("extracted_info") or {}
    risk_info = extracted_info.get("risk_info") or []
    if not isinstance(risk_info, list):
        risk_info = [risk_info]
    if any("天氣" in str(item) for item in risk_info):
        return True

    system_behavior = analysis_result.get("system_behavior") or []
    if not isinstance(system_behavior, list):
        system_behavior = [system_behavior]
    if any("天氣" in str(item) for item in system_behavior):
        return True

    weather_keywords = (
        "天氣",
        "下雨",
        "降雨",
        "氣溫",
        "溫度",
        "天候",
        "會不會熱",
        "會不會冷",
    )
    return any(keyword in user_text for keyword in weather_keywords)


def _is_direct_weather_question(user_text: str) -> bool:
    normalized = user_text.strip()
    if not normalized:
        return False

    question_markers = ("嗎", "呢", "怎麼樣", "如何", "會不會", "有沒有", "?")
    weather_keywords = (
        "天氣",
        "下雨",
        "降雨",
        "氣溫",
        "溫度",
        "天候",
        "會不會熱",
        "會不會冷",
    )
    return any(keyword in normalized for keyword in weather_keywords) and any(
        marker in normalized for marker in question_markers
    )


def _extract_weather_query_payload(
    user_text: str,
    analysis_result: dict[str, Any],
) -> dict[str, Any] | None:
    should_intervene = bool(analysis_result.get("should_intervene"))
    if not should_intervene and not _is_direct_weather_question(user_text):
        return None

    reply_trigger = str(analysis_result.get("reply_trigger") or "").strip()
    if reply_trigger not in {"functional_question", "explicit_request", "no_reply"}:
        return None

    if not _has_weather_request_signal(user_text, analysis_result):
        return None

    extracted_info = analysis_result.get("extracted_info") or {}
    locations = extracted_info.get("location") or []
    times = extracted_info.get("time") or []
    if not isinstance(locations, list):
        locations = [locations]
    if not isinstance(times, list):
        times = [times]

    location_text = next((str(item).strip() for item in locations if str(item).strip()), "")
    time_text = next((str(item).strip() for item in times if str(item).strip()), "")

    return {
        "query_text": user_text.strip(),
        "location_text": location_text,
        "time_text": time_text,
    }


def _handle_weather_recommendation_request(
    event: MessageEvent,
    conversation_key: str,
    scenario_code: str,
    line_group_id: str,
    *,
    query_text: str,
    location_text: str,
    time_text: str,
) -> bool:
    result = run_weather_recommendation(
        query_text=query_text,
        location_text=location_text,
        time_text=time_text,
        line_group_id=line_group_id,
    )
    group_message = str(result.get("group_message") or "").strip()
    if not group_message:
        return False

    current_user_message_count = _get_user_message_count(conversation_key)
    if _should_suppress_duplicate_reply(
        conversation_key,
        scenario_code,
        group_message,
        current_user_message_count,
    ):
        app.logger.info("Suppressed a duplicate weather recommendation reply")
        return True

    _reply_text_and_mark(
        event,
        conversation_key,
        scenario_code,
        group_message,
    )
    return True


def _build_location_query_text(
    user_text: str,
    analysis_result: dict[str, Any] | None,
) -> str:
    normalized_text = user_text.strip()
    if not normalized_text or not isinstance(analysis_result, dict):
        return normalized_text

    extracted_info = analysis_result.get("extracted_info") or {}
    activity_types = extracted_info.get("activity_types") or []
    constraints = extracted_info.get("constraints") or []

    if not isinstance(activity_types, list):
        activity_types = [activity_types]
    if not isinstance(constraints, list):
        constraints = [constraints]

    normalized_activities = [
        str(item).strip()
        for item in activity_types
        if str(item).strip()
    ]
    normalized_constraints = [
        str(item).strip()
        for item in constraints
        if str(item).strip()
    ]

    activity_map = {
        "餐廳": "附近餐廳",
        "咖啡廳": "附近咖啡廳",
        "景點": "附近景點",
        "購物": "附近可以逛的地方",
    }

    if normalized_activities:
        primary_activity = normalized_activities[0]
        base_query = activity_map.get(primary_activity, "")
        if base_query:
            if normalized_constraints:
                return f"{base_query} {' '.join(normalized_constraints)}".strip()
            return base_query

    followup_signals = ("那", "還有", "呢", "嗎")
    if any(signal in normalized_text for signal in followup_signals):
        keyword_groups = {
            "附近餐廳": ("吃", "餐廳", "美食", "用餐", "晚餐", "午餐", "宵夜"),
            "附近咖啡廳": ("咖啡", "咖啡廳"),
            "附近景點": ("景點", "走走", "散步", "出去玩"),
            "附近可以逛的地方": ("逛街", "百貨", "購物", "商圈", "夜市"),
        }
        for rewritten_query, keywords in keyword_groups.items():
            if any(keyword in normalized_text for keyword in keywords):
                return rewritten_query

    return normalized_text


def _handle_text_location_recommendation_request(
    event: MessageEvent,
    conversation_key: str,
    scenario_code: str,
    line_group_id: str,
    *,
    query_text: str,
    location_text: str,
    constraints: list[str],
    activity_types: list[str],
) -> bool:
    result = run_text_location_recommendation(
        query_text=query_text,
        location_text=location_text,
        constraints=constraints,
        activity_types=activity_types,
        line_group_id=line_group_id,
    )
    group_message = str(result.get("group_message") or "").strip()
    if not group_message:
        return False

    current_user_message_count = _get_user_message_count(conversation_key)
    if _should_suppress_duplicate_reply(
        conversation_key,
        scenario_code,
        group_message,
        current_user_message_count,
    ):
        app.logger.info("Suppressed a duplicate text-location recommendation reply")
        return True

    _reply_text_and_mark(
        event,
        conversation_key,
        scenario_code,
        group_message,
    )
    return True


def _build_liff_prompt_message(liff_url: str) -> TextMessage:
    prompt_text = (
        "我這邊需要你目前的位置，\n"
        "點一下下面的定位按鈕，分享位置後我就能直接幫你整理附近的推薦，"
        "也會把結果同步回原本的群組。\n"
    )
    return TextMessage(
        text=prompt_text,
        quick_reply=QuickReply(
            items=[
                QuickReplyItem(
                    action=URIAction(
                        label="開啟定位",
                        uri=liff_url,
                    )
                )
            ]
        ),
    )


def _reply_liff_prompt(
    event: MessageEvent,
    conversation_key: str,
    line_group_id: str,
    line_user_id: str,
    user_text: str,
) -> None:
    if not os.getenv("LIFF_ID", "").strip():
        _reply_text_and_mark(
            event,
            conversation_key,
            "liff_missing_config",
            "LIFF_ID 尚未設定完成，請先在 .env 補上後再使用定位推薦。",
        )
        return

    push_target_id = _get_push_target_id(event)
    if not push_target_id:
        _reply_text_and_mark(
            event,
            conversation_key,
            "liff_missing_target",
            "目前無法判斷要把推薦結果同步到哪個聊天室，請稍後再試一次。",
        )
        return

    session_token = str(uuid4())
    create_recommendation_session(
        token=session_token,
        push_target_id=push_target_id,
        conversation_key=conversation_key,
        line_user_id=line_user_id,
        line_group_id=line_group_id,
        query_text=user_text,
    )
    liff_url = build_liff_url(session_token, request.url_root)
    prompt_message = _build_liff_prompt_message(liff_url)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        _reply_message_object(line_bot_api, event.reply_token, prompt_message)

    _mark_reply_sent(
        conversation_key,
        "liff_location_prompt",
        prompt_message.text,
    )


def _handle_location_recommendation_request(
    event: MessageEvent,
    conversation_key: str,
    line_group_id: str,
    line_user_id: str,
    user_text: str,
    analysis_result: dict[str, Any] | None = None,
    *,
    record_user_message: bool = True,
) -> bool:
    if record_user_message:
        _note_user_message(conversation_key, user_text)

    effective_query_text = _build_location_query_text(user_text, analysis_result)

    beacon_context = get_recent_beacon_context(line_user_id) if line_user_id else None
    if beacon_context and beacon_context.has_coordinates:
        try:
            result = run_location_recommendation(
                line_user_id=line_user_id,
                line_group_id=line_group_id,
                query_text=effective_query_text,
                latitude=beacon_context.latitude,
                longitude=beacon_context.longitude,
                accuracy=None,
                location_source="beacon",
                beacon_context=beacon_context,
            )
        except Exception as exc:
            _log_failure("Beacon recommendation", exc)
        else:
            group_message = str(result.get("group_message") or "").strip()
            if group_message:
                _reply_text_and_mark(
                    event,
                    conversation_key,
                    "beacon_location_recommendation",
                    group_message,
                )
                return True

    recent_location_context = get_recent_location_context(
        conversation_key=conversation_key,
    )
    if recent_location_context is not None:
        try:
            result = run_location_recommendation(
                line_user_id=line_user_id,
                line_group_id=line_group_id,
                query_text=effective_query_text,
                latitude=recent_location_context.latitude,
                longitude=recent_location_context.longitude,
                accuracy=recent_location_context.accuracy,
                location_source="recent_location",
                beacon_context=None,
            )
        except Exception as exc:
            _log_failure("Recent location recommendation", exc)
        else:
            group_message = str(result.get("group_message") or "").strip()
            if group_message:
                _reply_text_and_mark(
                    event,
                    conversation_key,
                    "recent_location_recommendation",
                    group_message,
                )
                return True

    _reply_liff_prompt(
        event,
        conversation_key,
        line_group_id,
        line_user_id,
        effective_query_text,
    )
    return True


def _get_push_target_id(event: MessageEvent) -> str | None:
    source = getattr(event, "source", None)
    for attr_name in ("group_id", "room_id", "user_id"):
        target_id = getattr(source, attr_name, None)
        if target_id:
            return target_id
    return None


def _get_conversation_key(event: MessageEvent) -> str:
    push_target_id = _get_push_target_id(event)
    if push_target_id:
        return push_target_id
    return f"reply_token:{event.reply_token}"


def _normalize_text_for_compare(text: str) -> str:
    return "".join(char.lower() for char in text if char.isalnum())


def _get_openai_client() -> Any | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    return OpenAI(api_key=api_key)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    payload = text.strip()
    if payload.startswith("```"):
        parts = payload.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("{") and part.endswith("}"):
                payload = part
                break

    start = payload.find("{")
    end = payload.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(payload[start : end + 1])
    except json.JSONDecodeError:
        return None


def _call_small_json_model(
    *,
    model: str,
    purpose: str,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any] | None:
    client = _get_openai_client()
    if client is None:
        return None

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return _extract_json_object(content)
    except Exception as exc:
        _log_failure(purpose, exc)
        return None


def _cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = sum(a * a for a in vector_a) ** 0.5
    norm_b = sum(b * b for b in vector_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


def _judge_same_topic_with_llm(
    recent_messages: list[str],
    new_message: str,
) -> bool | None:
    trimmed_history = [message.strip() for message in recent_messages if message.strip()]
    normalized_text = new_message.strip()
    if not trimmed_history or not normalized_text:
        return None

    history_text = "\n".join(trimmed_history[-TOPIC_JUDGE_HISTORY_LIMIT:])
    result = _call_small_json_model(
        model=OPENAI_TOPIC_JUDGE_MODEL,
        purpose="Topic judge",
        system_prompt=(
            "你是群組對話主題判斷器。"
            "請判斷『新訊息』和『最近對話』是否仍屬於同一個討論主題。"
            "只輸出 JSON，格式必須是 {\"same_topic\": true/false, \"reason\": \"...\"}。"
        ),
        user_prompt=(
            f"最近對話：\n{history_text}\n\n"
            f"新訊息：\n{normalized_text}\n\n"
            "如果新訊息明顯延續原本討論，same_topic=true；"
            "如果新訊息已切到新的安排、新的地點、新的活動或新的決策方向，same_topic=false。"
        ),
    )
    if not isinstance(result, dict) or "same_topic" not in result:
        return None
    return bool(result.get("same_topic"))


def _should_reset_history_by_topic(
    recent_messages: list[str],
    new_message: str,
    query_embedding: list[float] | None = None,
) -> bool:
    trimmed_history = [message.strip() for message in recent_messages if message.strip()]
    normalized_text = new_message.strip()
    if not trimmed_history or not normalized_text:
        return False

    llm_result = _judge_same_topic_with_llm(trimmed_history, normalized_text)
    if llm_result is not None:
        return not llm_result

    if query_embedding:
        history_embedding = _build_text_embedding(
            "\n".join(trimmed_history[-TOPIC_JUDGE_HISTORY_LIMIT:])
        )
        if history_embedding:
            similarity = _cosine_similarity(history_embedding, query_embedding)
            return similarity < TOPIC_SWITCH_SIMILARITY_THRESHOLD

    return False


def semantic_duplicate_check(new_reply: str, previous_reply: str) -> bool:
    normalized_new = _normalize_text_for_compare(new_reply)
    normalized_previous = _normalize_text_for_compare(previous_reply)

    if not normalized_new or not normalized_previous:
        return False

    if normalized_new == normalized_previous:
        return True

    if normalized_new in normalized_previous or normalized_previous in normalized_new:
        return True

    result = _call_small_json_model(
        model=OPENAI_TOPIC_JUDGE_MODEL,
        purpose="Reply duplicate judge",
        system_prompt=(
            "你是回覆重複判斷器。"
            "請判斷兩句回覆在群組中是否屬於語意上幾乎相同、重複插嘴的內容。"
            "只輸出 JSON，格式必須是 {\"is_duplicate\": true/false}。"
        ),
        user_prompt=(
            f"回覆A：{new_reply.strip()}\n"
            f"回覆B：{previous_reply.strip()}\n\n"
            "如果兩句話表達的建議幾乎一樣，只是換句話說，請回傳 true。"
        ),
    )
    if isinstance(result, dict) and "is_duplicate" in result:
        return bool(result.get("is_duplicate"))

    new_embedding = _build_text_embedding(new_reply)
    previous_embedding = _build_text_embedding(previous_reply)
    if new_embedding and previous_embedding:
        similarity = _cosine_similarity(new_embedding, previous_embedding)
        return similarity >= SEMANTIC_DUPLICATE_SIMILARITY_THRESHOLD

    return False


def _build_text_embedding(text: str) -> list[float] | None:
    normalized_text = text.strip()
    if not normalized_text:
        return None

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        client = OpenAI(api_key=api_key)
        response = client.embeddings.create(
            model=OPENAI_EMBEDDING_MODEL,
            input=normalized_text,
        )
        embedding = response.data[0].embedding
        return list(embedding) if embedding else None
    except Exception as exc:
        _log_failure("Embedding generation", exc)
        return None


def _format_retrieved_messages(similar_messages: list[dict[str, Any]]) -> list[str]:
    formatted_messages: list[str] = []
    seen_texts: set[str] = set()

    for doc in similar_messages:
        message_text = str(doc.get("message_text") or "").strip()
        if not message_text or message_text in seen_texts:
            continue

        display_name = str(doc.get("display_name") or "").strip()
        message_role = str(doc.get("message_role") or "user").strip()
        prefix = display_name or ("Bot" if message_role == "bot" else "使用者")
        formatted_messages.append(f"{prefix}：{message_text}")
        seen_texts.add(message_text)

    return formatted_messages


def _build_conversation_context(
    conversation_key: str,
    user_text: str,
    line_group_id: str = "",
    query_embedding: list[float] | None = None,
    exclude_message_id: Any = None,
) -> tuple[list[str], str]:
    normalized_text = user_text.strip()
    imported_context = ""
    retrieved_messages: list[str] = []

    with conversation_lock:
        state = _get_or_create_state(conversation_key)
        previous_history = list(state.history)

    should_reset_history = _should_reset_history_by_topic(
        previous_history,
        normalized_text,
        query_embedding=query_embedding,
    )

    with conversation_lock:
        state = _get_or_create_state(conversation_key)
        if should_reset_history:
            state.history.clear()
            state.current_topic = ""
        state.history.append(normalized_text)
        state.user_message_count += 1
        history_snapshot = list(state.history)
        if state.imported_itinerary is not None:
            imported_context = build_itinerary_context(
                state.imported_itinerary,
                state.focused_spot,
            )

    if line_group_id and query_embedding:
        try:
            similar_messages = get_similar_messages(
                line_group_id=line_group_id,
                query_embedding=query_embedding,
                exclude_message_id=exclude_message_id,
                limit=RAG_RETRIEVAL_LIMIT,
                min_score=RAG_MIN_SIMILARITY_SCORE,
            )
            retrieved_messages = _format_retrieved_messages(similar_messages)
        except Exception as exc:
            _log_failure("RAG retrieval", exc)

    recent_context = "\n".join(history_snapshot)
    retrieved_context = "\n".join(retrieved_messages)
    context_text = f"[目前最近對話]\n{recent_context}"
    if retrieved_context:
        context_text = (
            f"[目前最近對話]\n{recent_context}\n\n"
            f"[歷史相關對話]\n{retrieved_context}"
        )
    if imported_context:
        context_text = f"{imported_context}\n\n{context_text}"
    return history_snapshot, context_text


def _get_user_message_count(conversation_key: str) -> int:
    with conversation_lock:
        _prune_conversation_states_locked()
        state = conversation_states.get(conversation_key)
        if state is None:
            return 0
        state.last_accessed_at = time.monotonic()
        return state.user_message_count


def _should_suppress_duplicate_reply(
    conversation_key: str,
    scenario_code: str,
    reply_text: str,
    current_user_message_count: int,
) -> bool:
    candidate_text = reply_text.strip()
    if not candidate_text:
        return False

    with conversation_lock:
        _prune_conversation_states_locked()
        state = conversation_states.get(conversation_key)
        if state is None or not state.last_reply_text:
            return False
        state.last_accessed_at = time.monotonic()

        messages_since_last_reply = (
            current_user_message_count - state.last_reply_message_count
        )
        if messages_since_last_reply >= MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY:
            return False

        is_same_reply = candidate_text == state.last_reply_text
        is_same_scenario = scenario_code == state.last_scenario_code
        is_semantic_duplicate = semantic_duplicate_check(
            candidate_text,
            state.last_reply_text,
        )

        return (is_same_reply and is_same_scenario) or is_semantic_duplicate


def _should_suppress_duplicate_candidates(
    conversation_key: str,
    scenario_code: str,
    current_user_message_count: int,
    *reply_texts: str,
) -> bool:
    for reply_text in reply_texts:
        if _should_suppress_duplicate_reply(
            conversation_key,
            scenario_code,
            reply_text,
            current_user_message_count,
        ):
            return True
    return False


def _mark_reply_sent(
    conversation_key: str,
    scenario_code: str,
    reply_text: str,
) -> None:
    normalized_text = reply_text.strip()
    if not normalized_text:
        return

    with conversation_lock:
        state = _get_or_create_state(conversation_key)
        state.last_reply_text = normalized_text
        state.last_scenario_code = scenario_code
        state.last_reply_message_count = state.user_message_count


def _resolve_final_reply_after_external_search(result: dict[str, Any]) -> str:
    suggested_reply = str(result.get("suggested_reply") or "").strip()
    if suggested_reply:
        return suggested_reply
    return DEFAULT_FINAL_REPLY


def _push_followup_after_external_search(
    conversation_key: str,
    push_target_id: str,
    scenario_code: str,
    result: dict[str, Any],
) -> None:
    final_reply = _resolve_final_reply_after_external_search(result)
    if not final_reply:
        app.logger.info("External search completed without a final reply")
        return

    if EXTERNAL_SEARCH_DELAY_SECONDS > 0:
        time.sleep(EXTERNAL_SEARCH_DELAY_SECONDS)

    try:
        _push_text(push_target_id, final_reply)
        _mark_reply_sent(conversation_key, scenario_code, final_reply)
    except Exception as exc:
        _log_failure("External-search follow-up push", exc)


def _handle_line_import_message(
    conversation_key: str,
    user_text: str,
) -> tuple[str, str] | None:
    command = extract_line_import_command(user_text)
    if command is None:
        return None

    if command.is_itinerary:
        itinerary = normalize_itinerary_payload(command.payload)
        focused_spot = itinerary["spots"][0] if itinerary["spots"] else None
        _store_imported_itinerary(conversation_key, itinerary, focused_spot)
        _note_user_message(conversation_key, f"[匯入行程] {itinerary['title']}")
        return (
            build_itinerary_import_reply(itinerary),
            f"[verified LINE itinerary import] {itinerary['title']}",
        )

    spot_payload = normalize_spot_payload(command.payload)
    itinerary, _ = _get_imported_itinerary_state(conversation_key)
    if itinerary is None or itinerary.get("itinerary_id") != spot_payload["itinerary_id"]:
        itinerary = create_placeholder_itinerary_from_spot(spot_payload)

    focused_spot = find_itinerary_spot(
        itinerary,
        spot_id=spot_payload["spot_id"],
        spot_name=spot_payload["spot_name"],
        sequence=spot_payload["sequence"],
    )
    if focused_spot is None:
        focused_spot = create_focus_spot_from_import(spot_payload)

    _store_imported_itinerary(conversation_key, itinerary, focused_spot)
    _note_user_message(conversation_key, f"[匯入景點] {focused_spot['name']}")
    return (
        build_spot_import_reply(itinerary, focused_spot),
        f"[verified LINE spot import] {focused_spot['name']}",
    )


def _reply_from_imported_itinerary(
    conversation_key: str,
    user_text: str,
) -> str | None:
    itinerary, focused_spot = _get_imported_itinerary_state(conversation_key)
    if itinerary is None:
        return None
    return build_itinerary_followup_reply(user_text, itinerary, focused_spot)


@app.route("/api/trip/import/sign", methods=["POST"])
def sign_trip_import():
    client_ip = _client_ip_address()
    if _is_rate_limited("line-import-sign", client_ip, LINE_IMPORT_RATE_LIMIT_PER_IP):
        response = jsonify({"ok": False, "error": "Too many requests. Please try again later."})
        response.status_code = 429
        response.headers["Retry-After"] = str(LIFF_RATE_LIMIT_WINDOW_SECONDS)
        return response
    if request.content_length is None:
        return jsonify({"ok": False, "error": "Content-Length is required."}), 411
    if request.content_length > LINE_IMPORT_MAX_JSON_BYTES:
        return jsonify({"ok": False, "error": "Request body is too large."}), 413
    if not request.is_json:
        return jsonify({"ok": False, "error": "Content-Type must be application/json."}), 415

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid JSON body."}), 400
    try:
        marker, token = create_signed_line_import_token(
            kind=str(payload.get("kind") or ""),
            itinerary_id=str(payload.get("itinerary_id") or ""),
            spot_id=str(payload.get("spot_id") or ""),
        )
    except LineImportError:
        return jsonify({"ok": False, "error": "The import request is invalid."}), 400
    return jsonify({"ok": True, "marker": marker, "token": token})


@app.route("/liff/location", methods=["GET"])
def serve_liff_location_page():
    return send_from_directory(LIFF_LOCATION_DIR, "index.html")


@app.route("/liff/location/styles.css", methods=["GET"])
def serve_liff_location_styles():
    return send_from_directory(
        LIFF_LOCATION_DIR,
        "styles.css",
        mimetype="text/css",
    )


@app.route("/liff/location/images/<path:filename>", methods=["GET"])
def serve_liff_location_image(filename: str):
    return send_from_directory(
        os.path.join(LIFF_LOCATION_DIR, "images"),
        filename,
    )


@app.route("/favicon.ico", methods=["GET"])
def serve_favicon():
    return send_from_directory(
        os.path.join(LIFF_LOCATION_DIR, "images"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/liff/location/app.js", methods=["GET"])
def serve_liff_location_script():
    return send_from_directory(
        LIFF_LOCATION_DIR,
        "location.js",
        mimetype="application/javascript",
    )


@app.route("/api/liff/location/recommendation", methods=["POST"])
def receive_liff_location_recommendation():
    client_ip = _client_ip_address()
    if _is_rate_limited("ip", client_ip, LIFF_RATE_LIMIT_PER_IP):
        response = jsonify({"ok": False, "error": "Too many requests. Please try again later."})
        response.status_code = 429
        response.headers["Retry-After"] = str(LIFF_RATE_LIMIT_WINDOW_SECONDS)
        return response

    if request.content_length is None:
        return jsonify({"ok": False, "error": "Content-Length is required."}), 411
    if request.content_length > LIFF_MAX_JSON_BYTES:
        return jsonify({"ok": False, "error": "Request body is too large."}), 413
    if not request.is_json:
        return jsonify({"ok": False, "error": "Content-Type must be application/json."}), 415

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Invalid JSON body."}), 400
    session_token = str(payload.get("session_token") or "").strip()
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    accuracy = payload.get("accuracy")

    if not session_token or len(session_token) > 128:
        return jsonify({"ok": False, "error": "Missing session token."}), 400

    session = get_recommendation_session(session_token)
    if session is None:
        return jsonify(
            {
                "ok": False,
                "error": "This LIFF session has expired. Please reopen it from LINE.",
            }
        ), 410

    if session.status != "pending":
        return jsonify(
            {
                "ok": False,
                "error": "This recommendation session has already been used.",
            }
        ), 409

    if not _expected_liff_channel_id():
        return jsonify({"ok": False, "error": "LIFF authentication is unavailable."}), 503

    access_token = _extract_bearer_token()
    if not access_token:
        return jsonify({"ok": False, "error": "A valid LIFF access token is required."}), 401
    authenticated_user_id = _verify_liff_access_token(access_token)
    if not authenticated_user_id or authenticated_user_id != session.line_user_id:
        return jsonify({"ok": False, "error": "LIFF authentication failed."}), 403

    if _is_rate_limited("user", authenticated_user_id, LIFF_RATE_LIMIT_PER_USER):
        response = jsonify({"ok": False, "error": "Too many requests. Please try again later."})
        response.status_code = 429
        response.headers["Retry-After"] = str(LIFF_RATE_LIMIT_WINDOW_SECONDS)
        return response

    try:
        if isinstance(latitude, bool) or isinstance(longitude, bool) or isinstance(accuracy, bool):
            raise ValueError
        latitude = float(latitude)
        longitude = float(longitude)
        accuracy_value = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid latitude or longitude."}), 400

    if not math.isfinite(latitude) or not -90.0 <= latitude <= 90.0:
        return jsonify({"ok": False, "error": "Latitude is out of range."}), 400
    if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
        return jsonify({"ok": False, "error": "Longitude is out of range."}), 400
    if accuracy_value is not None and (
        not math.isfinite(accuracy_value)
        or accuracy_value < 0
        or accuracy_value > LIFF_MAX_ACCURACY_METERS
    ):
        return jsonify({"ok": False, "error": "Accuracy is out of range."}), 400

    session, claim_error = claim_recommendation_session(
        session_token,
        authenticated_user_id,
    )
    if claim_error == "forbidden":
        return jsonify({"ok": False, "error": "LIFF authentication failed."}), 403
    if claim_error == "used":
        return jsonify(
            {"ok": False, "error": "This recommendation session has already been used."}
        ), 409
    if claim_error == "expired" or session is None:
        return jsonify({"ok": False, "error": "This LIFF session has expired."}), 410

    try:
        result = run_location_recommendation(
            line_user_id=session.line_user_id,
            line_group_id=session.line_group_id,
            query_text=session.query_text,
            latitude=latitude,
            longitude=longitude,
            accuracy=accuracy_value,
            location_source="liff",
            beacon_context=None,
        )
        save_recent_location_context(
            conversation_key=session.conversation_key,
            line_user_id=session.line_user_id,
            line_group_id=session.line_group_id,
            latitude=latitude,
            longitude=longitude,
            accuracy=accuracy_value,
        )
    except Exception as exc:
        _log_failure("LIFF location recommendation", exc)
        mark_session_failed(session_token, {"error_code": "recommendation_failed"})
        return jsonify(
            {"ok": False, "error": "The recommendation service is temporarily unavailable."}
        ), 502

    if not isinstance(result, dict):
        mark_session_failed(session_token, {"error_code": "invalid_backend_response"})
        return jsonify(
            {"ok": False, "error": "The recommendation service returned an invalid response."}
        ), 502

    group_message = str(result.get("group_message") or "").strip()[:5000]
    synced_to_group = False
    if group_message:
        try:
            _push_text(session.push_target_id, group_message)
            _mark_reply_sent(
                session.conversation_key,
                "liff_location_recommendation",
                group_message,
            )
            synced_to_group = True
        except Exception as exc:
            _log_failure("LIFF result push", exc)

    response_payload = _public_liff_result(result, synced_to_group=synced_to_group)
    finalize_session_result(session_token, {"consumed": True})

    return jsonify({"ok": True, **response_payload})


def _dispatch_webhook_event(event: Any) -> None:
    if isinstance(event, BeaconEvent):
        handle_beacon_event(event)
    elif isinstance(event, MessageEvent) and isinstance(event.message, LocationMessageContent):
        handle_location_message(event)
    elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
        handle_message(event)


@app.route("/callback", methods=["POST"])
def callback() -> str:
    signature = request.headers.get("X-Line-Signature", "").strip()
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)

    try:
        payload = handler.parser.parse(body, signature, as_payload=True)
    except InvalidSignatureError:
        abort(400)
    except (ValueError, TypeError) as exc:
        _log_failure("LINE webhook parsing", exc)
        abort(400)

    for event in payload.events:
        event_id = str(getattr(event, "webhook_event_id", "") or "").strip()
        if not event_id:
            event_id = hashlib.sha256(event.to_json().encode("utf-8")).hexdigest()
        if not _claim_webhook_event(event_id):
            continue
        try:
            _dispatch_webhook_event(event)
        except Exception as exc:
            _release_webhook_event(event_id)
            _log_failure("LINE webhook event handling", exc)
            abort(500)

    return "OK"


@handler.add(BeaconEvent)
def handle_beacon_event(event: BeaconEvent) -> None:
    source = getattr(event, "source", None)
    line_user_id = getattr(source, "user_id", None) or ""
    if not line_user_id:
        return

    beacon = getattr(event, "beacon", None)
    if beacon is None:
        return

    try:
        register_beacon_event(
            line_user_id=line_user_id,
            hwid=str(getattr(beacon, "hwid", "") or "").strip(),
            beacon_type=str(getattr(beacon, "type", "enter") or "enter").strip(),
            device_message=str(getattr(beacon, "dm", "") or "").strip(),
        )
    except Exception as exc:
        _log_failure("Beacon registration", exc)


@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location_message(event: MessageEvent) -> None:
    source = getattr(event, "source", None)
    line_group_id = getattr(source, "group_id", None) or ""
    line_user_id = getattr(source, "user_id", None) or ""
    conversation_key = _get_conversation_key(event)
    location_message = event.message

    try:
        result = run_location_recommendation(
            line_user_id=line_user_id,
            line_group_id=line_group_id,
            query_text="根據這個位置幫我整理附近推薦",
            latitude=float(location_message.latitude),
            longitude=float(location_message.longitude),
            accuracy=None,
            location_source="manual_location",
            beacon_context=None,
        )
        save_recent_location_context(
            conversation_key=conversation_key,
            line_user_id=line_user_id,
            line_group_id=line_group_id,
            latitude=float(location_message.latitude),
            longitude=float(location_message.longitude),
            accuracy=None,
        )
    except Exception as exc:
        _log_failure("Manual location recommendation", exc)
        _reply_text_and_mark(
            event,
            conversation_key,
            "manual_location_recommendation_error",
            "收到你分享的位置了，但推薦服務暫時忙碌，請稍後再試一次。",
        )
        return

    group_message = str(result.get("group_message") or "").strip()
    if not group_message:
        group_message = "收到你分享的位置了，但目前沒有拿到推薦結果。"

    _reply_text_and_mark(
        event,
        conversation_key,
        "manual_location_recommendation",
        group_message,
    )


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent) -> None:
    user_text = event.message.text.strip()
    if not user_text:
        return

    # ── 取得來源 ID ────────────────────────────────────────────
    source = getattr(event, "source", None)
    line_group_id = getattr(source, "group_id", None) or ""
    line_user_id  = getattr(source, "user_id",  None) or ""
    conversation_key = _get_conversation_key(event)

    _debug_print("=" * 50)
    _debug_print(f"DEBUG 收到訊息：{user_text}")

    if user_text.lower() == "#reset":
        _reset_conversation_state(conversation_key)
        try:
            _reply_text_and_mark(
                event,
                conversation_key,
                "manual_reset",
                "已清空這個群組目前的對話狀態，可以重新開始測試。",
            )
        except Exception as exc:
            _log_failure("Reset reply", exc)
        return

    try:
        import_result = _handle_line_import_message(conversation_key, user_text)
    except LineImportError:
        _note_user_message(conversation_key, "[rejected LINE import]")
        try:
            _reply_text_and_mark(
                event,
                conversation_key,
                "line_import_error",
                "LINE 匯入資料無效或已過期，請從網站重新分享。",
            )
        except Exception as reply_exc:
            _log_failure("LINE import error reply", reply_exc)
        return
    except Exception as exc:
        _log_failure("LINE import processing", exc)
        return

    if import_result:
        import_reply, safe_import_record = import_result
        try:
            if line_group_id:
                upsert_group(line_group_id)
                upsert_member(line_group_id, line_user_id)
            save_message(
                line_group_id,
                line_user_id,
                safe_import_record,
                conversation_key=conversation_key,
                embedding=None,
                topic_hint=None,
            )
        except Exception as db_exc:
            _log_failure("LINE import persistence", db_exc)
        try:
            _reply_text_and_mark(
                event,
                conversation_key,
                "line_import_received",
                import_reply,
            )
        except Exception as exc:
            _log_failure("LINE import acknowledgement", exc)
        return

    topic_hint = None
    query_embedding = _build_text_embedding(user_text)
    saved_message_id = None

    # ── DB：儲存訊息與群組資訊（失敗不中斷主流程）─────────────
    try:
        if line_group_id:
            upsert_group(line_group_id)
            upsert_member(line_group_id, line_user_id)
        saved_message_id = save_message(
            line_group_id,
            line_user_id,
            user_text,
            conversation_key=conversation_key,
            embedding=query_embedding,
            topic_hint=topic_hint,
        )
    except Exception as _db_exc:
        _log_failure("Message persistence", _db_exc)

    try:
        _recent_messages, context_text = _build_conversation_context(
            conversation_key,
            user_text,
            line_group_id=line_group_id,
            query_embedding=query_embedding,
            exclude_message_id=saved_message_id,
        )
        _debug_print(
            f"DEBUG 對話視窗 key={conversation_key}, "
            f"messages={len(_recent_messages)}/{CONVERSATION_WINDOW_SIZE}"
        )
        _debug_print("DEBUG 送進 AI 的上下文：")
        _debug_print(context_text)
        direct_reply = _reply_from_imported_itinerary(conversation_key, user_text)
        if direct_reply:
            _debug_print(f"DEBUG 命中匯入行程直接回覆：{direct_reply}")
            _reply_text_if_allowed(
                event,
                conversation_key,
                "imported_itinerary_context",
                direct_reply,
            )
            return

        result_obj = analyze_dialogue(context_text)
        result = result_obj.to_dict()
        _debug_print("DEBUG AI 判斷結果：")
        _debug_print(json.dumps(result, ensure_ascii=False, indent=4))

        # ── DB：儲存完整 AI 分析結果（失敗不中斷主流程）─────────
        try:
            save_summary(line_group_id or conversation_key, result)
        except Exception as _db_exc:
            _log_failure("Analysis persistence", _db_exc)
    except Exception as exc:
        _log_failure("AI analysis", exc)
        return

    should_intervene = bool(result.get("should_intervene"))
    scenario_code = str(result.get("scenario_code") or "")
    suggested_reply = str(result.get("suggested_reply") or "").strip()
    intermediate_reply = str(result.get("intermediate_reply") or "").strip()
    requires_external_search = bool(result.get("requires_external_search"))
    try:
        confidence_score = float(result.get("confidence_score", 0))
    except (TypeError, ValueError):
        confidence_score = 0.0

    try:
        if should_optimize_route(result):
            route_reply = build_optimized_route_reply(result)
            if route_reply:
                _reply_text_and_mark(event, conversation_key, "route_optimization", route_reply)
                _debug_print("Route optimization flow handled after AI decision")
                return
    except Exception as exc:
        _log_failure("Route optimization flow", exc)

    try:
        weather_payload = _extract_weather_query_payload(user_text, result)
        if weather_payload:
            if _handle_weather_recommendation_request(
                event,
                conversation_key,
                scenario_code,
                line_group_id,
                query_text=str(weather_payload["query_text"]),
                location_text=str(weather_payload["location_text"]),
                time_text=str(weather_payload["time_text"]),
            ):
                _debug_print("Weather recommendation flow handled after AI decision")
                return
    except Exception as exc:
        _log_failure("Weather recommendation flow", exc)
        if _has_weather_request_signal(user_text, result):
            return

    try:
        if _should_route_to_location_flow(user_text, result):
            if _handle_location_recommendation_request(
                event,
                conversation_key,
                line_group_id,
                line_user_id,
                user_text,
                result,
                record_user_message=False,
            ):
                _debug_print("Location recommendation flow handled after AI decision")
                return
    except Exception as exc:
        _log_failure("Location recommendation flow", exc)

    try:
        text_location_payload = _extract_text_location_query_payload(user_text, result)
        if text_location_payload:
            if _handle_text_location_recommendation_request(
                event,
                conversation_key,
                scenario_code,
                line_group_id,
                query_text=str(text_location_payload["query_text"]),
                location_text=str(text_location_payload["location_text"]),
                constraints=list(text_location_payload["constraints"]),
                activity_types=list(text_location_payload["activity_types"]),
            ):
                _debug_print("Text location recommendation flow handled after AI decision")
                return
    except Exception as exc:
        _log_failure("Text location recommendation flow", exc)

    if not should_intervene:
        _debug_print("AI 判斷不介入。")
        return

    if confidence_score < MIN_INTERVENTION_CONFIDENCE:
        _debug_print(
            "AI 有介入傾向，但信心不足，先不回覆。 "
            f"(confidence_score={confidence_score:.2f}, "
            f"threshold={MIN_INTERVENTION_CONFIDENCE:.2f})"
        )
        return

    current_user_message_count = _get_user_message_count(conversation_key)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        try:
            if requires_external_search:
                push_target_id = _get_push_target_id(event)
                final_reply = _resolve_final_reply_after_external_search(result)

                if _should_suppress_duplicate_candidates(
                    conversation_key,
                    scenario_code,
                    current_user_message_count,
                    intermediate_reply,
                    final_reply,
                ):
                    app.logger.info("Suppressed a duplicate external-search reply")
                    _debug_print("略過語意相近的重複回覆。")
                    return

                if push_target_id and intermediate_reply:
                    _debug_print(f"先回覆查詢中訊息：{intermediate_reply}")
                    _reply_text(line_bot_api, event.reply_token, intermediate_reply)
                    _mark_reply_sent(
                        conversation_key,
                        scenario_code,
                        intermediate_reply,
                    )

                    threading.Thread(
                        target=_push_followup_after_external_search,
                        args=(conversation_key, push_target_id, scenario_code, result),
                        daemon=True,
                    ).start()
                else:
                    fallback_text = final_reply or intermediate_reply
                    if fallback_text:
                        _debug_print(f"準備回覆：{fallback_text}")
                        _reply_text(line_bot_api, event.reply_token, fallback_text)
                        _mark_reply_sent(
                            conversation_key,
                            scenario_code,
                            fallback_text,
                        )
                    else:
                        app.logger.info("External search required but no reply text was available")

            elif suggested_reply:
                if _should_suppress_duplicate_reply(
                    conversation_key,
                    scenario_code,
                    suggested_reply,
                    current_user_message_count,
                ):
                    app.logger.info("Suppressed a duplicate suggested reply")
                    _debug_print(
                        f"略過語意相近的重複回覆。 "
                        f"(scenario_code={scenario_code}, text={suggested_reply})"
                    )
                    return

                _debug_print(f"準備回覆：{suggested_reply}")
                _reply_text(line_bot_api, event.reply_token, suggested_reply)
                _mark_reply_sent(conversation_key, scenario_code, suggested_reply)
                _debug_print("已送出 LINE 回覆。")
            else:
                app.logger.info("AI selected intervention without reply text")
                _debug_print("AI 判斷要介入，但沒有可回覆文字。")

        except Exception as exc:
            _log_failure("LINE reply", exc)


if __name__ == "__main__":
    app.run()
