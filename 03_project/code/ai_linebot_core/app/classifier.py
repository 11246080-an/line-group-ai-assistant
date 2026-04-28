from __future__ import annotations

"""Rule-based fallback classifier.

This module is retained as a backup strategy when the LLM-based scenario
judgment path is unavailable. It is no longer the primary classification path.
"""

# 這個檔案是備用判斷器。
# 平常主判斷會先交給 LLM，
# 只有 LLM 失敗時才會改用這裡的規則式判斷。

from app.knowledge_base import SCENARIOS
from app.models import ExtractedInfo, ScenarioDefinition


# 簡單判斷是否同時提到多個地點。
def _has_multiple_locations(info: ExtractedInfo) -> bool:
    return len(info.location) >= 2


# 簡單判斷是否同時出現多個選項。
def _has_multiple_options(info: ExtractedInfo) -> bool:
    return len(info.options) >= 2


# 看對話是否已經進入「在規劃細節」的階段。
def _is_planning(text: str) -> bool:
    return any(word in text for word in ["排行程", "順序", "景點", "餐廳", "路線", "動線"])


# 看大家是不是開始卡住，例如一直說「都可以、隨便」。
def _is_discussion_stalled(text: str) -> bool:
    stall_words = ["都可以", "隨便", "看你們", "再說"]
    return sum(1 for word in stall_words if word in text) >= 2


# 這個函式會幫每個劇本打分數。
# 分數越高，代表這段對話越像那個劇本。
# 注意：這是 fallback 用，所以仍然使用不少規則與關鍵字。
def _score_scenario(scenario: ScenarioDefinition, text: str, info: ExtractedInfo) -> tuple[int, list[str]]:
    score = 0
    evidence: list[str] = []

    for keyword in scenario.keywords:
        if keyword in text:
            score += 2
            evidence.append(f"出現關鍵詞：{keyword}")

    hints = set(scenario.feature_hints)

    if "has_time" in hints and info.time:
        score += 2
        evidence.append("已有時間資訊")
    if "has_location" in hints and info.location:
        score += 2
        evidence.append("已有地點資訊")
    if "has_people" in hints and info.people_count:
        score += 2
        evidence.append("已有人數資訊")
    if "has_constraints" in hints and info.constraints:
        score += 2
        evidence.append("已有限制條件")
    if "has_budget" in hints and info.budget:
        score += 2
        evidence.append("已有預算資訊")
    if "multiple_locations" in hints and _has_multiple_locations(info):
        score += 3
        evidence.append("出現多個地點")
    if "multiple_options" in hints and _has_multiple_options(info):
        score += 3
        evidence.append("出現多個選項")
    if "planning" in hints and _is_planning(text):
        score += 3
        evidence.append("對話已進入規劃階段")
    if "not_planning" in hints and not _is_planning(text):
        score += 1
        evidence.append("尚未正式進入規劃")
    if "route_optimization" in hints and any(word in text for word in ["順路", "繞路", "來回跑", "動線"]):
        score += 4
        evidence.append("有明確動線優化需求")
    if "meal_context" in hints and any(word in text for word in ["午餐", "晚餐", "吃什麼", "餐廳"]):
        score += 3
        evidence.append("對話聚焦餐飲安排")
    if "preference_conflict" in hints and any(word in text for word in ["我想吃", "還是", "不然"]):
        score += 3
        evidence.append("出現偏好分歧")
    if "discussion_stalled" in hints and _is_discussion_stalled(text):
        score += 4
        evidence.append("討論出現停滯語氣")
    if "voting" in hints and "投票" in text:
        score += 4
        evidence.append("對話明確提到投票")
    if "time_conflict" in hints and any(word in text for word in ["太趕", "來得及嗎", "移動也要時間", "提前進場"]):
        score += 4
        evidence.append("存在時間衝突風險")
    if "needs_extension" in hints and any(word in text for word in ["附近還可以幹嘛", "景點", "逛街"]):
        score += 3
        evidence.append("需要延伸資訊")
    if "decision_done" in hints and info.decision_state == "已定案":
        score += 5
        evidence.append("對話已經定案")
    if "fixed_event_time" in hints and any(word in text for word in ["音樂劇", "演唱會", "開演"]):
        score += 5
        evidence.append("固定開演時間情境")
    if "movie_session" in hints and any(word in text for word in ["電影", "場次"]):
        score += 5
        evidence.append("電影場次限制情境")
    if "exhibition_duration" in hints and any(word in text for word in ["展覽", "博物館", "會待多久"]):
        score += 5
        evidence.append("展覽停留時間估算情境")
    if "immediate_need" in hints and any(word in text for word in ["現在", "附近", "快點決定"]):
        score += 5
        evidence.append("即時需求情境")
    if "weather_risk" in hints and any(word in text for word in ["下雨", "天氣不太穩", "先看天氣"]):
        score += 5
        evidence.append("天氣風險情境")
    if "outdoor_activity" in hints and any(word in text for word in ["爬山", "戶外"]):
        score += 4
        evidence.append("戶外活動情境")
    if "not_has_location" in hints and not info.location:
        score += 2
        evidence.append("尚未明確地點")
    if "not_has_time_detail" in hints and len(info.time) <= 1:
        score += 1
        evidence.append("時間資訊仍偏少")

    return score, evidence


# 從 17 個劇本中選出分數最高的那個，
# 作為 fallback 的最終判斷結果。
def classify(text: str, info: ExtractedInfo) -> tuple[ScenarioDefinition, list[str], float]:
    scored: list[tuple[int, ScenarioDefinition, list[str]]] = []
    for scenario in SCENARIOS:
        score, evidence = _score_scenario(scenario, text, info)
        scored.append((score, scenario, evidence))

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_scenario, evidence = scored[0]
    confidence = min(1.0, 0.35 + best_score / 20.0)
    return best_scenario, evidence[:6], round(confidence, 2)
