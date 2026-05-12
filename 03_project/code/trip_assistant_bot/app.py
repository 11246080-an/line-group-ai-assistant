from collections import deque
from dataclasses import dataclass, field
import json
import os
import threading
import time
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, abort, request

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from ai_linebot_core.app.engine import analyze_dialogue

load_dotenv()

app = Flask(__name__)

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

EXTERNAL_SEARCH_DELAY_SECONDS = float(os.getenv("EXTERNAL_SEARCH_DELAY_SECONDS", "0"))
CONVERSATION_WINDOW_SIZE = max(1, int(os.getenv("CONVERSATION_WINDOW_SIZE", "15")))
MIN_INTERVENTION_CONFIDENCE = float(
    os.getenv("MIN_INTERVENTION_CONFIDENCE", "0.8")
)
MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY = max(
    1,
    int(os.getenv("MIN_NEW_MESSAGES_BEFORE_REPEAT_REPLY", "4")),
)
DEFAULT_FINAL_REPLY = "我先整理一個方向給大家參考。"


@dataclass
class ConversationState:
    history: deque[str] = field(
        default_factory=lambda: deque(maxlen=CONVERSATION_WINDOW_SIZE)
    )
    user_message_count: int = 0
    last_reply_text: str = ""
    last_scenario_code: str = ""
    last_reply_message_count: int = 0


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


def _reply_text(line_bot_api: MessagingApi, reply_token: str, text: str) -> None:
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)],
        )
    )


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

    with conversation_lock:
        state = conversation_states.get(conversation_key)
        if state is None:
            state = ConversationState()
            conversation_states[conversation_key] = state

        state.history.append(normalized_text)
        state.user_message_count += 1
        history_snapshot = list(state.history)

    context_text = "\n".join(history_snapshot)
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

        return (
            (is_same_reply and is_same_scenario) or is_semantic_duplicate
        )


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
        state = conversation_states.get(conversation_key)
        if state is None:
            state = ConversationState()
            conversation_states[conversation_key] = state

        state.last_reply_text = normalized_text
        state.last_scenario_code = scenario_code
        state.last_reply_message_count = state.user_message_count


def _resolve_final_reply_after_external_search(result: dict) -> str:
    suggested_reply = str(result.get("suggested_reply") or "").strip()
    if suggested_reply:
        return suggested_reply
    return DEFAULT_FINAL_REPLY


def _push_followup_after_external_search(
    conversation_key: str,
    push_target_id: str,
    scenario_code: str,
    result: dict,
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


@app.route("/callback", methods=["POST"])
def callback():
    # get X-Line-Signature header value
    signature = request.headers["X-Line-Signature"]
    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Webhook 簽章驗證失敗，請確認 channel access token / secret。")
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    if not user_text:
        return
    
    # 1. 取得 AI 判斷結果
    try:
        # 強制用 print 輸出，這一定會顯示在 Terminal
        print("\n" + "=" * 50)
        print(f"DEBUG 收到訊息：{user_text}")

        conversation_key = _get_conversation_key(event)
        recent_messages, context_text = _build_conversation_context(
            conversation_key,
            user_text,
        )
        print(
            f"DEBUG 對話視窗 key={conversation_key}, "
            f"messages={len(recent_messages)}/{CONVERSATION_WINDOW_SIZE}"
        )

        # 美化 JSON 輸出：indent=4 讓它一行一行，ensure_ascii=False 讓中文顯示正常
        print(f"DEBUG 送進 AI 的上下文：\n{context_text}")
        result_obj = analyze_dialogue(context_text)
        result = result_obj.to_dict()
        pretty_result = json.dumps(result, indent=4, ensure_ascii=False)
        print(f"DEBUG AI 判斷結果：\n{pretty_result}")
    except Exception as exc:
        print(f"AI 分析失敗：{exc}")
        return

    # 擷取關鍵變數
    should_intervene = bool(result.get("should_intervene"))
    scenario_code = str(result.get("scenario_code") or "")
    suggested_reply = str(result.get("suggested_reply") or "").strip()
    intermediate_reply = str(result.get("intermediate_reply") or "").strip()
    requires_external_search = bool(result.get("requires_external_search"))
    try:
        confidence_score = float(result.get("confidence_score", 0))
    except (TypeError, ValueError):
        confidence_score = 0.0

    # 2. 判斷是否需要介入
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

    # 3. 執行發送邏輯
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        try:
            if requires_external_search:
                # 情境 A: 需要外部搜尋
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
                    _mark_reply_sent(conversation_key, scenario_code, intermediate_reply)

                    # 這裡之後可以接外部查詢邏輯，再發送 suggested_reply
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
                        _mark_reply_sent(conversation_key, scenario_code, fallback_text)
                        print(f"直接回覆最終訊息：{fallback_text}")
                    else:
                        print("需要查資料，但沒有可送出的訊息。")

            elif suggested_reply:
                # 情境 B: 直接回覆
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
