from __future__ import annotations

import time

from app.engine import analyze_dialogue


def send_line_message(message: str) -> None:
    """Simulate sending a LINE message."""
    print(f"[LINE BOT SEND] {message}")


def handle_group_dialogue(group_text: str) -> None:
    """Example flow for integrating analyze_dialogue into a LINE Bot."""
    result = analyze_dialogue(group_text).to_dict()

    print("[ANALYSIS RESULT]")
    print(result)

    if not result["should_intervene"]:
        print("[LINE BOT] should_intervene = false -> 不回覆")
        return

    if result["requires_external_search"]:
        # Step 1: reply immediately so the group knows the bot is helping.
        send_line_message(result["intermediate_reply"])

        # Step 2: simulate external search work.
        print("[LINE BOT] 模擬外部查詢中...")
        time.sleep(1)

        # Step 3: send the final reply after the search is done.
        send_line_message(result["suggested_reply"])
        return

    # No external search needed: reply directly.
    send_line_message(result["suggested_reply"])


if __name__ == "__main__":
    sample_group_text = """A：現在要不要吃東西
B：好啊
C：附近有什麼
D：不要等太久
A：我有點餓了
B：簡單吃也行
C：最好近一點
D：不要太多人"""

    handle_group_dialogue(sample_group_text)
