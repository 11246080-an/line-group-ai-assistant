from __future__ import annotations

# 這個檔案是示範「之後接 LINE Bot 可以怎麼寫」。
# 它不是真的 webhook，只是用最簡單的方式模擬流程。

import time

from app.engine import analyze_dialogue


# 這裡用 print 來模擬 LINE Bot 發訊息。
# 之後如果真的接 LINE Bot，同學可以把這裡改成 LINE SDK 的回訊息方法。
def send_line_message(message: str) -> None:
    """Simulate sending a LINE message."""
    print(f"[LINE BOT SEND] {message}")


# 模擬收到一段群組對話後，LINE Bot 應該怎麼處理。
def handle_group_dialogue(group_text: str) -> None:
    """Example flow for integrating analyze_dialogue into a LINE Bot."""
    # 第一步：把對話丟給判斷核心。
    result = analyze_dialogue(group_text).to_dict()

    print("[ANALYSIS RESULT]")
    print(result)

    # 如果系統判斷不該介入，就什麼都不回。
    if not result["should_intervene"]:
        print("[LINE BOT] should_intervene = false -> 不回覆")
        return

    if result["requires_external_search"]:
        # 如果需要外部查資料，先回一句過渡訊息，
        # 讓群組知道 AI 正在幫忙處理。
        # Step 1: reply immediately so the group knows the bot is helping.
        send_line_message(result["intermediate_reply"])

        # 這裡先用 sleep 模擬查詢花掉的時間。
        # Step 2: simulate external search work.
        print("[LINE BOT] 模擬外部查詢中...")
        time.sleep(1)

        # 查完之後，再送出正式回覆。
        # Step 3: send the final reply after the search is done.
        send_line_message(result["suggested_reply"])
        return

    # 如果不需要外部查詢，代表可以直接正式回覆。
    # No external search needed: reply directly.
    send_line_message(result["suggested_reply"])


if __name__ == "__main__":
    # 這裡放一段簡單的測試對話，方便直接執行看流程。
    sample_group_text = """A：現在要不要吃東西
B：好啊
C：附近有什麼
D：不要等太久
A：我有點餓了
B：簡單吃也行
C：最好近一點
D：不要太多人"""

    handle_group_dialogue(sample_group_text)
