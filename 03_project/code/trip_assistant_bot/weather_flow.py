from __future__ import annotations

from datetime import datetime
import hashlib
import os
from typing import Any

import requests

from db import get_api_query_cache, save_api_query_cache


CWA_FORECAST_36H_URL = (
    "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
)
CWA_API_TIMEOUT_SECONDS = float(os.getenv("CWA_API_TIMEOUT_SECONDS", "10"))
CWA_WEATHER_CACHE_TTL_SECONDS = max(
    60,
    int(os.getenv("CWA_WEATHER_CACHE_TTL_SECONDS", "3600")),
)

COUNTY_ALIASES = {
    "基隆": "基隆市",
    "臺北": "臺北市",
    "台北": "臺北市",
    "新北": "新北市",
    "桃園": "桃園市",
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
    "花蓮": "花蓮縣",
    "臺東": "臺東縣",
    "台東": "臺東縣",
    "澎湖": "澎湖縣",
    "金門": "金門縣",
    "連江": "連江縣",
}


def _normalize_county_name(location_text: str, query_text: str = "") -> str:
    candidates = [str(location_text or "").strip(), str(query_text or "").strip()]
    for candidate in candidates:
        if not candidate:
            continue
        for alias in sorted(COUNTY_ALIASES, key=len, reverse=True):
            if alias and alias in candidate:
                return COUNTY_ALIASES[alias]
    return ""


def _build_weather_cache_key(county_name: str, query_text: str, time_text: str) -> str:
    payload = f"{county_name}|{query_text.strip()}|{time_text.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_time_map(location_record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    element_map: dict[str, list[dict[str, Any]]] = {}
    for element in location_record.get("weatherElement") or []:
        if not isinstance(element, dict):
            continue
        element_name = str(element.get("elementName") or "").strip()
        if not element_name:
            continue
        element_map[element_name] = list(element.get("time") or [])
    return element_map


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _pick_time_index(time_entries: list[dict[str, Any]], time_text: str, query_text: str) -> int:
    if not time_entries:
        return 0

    combined = f"{time_text} {query_text}".strip()
    if not combined:
        return 0

    if "明天" in combined:
        for index, entry in enumerate(time_entries):
            start_time = str(entry.get("startTime") or "")
            try:
                parsed = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.date() > datetime.now(parsed.tzinfo).date():
                return index
        return min(1, len(time_entries) - 1)

    if _contains_any(combined, ("今晚", "今天晚上", "晚上")):
        for index, entry in enumerate(time_entries):
            start_time = str(entry.get("startTime") or "")
            try:
                parsed = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.hour >= 18:
                return index

    return 0


def _get_parameter_name(
    element_map: dict[str, list[dict[str, Any]]],
    element_name: str,
    index: int,
) -> str:
    time_entries = element_map.get(element_name) or []
    if not time_entries:
        return ""
    safe_index = min(index, len(time_entries) - 1)
    parameter = time_entries[safe_index].get("parameter") or {}
    return str(parameter.get("parameterName") or "").strip()


def _format_time_label(start_time: str, end_time: str) -> str:
    try:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (
        f"{start.month}/{start.day} "
        f"{start.hour:02d}:00 - {end.month}/{end.day} {end.hour:02d}:00"
    )


def _build_weather_message(
    *,
    county_name: str,
    query_text: str,
    time_text: str,
    location_record: dict[str, Any],
) -> str:
    element_map = _extract_time_map(location_record)
    wx_entries = element_map.get("Wx") or []
    if not wx_entries:
        return f"我幫你查了 {county_name} 的天氣，但這次沒有拿到完整預報資料。"

    time_index = _pick_time_index(wx_entries, time_text, query_text)
    slot = wx_entries[min(time_index, len(wx_entries) - 1)]
    start_time = str(slot.get("startTime") or "")
    end_time = str(slot.get("endTime") or "")
    time_label = _format_time_label(start_time, end_time)

    weather = _get_parameter_name(element_map, "Wx", time_index)
    pop = _get_parameter_name(element_map, "PoP", time_index)
    min_temp = _get_parameter_name(element_map, "MinT", time_index)
    max_temp = _get_parameter_name(element_map, "MaxT", time_index)
    comfort = _get_parameter_name(element_map, "CI", time_index)

    lines = [f"我幫你查了一下，{county_name}接下來的天氣大致如下："]
    if time_label:
        lines.append(f"時段：{time_label}")
    if weather:
        lines.append(f"天氣：{weather}")
    if pop:
        lines.append(f"降雨機率：{pop}%")
    if min_temp or max_temp:
        lines.append(f"氣溫：{min_temp} - {max_temp} 度")
    if comfort:
        lines.append(f"體感：{comfort}")

    weather_risk = False
    try:
        weather_risk = int(pop or "0") >= 40
    except ValueError:
        weather_risk = False
    if "雨" in weather:
        weather_risk = True

    if weather_risk:
        lines.append("如果你們是戶外行程，建議先準備雨具，也可以順手想一下室內備案。")
    else:
        lines.append("如果你們要安排外出行程，這個時段看起來算蠻可以的。")

    return "\n".join(lines).strip()


def run_weather_recommendation(
    *,
    query_text: str,
    location_text: str = "",
    time_text: str = "",
) -> dict[str, Any]:
    county_name = _normalize_county_name(location_text, query_text)
    if not county_name:
        return {
            "provider": "cwa_weather",
            "county_name": "",
            "query_text": query_text,
            "group_message": "你們想查哪個地區的天氣呢？像是台北、宜蘭、台中都可以。",
            "results": [],
        }

    authorization_key = os.getenv("CWA_AUTHORIZATION_KEY", "").strip()
    if not authorization_key:
        raise RuntimeError("CWA_AUTHORIZATION_KEY is not configured.")

    cache_key = _build_weather_cache_key(county_name, query_text, time_text)
    cached = get_api_query_cache("weather_cwa_36h", cache_key)
    if isinstance(cached, dict):
        print(
            "CWA weather cache hit:",
            {"county_name": county_name, "query_text": query_text},
        )
        return cached

    params = {
        "Authorization": authorization_key,
        "format": "JSON",
        "locationName": county_name,
    }
    response = requests.get(
        CWA_FORECAST_36H_URL,
        params=params,
        timeout=CWA_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    raw = response.json()

    records = raw.get("records") or {}
    locations = records.get("location") or []
    if not locations:
        raise RuntimeError(f"CWA weather returned no location data for {county_name}.")

    location_record = locations[0]
    group_message = _build_weather_message(
        county_name=county_name,
        query_text=query_text,
        time_text=time_text,
        location_record=location_record,
    )
    payload = {
        "provider": "cwa_weather",
        "county_name": county_name,
        "query_text": query_text,
        "group_message": group_message,
        "results": [],
    }
    save_api_query_cache(
        "weather_cwa_36h",
        cache_key,
        payload,
        query_params=params,
        ttl_seconds=CWA_WEATHER_CACHE_TTL_SECONDS,
    )
    return payload
