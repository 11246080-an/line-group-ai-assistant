from __future__ import annotations

# 這個檔案是給本機測試用的入口。
# 不需要接 LINE Bot，也可以直接在終端機測看看系統判斷結果。

import argparse
import json
from pathlib import Path

from .engine import analyze_dialogue


# 如果有給檔案路徑，就讀檔案；
# 沒有的話，就讓使用者直接在終端機貼上對話。
def _read_input(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")

    print("請貼上群組對話，輸入空白行後按 Ctrl+Z 再 Enter 結束：")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        lines.append(line)
    return "\n".join(lines).strip()


# CLI 主函式。
# 會讀取輸入文字，交給 analyze_dialogue，
# 再把結果印成 JSON。
def main() -> None:
    parser = argparse.ArgumentParser(description="LINE 群組行程助理 prototype")
    parser.add_argument("--input", help="包含群組對話的 UTF-8 文字檔")
    args = parser.parse_args()

    text = _read_input(args.input)
    if not text:
        raise SystemExit("沒有收到群組對話內容。")

    # 這裡直接呼叫專案的統一入口。
    result = analyze_dialogue(text)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
