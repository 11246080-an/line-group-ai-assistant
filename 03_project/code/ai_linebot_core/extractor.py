from __future__ import annotations

# 這個檔案負責「先把對話整理成重點」。
# 白話來說，就是先從一大串群組聊天裡，
# 抓出時間、地點、活動、預算、風險這些資訊。

import re

from app.models import ExtractedInfo


TIME_PATTERNS = [
    r"(這週末|週末|這週六|週六|週日|明天|今天|晚上|中午|午餐|晚餐|晚場|早一點)",
    r"(\d{1,2}[:：]\d{2})",
    r"(\d{1,2}點半|\d{1,2}點)",
]

PEOPLE_PATTERNS = [
    r"(\d+個人)",
    r"(\d+人)",
    r"(大家)",
]

BUDGET_PATTERNS = [
    r"(\d+\s*內)",
    r"(一人\s*\d+)",
    r"(\d+\s*元)",
]

LOCATION_KEYWORDS = [
    "淡水",
    "淡水老街",
    "漁人碼頭",
    "老街",
    "九份",
    "八里",
    "台北市",
    "附近",
]

ACTIVITY_KEYWORDS = [
    "吃飯",
    "吃東西",
    "出去走走",
    "放鬆",
    "看海",
    "逛街",
    "看電影",
    "看展覽",
    "博物館",
    "音樂劇",
    "演唱會",
    "爬山",
]

CONSTRAINT_KEYWORDS = [
    "不要太晚",
    "不要太遠",
    "不要太貴",
    "不要等太久",
    "近一點",
    "不要太多人",
    "太晚的不行",
    "先看天氣",
    "不要遲到",
]

RISK_KEYWORDS = [
    "來不及",
    "太趕",
    "移動也要時間",
    "提前進場",
    "下雨",
    "危險",
    "遲到",
    "天氣不太穩",
]

OPTION_KEYWORDS = [
    "火鍋",
    "燒肉",
    "義大利麵",
    "韓式",
    "九份",
    "淡水",
    "八里",
]


# 用正則規則抓出像「週六」、「13:00」、「500內」這種明確資訊。
def _find_matches(patterns: list[str], text: str) -> list[str]:
    results: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            value = match.strip()
            if value and value not in results:
                results.append(value)
    return results


# 用簡單關鍵字找出已知地點、活動、風險等。
# 這一步不是最後判斷劇本，只是幫後面整理資料。
def _find_keywords(keywords: list[str], text: str) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for keyword in keywords:
        if keyword.lower() in lowered and keyword not in found:
            found.append(keyword)
    return found


# 這是資訊抽取的主函式。
# 它會把原始聊天內容整理成 ExtractedInfo，
# 讓後面的 LLM 或 fallback classifier 更容易理解對話。
def extract_info(text: str) -> ExtractedInfo:
    info = ExtractedInfo()

    # 先抽取比較具體、容易辨認的欄位。
    info.time = _find_matches(TIME_PATTERNS, text)
    info.location = _find_keywords(LOCATION_KEYWORDS, text)
    info.people_count = _find_matches(PEOPLE_PATTERNS, text)
    info.budget = _find_matches(BUDGET_PATTERNS, text)
    info.constraints = _find_keywords(CONSTRAINT_KEYWORDS, text)
    info.activity_types = _find_keywords(ACTIVITY_KEYWORDS, text)
    info.options = _find_keywords(OPTION_KEYWORDS, text)
    info.risk_info = _find_keywords(RISK_KEYWORDS, text)

    # need_type 可以理解成「這段對話目前主要需要什麼幫助」。
    # 例如是需要投票收斂，還是需要外部查資料。
    if any(word in text for word in ["投票", "選項有點多"]):
        info.need_type = "決策收斂"
    elif any(
        phrase in text
        for phrase in [
            "現在要不要",
            "現在去哪",
            "現在吃什麼",
            "附近有什麼",
            "幫我查",
            "查一下",
            "幫我找",
            "快點決定",
        ]
    ):
        info.need_type = "外部資訊查詢"
    elif any(word in text for word in ["排行程", "順序", "動線"]):
        info.need_type = "規劃建議"

    # decision_state 是在判斷大家討論到哪一步。
    # 例如：還沒定、正在收斂、還是已經定案。
    if any(word in text for word in ["那就這家吧", "先定了", "不用再選"]):
        info.decision_state = "已定案"
    elif any(word in text for word in ["投票", "選項", "還是", "不然"]):
        info.decision_state = "收斂中"
    else:
        info.decision_state = "未定"

    return info
