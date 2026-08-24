from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from db import (
    get_api_query_cache,
    get_tourism_attractions,
    get_tourism_events,
    save_api_query_cache,
)


LOGGER = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BEACON_REGISTRY_PATH = BASE_DIR / "beacon_registry.json"
DEFAULT_ITINERARY_DATA_PATH = BASE_DIR / "trip_website" / "data" / "itineraries.json"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BEACON_CONTEXT_TTL_SECONDS = _env_float("BEACON_CONTEXT_TTL_MINUTES", 10.0) * 60
LIFF_SESSION_TTL_SECONDS = _env_float("LIFF_SESSION_TTL_MINUTES", 15.0) * 60
RECENT_LOCATION_CONTEXT_TTL_SECONDS = _env_float(
    "RECENT_LOCATION_CONTEXT_TTL_MINUTES",
    15.0,
) * 60
LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS = _env_float(
    "LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS",
    10.0,
)
LOCATION_RECOMMENDATION_MAX_RESPONSE_BYTES = max(
    1024,
    _env_int("LOCATION_RECOMMENDATION_MAX_RESPONSE_BYTES", 1_048_576),
)
DEFAULT_LOCATION_LINK_HOSTS = (
    "www.google.com",
    "maps.google.com",
    "maps.app.goo.gl",
)
EXTERNAL_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？）】》"
GROUP_RESULT_LIMIT = max(
    1,
    _env_int(
        "LOCATION_GROUP_RESULT_LIMIT",
        _env_int("LOCATION_RECOMMENDATION_RESULT_LIMIT", 3),
    ),
)
LIFF_RESULT_LIMIT = max(
    GROUP_RESULT_LIMIT,
    _env_int("LOCATION_LIFF_RESULT_LIMIT", 10),
)


@dataclass
class BeaconContext:
    line_user_id: str
    hwid: str
    beacon_type: str
    name: str
    address: str
    latitude: float | None
    longitude: float | None
    detected_at: float
    raw: dict[str, Any]

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass
class RecommendationSession:
    token: str
    push_target_id: str
    conversation_key: str
    line_user_id: str
    line_group_id: str
    query_text: str
    created_at: float
    status: str = "pending"
    result: dict[str, Any] | None = None


@dataclass
class RecentLocationContext:
    conversation_key: str
    line_user_id: str
    line_group_id: str
    latitude: float
    longitude: float
    accuracy: float | None
    saved_at: float


_beacon_lock = threading.Lock()
_session_lock = threading.Lock()
_recent_location_lock = threading.Lock()
_beacon_contexts: dict[str, BeaconContext] = {}
_recommendation_sessions: dict[str, RecommendationSession] = {}
_recent_location_contexts: dict[str, RecentLocationContext] = {}
_registry_cache: dict[str, Any] = {"path": None, "mtime": None, "data": {}}
_catalog_cache: dict[str, Any] = {"mtime": None, "data": []}

FOOD_QUERY_KEYWORDS = (
    "\u5403",
    "\u597d\u5403",
    "\u7f8e\u98df",
    "\u9910\u5ef3",
    "\u5c0f\u5403",
    "\u591c\u5e02",
    "\u5bb5\u591c",
    "\u5496\u5561",
    "\u751c\u9ede",
    "\u98ef",
)
ATTRACTION_QUERY_KEYWORDS = (
    "景點",
    "玩",
    "旅遊",
    "行程",
    "一日遊",
    "半日遊",
    "出遊",
    "逛",
    "拍照",
    "散步",
)
FOOD_CONTENT_KEYWORDS = FOOD_QUERY_KEYWORDS + (
    "\u98f2\u6599",
    "\u65e9\u9910",
    "\u5348\u9910",
    "\u665a\u9910",
    "\u706b\u934b",
    "\u71d2\u8089",
    "\u6d77\u9bae",
)
FOOD_ITINERARY_ID_KEYWORDS = ("food", "cafe", "market")
OPENAI_FALLBACK_LOCAL_RADIUS_KM = 15.0
OPENAI_FALLBACK_CANDIDATE_LIMIT = max(10, LIFF_RESULT_LIMIT)
GOOGLE_PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_PLACES_SEARCH_RADIUS_METERS = max(
    500,
    _env_int("GOOGLE_PLACES_SEARCH_RADIUS_METERS", 5000),
)
GOOGLE_PLACES_CACHE_TTL_SECONDS = max(
    60,
    _env_int("GOOGLE_PLACES_CACHE_TTL_SECONDS", 3600),
)
LOCATION_NORMALIZER_MODEL = os.getenv("LOCATION_NORMALIZER_MODEL", "gpt-4.1-mini")
LOCATION_TEXT_ALIASES = {
    "北車": "台北車站",
    "台北車站": "台北車站",
    "西門": "西門町",
    "台大": "台灣大學",
    "師大": "師大夜市",
}
AMBIGUOUS_TOURISM_CITY_ALIASES = {
    "嘉義": ["嘉義市", "嘉義縣"],
    "新竹": ["新竹市", "新竹縣"],
}
TOURISM_CITY_ALIASES = {
    "基隆": "基隆市",
    "臺北": "臺北市",
    "台北": "臺北市",
    "新北": "新北市",
    "淡水": "新北市",
    "淡水老街": "新北市",
    "八里": "新北市",
    "九份": "新北市",
    "瑞芳": "新北市",
    "桃園": "桃園市",
    "嘉義": "嘉義市",
    "新竹": "新竹市",
    "新竹市": "新竹市",
    "新竹縣": "新竹縣",
    "苗栗": "苗栗縣",
    "臺中": "臺中市",
    "台中": "臺中市",
    "彰化": "彰化縣",
    "南投": "南投縣",
    "雲林": "雲林縣",
    "嘉義市": "嘉義市",
    "嘉義縣": "嘉義縣",
    "臺南": "臺南市",
    "台南": "臺南市",
    "高雄": "高雄市",
    "屏東": "屏東縣",
    "宜蘭": "宜蘭縣",
    "礁溪": "宜蘭縣",
    "羅東": "宜蘭縣",
    "蘇澳": "宜蘭縣",
    "冬山河": "宜蘭縣",
    "花蓮": "花蓮縣",
    "太魯閣": "花蓮縣",
    "臺東": "臺東縣",
    "台東": "臺東縣",
    "澎湖": "澎湖縣",
    "金門": "金門縣",
    "連江": "連江縣",
}


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return any(keyword.casefold() in normalized for keyword in keywords if keyword)


def _count_keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    normalized = text.casefold()
    return sum(1 for keyword in keywords if keyword and keyword.casefold() in normalized)


def _detect_query_intent(query_text: str) -> str:
    if _contains_any_keyword(query_text, FOOD_QUERY_KEYWORDS):
        return "food"
    if _contains_any_keyword(query_text, ATTRACTION_QUERY_KEYWORDS):
        return "attraction"
    return "general"


def _infer_intent_from_activity_types(activity_types: list[str] | None) -> str:
    if not activity_types:
        return "general"

    joined = " ".join(str(item).strip() for item in activity_types if str(item).strip())
    if not joined:
        return "general"

    if _contains_any_keyword(joined, FOOD_QUERY_KEYWORDS):
        return "food"
    if _contains_any_keyword(joined, ATTRACTION_QUERY_KEYWORDS):
        return "attraction"
    return "general"


