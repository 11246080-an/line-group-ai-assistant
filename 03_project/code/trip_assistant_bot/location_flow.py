from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests


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
LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS = _env_float(
    "LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS",
    10.0,
)
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


_beacon_lock = threading.Lock()
_session_lock = threading.Lock()
_beacon_contexts: dict[str, BeaconContext] = {}
_recommendation_sessions: dict[str, RecommendationSession] = {}
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
    return "general"


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


def build_liff_url(session_token: str, request_base_url: str) -> str:
    endpoint = os.getenv("LIFF_LOCATION_ENDPOINT_URL", "").strip()
    if not endpoint:
        endpoint = f"{request_base_url.rstrip('/')}/liff/location"

    params = {
        "session_token": session_token,
    }
    liff_id = os.getenv("LIFF_ID", "").strip()
    if liff_id:
        params["liff_id"] = liff_id

    return f"{endpoint}?{urlencode(params)}"


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

    if os.getenv("OPENAI_API_KEY", "").strip():
        try:
            return _build_openai_fallback_recommendation(
                query_text=query_text,
                latitude=latitude,
                longitude=longitude,
                location_source=location_source,
            )
        except Exception as exc:
            print(f"OpenAI location fallback failed, using local demo fallback: {exc}")

    return _build_local_fallback_recommendation(
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
        location_source=location_source,
    )


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

    headers: dict[str, str] = {}
    api_key = os.getenv("LOCATION_RECOMMENDATION_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(
        backend_url,
        json=payload,
        headers=headers,
        timeout=LOCATION_RECOMMENDATION_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    try:
        raw = response.json()
    except ValueError:
        raw = {"group_message": response.text}

    normalized = _normalize_backend_response(
        raw=raw,
        query_text=query_text,
        latitude=latitude,
        longitude=longitude,
        location_source=location_source,
    )
    normalized["backend_payload"] = payload
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
        group_message = _format_group_message(results, query_text, location_source)
        return {
            "group_message": group_message,
            "results": results,
            "location_source": location_source,
            "query_text": query_text,
        }

    if not isinstance(raw, dict):
        return {
            "group_message": str(raw),
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
        "group_message": str(group_message).strip(),
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

    maps_url = str(item.get("maps_url") or item.get("mapsUrl") or "").strip()
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

    if location_source == "beacon":
        source_label = "Beacon 定位"
    elif location_source in {"liff", "manual_location"}:
        source_label = "手機定位"
    else:
        source_label = "定位"

    if query_text:
        lines.append(
            f'根據您的原始需求：「{query_text}」，結合本次{source_label}結果，為您推薦以下行程：'
        )
    else:
        lines.append(f"結合本次{source_label}結果，為您推薦以下行程：")

    if not results:
        lines.append("目前沒有拿到合適的推薦結果，請稍後再試一次。")
    else:
        for index, item in enumerate(results[:GROUP_RESULT_LIMIT], start=1):
            lines.append(f"{index}. {item.get('name', '未命名推薦')}")

            subtitle = str(item.get("subtitle") or "").strip()
            distance_km = item.get("distance_km")
            description = str(item.get("description") or "").strip()

            if subtitle and distance_km is not None:
                lines.append(f"所屬行程：{subtitle}（約 {distance_km:.2f} 公里）")
            elif subtitle:
                lines.append(f"所屬行程：{subtitle}")
            elif distance_km is not None:
                lines.append(f"距離：約 {distance_km:.2f} 公里")

            if description:
                lines.append(f"特點：{description}")

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
