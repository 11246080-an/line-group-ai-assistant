from __future__ import annotations

# 這個檔案負責和 OpenAI LLM 溝通。
# 可以把它想成「把資料整理好，交給 AI 模型判斷，
# 再把模型回傳結果整理成固定格式」的地方。

import csv
import json
import os
from pathlib import Path
from typing import Any

from .knowledge_base import SCENARIOS
from .models import AnalysisResult, ExtractedInfo, ScenarioDefinition


class LLMJudgeError(RuntimeError):
    """當 LLM 路線不能用時，丟出這個錯誤給 engine 處理。"""


ROOT_DIR = Path(__file__).resolve().parent.parent.parent


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


def _find_scenario_definition(scenario_code: str) -> ScenarioDefinition | None:
    normalized_code = _normalize_scenario_code_value(scenario_code)
    for scenario in SCENARIOS:
        if scenario.code == normalized_code:
            return scenario
    return None


def _normalize_scenario_code_value(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return text

    for scenario in SCENARIOS:
        if text in {scenario.code, scenario.name}:
            return scenario.code

    compact = text.replace(" ", "").replace("　", "").replace("劇本", "")
    if compact.isdigit():
        index = int(compact) - 1
        if 0 <= index < len(SCENARIOS):
            return SCENARIOS[index].code

    chinese_number_map = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
        "十三": 13,
        "十四": 14,
        "十五": 15,
        "十六": 16,
        "十七": 17,
    }
    if compact in chinese_number_map:
        index = chinese_number_map[compact] - 1
        if 0 <= index < len(SCENARIOS):
            return SCENARIOS[index].code

    return text


def _default_suggested_reply_for_scenario(scenario_code: str) -> str:
    scenario = _find_scenario_definition(scenario_code)
    if scenario is None:
        return ""
    return scenario.suggested_reply


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

    return text


def _normalize_suggested_reply(value: Any, scenario_code: str, should_intervene: bool) -> str:
    if not should_intervene:
        return ""

    text = str(value or "").strip()
    if text:
        return text

    return _default_suggested_reply_for_scenario(scenario_code)


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

    raw_info = data.get("extracted_info") or {}
    merged_info = fallback_info.to_dict()
    if isinstance(raw_info, dict):
        merged_info.update(raw_info)

    reply_trigger = str(data["reply_trigger"]).strip()

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

    if reply_trigger == "no_reply":
        should_intervene = False
    elif reply_trigger in {"explicit_request", "functional_question", "stuck_discussion"}:
        should_intervene = True

    normalized = {
        "scenario_code": _normalize_scenario_code_value(data["scenario_code"]),
        "scenario_name": str(data["scenario_name"]),
        "stage": str(data["stage"]),
        "should_intervene": bool(should_intervene),
        "reply_trigger": reply_trigger,
        "intervention_type": str(data["intervention_type"]),
        "confidence_score": float(data["confidence_score"]),
        "evidence": _normalize_string_list(data["evidence"], "evidence"),
        "system_behavior": _normalize_string_list(data["system_behavior"], "system_behavior"),
        "requires_external_search": bool(requires_external_search),
        "intermediate_reply": _normalize_intermediate_reply(
            data.get("intermediate_reply", ""),
            bool(requires_external_search),
        ),
        "suggested_reply": _normalize_suggested_reply(
            data.get("suggested_reply", ""),
            _normalize_scenario_code_value(data["scenario_code"]),
            bool(should_intervene),
        ),
        "extracted_info": merged_info,
    }
    return AnalysisResult.from_dict(normalized)


