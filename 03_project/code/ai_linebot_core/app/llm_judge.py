from __future__ import annotations

# 這個檔案負責和 OpenAI LLM 溝通。
# 可以把它想成「把資料整理好，交給 AI 模型判斷，
# 再把模型回傳結果整理成固定格式」的地方。

import csv
import json
import os
from pathlib import Path
from typing import Any

from app.knowledge_base import SCENARIOS
from app.models import AnalysisResult, ExtractedInfo


class LLMJudgeError(RuntimeError):
    """當 LLM 路線不能用時，丟出這個錯誤給 engine 處理。"""


ROOT_DIR = Path(__file__).resolve().parent.parent


# 讀取 .env 檔，把 API key 之類的設定放進環境變數。
def _load_env_file() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# 有些文字檔可能不是同一種編碼存出來的。
# 這個函式會依序嘗試多種常見編碼，避免讀檔直接失敗。
def _load_text_file(filename: str) -> str:
    path = ROOT_DIR / filename
    encodings = ("utf-8-sig", "utf-8", "utf-16", "cp950")
    last_error: UnicodeDecodeError | None = None

    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise LLMJudgeError(
            f"無法使用支援的編碼讀取檔案 {filename}: {last_error}"
        ) from last_error

    raise LLMJudgeError(f"無法讀取檔案 {filename}")


# 讀取標準答案 CSV，整理成一段簡短文字，
# 讓 LLM 了解 17 個劇本大致應該怎麼判。
def _load_standard_answer_summaries() -> str:
    path = ROOT_DIR / "standard_answers.csv"
    rows: list[str] = []
    with path.open(encoding="utf-8", newline="") as handle:
        raw_rows = list(csv.reader(handle))

    header_index = next(
        (index for index, row in enumerate(raw_rows) if row and row[0] == "劇本編號"),
        None,
    )
    if header_index is None:
        raise LLMJudgeError("無法解析 standard_answers.csv 標題列")

    header = raw_rows[header_index]
    for row_values in raw_rows[header_index + 1 :]:
        if not row_values or not row_values[0].strip():
            continue
        row = dict(zip(header, row_values))
        code = row["劇本編號"].strip()
        name = row["劇本名稱"].strip()
        stage = row["情境類型"].strip()
        intervene = row["是否應介入"].strip()
        intervention_type = row["介入類型"].strip()
        basis = row["判斷依據"].strip()
        behavior = row["建議系統行為"].strip()
        reply = row["建議回應"].strip()
        rows.append(
            f"- {code}｜{name}｜{stage}｜是否介入:{intervene}｜介入類型:{intervention_type}"
            f"｜判斷依據:{basis}｜建議系統行為:{behavior}｜建議回應:{reply or '空字串'}"
        )
    return "\n".join(rows)


# 把 knowledge base 裡的劇本定義整理成 prompt 文字。
# 這樣 LLM 不只是看原始對話，還會一起參考劇本資料。
def _scenario_context() -> str:
    lines: list[str] = []
    for scenario in SCENARIOS:
        lines.append(
            f"- {scenario.code}｜{scenario.name}｜{scenario.stage}"
            f"｜should_intervene={scenario.should_intervene}"
            f"｜intervention_type={scenario.intervention_type}"
            f"｜system_behavior={','.join(scenario.system_behavior)}"
            f"｜suggested_reply={scenario.suggested_reply or '空字串'}"
        )
    return "\n".join(lines)


