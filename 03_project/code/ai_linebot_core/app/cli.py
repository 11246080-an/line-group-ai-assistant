from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.engine import analyze_dialogue


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


def main() -> None:
    parser = argparse.ArgumentParser(description="LINE 群組行程助理 prototype")
    parser.add_argument("--input", help="包含群組對話的 UTF-8 文字檔")
    args = parser.parse_args()

    text = _read_input(args.input)
    if not text:
        raise SystemExit("沒有收到群組對話內容。")

    result = analyze_dialogue(text)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
