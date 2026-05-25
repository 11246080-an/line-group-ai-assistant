from __future__ import annotations

# 這個檔案可以想成「總控台」。
# 它負責把：
# 1. 對話資訊抽取
# 2. LLM 判斷
# 3. fallback 備援判斷
# 4. 回覆欄位整理
# 全部串在一起。

from app.classifier import classify
from app.extractor import extract_info
from app.models import AnalysisResult, ExtractedInfo
from app.llm_judge import LLMJudgeError, judge_with_llm


# 這些劇本通常代表 AI 需要去外部查資料，
# 例如查場次、查天氣、查路線、查附近餐廳。
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


# 這個函式負責決定：
# 1. 這次判斷是否需要外部查詢
# 2. 如果要查，先給群組什麼樣的中繼回覆
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

    # intermediate_reply 是「先回一句，讓群組知道 AI 已經開始幫忙」。
    # suggested_reply 則是查完資料後真正的正式回覆。
    intermediate_reply = "我先幫你們看一下，等等整理幾個選項給你們～"
    if scenario_code == "劇本十四":
        intermediate_reply = "我先幫你們看一下場次，等等整理幾個可行時間給你們～"
    elif scenario_code == "劇本十七":
        intermediate_reply = "我先幫你們看一下天氣，等等整理建議跟備案給你們～"
    elif scenario_code in {"劇本五", "劇本十二", "劇本十三"}:
        intermediate_reply = "我先幫你們看一下路線跟相關資訊，等等整理給你們～"

    return True, intermediate_reply


# 這是整個專案最重要的統一入口。
# LINE Bot 同學之後如果要用這個專案，
# 基本上就是把群組對話文字丟進這個函式。
def analyze_dialogue(text: str) -> AnalysisResult:
    # 主流程改成直接把整段對話交給 LLM 判斷，
    # 不再把 extractor 當成主要輸入來源。
    # extractor 只保留給 fallback classifier 使用。
    info = ExtractedInfo()
    fallback_reason = ""

    try:
        # 第二步：優先交給 LLM 判斷。
        # 這就是 LLM-first 的意思：
        # 主要判斷工作先由大語言模型處理。
        return judge_with_llm(text, info)
    except LLMJudgeError as exc:
        # 如果 LLM 失敗，就改用備援方案。
        # fallback 可以理解成「主方案壞掉時的替代方案」。
        fallback_reason = str(exc)
        info = extract_info(text)
        scenario, evidence, confidence = classify(text, info)

    # 如果 AI 根本不該介入，就不需要正式回覆。
    if scenario.should_intervene:
        reply = scenario.suggested_reply
    else:
        reply = ""

    # 根據劇本與需求類型，補上：
    # - 是否需要外部查詢
    # - 查詢前先回什麼
    requires_external_search, intermediate_reply = _derive_external_search_fields(
        scenario.code,
        info.need_type,
    )

    # 這裡會把 fallback 原因寫進 evidence，
    # 方便之後 debug 或跟老師說明這次為什麼沒走 LLM。
    if not evidence:
        evidence = ["LLM 無法使用，使用 rule-based fallback 進行近似判斷"]
    else:
        evidence.insert(0, f"LLM 無法使用，改用 rule-based fallback：{fallback_reason}")

    # 最後把所有結果包成固定格式。
    # 之後不管是 CLI、測試、還是 LINE Bot，都可以吃同一種格式。
    return AnalysisResult(
        scenario_code=scenario.code,
        scenario_name=scenario.name,
        stage=scenario.stage,
        should_intervene=scenario.should_intervene,
        reply_trigger="no_reply" if not scenario.should_intervene else "functional_question",
        intervention_type=scenario.intervention_type if scenario.should_intervene else "不介入",
        confidence_score=confidence,
        evidence=evidence,
        system_behavior=scenario.system_behavior,
        requires_external_search=requires_external_search,
        intermediate_reply=intermediate_reply,
        suggested_reply=reply,
        extracted_info=info,
    )
