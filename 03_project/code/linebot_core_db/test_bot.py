"""
本機模擬測試：直接呼叫 handle_message 邏輯，不需要 LINE 或 ngrok。
執行方式：python test_bot.py
"""
import sys, json
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from db import ensure_indexes, get_db, save_message, save_summary, upsert_group, upsert_member
from ai_linebot_core.app.engine import analyze_dialogue

# ── 測試設定 ──────────────────────────────────────────────────
GROUP_ID = "C_local_test_001"
USER_ID  = "U_local_test_001"

TEST_MESSAGES = [
    "大家好，我想規劃一個日本旅遊",
    "我希望去京都和大阪",
    "預算大概每人三萬台幣",
    "日期想排在七月中旬",
    "大家覺得這樣可以嗎？",
]

# ── 初始化 ────────────────────────────────────────────────────
ensure_indexes()
print("=" * 55)
print("  本機模擬測試開始")
print("=" * 55)

upsert_group(GROUP_ID)
upsert_member(GROUP_ID, USER_ID, "測試用戶")

context_lines = []

for i, msg in enumerate(TEST_MESSAGES, 1):
    print(f"\n[{i}] 使用者：{msg}")

    # 1. 存訊息到 DB
    save_message(GROUP_ID, USER_ID, msg)
    context_lines.append(msg)
    context_text = "\n".join(context_lines)

    # 2. AI 分析
    result_obj = analyze_dialogue(context_text)
    result = result_obj.to_dict()

    should_intervene = result.get("should_intervene", False)
    confidence      = result.get("confidence_score", 0)
    scenario        = result.get("scenario_code", "")
    reply           = result.get("suggested_reply", "")

    # 存分析結果
    save_summary(GROUP_ID, result)

    print(f"    scenario={scenario}  confidence={confidence}  intervene={should_intervene}")
    if should_intervene and reply:
        print(f"    Bot reply: {reply}")
    else:
        print(f"    Bot: no intervention")

# ── 確認 DB 寫入 ──────────────────────────────────────────────
db = get_db()
msg_count = db.messages.count_documents({"line_group_id": GROUP_ID})
sum_count  = db.summaries.count_documents({"line_group_id": GROUP_ID})

print("\n" + "=" * 55)
print(f"  DB 寫入確認")
print(f"  messages  : {msg_count} 筆")
print(f"  summaries : {sum_count} 筆")
print("=" * 55)
print("測試完成！")