def _effective_recommendation_intent(
    query_text: str,
    activity_types: list[str] | None = None,
) -> str:
    query_intent = _detect_query_intent(query_text)
    if query_intent != "general":
        return query_intent
    return _infer_intent_from_activity_types(activity_types)


def _normalize_tourism_city(location_text: str, query_text: str = "") -> str:
    cities = _normalize_tourism_cities(location_text, query_text)
    return cities[0] if cities else ""


def _normalize_tourism_cities(location_text: str, query_text: str = "") -> list[str]:
    candidates = [
        str(location_text or "").strip(),
        str(query_text or "").strip(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        for alias in sorted(TOURISM_CITY_ALIASES, key=len, reverse=True):
            if alias in AMBIGUOUS_TOURISM_CITY_ALIASES:
                continue
            if alias and alias in candidate:
                return [TOURISM_CITY_ALIASES[alias]]
        for alias in sorted(AMBIGUOUS_TOURISM_CITY_ALIASES, key=len, reverse=True):
            if alias and alias in candidate:
                return list(AMBIGUOUS_TOURISM_CITY_ALIASES[alias])
        for alias in sorted(TOURISM_CITY_ALIASES, key=len, reverse=True):
            if alias and alias in candidate:
                return [TOURISM_CITY_ALIASES[alias]]
    return []


def _normalize_tourism_area(location_text: str, query_text: str = "") -> str:
    candidates = [
        str(location_text or "").strip(),
        str(query_text or "").strip(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        cleaned = re.sub(
            r"(附近|周邊|有什麼|可以玩|景點|活動|推薦|想去|想玩|旅遊|行程)",
            "",
            candidate,
        ).strip()
        for alias in sorted(TOURISM_CITY_ALIASES, key=len, reverse=True):
            if alias and alias in cleaned:
                return alias
    return ""


def _tourism_area_score(item: dict[str, Any], area_text: str) -> int:
    if not area_text:
        return 0

    score = 0
    town = str(item.get("town") or "")
    name = str(item.get("name") or "")
    address = str(item.get("address") or "")
    description = str(item.get("description") or "")

    if area_text in town:
        score += 8
    if area_text in name:
        score += 5
    if area_text in address:
        score += 4
    if area_text in description:
        score += 1
    return score


def _rank_tourism_items_by_area(
    items: list[dict[str, Any]],
    *,
    area_text: str,
    city: str,
) -> list[dict[str, Any]]:
    if not items:
        return []

    city_aliases = {
        alias
        for alias, canonical_city in TOURISM_CITY_ALIASES.items()
        if canonical_city == city
    }
    should_filter_by_area = bool(
        area_text
        and area_text not in city_aliases
        and area_text not in AMBIGUOUS_TOURISM_CITY_ALIASES
    )
    scored_items = [
        (_tourism_area_score(item, area_text), index, item)
        for index, item in enumerate(items)
    ]

    if should_filter_by_area:
        area_matches = [
            (score, index, item)
            for score, index, item in scored_items
            if score > 0
        ]
        if area_matches:
            scored_items = area_matches

    scored_items.sort(key=lambda entry: (-entry[0], entry[1]))
    return [item for _score, _index, item in scored_items]


def _is_tourism_lookup_request(
    query_text: str,
    activity_types: list[str] | None = None,
) -> bool:
    if _effective_recommendation_intent(query_text, activity_types) == "food":
        return False

    combined = " ".join(
        [query_text]
        + [str(item) for item in activity_types or [] if str(item).strip()]
    )
    return _contains_any_keyword(
        combined,
        ATTRACTION_QUERY_KEYWORDS
        + (
            "景區",
            "觀光",
            "活動",
            "展覽",
            "節慶",
            "博物館",
            "公園",
            "老街",
        ),
    )


def _is_tourism_event_lookup_request(
    query_text: str,
    activity_types: list[str] | None = None,
) -> bool:
    combined = " ".join(
        [query_text]
        + [str(item) for item in activity_types or [] if str(item).strip()]
    )
    return _contains_any_keyword(
        combined,
        ("活動", "展覽", "節慶", "市集", "演出", "表演"),
    )


def _shorten_tourism_text(value: Any, max_length: int = 42) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_length:
        return text
    return f"{text[:max_length].rstrip()}..."


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


def _parse_tourism_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TAIPEI_TIMEZONE)
    return parsed.astimezone(TAIPEI_TIMEZONE)


def _format_tourism_datetime(value: Any) -> str:
    parsed = _parse_tourism_datetime(value)
    if parsed is None:
        return _shorten_tourism_text(value, 16)
    return parsed.strftime("%Y/%m/%d %H:%M")


def _is_active_tourism_event(item: dict[str, Any]) -> bool:
    end_time = _parse_tourism_datetime(item.get("end_time"))
    if end_time is None:
        return True
    now = datetime.now(TAIPEI_TIMEZONE)
    return end_time >= now


def _format_tourism_description(item: dict[str, Any], *, item_type: str) -> str:
    parts: list[str] = []

    fee_info = _shorten_tourism_text(item.get("fee_info"), 48)
    if fee_info:
        parts.append(f"門票：{fee_info}")
    elif item.get("is_accessible_for_free") is True:
        parts.append("門票：免費")

    if item_type == "event":
        start_time = _format_tourism_datetime(item.get("start_time"))
        if start_time:
            parts.append(f"開始：{start_time}")
        end_time = _format_tourism_datetime(item.get("end_time"))
        if end_time:
            parts.append(f"結束：{end_time}")

    website_url = str(item.get("website_url") or "").strip()
    if website_url:
        parts.append(f"網址：{website_url}")

    return "｜".join(parts)


def _tourism_item_to_result(item: dict[str, Any], *, item_type: str) -> dict[str, Any]:
    city = str(item.get("city") or "").strip()
    town = str(item.get("town") or "").strip()
    address = str(item.get("address") or "").strip()
    subtitle = address or " ".join(part for part in (city, town) if part).strip()

    return {
        "name": str(item.get("name") or "未命名景點").strip(),
        "subtitle": subtitle,
        "description": _format_tourism_description(item, item_type=item_type),
        "distance_km": None,
        "address": address,
        "maps_url": str(item.get("website_url") or "").strip(),
        "latitude": _coerce_float(item.get("latitude")),
        "longitude": _coerce_float(item.get("longitude")),
        "provider": f"tourism_{item_type}",
    }


def _merge_recommendation_results(
    primary_results: list[dict[str, Any]],
    secondary_results: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for item in primary_results + secondary_results:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        normalized_name = re.sub(r"\s+", "", name).casefold()
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        merged.append(item)
        if len(merged) >= limit:
            break

    return merged


def _build_tourism_text_recommendation(
    *,
    query_text: str,
    location_text: str,
    activity_types: list[str] | None = None,
) -> dict[str, Any] | None:
    if not _is_tourism_lookup_request(query_text, activity_types):
        return None

    cities = _normalize_tourism_cities(location_text, query_text)
    if not cities:
        return None
    city_label = "、".join(cities)
    area_text = _normalize_tourism_area(location_text, query_text)

    event_lookup = _is_tourism_event_lookup_request(query_text, activity_types)

    results: list[dict[str, Any]] = []
    try:
        lookup_limit = max(LIFF_RESULT_LIMIT * 8, 80)
        for city in cities:
            if event_lookup:
                events = get_tourism_events(city=city, limit=lookup_limit)
                events = [
                    item
                    for item in events
                    if isinstance(item, dict) and _is_active_tourism_event(item)
                ]
                events = _rank_tourism_items_by_area(
                    events,
                    area_text=area_text,
                    city=city,
                )
                results.extend(
                    _tourism_item_to_result(item, item_type="event")
                    for item in events[:LIFF_RESULT_LIMIT]
                )
            else:
                attractions = get_tourism_attractions(city=city, limit=lookup_limit)
                attractions = _rank_tourism_items_by_area(
                    attractions,
                    area_text=area_text,
                    city=city,
                )
                results.extend(
                    _tourism_item_to_result(item, item_type="attraction")
                    for item in attractions[:LIFF_RESULT_LIMIT]
                    if isinstance(item, dict)
                )
    except Exception as exc:
        _log_external_failure("Tourism open data recommendation", exc)
        return None

    results = [item for item in results if item.get("name")]
    results = _merge_recommendation_results(results, [], limit=LIFF_RESULT_LIMIT)
    if not results and event_lookup:
        return {
            "group_message": f"我查了一下觀光署活動資料，目前沒有找到{city_label}近期適合顯示的活動。",
            "results": [],
            "location_source": "text_location",
            "query_text": query_text.strip(),
            "provider": "tourism_open_data",
            "tourism_city": city_label,
            "tourism_area": area_text,
            "tourism_kind": "event",
        }
    if not results:
        return None

    text_query = _build_text_search_query(
        query_text=query_text,
        location_text=location_text,
        activity_types=activity_types,
    )
    return {
        "group_message": _format_group_message(
            results,
            text_query or query_text,
            "text_location",
        ),
        "results": results,
        "location_source": "text_location",
        "query_text": text_query or query_text,
        "provider": "tourism_open_data",
        "tourism_city": city_label,
        "tourism_area": area_text,
        "tourism_kind": "event" if event_lookup else "attraction",
    }


def _get_openai_client() -> Any | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    return OpenAI(api_key=api_key)


def _extract_json_object(text: str) -> dict[str, Any] | None:
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
        return None

    try:
        return json.loads(payload[start : end + 1])
    except json.JSONDecodeError:
        return None


def _normalize_location_text_with_llm(location_text: str, query_text: str) -> str:
    client = _get_openai_client()
    if client is None:
        return ""

    try:
        response = client.chat.completions.create(
            model=LOCATION_NORMALIZER_MODEL,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是台灣地點名稱正規化助手。"
                        "請把使用者口語中的地點，轉成最適合拿去查 Google Places 的正式地點名稱。"
                        "例如北車 -> 台北車站，北商 -> 國立臺北商業大學。"
                        "只輸出 JSON，格式必須是 {\"normalized_location\": \"...\"}。"
                        "如果無法更正規化，就原樣輸出。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "location_text": location_text,
                            "query_text": query_text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        data = _extract_json_object(content)
        if not isinstance(data, dict):
            return ""
        normalized = str(data.get("normalized_location") or "").strip()
        return normalized
    except Exception as exc:
        _log_external_failure("Location normalization", exc)
        return ""


def _normalize_location_text(location_text: str, query_text: str = "") -> str:
    normalized = location_text.strip()
    if not normalized:
        return ""

    llm_normalized = _normalize_location_text_with_llm(normalized, query_text)
    if llm_normalized:
        return llm_normalized

    for alias, canonical in LOCATION_TEXT_ALIASES.items():
        if alias in normalized:
            return normalized.replace(alias, canonical)
    return normalized


def _build_google_places_cache_key(
    *,
    query_text: str,
    latitude: float,
    longitude: float,
    location_source: str,
) -> str:
    raw_key = "|".join(
        [
            query_text.strip(),
            f"{latitude:.5f}",
            f"{longitude:.5f}",
            location_source.strip(),
        ]
    )
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _has_no_spicy_constraint(constraints: list[str] | None) -> bool:
    joined = " ".join(str(item).strip() for item in constraints or [] if str(item).strip())
    return any(token in joined for token in ("不吃辣", "不要辣", "不能吃辣", "不辣", "怕辣"))


def _is_likely_spicy_place(item: dict[str, Any]) -> bool:
    searchable_text = " ".join(
        str(item.get(key) or "")
        for key in ("name", "subtitle", "description", "address")
    )
    spicy_signals = (
        "麻辣",
        "川菜",
        "四川",
        "串串",
        "串串香",
        "酸菜魚",
        "水煮魚",
        "辣",
        "剁椒",
        "湘菜",
        "泰式",
        "韓式",
        "韓國",
        "烤肉",
        "火鍋",
    )
    return any(signal in searchable_text for signal in spicy_signals)


def _filter_results_by_constraints(
    results: list[dict[str, Any]],
    constraints: list[str] | None,
) -> list[dict[str, Any]]:
    if not _has_no_spicy_constraint(constraints):
        return results

    filtered = [item for item in results if not _is_likely_spicy_place(item)]
    return filtered if filtered else results


def _format_google_place_description(place: dict[str, Any]) -> str:
    parts: list[str] = []

    rating = place.get("rating")
    if isinstance(rating, (int, float)):
        parts.append(f"評分 {rating:.1f}")

    price_level = str(place.get("priceLevel") or "").strip()
    if price_level:
        price_map = {
            "PRICE_LEVEL_FREE": "免費",
            "PRICE_LEVEL_INEXPENSIVE": "平價",
            "PRICE_LEVEL_MODERATE": "中價位",
            "PRICE_LEVEL_EXPENSIVE": "偏高",
            "PRICE_LEVEL_VERY_EXPENSIVE": "高價位",
        }
        parts.append(price_map.get(price_level, price_level))

    primary_type = (
        ((place.get("primaryTypeDisplayName") or {}).get("text"))
        if isinstance(place.get("primaryTypeDisplayName"), dict)
        else ""
    )
    if primary_type:
        parts.append(str(primary_type).strip())

    return "｜".join(part for part in parts if part)


def _google_place_to_result(place: dict[str, Any], latitude: float, longitude: float) -> dict[str, Any]:
    display_name = place.get("displayName") or {}
    place_location = place.get("location") or {}
    place_latitude = _coerce_float(place_location.get("latitude"))
    place_longitude = _coerce_float(place_location.get("longitude"))

    distance_km = None
    if place_latitude is not None and place_longitude is not None:
        distance_km = _haversine_km(latitude, longitude, place_latitude, place_longitude)

    return {
        "name": str(display_name.get("text") or "未命名地點").strip(),
        "subtitle": str(place.get("formattedAddress") or "").strip(),
        "description": _format_google_place_description(place),
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "address": str(place.get("formattedAddress") or "").strip(),
        "maps_url": str(place.get("googleMapsUri") or "").strip(),
        "latitude": place_latitude,
        "longitude": place_longitude,
    }


def _build_google_places_request_body(
    *,
    query_text: str,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "textQuery": query_text.strip(),
        "pageSize": LIFF_RESULT_LIMIT,
        "languageCode": "zh-TW",
        "regionCode": "TW",
        "locationBias": {
            "circle": {
                "center": {
                    "latitude": latitude,
                    "longitude": longitude,
                },
                "radius": float(GOOGLE_PLACES_SEARCH_RADIUS_METERS),
            }
        },
    }

    if _detect_query_intent(query_text) == "food":
        body["includedType"] = "restaurant"
        body["strictTypeFiltering"] = False

    return body


def _build_google_places_text_request_body(
    *,
    text_query: str,
) -> dict[str, Any]:
    return {
        "textQuery": text_query.strip(),
        "pageSize": LIFF_RESULT_LIMIT,
        "languageCode": "zh-TW",
        "regionCode": "TW",
    }


def _build_text_search_query(
    *,
    query_text: str,
    location_text: str,
    constraints: list[str] | None = None,
    activity_types: list[str] | None = None,
) -> str:
    normalized_location = _normalize_location_text(location_text, query_text)
    query_intent = _detect_query_intent(query_text)
    activity_intent = _infer_intent_from_activity_types(activity_types)
    effective_intent = (
        query_intent
        if query_intent != "general"
        else activity_intent
    )
    parts: list[str] = []

    cleaned_query = query_text.strip()
    low_signal_phrases = (
        "幫我們找",
        "幫我找",
        "可以請ai旅遊行程助理幫我們找",
        "可以請ai幫我們找",
        "請ai幫我們找",
        "請幫我們找",
    )
    if cleaned_query and not any(phrase in cleaned_query.lower() for phrase in low_signal_phrases):
        parts.append(cleaned_query)

    for value in constraints or []:
        stripped = str(value).strip()
        if stripped and stripped not in parts:
            parts.append(stripped)

    for value in activity_types or []:
        stripped = str(value).strip()
        if stripped and stripped not in parts:
            parts.append(stripped)

    if normalized_location and normalized_location not in " ".join(parts):
        parts.append(normalized_location)

    joined = " ".join(parts)
    if effective_intent == "food":
        if "餐廳" not in joined and "美食" not in joined and "吃" not in joined:
            parts.append("餐廳")
    elif effective_intent == "attraction":
        if "景點" not in joined and "旅遊" not in joined and "玩的地方" not in joined:
            parts.append("景點")

    if not parts and normalized_location:
        parts.append(normalized_location)

    joined = " ".join(parts)
    if effective_intent == "food":
        joined = " ".join(parts)
        if "餐廳" not in joined and "美食" not in joined and "吃" not in joined:
            parts.append("餐廳")

    return " ".join(part for part in parts if part).strip()


def _build_google_places_recommendation(
    *,
    query_text: str,
    latitude: float,
    longitude: float,
    location_source: str,
    line_group_id: str = "",
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured.")

    cache_key = _build_google_places_cache_key(
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
        location_source=location_source,
    )
    cached = get_api_query_cache(
        "google_places_text_search",
        line_group_id,
        cache_key,
    )
    if isinstance(cached, dict):
        LOGGER.info("Google Places coordinate cache hit")
        return cached

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ",".join(
            [
                "places.displayName",
                "places.formattedAddress",
                "places.googleMapsUri",
                "places.location",
                "places.primaryTypeDisplayName",
                "places.rating",
                "places.priceLevel",
            ]
        ),
    }
    request_body = _build_google_places_request_body(
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
    )

    response = requests.post(
        GOOGLE_PLACES_TEXT_SEARCH_URL,
        headers=headers,
        json=request_body,
        timeout=LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    raw = response.json()

    places = raw.get("places") or []
    if not isinstance(places, list):
        places = []

    results = [
        _google_place_to_result(place, latitude, longitude)
        for place in places[:LIFF_RESULT_LIMIT]
        if isinstance(place, dict)
    ]
    results = [item for item in results if item.get("name")]
    results = _filter_results_by_constraints(results, constraints)
    LOGGER.info("Google Places coordinate search completed")

    payload = {
        "group_message": _format_group_message(results, query_text, location_source),
        "results": results,
        "location_source": location_source,
        "query_text": query_text,
        "provider": "google_places",
    }
    save_api_query_cache(
        "google_places_text_search",
        line_group_id,
        cache_key,
        payload,
        query_params=request_body,
        ttl_seconds=GOOGLE_PLACES_CACHE_TTL_SECONDS,
    )
    return payload


def _build_google_places_text_recommendation(
    *,
    query_text: str,
    location_text: str,
    constraints: list[str] | None = None,
    activity_types: list[str] | None = None,
    location_source: str = "text_location",
    line_group_id: str = "",
) -> dict[str, Any]:
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured.")

    text_query = _build_text_search_query(
        query_text=query_text,
        location_text=location_text,
        constraints=constraints,
        activity_types=activity_types,
    )
    if not text_query:
        raise RuntimeError("No text query available for Google Places text search.")

    cache_basis = {
        "text_query": text_query,
        "constraints": [str(item).strip() for item in constraints or [] if str(item).strip()],
        "activity_types": [
            str(item).strip() for item in activity_types or [] if str(item).strip()
        ],
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    cached = get_api_query_cache(
        "google_places_text_query",
        line_group_id,
        cache_key,
    )
    if isinstance(cached, dict):
        LOGGER.info("Google Places text cache hit")
        return cached

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ",".join(
            [
                "places.displayName",
                "places.formattedAddress",
                "places.googleMapsUri",
                "places.location",
                "places.primaryTypeDisplayName",
                "places.rating",
                "places.priceLevel",
            ]
        ),
    }
    request_body = _build_google_places_text_request_body(text_query=text_query)
    response = requests.post(
        GOOGLE_PLACES_TEXT_SEARCH_URL,
        headers=headers,
        json=request_body,
        timeout=LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    raw = response.json()

    places = raw.get("places") or []
    if not isinstance(places, list):
        places = []

    results: list[dict[str, Any]] = []
    for place in places[:LIFF_RESULT_LIMIT]:
        if not isinstance(place, dict):
            continue
        normalized = _google_place_to_result(place, 0.0, 0.0)
        normalized["distance_km"] = None
        results.append(normalized)
    results = _filter_results_by_constraints(results, constraints)
    LOGGER.info("Google Places text search completed")

    payload = {
        "group_message": _format_group_message(
            results,
            text_query,
            location_source,
        ),
        "results": results,
        "location_source": location_source,
        "query_text": text_query,
        "provider": "google_places_text",
    }
    save_api_query_cache(
        "google_places_text_query",
        line_group_id,
        cache_key,
        payload,
        query_params=request_body,
        ttl_seconds=GOOGLE_PLACES_CACHE_TTL_SECONDS,
    )
    return payload


def _build_spot_search_blob(spot: dict[str, Any]) -> str:
    return " ".join(
        str(
            spot.get(key) or ""
        ).strip()
        for key in (
            "itinerary_id",
            "itinerary_title",
            "itinerary_type",
            "itinerary_summary",
            "spot_name",
            "description",
        )
    )


def _intent_match_score(spot: dict[str, Any], intent: str) -> int:
    if intent != "food":
        return 0

    score = 0
    itinerary_id = str(spot.get("itinerary_id") or "").casefold()
    score += sum(3 for keyword in FOOD_ITINERARY_ID_KEYWORDS if keyword in itinerary_id)

    search_blob = _build_spot_search_blob(spot)
    score += min(4, _count_keyword_hits(search_blob, FOOD_CONTENT_KEYWORDS))
    return score


def _rank_local_fallback_spots(
    *,
    spots: list[dict[str, Any]],
    query_text: str,
    latitude: float,
    longitude: float,
) -> list[dict[str, Any]]:
    intent = _detect_query_intent(query_text)
    ranked: list[dict[str, Any]] = []

    for spot in spots:
        ranked.append(
            {
                "itinerary_id": spot.get("itinerary_id", ""),
                "itinerary_title": spot["itinerary_title"],
                "itinerary_type": spot.get("itinerary_type", ""),
                "spot_name": spot["spot_name"],
                "description": spot["description"],
                "latitude": spot["latitude"],
                "longitude": spot["longitude"],
                "distance_km": _haversine_km(
                    latitude,
                    longitude,
                    spot["latitude"],
                    spot["longitude"],
                ),
                "intent_score": _intent_match_score(spot, intent),
            }
        )

    if intent == "food":
        matched = [item for item in ranked if item["intent_score"] > 0]
        if matched:
            nearby_matched = [
                item
                for item in matched
                if item["distance_km"] <= OPENAI_FALLBACK_LOCAL_RADIUS_KM
            ]
            ranked = nearby_matched or []
        else:
            ranked = []

    ranked.sort(
        key=lambda item: (
            -item["intent_score"],
            item["distance_km"],
            item["spot_name"],
        )
    )
    return ranked[:LIFF_RESULT_LIMIT]


def _build_openai_candidate_pool(
    *,
    spots: list[dict[str, Any]],
    query_text: str,
    latitude: float,
    longitude: float,
) -> tuple[list[dict[str, Any]], bool]:
    intent = _detect_query_intent(query_text)
    enriched: list[dict[str, Any]] = []

    for spot in spots:
        distance_km = _haversine_km(
            latitude,
            longitude,
            spot["latitude"],
            spot["longitude"],
        )
        enriched.append(
            {
                "name": spot["spot_name"],
                "subtitle": spot["itinerary_title"],
                "description": spot["description"],
                "distance_km": round(distance_km, 2),
                "address": "",
                "maps_url": (
                    "https://www.google.com/maps/search/?api=1&query="
                    f"{spot['latitude']},{spot['longitude']}"
                ),
                "itinerary_id": spot.get("itinerary_id", ""),
                "itinerary_type": spot.get("itinerary_type", ""),
                "itinerary_summary": spot.get("itinerary_summary", ""),
                "intent_score": _intent_match_score(spot, intent),
            }
        )

    nearby_candidates = sorted(
        enriched,
        key=lambda item: (item["distance_km"], item["name"]),
    )[:OPENAI_FALLBACK_CANDIDATE_LIMIT]

    no_local_food_match = False
    if intent != "food":
        return nearby_candidates, no_local_food_match

    local_food_candidates = [
        item
        for item in enriched
        if item["intent_score"] > 0
        and item["distance_km"] <= OPENAI_FALLBACK_LOCAL_RADIUS_KM
    ]
    if not local_food_candidates:
        no_local_food_match = True
        return [], no_local_food_match

    prioritized = sorted(
        local_food_candidates,
        key=lambda item: (-item["intent_score"], item["distance_km"], item["name"]),
    )[:OPENAI_FALLBACK_CANDIDATE_LIMIT]
    return prioritized, no_local_food_match


def _result_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(candidate.get("name") or "未命名推薦").strip(),
        "subtitle": str(candidate.get("subtitle") or "").strip(),
        "description": str(candidate.get("description") or "").strip(),
        "distance_km": _coerce_float(candidate.get("distance_km")),
        "address": str(candidate.get("address") or "").strip(),
        "maps_url": str(candidate.get("maps_url") or "").strip(),
    }


def _build_missing_food_sample_recommendation(
    *,
    query_text: str,
    location_source: str,
    prefix: str = "",
) -> dict[str, Any]:
    lines: list[str] = []
    if prefix:
        lines.append(prefix)
    lines.append(
        f"本地示範資料目前沒有你附近 {OPENAI_FALLBACK_LOCAL_RADIUS_KM:.0f} 公里內的美食樣本。"
    )
    lines.append("因此先不顯示不相干的玩樂景點。")
    lines.append("如果要測真正的附近美食，之後建議接推薦後端或地圖 API。")
    if query_text:
        lines.append(f"原始需求：{query_text}")

    return {
        "group_message": "\n".join(lines).strip(),
        "results": [],
        "location_source": location_source,
        "query_text": query_text,
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = "".join(part for index, part in enumerate(parts) if index % 2 == 1 or part.strip()).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("OpenAI fallback did not return a JSON object.")
    return parsed


def _build_openai_fallback_recommendation(
    *,
    query_text: str,
    latitude: float,
    longitude: float,
    location_source: str,
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed.") from exc

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    spots = _load_catalog_spots()
    candidates, no_local_food_match = _build_openai_candidate_pool(
        spots=spots,
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
    )
    intent = _detect_query_intent(query_text)

    if intent == "food" and no_local_food_match:
        return _build_missing_food_sample_recommendation(
            query_text=query_text,
            location_source=location_source,
        )

    if not candidates:
        return _build_local_fallback_recommendation(
            query_text=query_text,
            latitude=latitude,
            longitude=longitude,
            location_source=location_source,
        )

    client = OpenAI(api_key=api_key)
    system_prompt = (
        "You are helping format nearby travel recommendations for a LINE bot in Taiwan. "
        "Use only the provided candidate list. "
        "Return one strict JSON object with keys group_message and results. "
        f"results must contain at most {LIFF_RESULT_LIMIT} items. "
            "Each result item must include name, subtitle, description, distance_km, address, and maps_url. "
        "Write group_message in Traditional Chinese. "
        "Prefer closer candidates. "
        "Do not invent places outside the candidate list."
    )
    user_prompt = json.dumps(
        {
            "query_text": query_text,
            "location_source": location_source,
            "user_location": {
                "latitude": latitude,
                "longitude": longitude,
            },
            "no_local_food_match": no_local_food_match,
            "instruction": (
                "If the user is asking for food and no_local_food_match is true, "
                "state that the local demo dataset currently has no nearby food samples, "
                "then provide the closest reasonable backup options from the candidates."
            ),
            "candidates": candidates,
        },
        ensure_ascii=False,
    )

    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    message_content = response.choices[0].message.content or "{}"
    raw = _extract_json_object(message_content)
    normalized = _normalize_backend_response(
        raw=raw,
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
        location_source=location_source,
    )
    results = list(normalized.get("results") or [])
    if not results:
        results = [
            _result_from_candidate(candidate)
            for candidate in candidates[:LIFF_RESULT_LIMIT]
        ]
        normalized["results"] = results

    normalized["group_message"] = _format_group_message(
        results,
        query_text,
        location_source,
    )
    normalized["fallback_provider"] = "openai"
    return normalized


def _registry_path() -> Path:
    configured = os.getenv("BEACON_REGISTRY_PATH", "").strip()
    if configured:
        return (BASE_DIR / configured).resolve()
    return DEFAULT_BEACON_REGISTRY_PATH


def _load_beacon_registry() -> dict[str, dict[str, Any]]:
    path = _registry_path()
    if not path.exists():
        return {}

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}

    cached_path = _registry_cache.get("path")
    cached_mtime = _registry_cache.get("mtime")
    if cached_path == str(path) and cached_mtime == mtime:
        return _registry_cache.get("data", {})

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}

    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((str(item.get("hwid", "")), item) for item in raw if isinstance(item, dict))
    else:
        items = ()

    for hwid, payload in items:
        if not hwid:
            continue
        if not isinstance(payload, dict):
            continue
        normalized[str(hwid).strip().lower()] = payload

    _registry_cache["path"] = str(path)
    _registry_cache["mtime"] = mtime
    _registry_cache["data"] = normalized
    return normalized


def register_beacon_event(
    line_user_id: str,
    hwid: str,
    beacon_type: str,
    device_message: str = "",
) -> BeaconContext:
    registry = _load_beacon_registry()
    registered = registry.get(str(hwid).strip().lower(), {})
    context = BeaconContext(
        line_user_id=line_user_id,
        hwid=str(hwid).strip(),
        beacon_type=str(beacon_type or "enter"),
        name=str(registered.get("name") or registered.get("title") or "").strip(),
        address=str(registered.get("address") or "").strip(),
        latitude=_coerce_float(registered.get("latitude") or registered.get("lat")),
        longitude=_coerce_float(registered.get("longitude") or registered.get("lng")),
        detected_at=time.time(),
        raw={
            "device_message": device_message,
            "registry": registered,
        },
    )

    with _beacon_lock:
        _prune_state_locked()
        _beacon_contexts[line_user_id] = context
    return context


def get_recent_beacon_context(line_user_id: str) -> BeaconContext | None:
    with _beacon_lock:
        _prune_state_locked()
        context = _beacon_contexts.get(line_user_id)
        if context is None:
            return None
        if (time.time() - context.detected_at) > BEACON_CONTEXT_TTL_SECONDS:
            _beacon_contexts.pop(line_user_id, None)
            return None
        return context


def create_recommendation_session(
    *,
    token: str,
    push_target_id: str,
    conversation_key: str,
    line_user_id: str,
    line_group_id: str,
    query_text: str,
) -> RecommendationSession:
    session = RecommendationSession(
        token=token,
        push_target_id=push_target_id,
        conversation_key=conversation_key,
        line_user_id=line_user_id,
        line_group_id=line_group_id,
        query_text=query_text,
        created_at=time.time(),
    )
    with _session_lock:
        _prune_state_locked()
        _recommendation_sessions[token] = session
    return session


def get_recommendation_session(session_token: str) -> RecommendationSession | None:
    with _session_lock:
        _prune_state_locked()
        session = _recommendation_sessions.get(session_token)
        if session is None:
            return None
        if (time.time() - session.created_at) > LIFF_SESSION_TTL_SECONDS:
            _recommendation_sessions.pop(session_token, None)
            return None
        return session


def claim_recommendation_session(
    session_token: str,
    line_user_id: str,
) -> tuple[RecommendationSession | None, str]:
    """Atomically bind and transition a pending LIFF session to processing."""
    with _session_lock:
        _prune_state_locked()
        session = _recommendation_sessions.get(session_token)
        if session is None:
            return None, "expired"
        if session.line_user_id and session.line_user_id != line_user_id:
            return None, "forbidden"
        if not session.line_user_id:
            session.line_user_id = line_user_id
        if session.status != "pending":
            return None, "used"
        session.status = "processing"
        return session, ""


def save_recent_location_context(
    *,
    conversation_key: str,
    line_user_id: str,
    line_group_id: str,
    latitude: float,
    longitude: float,
    accuracy: float | None,
) -> RecentLocationContext:
    context = RecentLocationContext(
        conversation_key=conversation_key,
        line_user_id=line_user_id,
        line_group_id=line_group_id,
        latitude=latitude,
        longitude=longitude,
        accuracy=accuracy,
        saved_at=time.time(),
    )
    with _recent_location_lock:
        _prune_state_locked()
        _recent_location_contexts[conversation_key] = context
    return context


def get_recent_location_context(
    *,
    conversation_key: str,
) -> RecentLocationContext | None:
    with _recent_location_lock:
        _prune_state_locked()
        context = _recent_location_contexts.get(conversation_key)
        if context is None:
            return None
        if (time.time() - context.saved_at) > RECENT_LOCATION_CONTEXT_TTL_SECONDS:
            _recent_location_contexts.pop(conversation_key, None)
            return None
        return context


def clear_recent_location_context(conversation_key: str) -> None:
    with _recent_location_lock:
        _recent_location_contexts.pop(conversation_key, None)


def build_liff_url(session_token: str, request_base_url: str) -> str:
    endpoint = os.getenv("LIFF_LOCATION_ENDPOINT_URL", "").strip()
    if not endpoint:
        endpoint = f"{request_base_url.rstrip('/')}/liff/location"

    endpoint_parts = urlsplit(endpoint)
    query_params = [
        (key, value)
        for key, value in parse_qsl(endpoint_parts.query, keep_blank_values=True)
        if key != "session_token"
    ]
    liff_id = os.getenv("LIFF_ID", "").strip()
    if liff_id:
        query_params = [(key, value) for key, value in query_params if key != "liff_id"]
        query_params.append(("liff_id", liff_id))
    query_params.append(("session_token", session_token))

    return urlunsplit(
        (
            endpoint_parts.scheme,
            endpoint_parts.netloc,
            endpoint_parts.path,
            urlencode(query_params),
            "",
        )
    )


def run_location_recommendation(
    *,
    line_user_id: str,
    line_group_id: str,
    query_text: str,
    latitude: float,
    longitude: float,
    accuracy: float | None,
    location_source: str,
    beacon_context: BeaconContext | None = None,
) -> dict[str, Any]:
    try:
        backend_url = os.getenv("LOCATION_RECOMMENDATION_API_URL", "").strip()
        if backend_url:
            return _request_backend_recommendation(
                backend_url=backend_url,
                line_user_id=line_user_id,
                line_group_id=line_group_id,
                query_text=query_text,
                latitude=latitude,
                longitude=longitude,
                accuracy=accuracy,
                location_source=location_source,
                beacon_context=beacon_context,
            )

        if os.getenv("GOOGLE_PLACES_API_KEY", "").strip():
            try:
                return _build_google_places_recommendation(
                    query_text=query_text,
                    latitude=latitude,
                    longitude=longitude,
                    location_source=location_source,
                    line_group_id=line_group_id,
                )
            except Exception as exc:
                _log_external_failure("Google Places recommendation", exc)

        if os.getenv("OPENAI_API_KEY", "").strip():
            try:
                return _build_openai_fallback_recommendation(
                    query_text=query_text,
                    latitude=latitude,
                    longitude=longitude,
                    location_source=location_source,
                )
            except Exception as exc:
                _log_external_failure("OpenAI location fallback", exc)

        return _build_local_fallback_recommendation(
            query_text=query_text,
            latitude=latitude,
            longitude=longitude,
            location_source=location_source,
        )
    except Exception as exc:
        _log_external_failure("Location recommendation pipeline", exc)
        return {
            "group_message": "我有收到你的位置，但這次整理附近推薦時出了點問題，請稍後再試一次。",
            "results": [],
            "location_source": location_source,
            "query_text": query_text,
        }


def run_text_location_recommendation(
    *,
    query_text: str,
    location_text: str,
    constraints: list[str] | None = None,
    activity_types: list[str] | None = None,
    line_group_id: str = "",
) -> dict[str, Any]:
    tourism_payload = _build_tourism_text_recommendation(
        query_text=query_text,
        location_text=location_text,
        activity_types=activity_types,
    )
    if tourism_payload and tourism_payload.get("tourism_kind") == "event":
        return tourism_payload

    if os.getenv("GOOGLE_PLACES_API_KEY", "").strip():
        google_payload = _build_google_places_text_recommendation(
            query_text=query_text,
            location_text=location_text,
            constraints=constraints,
            activity_types=activity_types,
            location_source="text_location",
            line_group_id=line_group_id,
        )
        if tourism_payload:
            merged_results = _merge_recommendation_results(
                list(tourism_payload.get("results") or []),
                list(google_payload.get("results") or []),
                limit=LIFF_RESULT_LIMIT,
            )
            merged_query_text = str(
                tourism_payload.get("query_text")
                or google_payload.get("query_text")
                or query_text
            )
            return {
                "group_message": _format_group_message(
                    merged_results,
                    merged_query_text,
                    "text_location",
                ),
                "results": merged_results,
                "location_source": "text_location",
                "query_text": merged_query_text,
                "provider": "tourism_open_data+google_places_text",
                "tourism_city": tourism_payload.get("tourism_city"),
            }
        return google_payload

    if tourism_payload:
        return tourism_payload

    raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured.")


def finalize_session_result(session_token: str, result: dict[str, Any]) -> RecommendationSession | None:
    with _session_lock:
        session = _recommendation_sessions.get(session_token)
        if session is None:
            return None
        session.status = "completed"
        session.result = result
        return session


def mark_session_failed(session_token: str, result: dict[str, Any]) -> RecommendationSession | None:
    with _session_lock:
        session = _recommendation_sessions.get(session_token)
        if session is None:
            return None
        session.status = "failed"
        session.result = result
        return session


def _prune_state_locked() -> None:
    now = time.time()
    expired_users = [
        line_user_id
        for line_user_id, context in _beacon_contexts.items()
        if (now - context.detected_at) > BEACON_CONTEXT_TTL_SECONDS
    ]
    for line_user_id in expired_users:
        _beacon_contexts.pop(line_user_id, None)

    expired_sessions = [
        token
        for token, session in _recommendation_sessions.items()
        if (now - session.created_at) > LIFF_SESSION_TTL_SECONDS
    ]
    for token in expired_sessions:
        _recommendation_sessions.pop(token, None)

    expired_recent_locations = [
        conversation_key
        for conversation_key, context in _recent_location_contexts.items()
        if (now - context.saved_at) > RECENT_LOCATION_CONTEXT_TTL_SECONDS
    ]
    for conversation_key in expired_recent_locations:
        _recent_location_contexts.pop(conversation_key, None)


def _request_backend_recommendation(
    *,
    backend_url: str,
    line_user_id: str,
    line_group_id: str,
    query_text: str,
    latitude: float,
    longitude: float,
    accuracy: float | None,
    location_source: str,
    beacon_context: BeaconContext | None,
) -> dict[str, Any]:
    _validate_recommendation_backend_url(backend_url)
    api_key = os.getenv("LOCATION_RECOMMENDATION_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("LOCATION_RECOMMENDATION_API_KEY is required.")

    payload = {
        "query_text": query_text,
        "line_user_id": line_user_id,
        "line_group_id": line_group_id,
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_meters": accuracy,
            "source": location_source,
        },
    }
    if beacon_context is not None:
        payload["beacon"] = {
            "hwid": beacon_context.hwid,
            "name": beacon_context.name,
            "address": beacon_context.address,
            "detected_at": beacon_context.detected_at,
        }

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    response = requests.post(
        backend_url,
        json=payload,
        headers=headers,
        timeout=LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        raise RuntimeError("Recommendation backend redirects are not allowed.")
    response.raise_for_status()

    content_length = response.headers.get("Content-Length", "").strip()
    if content_length:
        try:
            if int(content_length) > LOCATION_RECOMMENDATION_MAX_RESPONSE_BYTES:
                raise RuntimeError("Recommendation backend response is too large.")
        except ValueError as exc:
            raise RuntimeError("Recommendation backend returned an invalid Content-Length.") from exc
    response_body = response.content
    if len(response_body) > LOCATION_RECOMMENDATION_MAX_RESPONSE_BYTES:
        raise RuntimeError("Recommendation backend response is too large.")

    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise RuntimeError("Recommendation backend must return application/json.")
    try:
        raw = response.json()
    except ValueError as exc:
        raise RuntimeError("Recommendation backend returned invalid JSON.") from exc

    normalized = _normalize_backend_response(
        raw=raw,
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
        location_source=location_source,
    )
    return normalized


def _normalize_backend_response(
    *,
    raw: Any,
    query_text: str,
    latitude: float,
    longitude: float,
    location_source: str,
) -> dict[str, Any]:
    if isinstance(raw, list):
        results = [_normalize_result_item(item, latitude, longitude) for item in raw]
        group_message = _sanitize_group_message_links(
            _format_group_message(results, query_text, location_source)
        )
        return {
            "group_message": group_message,
            "results": results,
            "location_source": location_source,
            "query_text": query_text,
        }

    if not isinstance(raw, dict):
        return {
            "group_message": _sanitize_group_message_links(str(raw)),
            "results": [],
            "location_source": location_source,
            "query_text": query_text,
        }

    results_raw = (
        raw.get("results")
        or raw.get("recommendations")
        or raw.get("items")
        or raw.get("data")
        or []
    )
    if isinstance(results_raw, dict):
        results_raw = results_raw.get("items") or results_raw.get("results") or []
    if not isinstance(results_raw, list):
        results_raw = []

    results = [
        _normalize_result_item(item, latitude, longitude)
        for item in results_raw
        if item is not None
    ]
    group_message = (
        raw.get("group_message")
        or raw.get("groupMessage")
        or raw.get("reply")
        or raw.get("message")
        or raw.get("text")
        or ""
    )
    if not group_message:
        group_message = _format_group_message(results, query_text, location_source)

    return {
        "group_message": _sanitize_group_message_links(str(group_message).strip()),
        "results": results,
        "location_source": location_source,
        "query_text": query_text,
    }


def _normalize_result_item(item: Any, origin_latitude: float, origin_longitude: float) -> dict[str, Any]:
    if isinstance(item, str):
        return {
            "name": item,
            "subtitle": "",
            "description": "",
            "distance_km": None,
            "address": "",
            "maps_url": "",
        }

    if not isinstance(item, dict):
        return {
            "name": str(item),
            "subtitle": "",
            "description": "",
            "distance_km": None,
            "address": "",
            "maps_url": "",
        }

    name = str(
        item.get("name")
        or item.get("title")
        or item.get("spot_name")
        or item.get("restaurant_name")
        or "未命名推薦"
    ).strip()
    subtitle = str(
        item.get("subtitle")
        or item.get("category")
        or item.get("region")
        or item.get("type")
        or ""
    ).strip()
    description = str(
        item.get("description")
        or item.get("summary")
        or item.get("comment")
        or ""
    ).strip()
    address = str(item.get("address") or "").strip()

    latitude = _coerce_float(item.get("latitude") or item.get("lat"))
    longitude = _coerce_float(item.get("longitude") or item.get("lng"))
    distance_km = _coerce_float(item.get("distance_km") or item.get("distanceKm"))
    if distance_km is None and latitude is not None and longitude is not None:
        distance_km = _haversine_km(origin_latitude, origin_longitude, latitude, longitude)

    maps_url = _sanitize_location_link(
        str(item.get("maps_url") or item.get("mapsUrl") or "").strip()
    )
    if not maps_url and latitude is not None and longitude is not None:
        maps_url = f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"

    return {
        "name": name,
        "subtitle": subtitle,
        "description": description,
        "distance_km": round(distance_km, 2) if distance_km is not None else None,
        "address": address,
        "maps_url": maps_url,
    }


def _log_external_failure(operation: str, exc: Exception) -> None:
    """Log enough for operations without leaking requests, locations, or responses."""
    LOGGER.warning("%s failed (%s)", operation, type(exc).__name__)


def _split_configured_hosts(name: str, defaults: tuple[str, ...] = ()) -> set[str]:
    configured = os.getenv(name, "")
    values = configured.split(",") if configured.strip() else defaults
    return {value.strip().lower().rstrip(".") for value in values if value.strip()}


def _validate_recommendation_backend_url(backend_url: str) -> None:
    parsed = urlsplit(backend_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError("LOCATION_RECOMMENDATION_API_URL must be an HTTPS URL without credentials or fragments.")

    allowed_hosts = _split_configured_hosts("LOCATION_RECOMMENDATION_ALLOWED_HOSTS")
    if not allowed_hosts:
        raise RuntimeError("LOCATION_RECOMMENDATION_ALLOWED_HOSTS is required.")
    if parsed.hostname.lower().rstrip(".") not in allowed_hosts:
        raise RuntimeError("LOCATION_RECOMMENDATION_API_URL host is not allowed.")


def _sanitize_location_link(url: str) -> str:
    if not url or len(url) > 2048:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    allowed_hosts = _split_configured_hosts(
        "LOCATION_ALLOWED_LINK_HOSTS",
        DEFAULT_LOCATION_LINK_HOSTS,
    )
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname.lower().rstrip(".") not in allowed_hosts
    ):
        return ""
    return url


def _sanitize_group_message_links(message: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        url = raw_url.rstrip(URL_TRAILING_PUNCTUATION)
        trailing = raw_url[len(url):]
        safe_url = _sanitize_location_link(url)
        return f"{safe_url or '[已移除不受信任連結]'}{trailing}"

    return EXTERNAL_URL_PATTERN.sub(replace_url, message)


def _build_local_fallback_recommendation(
    *,
    query_text: str,
    latitude: float,
    longitude: float,
    location_source: str,
) -> dict[str, Any]:
    spots = _load_catalog_spots()
    ranked = _rank_local_fallback_spots(
        spots=spots,
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
    )
    intent = _detect_query_intent(query_text)

    if intent == "food" and not ranked:
        return _build_missing_food_sample_recommendation(
            query_text=query_text,
            location_source=location_source,
        )

    results = [
        {
            "name": item["spot_name"],
            "subtitle": item["itinerary_title"],
            "description": item["description"],
            "distance_km": round(item["distance_km"], 2),
            "address": "",
            "maps_url": (
                f"https://www.google.com/maps/search/?api=1&query="
                f"{item['latitude']},{item['longitude']}"
            ),
        }
        for item in ranked
    ]

    group_message = _format_group_message(results, query_text, location_source)
    return {
        "group_message": group_message,
        "results": results,
        "location_source": location_source,
        "query_text": query_text,
    }


def _load_catalog_spots() -> list[dict[str, Any]]:
    path = DEFAULT_ITINERARY_DATA_PATH
    if not path.exists():
        return []

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []

    cached_mtime = _catalog_cache.get("mtime")
    if cached_mtime == mtime:
        return _catalog_cache.get("data", [])

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = []

    spots: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for itinerary in raw:
            if not isinstance(itinerary, dict):
                continue
            itinerary_id = str(itinerary.get("id") or "").strip()
            itinerary_title = str(itinerary.get("title") or "未命名行程").strip()
            itinerary_type = str(itinerary.get("type") or "").strip()
            itinerary_summary = str(
                itinerary.get("summary")
                or itinerary.get("description")
                or ""
            ).strip()
            for spot in itinerary.get("spots") or []:
                if not isinstance(spot, dict):
                    continue
                lat = _coerce_float(spot.get("lat") or spot.get("latitude"))
                lng = _coerce_float(spot.get("lng") or spot.get("longitude"))
                if lat is None or lng is None:
                    continue
                spots.append(
                    {
                        "itinerary_id": itinerary_id,
                        "itinerary_title": itinerary_title,
                        "itinerary_type": itinerary_type,
                        "itinerary_summary": itinerary_summary,
                        "spot_name": str(spot.get("name") or "未命名景點").strip(),
                        "description": str(spot.get("description") or "").strip(),
                        "latitude": lat,
                        "longitude": lng,
                    }
                )

    _catalog_cache["mtime"] = mtime
    _catalog_cache["data"] = spots
    return spots


def _format_group_message(
    results: list[dict[str, Any]],
    query_text: str,
    location_source: str,
    *,
    prefix: str = "",
) -> str:
    lines: list[str] = []
    if prefix:
        lines.append(prefix)

    intent = _detect_query_intent(query_text)
    is_text_location = location_source == "text_location"

    if intent == "food":
        if query_text:
            lines.append(f"我幫你看了一下，這幾家餐廳可以先參考：")
        else:
            lines.append("我幫你看了一下，這幾家餐廳可以先參考：")
    else:
        if location_source == "beacon":
            source_label = "Beacon 定位"
        elif location_source in {"liff", "manual_location"}:
            source_label = "手機定位"
        elif is_text_location:
            source_label = "文字地點查詢"
        else:
            source_label = "定位"

        if "活動" in query_text or "展覽" in query_text or "節慶" in query_text or "市集" in query_text:
            lines.append("我幫你看了一下，這幾個近期活動可以先參考：")
        elif intent == "attraction":
            lines.append("我幫你看了一下，附近有幾個可以去走走的地方：")
        elif "購物" in query_text or "逛" in query_text or "百貨" in query_text or "夜市" in query_text:
            lines.append("我幫你找了幾個附近可以逛的地方：")
        elif query_text:
            lines.append(f"我幫你看了一下，這幾個{source_label}附近的選項可以先參考：")
        else:
            lines.append(f"我幫你看了一下，這幾個{source_label}附近的選項可以先參考：")

    if not results:
        lines.append("我這次沒有找到比較適合的結果，要不要換個類型試試看？")
    else:
        for index, item in enumerate(results[:GROUP_RESULT_LIMIT], start=1):
            lines.append(f"{index}. {item.get('name', '未命名推薦')}")

            subtitle = str(item.get("subtitle") or "").strip()
            distance_km = item.get("distance_km")
            description = str(item.get("description") or "").strip()

            if intent == "food":
                if subtitle and distance_km is not None:
                    lines.append(f"地址：{subtitle}")
                    lines.append(f"距離：約 {distance_km:.2f} 公里")
                elif subtitle:
                    lines.append(f"地址：{subtitle}")
                elif distance_km is not None:
                    lines.append(f"距離：約 {distance_km:.2f} 公里")
            else:
                if subtitle and distance_km is not None:
                    lines.append(f"地址：{subtitle}")
                    lines.append(f"距離：約 {distance_km:.2f} 公里")
                elif subtitle:
                    lines.append(f"地址：{subtitle}")
                elif distance_km is not None:
                    lines.append(f"距離：約 {distance_km:.2f} 公里")

            if description:
                detail_line = description
                if "｜" in description:
                    parts = [part.strip() for part in description.split("｜") if part.strip()]
                    normalized_parts: list[str] = []
                    for part in parts:
                        if part.startswith("評分"):
                            normalized_parts.append(
                                part if "：" in part else part.replace("評分", "評分：", 1)
                            )
                        elif part.startswith("類型"):
                            normalized_parts.append(
                                part if "：" in part else part.replace("類型", "類型：", 1)
                            )
                        elif part.startswith(("門票", "票價", "網址", "連結", "開始", "結束")):
                            normalized_parts.append(part)
                        else:
                            normalized_parts.append(f"類型：{part}")
                    detail_line = "｜".join(normalized_parts)
                lines.append(detail_line)

            if index != min(len(results), GROUP_RESULT_LIMIT):
                lines.append("")

    return "\n".join(lines).strip()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_km * c
