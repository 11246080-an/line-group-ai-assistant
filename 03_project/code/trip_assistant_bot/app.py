import os
from dotenv import load_dotenv, find_dotenv

from flask import Flask, request, abort

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

from ai_linebot_core.app.engine import analyze_dialogue

load_dotenv()

app = Flask(__name__)

configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))


@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text
    # 1. 取得 AI 判斷結果 [cite: 25, 26, 27]
    result_obj = analyze_dialogue(user_text)
    result = result_obj.to_dict()
    
    should_intervene = result.get("should_intervene")
    suggested_reply = result.get("suggested_reply")
    intermediate_reply = result.get("intermediate_reply")
    requires_external_search = result.get("requires_external_search")

    # 2. 判斷是否需要介入 [cite: 36, 37, 38, 39, 40]
    if not should_intervene:
        return 

    # 3. 執行發送邏輯 
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        # 情境 A: 需要外部搜尋 (兩段式回覆)
        if requires_external_search and intermediate_reply:
            # 先發送中間回覆
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=intermediate_reply)]
            ))
            # 這裡之後可以接外部查詢邏輯，再發送 suggested_reply
            
        # 情境 B: 直接回覆
        elif suggested_reply:
            line_bot_api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=suggested_reply)]
            ))

if __name__ == "__main__":
    app.run()