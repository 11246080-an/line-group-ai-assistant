from collections import deque
from dataclasses import dataclass, field
import json
import os
import threading
import time
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory

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
    create_focus_spot_from_import,
    create_placeholder_itinerary_from_spot,
    extract_line_import_command,
    find_itinerary_spot,
    normalize_itinerary_payload,
    normalize_spot_payload,
)
from db import (
    ensure_indexes,
    save_message,
    save_summary,
    upsert_group,
    upsert_member,
)
from location_flow import (
    build_liff_url,
    create_recommendation_session,
    finalize_session_result,
    get_recent_beacon_context,
    get_recommendation_session,
    mark_session_failed,
    register_beacon_event,
    run_location_recommendation,
)

load_dotenv()

app = Flask(__name__)

try:
    ensure_indexes()
    print("MongoDB 索引建立完成")
except Exception as _db_exc:
    print(f"MongoDB 連線失敗，繼續啟動（無持久化）：{_db_exc}")

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

# 這幾個參數控制 Bot 的對話視窗、最低介入信心，以及重複回覆抑制。
EXTERNAL_SEARCH_DELAY_SECONDS = float(os.getenv("EXTERNAL_SEARCH_DELAY_SECONDS", "0"))
CONVERSATION_WINDOW_SIZE = max(1, int(os.getenv("CONVERSATION_WINDOW_SIZE", "15")))
MIN_INTERVENTION_CONFIDENCE = float(os.getenv("MIN_INTERVENTION_CONFIDENCE", "0.8"))
MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY = max(
    1,
    int(os.getenv("MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY", "4")),
)
DEFAULT_FINAL_REPLY = "我先整理一個方向給大家參考。"
LIFF_LOCATION_DIR = os.path.join(app.root_path, "liff_app")
LOCATION_TRIGGER_KEYWORDS = (
    "附近",
    "好吃",
    "吃什麼",
    "餐廳",
    "美食",
    "下一站",
    "下一個點",
    "下一個景點",
    "接下來去哪",
    "附近有什麼",
)


@dataclass
class ConversationState:
    # 保存群組最近訊息，讓 AI 能看到短期上下文。
    history: deque[str] = field(
        default_factory=lambda: deque(maxlen=CONVERSATION_WINDOW_SIZE)
    )
    user_message_count: int = 0
    # 保存 Bot 上一次回覆，用來避免短時間重複講相近內容。
    last_reply_text: str = ""
    last_scenario_code: str = ""
    last_reply_message_count: int = 0
    # 保存網站匯入的行程與目前焦點景點。
    imported_itinerary: dict[str, Any] | None = None
    focused_spot: dict[str, Any] | None = None


conversation_states: dict[str, ConversationState] = {}
conversation_lock = threading.Lock()

SEMANTIC_TOPICS = {
    "booking": ("訂房", "住宿", "飯店", "旅館", "民宿"),
    "budget": ("預算", "太貴", "省一點", "花費", "負擔"),
    "vote": ("投票", "表決", "選哪個", "票選"),
    "time": ("日期", "時間", "幾點", "改天", "喬時間"),
    "location": ("地點", "去哪", "景點", "餐廳", "位置"),
    "route": ("路線", "交通", "順路", "移動", "行程順序"),
    "weather": ("天氣", "下雨", "溫度"),
    "dining": ("吃什麼", "午餐", "晚餐", "早餐", "美食"),
}


def _get_or_create_state(conversation_key: str) -> ConversationState:
    state = conversation_states.get(conversation_key)
    if state is None:
        state = ConversationState()
        conversation_states[conversation_key] = state
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
        state = conversation_states.get(conversation_key)
        if state is None:
            return None, None
        return state.imported_itinerary, state.focused_spot


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


def _is_location_recommendation_request(text: str) -> bool:
    normalized_text = text.strip()
    if not normalized_text:
        return False
    return any(keyword in normalized_text for keyword in LOCATION_TRIGGER_KEYWORDS)


