from __future__ import annotations

# 這個檔案負責和 OpenAI LLM 溝通。
# 可以把它想成「把資料整理好，交給 AI 模型判斷，
# 再把模型回傳結果整理成固定格式」的地方。

import csv
import json
import os
from pathlib import Path
from typing import Any, Callable

from .knowledge_base import SCENARIOS
from .models import AnalysisResult, ExtractedInfo, ScenarioDefinition


class LLMJudgeError(RuntimeError):
    """當 LLM 路線不能用時，丟出這個錯誤給 engine 處理。"""


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
PROCESSING_REQUIRED_SIGNAL = "這個回答要很久"


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


def _repair_scenario_code(
    raw_scenario_code: Any,
    reply_trigger: str,
    merged_info: dict[str, Any],
) -> str:
    normalized_code = _normalize_scenario_code_value(raw_scenario_code)
    if _find_scenario_definition(normalized_code) is not None:
        return normalized_code

    activity_types = merged_info.get("activity_types") or []
    if not isinstance(activity_types, list):
        activity_types = [activity_types]
    activity_text = " ".join(str(item).strip() for item in activity_types if str(item).strip())

    locations = merged_info.get("location") or []
    if not isinstance(locations, list):
        locations = [locations]
    location_text = " ".join(str(item).strip() for item in locations if str(item).strip())

    budget = merged_info.get("budget") or []
    if not isinstance(budget, list):
        budget = [budget]
    constraints = merged_info.get("constraints") or []
    if not isinstance(constraints, list):
        constraints = [constraints]

    if reply_trigger == "stuck_discussion":
        return "劇本八"

    if reply_trigger in {"explicit_request", "functional_question"}:
        if any(token in activity_text for token in ("行程", "半日", "一日", "動物園參觀")):
            return "劇本四"
        if locations and ("附近" in location_text or budget or constraints):
            return "劇本六"
        return "劇本十六"

    return normalized_code


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


def _looks_like_vague_nearby_query(text: str) -> bool:
    normalized_text = str(text or "").strip().replace(" ", "")
    if not normalized_text:
        return False

    query_signals = (
        "附近有什麼",
        "附近有甚麼",
        "附近有什麼嗎",
        "附近有甚麼嗎",
        "附近有推薦嗎",
        "附近有推薦的嗎",
    )
    if any(signal in normalized_text for signal in query_signals):
        return True

    return "附近" in normalized_text and "有沒有" in normalized_text


