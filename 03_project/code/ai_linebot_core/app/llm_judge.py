from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from app.knowledge_base import SCENARIOS
from app.models import AnalysisResult, ExtractedInfo


class LLMJudgeError(RuntimeError):
    """Raised when the LLM judgment path is unavailable or invalid."""


ROOT_DIR = Path(__file__).resolve().parent.parent


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


def _normalize_result(data: dict[str, Any], fallback_info: ExtractedInfo) -> AnalysisResult:
    required_fields = {
        "scenario_code",
        "scenario_name",
        "stage",
        "should_intervene",
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


def judge_with_llm(text: str, extracted_info: ExtractedInfo) -> AnalysisResult:
    _load_env_file()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise LLMJudgeError("未設定 GROQ_API_KEY")

    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    try:
        from groq import Groq
    except ImportError as exc:
        raise LLMJudgeError("未安裝 groq 套件") from exc

    client = Groq(api_key=api_key)
    messages = _build_messages(text, extracted_info)

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=messages,
        )
    except Exception as exc:  # pragma: no cover - network/sdk path
        raise LLMJudgeError(f"Groq 呼叫失敗: {exc}") from exc

    content = response.choices[0].message.content or ""
    data = _extract_json(content)
    return _normalize_result(data, extracted_info)