def _build_liff_prompt_message(liff_url: str) -> TextMessage:
    prompt_text = (
        "目前系統無法偵測到您當前的位置，\n"
        "請您點選下方的定位按鈕分享您當前的位置，系統會依照您現在的位置整理推薦，"
        "並同步一份摘要回到原本的群組。\n"
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
) -> bool:
    if not _is_location_recommendation_request(user_text):
        return False

    _note_user_message(conversation_key, user_text)

    beacon_context = get_recent_beacon_context(line_user_id) if line_user_id else None
    if beacon_context and beacon_context.has_coordinates:
        try:
            result = run_location_recommendation(
                line_user_id=line_user_id,
                line_group_id=line_group_id,
                query_text=user_text,
                latitude=beacon_context.latitude,
                longitude=beacon_context.longitude,
                accuracy=None,
                location_source="beacon",
                beacon_context=beacon_context,
            )
        except Exception as exc:
            print(f"Beacon recommendation failed, falling back to LIFF: {exc}")
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

    _reply_liff_prompt(
        event,
        conversation_key,
        line_group_id,
        line_user_id,
        user_text,
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


def _reply_topic(text: str) -> str:
    lowered = text.lower()
    for topic, keywords in SEMANTIC_TOPICS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            return topic
    return ""


def semantic_duplicate_check(new_reply: str, previous_reply: str) -> bool:
    normalized_new = _normalize_text_for_compare(new_reply)
    normalized_previous = _normalize_text_for_compare(previous_reply)

    if not normalized_new or not normalized_previous:
        return False

    if normalized_new == normalized_previous:
        return True

    if normalized_new in normalized_previous or normalized_previous in normalized_new:
        return True

    new_topic = _reply_topic(new_reply)
    previous_topic = _reply_topic(previous_reply)
    if new_topic and new_topic == previous_topic:
        return True

    return False


def _build_conversation_context(
    conversation_key: str,
    user_text: str,
) -> tuple[list[str], str]:
    normalized_text = user_text.strip()
    imported_context = ""

    with conversation_lock:
        state = _get_or_create_state(conversation_key)
        state.history.append(normalized_text)
        state.user_message_count += 1
        history_snapshot = list(state.history)
        if state.imported_itinerary is not None:
            imported_context = build_itinerary_context(
                state.imported_itinerary,
                state.focused_spot,
            )

    context_text = "\n".join(history_snapshot)
    if imported_context:
        context_text = f"{imported_context}\n\n[群組最近訊息]\n{context_text}"
    return history_snapshot, context_text


def _get_user_message_count(conversation_key: str) -> int:
    with conversation_lock:
        state = conversation_states.get(conversation_key)
        if state is None:
            return 0
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
        state = conversation_states.get(conversation_key)
        if state is None or not state.last_reply_text:
            return False

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
        print("查詢完成後沒有可發送的最終回覆。")
        return

    if EXTERNAL_SEARCH_DELAY_SECONDS > 0:
        time.sleep(EXTERNAL_SEARCH_DELAY_SECONDS)

    try:
        _push_text(push_target_id, final_reply)
        _mark_reply_sent(conversation_key, scenario_code, final_reply)
        print(f"已補送最終回覆：{final_reply}")
    except Exception as exc:
        print(f"補送最終回覆失敗：{exc}")


def _handle_line_import_message(
    conversation_key: str,
    user_text: str,
) -> str | None:
    command = extract_line_import_command(user_text)
    if command is None:
        return None

    if command.is_itinerary:
        itinerary = normalize_itinerary_payload(command.payload)
        focused_spot = itinerary["spots"][0] if itinerary["spots"] else None
        _store_imported_itinerary(conversation_key, itinerary, focused_spot)
        _note_user_message(conversation_key, f"[匯入行程] {itinerary['title']}")
        return build_itinerary_import_reply(itinerary)

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
    return build_spot_import_reply(itinerary, focused_spot)


def _reply_from_imported_itinerary(
    conversation_key: str,
    user_text: str,
) -> str | None:
    itinerary, focused_spot = _get_imported_itinerary_state(conversation_key)
    if itinerary is None:
        return None
    return build_itinerary_followup_reply(user_text, itinerary, focused_spot)


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
    payload = request.get_json(silent=True) or {}
    session_token = str(payload.get("session_token") or "").strip()
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    accuracy = payload.get("accuracy")

    if not session_token:
        return jsonify({"ok": False, "error": "Missing session token."}), 400

    session = get_recommendation_session(session_token)
    if session is None:
        return jsonify(
            {
                "ok": False,
                "error": "This LIFF session has expired. Please reopen it from LINE.",
            }
        ), 410

    if session.status == "completed" and session.result is not None:
        cached_payload = dict(session.result)
        cached_payload["ok"] = True
        cached_payload["cached"] = True
        return jsonify(cached_payload)

    if session.status == "processing":
        return jsonify(
            {
                "ok": False,
                "error": "This recommendation request is already in progress.",
            }
        ), 409

    try:
        latitude = float(latitude)
        longitude = float(longitude)
        accuracy_value = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid latitude or longitude."}), 400

    session.status = "processing"

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
    except Exception as exc:
        failure_result = {
            "group_message": "",
            "results": [],
            "location_source": "liff",
            "query_text": session.query_text,
            "synced_to_group": False,
            "error": str(exc),
        }
        mark_session_failed(session_token, failure_result)
        return jsonify({"ok": False, "error": str(exc)}), 502

    group_message = str(result.get("group_message") or "").strip()
    synced_to_group = False
    push_error = ""
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
            push_error = str(exc)

    response_payload = dict(result)
    response_payload["synced_to_group"] = synced_to_group
    if push_error:
        response_payload["push_error"] = push_error

    finalize_session_result(session_token, response_payload)

    return jsonify({"ok": True, **response_payload})


@app.route("/callback", methods=["POST"])
def callback() -> str:
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    app.logger.info("Request body: %s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Webhook 簽章驗證失敗，請確認 channel access token / secret。")
        abort(400)

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
        context = register_beacon_event(
            line_user_id=line_user_id,
            hwid=str(getattr(beacon, "hwid", "") or "").strip(),
            beacon_type=str(getattr(beacon, "type", "enter") or "enter").strip(),
            device_message=str(getattr(beacon, "dm", "") or "").strip(),
        )
        print(
            "Beacon context updated: "
            f"user_id={line_user_id}, hwid={context.hwid}, name={context.name or 'unmapped'}"
        )
    except Exception as exc:
        print(f"Failed to register beacon context: {exc}")


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
    except Exception as exc:
        print(f"Manual location recommendation failed: {exc}")
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

    # ── DB：儲存訊息與群組資訊（失敗不中斷主流程）─────────────
    try:
        if line_group_id:
            upsert_group(line_group_id)
            upsert_member(line_group_id, line_user_id)
        save_message(line_group_id, line_user_id, user_text)
    except Exception as _db_exc:
        print(f"DB 寫入訊息失敗（繼續處理）：{_db_exc}")

    conversation_key = _get_conversation_key(event)

    # 先處理網站分享進來的行程 / 景點匯入訊息。
    try:
        import_reply = _handle_line_import_message(conversation_key, user_text)
    except LineImportError as exc:
        _note_user_message(conversation_key, "[匯入資料解析失敗]")
        try:
            _reply_text_and_mark(
                event,
                conversation_key,
                "line_import_error",
                f"匯入資料格式有誤，請重新分享一次：{exc}",
            )
        except Exception as reply_exc:
            print(f"LINE 匯入錯誤回覆失敗：{reply_exc}")
        return
    except Exception as exc:
        print(f"LINE 匯入處理失敗：{exc}")
        return

    if import_reply:
        try:
            _reply_text_and_mark(
                event,
                conversation_key,
                "line_import_received",
                import_reply,
            )
        except Exception as exc:
            print(f"LINE 匯入成功回覆失敗：{exc}")
        return

    try:
        if _handle_location_recommendation_request(
            event,
            conversation_key,
            line_group_id,
            line_user_id,
            user_text,
        ):
            print("Location recommendation flow handled in app.py")
            return
    except Exception as exc:
        print(f"Location recommendation flow error: {exc}")

    try:
        print("\n" + "=" * 50)
        print(f"DEBUG 收到訊息：{user_text}")

        recent_messages, context_text = _build_conversation_context(
            conversation_key,
            user_text,
        )
        print(
            f"DEBUG 對話視窗 key={conversation_key}, "
            f"messages={len(recent_messages)}/{CONVERSATION_WINDOW_SIZE}"
        )

        direct_reply = _reply_from_imported_itinerary(conversation_key, user_text)
        if direct_reply:
            _reply_text_if_allowed(
                event,
                conversation_key,
                "imported_itinerary_context",
                direct_reply,
            )
            print("已依照匯入行程直接回覆。")
            print("=" * 50 + "\n")
            return

        print(f"DEBUG 送進 AI 的上下文：\n{context_text}")
        result_obj = analyze_dialogue(context_text)
        result = result_obj.to_dict()
        pretty_result = json.dumps(result, indent=4, ensure_ascii=False)
        print(f"DEBUG AI 判斷結果：\n{pretty_result}")

        # ── DB：儲存完整 AI 分析結果（失敗不中斷主流程）─────────
        try:
            save_summary(line_group_id or conversation_key, result)
        except Exception as _db_exc:
            print(f"DB 寫入分析結果失敗（繼續處理）：{_db_exc}")
    except Exception as exc:
        print(f"AI 分析失敗：{exc}")
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

    if not should_intervene:
        print("AI 判斷不介入。")
        print("=" * 50 + "\n")
        return

    if confidence_score < MIN_INTERVENTION_CONFIDENCE:
        print(
            "AI 有介入傾向，但信心不足，先不回覆。"
            f" (confidence_score={confidence_score:.2f}, "
            f"threshold={MIN_INTERVENTION_CONFIDENCE:.2f})"
        )
        print("=" * 50 + "\n")
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
                    print(
                        "略過語意相近的重複回覆。 "
                        f"(scenario_code={scenario_code})"
                    )
                    print("=" * 50 + "\n")
                    return

                if push_target_id and intermediate_reply:
                    _reply_text(line_bot_api, event.reply_token, intermediate_reply)
                    _mark_reply_sent(
                        conversation_key,
                        scenario_code,
                        intermediate_reply,
                    )
                    print(f"先回覆查詢中訊息：{intermediate_reply}")

                    threading.Thread(
                        target=_push_followup_after_external_search,
                        args=(conversation_key, push_target_id, scenario_code, result),
                        daemon=True,
                    ).start()
                else:
                    fallback_text = final_reply or intermediate_reply
                    if fallback_text:
                        _reply_text(line_bot_api, event.reply_token, fallback_text)
                        _mark_reply_sent(
                            conversation_key,
                            scenario_code,
                            fallback_text,
                        )
                        print(f"直接回覆最終訊息：{fallback_text}")
                    else:
                        print("需要查資料，但沒有可送出的訊息。")

            elif suggested_reply:
                if _should_suppress_duplicate_reply(
                    conversation_key,
                    scenario_code,
                    suggested_reply,
                    current_user_message_count,
                ):
                    print(
                        "略過語意相近的重複回覆。 "
                        f"(scenario_code={scenario_code}, text={suggested_reply})"
                    )
                    print("=" * 50 + "\n")
                    return

                print(f"準備回覆：{suggested_reply}")
                _reply_text(line_bot_api, event.reply_token, suggested_reply)
                _mark_reply_sent(conversation_key, scenario_code, suggested_reply)
                print("已送出 LINE 回覆。")
            else:
                print("AI 判斷要介入，但沒有可送出的 suggested_reply。")

            print("=" * 50 + "\n")
        except Exception as exc:
            print(f"LINE 回覆失敗：{exc}")


if __name__ == "__main__":
    app.run()