def _latest_message_text(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""

    for line in reversed(lines):
        if line.startswith("[") and line.endswith("]"):
            continue
        normalized_line = line
        for prefix in ("使用者：", "使用者:", "A：", "A:", "B：", "B:", "C：", "C:", "D：", "D:", "E：", "E:"):
            if normalized_line.startswith(prefix):
                normalized_line = normalized_line[len(prefix) :].strip()
                break
        if normalized_line:
            return normalized_line

    return lines[-1]


def _normalize_category_reply(text: str) -> str:
    normalized_text = str(text or "").strip().replace(" ", "")
    exact_map = {
        "餐廳": "餐廳",
        "吃的": "餐廳",
        "美食": "餐廳",
        "咖啡廳": "咖啡廳",
        "咖啡店": "咖啡廳",
        "景點": "景點",
        "景點吧": "景點",
        "景點喔": "景點",
        "玩的": "景點",
        "逛街": "購物",
        "百貨公司": "購物",
        "購物": "購物",
        "商圈": "購物",
        "夜市": "購物",
    }
    if normalized_text in exact_map:
        return exact_map[normalized_text]

    keyword_groups = {
        "餐廳": ("吃", "餐廳", "美食", "用餐", "晚餐", "午餐", "宵夜"),
        "咖啡廳": ("咖啡", "咖啡廳", "喝咖啡"),
        "景點": ("景點", "走走", "出去玩", "玩", "散步"),
        "購物": ("逛街", "百貨", "購物", "商圈", "夜市", "可以逛", "能逛", "逛的地方"),
    }
    for category, keywords in keyword_groups.items():
        if any(keyword in normalized_text for keyword in keywords):
            return category

    return ""


def _apply_clarifying_question_override(
    text: str,
    normalized: dict[str, Any],
) -> dict[str, Any]:
    latest_message = _latest_message_text(text)
    requested_category = _normalize_category_reply(latest_message)
    extracted_info = normalized.get("extracted_info") or {}
    locations = extracted_info.get("location") or []
    if not isinstance(locations, list):
        locations = [locations]
    normalized_locations = [str(item).strip() for item in locations if str(item).strip()]
    usable_locations = [
        item
        for item in normalized_locations
        if item not in {"附近", "目前位置", "現在位置", "當前位置"}
    ]

    if requested_category and _looks_like_vague_nearby_query(text):
        if usable_locations:
            normalized["scenario_code"] = "劇本十六"
            normalized["scenario_name"] = "臨時決定（即時需求型）"
            normalized["stage"] = "特殊情境"
            normalized["reply_trigger"] = "functional_question"
            normalized["should_intervene"] = True
            normalized["intervention_type"] = "顯性介入"
            normalized["requires_external_search"] = True
            normalized["intermediate_reply"] = "我先幫你們看一下，等等整理給你們～"
            normalized["suggested_reply"] = ""
            normalized["confidence_score"] = max(
                float(normalized.get("confidence_score", 0.0)),
                0.85,
            )

            evidence = list(normalized.get("evidence") or [])
            evidence.append(
                f"已沿用前文地點「{usable_locations[0]}」，並補上查詢類型為{requested_category}。"
            )
            normalized["evidence"] = evidence

            behavior = list(normalized.get("system_behavior") or [])
            behavior.extend(["沿用前文地點", "依補充的查詢類型啟動外部查詢"])
            normalized["system_behavior"] = behavior

            extracted_info["activity_types"] = [requested_category]
            normalized["extracted_info"] = extracted_info
            return normalized

        normalized["scenario_code"] = "劇本十六"
        normalized["scenario_name"] = "臨時決定（即時需求型）"
        normalized["stage"] = "特殊情境"
        normalized["reply_trigger"] = "functional_question"
        normalized["should_intervene"] = True
        normalized["intervention_type"] = "顯性介入"
        normalized["requires_external_search"] = True
        normalized["intermediate_reply"] = "我先幫你們看一下，等等整理給你們～"
        normalized["suggested_reply"] = ""
        normalized["confidence_score"] = max(
            float(normalized.get("confidence_score", 0.0)),
            0.85,
        )

        evidence = list(normalized.get("evidence") or [])
        evidence.append(
            f"使用者已補充查詢類型為{requested_category}，且未提供其他地點，因此沿用目前位置作為附近查詢基準。"
        )
        normalized["evidence"] = evidence

        behavior = list(normalized.get("system_behavior") or [])
        behavior.extend(["確認查詢類型", "預設以目前位置作為附近查詢基準"])
        normalized["system_behavior"] = behavior

        extracted_info["location"] = ["目前位置"]
        extracted_info["activity_types"] = [requested_category]
        normalized["extracted_info"] = extracted_info
        return normalized

    if not _looks_like_vague_nearby_query(text):
        return normalized

    activity_types = extracted_info.get("activity_types") or []
    constraints = extracted_info.get("constraints") or []

    if any((locations, activity_types, constraints)):
        return normalized

    normalized["scenario_code"] = "劇本十六"
    normalized["scenario_name"] = "臨時決定（即時需求型）"
    normalized["stage"] = "特殊情境"
    normalized["reply_trigger"] = "functional_question"
    normalized["should_intervene"] = True
    normalized["intervention_type"] = "顯性介入"
    normalized["requires_external_search"] = False
    normalized["intermediate_reply"] = ""
    normalized["suggested_reply"] = "你們比較想找吃的，還是想找附近可以逛的地方？"
    normalized["confidence_score"] = max(
        float(normalized.get("confidence_score", 0.0)),
        0.8,
    )

    evidence = list(normalized.get("evidence") or [])
    evidence.append("使用者有附近查詢意圖，但目前仍缺少查詢類型，因此先補問需求方向。")
    normalized["evidence"] = evidence

    behavior = list(normalized.get("system_behavior") or [])
    behavior.extend(["先補問需求類型", "將附近預設理解為目前位置附近"])
    normalized["system_behavior"] = behavior

    normalized["suggested_reply"] = "你們比較想找吃的，還是想找附近可以逛的地方？"
    return normalized


def _normalize_result(
    data: dict[str, Any],
    fallback_info: ExtractedInfo,
    source_text: str,
) -> AnalysisResult:
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

    repaired_scenario_code = _repair_scenario_code(
        data["scenario_code"],
        reply_trigger,
        merged_info,
    )

    normalized = {
        "scenario_code": repaired_scenario_code,
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
            repaired_scenario_code,
            bool(should_intervene),
        ),
        "extracted_info": merged_info,
    }
    normalized = _apply_clarifying_question_override(source_text, normalized)
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
- 這一階段不要生成最終答案，suggested_reply 一律輸出空字串。
- 請先評估後續回答是否需要查外部資料、呼叫 API、整理多個結果，或需要較長時間思考。
- 若預估會耗時，intermediate_reply 必須只輸出固定字串「這個回答要很久」，不可增加其他文字。
- 若可立即完成，intermediate_reply 輸出空字串。
- 若群組對話仍處於剛開始討論階段，成員還在提出初步想法、補充條件或交換意見，應優先持續觀察，不要太早介入。
- functional_question 雖表示存在功能性需求，但若對話仍在早期發散階段，且只出現單一模糊問題、缺少明確條件時，才應優先判定為 no_reply。
- 若對話中已累積 2 個以上明確條件，例如時間、地點、預算、人數、飲食限制、活動偏好，且又出現查詢、推薦、比較、安排、或「有沒有適合...」這類功能性提問，應優先判定為 functional_question，不應繼續判為 no_reply。
- 若情境是在詢問符合條件的餐廳、景點、行程、路線、天氣、電影場次或其他候選選項，即使使用者沒有直接說「幫我」，只要需求已具體，也應視為 functional_question。
- 若使用者是在問「有沒有適合 4 個人一起吃的店」、「學校附近有沒有預算 400 內的餐廳」這類已具備多個限制條件的問題，should_intervene 應為 true，requires_external_search 應為 true。
- 若使用者是直接詢問某個明確地點附近的餐廳、咖啡廳、美食或景點，例如「北車附近有什麼可以吃」「西門町附近有沒有咖啡廳」，即使尚未提供時間、預算或人數，也應視為 functional_question，因為這已經是可執行的查詢需求。
- 若對話已包含明確地點，且問題本身是在詢問「附近有什麼」「有沒有推薦」「有沒有某種類型的店」這類內容，requires_external_search 應為 true，不應判為一般聊天。

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


