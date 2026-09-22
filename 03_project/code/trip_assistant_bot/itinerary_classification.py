"""Read-time classification for public itineraries.

The classifier deliberately does not write its result back to MongoDB.  It is
used by the public API to give legacy itineraries with an empty ``type`` a
stable category without turning incidental meal stops into the main theme.
"""

from __future__ import annotations

import re
from typing import Any


ITINERARY_TYPES = ("山城", "都市", "河岸", "自然", "美食", "文化", "海線")
FALLBACK_ITINERARY_TYPE = "綜合"

_CATEGORY_PATTERNS = {
    "海線": re.compile(r"海岸|海邊|海景|沙灘|漁港|海港|濱海|海洋|燈塔"),
    "河岸": re.compile(r"河岸|河濱|溪流|湖畔|水岸|河堤|碼頭|湖景"),
    "美食": re.compile(r"美食|小吃|夜市|餐廳|咖啡|市場|用餐|素食|料理"),
    "文化": re.compile(r"文化|古蹟|老街|博物館|美術館|寺廟|歷史|文創|故宮|古厝|車站|藝術"),
    "山城": re.compile(r"山城|山區|登山|步道|森林|林場|山景|高山|丘陵"),
    "自然": re.compile(r"自然|公園|瀑布|農場|牧場|濕地|生態|草原|植物園|動物園|水族館|Xpark|採摘|採果|草莓園"),
    "都市": re.compile(r"都市|市區|商圈|購物|百貨|夜景|捷運|都會|展覽|室內"),
}

# These phrases usually describe a supporting meal stop.  They may add a food
# tag, but receive much less weight than a core attraction.
_SUPPORTING_FOOD_PATTERN = re.compile(
    r"附近(?:用餐|餐廳|咖啡)|用餐|親子友善餐廳|素食餐廳|咖啡廳"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _add_matches(scores: dict[str, float], value: Any, weight: float) -> set[str]:
    text = _text(value)
    matched: set[str] = set()
    if not text:
        return matched
    for category, pattern in _CATEGORY_PATTERNS.items():
        if pattern.search(text):
            scores[category] += weight
            matched.add(category)
    return matched


def _score_itinerary(itinerary: dict[str, Any]) -> tuple[dict[str, float], set[str]]:
    scores = {category: 0.0 for category in ITINERARY_TYPES}
    title_matches = _add_matches(scores, itinerary.get("title"), 6.0)
    _add_matches(scores, itinerary.get("summary"), 2.0)
    _add_matches(scores, itinerary.get("description"), 1.0)

    for spot in itinerary.get("spots") or []:
        if not isinstance(spot, dict):
            continue
        name = _text(spot.get("name"))
        description = _text(spot.get("description"))
        supporting_food = bool(_SUPPORTING_FOOD_PATTERN.search(name))

        name_matches = _add_matches(scores, name, 3.5)
        description_matches = _add_matches(scores, description, 0.75)
        if supporting_food:
            # Undo most of the normal food weight.  A lunch stop should not
            # outweigh museums, farms, old streets, or other core attractions.
            if "美食" in name_matches:
                scores["美食"] -= 2.5
            if "美食" in description_matches:
                scores["美食"] -= 0.5

    return scores, title_matches


def classify_itinerary(itinerary: dict[str, Any]) -> dict[str, Any]:
    """Return a display-only classification without mutating ``itinerary``."""
    provided_type = _text(itinerary.get("type"))
    scores, title_matches = _score_itinerary(itinerary)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], ITINERARY_TYPES.index(item[0])))
    top_type, top_score = ranked[0]
    second_score = ranked[1][1]

    inferred_tags = [
        category
        for category, score in ranked
        if score >= 2.0 and (top_score <= 0 or score >= top_score * 0.35)
    ]

    if provided_type in ITINERARY_TYPES:
        primary_type = provided_type
        source = "stored"
        confidence = 1.0
    else:
        clearly_ahead = second_score <= 0 or top_score >= second_score * 1.25
        title_breaks_tie = top_type in title_matches and top_score >= second_score + 2.0
        if top_score >= 3.0 and (clearly_ahead or title_breaks_tie):
            primary_type = top_type
        else:
            primary_type = FALLBACK_ITINERARY_TYPE
        source = "computed"
        total_score = sum(scores.values())
        confidence = round(top_score / total_score, 3) if total_score else 0.0

    tags = list(inferred_tags)
    if primary_type in ITINERARY_TYPES and primary_type not in tags:
        tags.insert(0, primary_type)

    return {
        "primary_type": primary_type,
        "tags": tags,
        "source": source,
        "confidence": confidence,
    }