# LLM 有時會回傳多餘文字或 Markdown。
# 這個函式負責把真正的 JSON 內容挖出來。
def _extract_json(text: str) -> dict[str, Any]:
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
        raise LLMJudgeError("LLM 未回傳可解析的 JSON 內容")

    try:
        return json.loads(payload[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMJudgeError(f"LLM JSON 解析失敗: {exc}") from exc


# LLM 有時候會把清單欄位回成：
# - 真正的 list
# - 一整串字串
# - 用逗號、頓號、換行分開的字串
# 這個函式會把它整理成一致的「字串列表」。
def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if isinstance(value, list):
        normalized: list[str] = []
        for item in value:
            item_text = str(item).strip()
            if item_text:
                normalized.append(item_text)
        return normalized

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []

        separators = ("、", "，", ",", "\n", "\r\n", ";", "；")
        parts = [text]
        for separator in separators:
            next_parts: list[str] = []
            for part in parts:
                next_parts.extend(part.split(separator))
            parts = next_parts

        normalized = [part.strip() for part in parts if part.strip()]
        if normalized:
            return normalized

        return [text]

    raise LLMJudgeError(f"LLM 輸出的 {field_name} 格式不正確")


# intermediate_reply 是查資料前先回的一句話。
# 這個函式會把語氣調整得像 LINE 群組聊天，
# 避免出現太像系統訊息的句子。
def _normalize_intermediate_reply(value: Any, requires_external_search: bool) -> str:
    if not requires_external_search:
        return ""

    text = str(value or "").strip()
    if not text:
        return "我先幫你們看一下，等等整理給你們～"

    banned_phrases = (
        "正在查詢",
        "系統處理中",
        "系統正在",
        "正在處理",
        "查詢中",
    )
    if any(phrase in text for phrase in banned_phrases):
        return "我先幫你們看一下，等等整理給你們～"

    # Keep the reply short and conversational for LINE-style group chat.
    return text


# 把 LLM 回傳的資料檢查、補齊、整理成 AnalysisResult。
# 這一步很重要，因為它保證輸出格式固定。
def _normalize_result(data: dict[str, Any], fallback_info: ExtractedInfo) -> AnalysisResult:
    required_fields = {
        "scenario_code",
        "scenario_name",
        "stage",
        "should_intervene",
        "reply_trigger",
        "intervention_type",
        "confidence_score",
        "evidence",
        "system_behavior",
        "requires_external_search",
        "intermediate_reply",
        "suggested_reply",
        "extracted_info",
    }
    missing = required_fields - set(data)
    if missing:
        raise LLMJudgeError(f"LLM 輸出缺少必要欄位: {sorted(missing)}")

    # 如果 LLM 漏掉某些 extracted_info 欄位，
    # 就先用 extractor 先前抓到的資料補上。
    raw_info = data.get("extracted_info") or {}
    merged_info = fallback_info.to_dict()
    if isinstance(raw_info, dict):
        merged_info.update(raw_info)

    should_intervene = data["should_intervene"]
    if isinstance(should_intervene, str):
        should_intervene = should_intervene.strip().lower() in {"true", "1", "yes", "是"}

    requires_external_search = data["requires_external_search"]
    if isinstance(requires_external_search, str):
        requires_external_search = requires_external_search.strip().lower() in {
            "true",
            "1",
            "yes",
            "是",
        }

    normalized = {
        "scenario_code": str(data["scenario_code"]),
        "scenario_name": str(data["scenario_name"]),
        "stage": str(data["stage"]),
        "should_intervene": bool(should_intervene),
        "reply_trigger": str(data["reply_trigger"]),
        "intervention_type": str(data["intervention_type"]),
        "confidence_score": float(data["confidence_score"]),
        "evidence": _normalize_string_list(data["evidence"], "evidence"),
        "system_behavior": _normalize_string_list(data["system_behavior"], "system_behavior"),
        "requires_external_search": bool(requires_external_search),
        "intermediate_reply": _normalize_intermediate_reply(
            data.get("intermediate_reply", ""),
            bool(requires_external_search),
        ),
        "suggested_reply": str(data.get("suggested_reply", "")),
        "extracted_info": merged_info,
    }
    return AnalysisResult.from_dict(normalized)


# 建立要送給 LLM 的 prompt。
# 內容包含：
# - 原始對話
# - extractor 摘要資訊
# - AI 判斷邏輯
# - 17 劇本定義
# - 標準答案摘要
# 目的是讓 LLM 根據整體語意判斷，而不是只看關鍵字。
def _build_messages(text: str, extracted_info: ExtractedInfo) -> list[dict[str, str]]:
    prompt_template = _load_text_file("prompt_templates.txt")
    ai_logic = _load_text_file("ai_logic.txt")
    standard_answers = _load_standard_answer_summaries()
    scenarios = _scenario_context()

    system_prompt = f"""
你是「LINE 群組行程助理」的情境判斷核心模組。
你的主要任務不是看關鍵字，而是根據整體語意、對話進展、使用者之間的互動、已抽取摘要資訊，
並參考 17 個劇本定義、AI 判斷邏輯與標準答案風格，判斷目前最符合哪一個劇本。

請嚴格輸出 JSON，不要輸出 Markdown，不要加註解，不要補充多餘說明。
不可捏造對話中不存在的資訊；若資訊不足，保留空陣列、空字串，或沿用摘要中的已知值。

輸出 JSON 必須包含以下欄位：
- scenario_code
- scenario_name
- stage
- should_intervene
- reply_trigger
- intervention_type
- confidence_score
- evidence
- system_behavior
- requires_external_search
- intermediate_reply
- suggested_reply
- extracted_info


規則補充：
- 若情境需要查詢外部資訊，例如附近餐廳、電影場次、天氣、餐廳推薦、路線或交通查詢，requires_external_search 必須為 true。
- 若 requires_external_search = true，intermediate_reply 必須先提供一句自然的 LINE 群組回覆，表示 AI 正在查詢中。
- suggested_reply 則是查詢完成後的正式回覆。
- 若情境不需要外部查詢，例如討論停滯、投票決策、時間衝突提醒，requires_external_search 應為 false，intermediate_reply 應為空字串。
- intermediate_reply 的語氣要像 LINE 群組聊天，必須自然、口語、簡短。
- 可以使用「我先幫你們...」、「等等整理給你們」、「稍等一下」這類說法。
- 避免使用「我正在查詢」、「正在處理中」、「系統處理中」等機械式語句。
- 可以適度使用「～」，但不要太多。
- reply_trigger 只能是以下其中一種：
  - explicit_request：使用者明確向 AI 求助、要求幫忙、要求整理、要求推薦
  - functional_question：使用者提出具有功能性的問題，例如查詢、推薦、規劃、比較、排序
  - stuck_discussion：群組討論明顯卡住，成員反覆出現「都可以」、「隨便」、「沒意見」、「你們決定」等附和語句，且沒有新增具體選項、條件或決策方向，對話仍無法推進時
  - no_reply：一般聊天、寒暄、附和、閒聊、情緒反應，或尚未形成明確需求時
- 若對話中已明確表現出「還是沒決定」、「還沒想法」、「不知道怎麼選」等無法收斂的語意，應優先判定為 stuck_discussion，而非 no_reply。
- 如果 reply_trigger = no_reply，則 should_intervene 必須為 false。
- 對於一般聊天、附和、寒暄、情緒反應、單純延續話題但未形成明確需求的訊息，應優先判定為 no_reply，不應主動回覆。



其中 extracted_info 必須是物件，欄位包含：
- time
- location
- people_count
- budget
- constraints
- activity_types
- options
- decision_state
- risk_info
- need_type

參考資料：
[Prompt 風格]
{prompt_template}

[AI 判斷邏輯]
{ai_logic}

[17 劇本定義]
{scenarios}

[17 劇本標準答案摘要]
{standard_answers}
""".strip()

    user_prompt = f"""
以下是要判斷的群組對話：
{text}

以下是前處理抽取出的摘要資訊：
{json.dumps(extracted_info.to_dict(), ensure_ascii=False, indent=2)}

請依據整體語意、對話進展與摘要資訊，輸出固定 JSON。
""".strip()

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


# 這是 LLM 判斷的主要函式。
# engine.py 會先呼叫它；如果失敗，才會改走 fallback classifier。
def judge_with_llm(text: str, extracted_info: ExtractedInfo) -> AnalysisResult:
    # 先載入 .env 設定，讓程式可以讀到 API key。
    _load_env_file()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMJudgeError("未設定 OPENAI_API_KEY")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMJudgeError("未安裝 openai 套件") from exc

    # 建立 OpenAI client，準備呼叫模型。
    client = OpenAI(api_key=api_key)
    messages = _build_messages(text, extracted_info)

    try:
        # 把整理好的 prompt 丟給 LLM，
        # 請它直接回固定格式的 JSON。
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # pragma: no cover - network/sdk path
        raise LLMJudgeError(f"OpenAI 呼叫失敗: {exc}") from exc

    # 拿到模型回應後，先找出 JSON，再整理成固定結果格式。
    content = response.choices[0].message.content or ""
    data = _extract_json(content)
    return _normalize_result(data, extracted_info)
