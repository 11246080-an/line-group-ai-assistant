from __future__ import annotations

from .classifier import classify
from .extractor import extract_info
from .llm_judge import LLMJudgeError, judge_with_llm
from .models import AnalysisResult


EXTERNAL_SEARCH_SCENARIOS = {
    "劇本四",
    "劇本五",
    "劇本六",
    "劇本十二",
    "劇本十四",
    "劇本十五",
    "劇本十六",
    "劇本十七",
}


def _derive_external_search_fields(
    scenario_code: str,
    need_type: str | None,
) -> tuple[bool, str]:
    requires_external_search = (
        scenario_code in EXTERNAL_SEARCH_SCENARIOS
        or need_type == "外部資訊查詢"
    )
    if not requires_external_search:
        return False, ""

    intermediate_reply = "我先幫大家查一下，整理好再回。"
    if scenario_code == "劇本十七":
        intermediate_reply = "我先幫大家查一下天氣和備案，再整理給你們。"
    elif scenario_code == "劇本十四":
        intermediate_reply = "我先幫大家確認場次和時間，再整理給你們。"
    elif scenario_code in {"劇本四", "劇本五", "劇本六"}:
        intermediate_reply = "我先幫大家整理一下行程資訊，再回你們。"

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
        evidence = ["LLM 判斷失敗，改用 rule-based fallback。"]
    else:
        evidence.insert(0, f"LLM 判斷失敗，改用 rule-based fallback：{fallback_reason}")

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
