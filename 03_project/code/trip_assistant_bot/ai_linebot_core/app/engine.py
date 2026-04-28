from __future__ import annotations

from .classifier import classify
from .extractor import extract_info
from .models import AnalysisResult
from .llm_judge import LLMJudgeError, judge_with_llm


EXTERNAL_SEARCH_SCENARIOS = {
    "劇本五",
    "劇本六",
    "劇本十一",
    "劇本十二",
    "劇本十三",
    "劇本十四",
    "劇本十六",
    "劇本十七",
}


def _derive_external_search_fields(
    scenario_code: str,
    need_type: str | None,
) -> tuple[bool, str]:
    requires_external_search = (
        scenario_code in EXTERNAL_SEARCH_SCENARIOS
        or need_type in {"外部資訊查詢", "資訊查詢"}
    )
    if not requires_external_search:
        return False, ""

    intermediate_reply = "我先幫你們看一下，等等整理幾個選項給你們～"
    if scenario_code == "劇本十四":
        intermediate_reply = "我先幫你們看一下場次，等等整理幾個可行時間給你們～"
    elif scenario_code == "劇本十七":
        intermediate_reply = "我先幫你們看一下天氣，等等整理建議跟備案給你們～"
    elif scenario_code in {"劇本五", "劇本十二", "劇本十三"}:
        intermediate_reply = "我先幫你們看一下路線跟相關資訊，等等整理給你們～"

    return True, intermediate_reply


def analyze_dialogue(text: str) -> AnalysisResult:
    info = extract_info(text)
    fallback_reason = ""

    try:
        return judge_with_llm(text, info)
    except LLMJudgeError as exc:
        fallback_reason = str(exc)
        scenario, evidence, confidence = classify(text, info)

    reply = scenario.suggested_reply if scenario.should_intervene else ""
    requires_external_search, intermediate_reply = _derive_external_search_fields(
        scenario.code,
        info.need_type,
    )
    if not evidence:
        evidence = ["LLM 無法使用，使用 rule-based fallback 進行近似判斷"]
    else:
        evidence.insert(0, f"LLM 無法使用，改用 rule-based fallback：{fallback_reason}")

    return AnalysisResult(
        scenario_code=scenario.code,
        scenario_name=scenario.name,
        stage=scenario.stage,
        should_intervene=scenario.should_intervene,
        intervention_type=scenario.intervention_type if scenario.should_intervene else "不介入",
        confidence_score=confidence,
        evidence=evidence,
        system_behavior=scenario.system_behavior,
        requires_external_search=requires_external_search,
        intermediate_reply=intermediate_reply,
        suggested_reply=reply,
        extracted_info=info,
    )