def _build_judgment_messages(text: str, extracted_info: ExtractedInfo) -> list[dict[str, str]]:
    ai_logic = _load_text_file("ai_logic.txt")
    scenarios = _scenario_context()
    standard_answers = _load_standard_answer_summaries()

    system_prompt = f"""
你是「LINE 群組行程助理」的情境判斷核心模組。
你的工作是根據整段群組對話、對話進展、多人互動方式與摘要資訊，
判斷目前最符合哪一個劇本，以及 AI 是否需要介入。

這一階段只負責「判斷」，不負責最終回覆生成。
所以在這一階段：
- intermediate_reply 一律輸出空字串
- suggested_reply 一律輸出空字串

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

reply_trigger 必須是以下其中一種：
- explicit_request：使用者明確向 AI 求助、要求幫忙、要求整理、要求推薦
- functional_question：使用者提出具有功能性的問題，例如查詢、推薦、規劃、比較、排序
- stuck_discussion：群組討論明顯卡住，成員反覆出現「都可以」、「隨便」、「沒意見」、「你們決定」等附和語句，且沒有新增具體選項、條件或決策方向，對話仍無法推進時
- no_reply：一般聊天、寒暄、附和、閒聊、情緒反應，或尚未形成明確需求時

重要規則：
- 如果 reply_trigger = no_reply，則 should_intervene 必須為 false。
- 如果 reply_trigger 是 explicit_request、functional_question 或 stuck_discussion，則 should_intervene 必須為 true。
- 如果 reply_trigger 是 explicit_request、functional_question 或 stuck_discussion，就不應選擇原本 should_intervene = false 的劇本。
- 對於一般聊天、附和、寒暄、情緒反應、單純延續話題但未形成明確需求的訊息，應優先判定為 no_reply，不應主動回覆。
- 若使用者是在詢問資訊、選項、推薦、比較或安排方式，但沒有直接以「幫我」、「麻煩你」、「你幫我」等語句要求 AI 執行動作，應優先判定為 functional_question，而非 explicit_request。
- 若情境需要查詢外部資訊，例如附近餐廳、電影場次、天氣、餐廳推薦、路線或交通查詢，requires_external_search 必須為 true。
- 若情境不需要外部查詢，例如討論停滯、投票決策、時間衝突提醒，requires_external_search 應為 false。
- 這一階段不要生成 intermediate_reply 或 suggested_reply，兩者皆輸出空字串。

extracted_info 欄位必須包含以下欄位：
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

以下是目前可用的摘要資訊（若無則可能為空）：
{json.dumps(extracted_info.to_dict(), ensure_ascii=False, indent=2)}

請優先依據整段對話脈絡進行判斷，再把可用摘要資訊當作輔助參考，輸出固定 JSON。
""".strip()

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _build_generation_messages(text: str, judgment: AnalysisResult) -> list[dict[str, str]]:
    scenario = _find_scenario_definition(judgment.scenario_code)
    if scenario is None:
        raise LLMJudgeError(f"找不到對應劇本定義：{judgment.scenario_code}")

    is_active_intervention = (
        judgment.intervention_type == "顯性介入"
        or judgment.reply_trigger in {"explicit_request", "stuck_discussion"}
    )
    intervention_mode = "主動介入" if is_active_intervention else "被動回應"

    if is_active_intervention:
        behavior_instruction = """
你現在要用「主動介入」方式生成回覆。
- 主動介入代表你可以主動整理方向、縮小選項、推進討論。
- 但不要替群組直接做最後決定，要保留選擇空間。
- 回覆要像群組助理，不要像客服或公告系統。
""".strip()
    else:
        behavior_instruction = """
你現在要用「被動回應」方式生成回覆。
- 被動回應代表你主要是在回答眼前問題，不要搶主導權。
- 以補充資訊、提供候選選項、回應使用者需求為主。
- 回覆要自然、簡短、像 LINE 群組中的助理。
""".strip()

    if judgment.requires_external_search:
        output_instruction = """
此情境需要外部查詢。
- intermediate_reply 必須是一句很短、自然、口語的群組回覆，表示 AI 先幫忙查。
- suggested_reply 則是查完資料後要貼進群組的正式回覆。
""".strip()
    else:
        output_instruction = """
此情境不需要外部查詢。
- intermediate_reply 必須輸出空字串。
- suggested_reply 必須直接給出可貼進 LINE 群組的正式回覆。
""".strip()

    system_prompt = f"""
你是「LINE 群組行程助理」的回覆生成模組。
你現在處理的劇本是：
- 劇本代碼：{scenario.code}
- 劇本名稱：{scenario.name}
- 劇本階段：{scenario.stage}
- 預設介入方式：{scenario.intervention_type}
- 劇本建議行為：{", ".join(scenario.system_behavior)}
- 劇本預設建議回覆：{scenario.suggested_reply or "空字串"}
- 目前介入模式：{intervention_mode}

{behavior_instruction}

{output_instruction}

請嚴格輸出 JSON，不要輸出 Markdown，不要加註解。
輸出欄位只需要：
- intermediate_reply
- suggested_reply

回覆要求：
- 必須結合目前群組對話脈絡，不可憑空捏造不存在的資訊。
- 必須符合這個劇本的任務，不同劇本的回覆角度要不同。
- 若是討論卡住，重點是幫忙整理方向或縮小選項。
- 若是功能性問題，重點是回應問題本身，不要太像硬插話。
- 若是明確求助，重點是直接幫忙處理需求。
- 口氣要自然、簡短、適合 LINE 群組。
""".strip()

    user_prompt = f"""
以下是目前整段群組對話：
{text}

以下是上一階段的判斷結果：
{json.dumps(judgment.to_dict(), ensure_ascii=False, indent=2)}

請根據這個劇本與介入方式，生成最終回覆 JSON。
""".strip()

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _call_openai_json(
    client: Any,
    model: str,
    messages: list[dict[str, str]],
    *,
    purpose: str,
) -> dict[str, Any]:
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # pragma: no cover - network/sdk path
        raise LLMJudgeError(f"OpenAI {purpose} 失敗: {exc}") from exc

    content = response.choices[0].message.content or ""
    return _extract_json(content)


def _merge_generated_reply(
    judgment: AnalysisResult,
    generated_reply: dict[str, Any],
) -> AnalysisResult:
    merged = judgment.to_dict()
    merged["intermediate_reply"] = _normalize_intermediate_reply(
        generated_reply.get("intermediate_reply", ""),
        judgment.requires_external_search,
    )
    merged["suggested_reply"] = _normalize_suggested_reply(
        generated_reply.get("suggested_reply", ""),
        judgment.scenario_code,
        judgment.should_intervene,
    )
    return AnalysisResult.from_dict(merged)


def judge_with_llm(text: str, extracted_info: ExtractedInfo) -> AnalysisResult:
    _load_env_file()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMJudgeError("未設定 OPENAI_API_KEY")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMJudgeError("未安裝 openai 套件") from exc

    client = OpenAI(api_key=api_key)

    judgment_messages = _build_judgment_messages(text, extracted_info)
    judgment_data = _call_openai_json(
        client,
        model,
        judgment_messages,
        purpose="情境判斷",
    )
    judgment_result = _normalize_result(judgment_data, extracted_info)

    if not judgment_result.should_intervene:
        return judgment_result

    generation_messages = _build_generation_messages(text, judgment_result)
    generated_reply = _call_openai_json(
        client,
        model,
        generation_messages,
        purpose="回覆生成",
    )
    return _merge_generated_reply(judgment_result, generated_reply)