def _build_generation_messages(
    text: str,
    judgment: AnalysisResult,
    *,
    user_was_notified: bool = False,
) -> list[dict[str, str]]:
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
- 若劇本是「劇本四／自動行程生成」，且使用者已明確要求排半日行程、一日行程或行程草案，suggested_reply 應直接給出一版可貼進群組的行程安排，不要再反問「要不要先看一版」。
- 對於行程生成類回覆，至少要包含時間順序、主要活動或景點安排；若資訊不足，可以用「早上／中午／下午」這種程度給出初步草案。
- 口氣要自然、簡短、適合 LINE 群組。
""".strip()

    processing_instruction = (
        "後端已提醒使用者需要稍候，現在請開始查詢、思考並產生最終答案。"
        if user_was_notified
        else "請直接產生最終答案。"
    )

    user_prompt = f"""
以下是目前整段群組對話：
{text}

以下是上一階段的判斷結果：
{json.dumps(judgment.to_dict(), ensure_ascii=False, indent=2)}

{processing_instruction}

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


def judge_with_llm(
    text: str,
    extracted_info: ExtractedInfo,
    *,
    on_processing_required: Callable[[str], None] | None = None,
) -> AnalysisResult:
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
    judgment_result = _normalize_result(judgment_data, extracted_info, text)

    if not judgment_result.should_intervene:
        return judgment_result

    raw_processing_signal = str(judgment_data.get("intermediate_reply") or "").strip()
    processing_required = (
        raw_processing_signal == PROCESSING_REQUIRED_SIGNAL
        or judgment_result.requires_external_search
        or judgment_result.scenario_code == "劇本五"
    )
    user_was_notified = False
    if processing_required and on_processing_required is not None:
        on_processing_required(PROCESSING_REQUIRED_SIGNAL)
        user_was_notified = True

    generation_messages = _build_generation_messages(
        text,
        judgment_result,
        user_was_notified=user_was_notified,
    )
    generated_reply = _call_openai_json(
        client,
        model,
        generation_messages,
        purpose="回覆生成",
    )
    return _merge_generated_reply(judgment_result, generated_reply)
