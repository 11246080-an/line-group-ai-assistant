from __future__ import annotations

# 這個檔案放的是「手動測試案例」。
# 用來快速確認系統在幾種典型情境下的輸出是否合理。

import json

from app.engine import analyze_dialogue


# 這裡先準備幾段代表性的群組對話。
CASES = {
    "case_4": """A：目前時間、地點跟人數是不是差不多都定好了
B：對啊，現在應該可以開始排一下行程了
C：如果是去淡水的話，可以先想一下要去哪些地方
D：而且吃飯也可以一起排進去
A：那我們可以先排個大概順序
B：好啊，有個初步順序之後，我們再看要不要微調""",
    "case_10": """A：那晚餐約七點
B：可以啊
C：OK，那八點看電影嗎
D：這樣會不會太趕
A：吃飯可能不只一小時
B：對，移動也要時間
C：而且還要提前進場""",
    "case_16": """A：現在要不要吃東西
B：好啊
C：附近有什麼
D：不要等太久
A：先找幾個選項
B：好，快點決定""",
}


# 測試主函式。
# 逐一把每段案例丟進 analyze_dialogue，
# 再把結果印出來看。
def main() -> None:
    for name, text in CASES.items():
        result = analyze_dialogue(text)
        print(f"=== {name} ===")
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
