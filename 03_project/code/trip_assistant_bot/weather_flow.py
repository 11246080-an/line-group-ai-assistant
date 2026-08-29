from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

import requests

from db import get_weather_daily_cache, save_weather_daily_cache


CWA_FORECAST_36H_URL = (
    "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
)
CWA_API_TIMEOUT_SECONDS = float(os.getenv("CWA_API_TIMEOUT_SECONDS", "10"))
CWA_DAILY_CACHE_TTL_SECONDS = max(
    3600,
    int(os.getenv("CWA_DAILY_CACHE_TTL_SECONDS", str(2 * 24 * 60 * 60))),
)
TAIPEI_TZ = timezone(timedelta(hours=8))
CWA_PROVIDER = "cwa_weather"
CWA_FORECAST_TYPE = "36h"
ENABLE_VERBOSE_DEBUG = os.getenv("ENABLE_VERBOSE_DEBUG", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _debug_print(message: str, payload: dict[str, Any] | None = None) -> None:
    if not ENABLE_VERBOSE_DEBUG:
        return
    if payload is None:
        print(message, flush=True)
        return
    print(message, payload, flush=True)

COUNTY_ALIASES = {
    "基隆": "基隆市",
    "臺北": "臺北市",
    "台北": "臺北市",
    "新北": "新北市",
    "淡水": "新北市",
    "淡水老街": "新北市",
    "八里": "新北市",
    "九份": "新北市",
    "瑞芳": "新北市",
    "板橋": "新北市",
    "新莊": "新北市",
    "三重": "新北市",
    "中和": "新北市",
    "永和": "新北市",
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
ALL_CWA_COUNTIES = sorted(set(COUNTY_ALIASES.values()))


def _normalize_county_name(location_text: str, query_text: str = "") -> str:
    candidates = [str(location_text or "").strip(), str(query_text or "").strip()]
    for candidate in candidates:
        if not candidate:
            continue
        for alias in sorted(COUNTY_ALIASES, key=len, reverse=True):
            if alias and alias in candidate:
                return COUNTY_ALIASES[alias]
    return ""


def _source_date(now: datetime | None = None) -> str:
    current = now or datetime.now(TAIPEI_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TZ)
    return current.astimezone(TAIPEI_TZ).date().isoformat()


def _authorization_key() -> str:
    return os.getenv("CWA_AUTHORIZATION_KEY", "").strip()


def _fetch_cwa_weather(county_name: str = "") -> dict[str, Any]:
    authorization_key = _authorization_key()
    if not authorization_key:
        raise RuntimeError("CWA_AUTHORIZATION_KEY is not configured.")

    params = {
        "Authorization": authorization_key,
        "format": "JSON",
    }
    if county_name:
        params["locationName"] = county_name

    response = requests.get(
        CWA_FORECAST_36H_URL,
        params=params,
        timeout=CWA_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _locations_by_county(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = raw.get("records") or {}
    locations = records.get("location") or []
    result: dict[str, dict[str, Any]] = {}
    for location in locations:
        if not isinstance(location, dict):
            continue
        county_name = str(location.get("locationName") or "").strip()
        if county_name:
            result[county_name] = location
    return result


def _single_location_raw(location_record: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": "true",
        "records": {
            "location": [location_record],
        },
    }


def _location_record_from_daily_cache(
    county_name: str,
    source_date: str,
) -> dict[str, Any] | None:
    cached = get_weather_daily_cache(
        CWA_PROVIDER,
        county_name,
        source_date,
        forecast_type=CWA_FORECAST_TYPE,
    )
    if not isinstance(cached, dict):
        return None
    raw_data = cached.get("raw_data") or {}
    locations = _locations_by_county(raw_data)
    return locations.get(county_name)


def _save_daily_location_record(
    *,
    county_name: str,
    source_date: str,
    location_record: dict[str, Any],
) -> None:
    save_weather_daily_cache(
        CWA_PROVIDER,
        county_name,
        source_date,
        _single_location_raw(location_record),
        forecast_type=CWA_FORECAST_TYPE,
        ttl_seconds=CWA_DAILY_CACHE_TTL_SECONDS,
    )


def sync_cwa_weather_daily_cache(
    *,
    now: datetime | None = None,
    counties: list[str] | None = None,
) -> dict[str, int | str]:
    """每天固定同步一次中央氣象署 36 小時天氣資料到 weather_daily_cache。"""
    source_date = _source_date(now)
    county_names = counties or ALL_CWA_COUNTIES
    missing_counties = [
        county_name
        for county_name in county_names
        if _location_record_from_daily_cache(county_name, source_date) is None
    ]
    if not missing_counties:
        return {"source_date": source_date, "saved": 0, "skipped": len(county_names)}

    raw = _fetch_cwa_weather()
    locations = _locations_by_county(raw)
    saved = 0
    for county_name in missing_counties:
        location_record = locations.get(county_name)
        if not location_record:
            continue
        _save_daily_location_record(
            county_name=county_name,
            source_date=source_date,
            location_record=location_record,
        )
        saved += 1

    _debug_print(
        "CWA weather daily sync:",
        {
            "source_date": source_date,
            "saved": saved,
            "requested": len(missing_counties),
        },
    )
    return {"source_date": source_date, "saved": saved, "skipped": len(county_names) - saved}


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
    line_group_id: str = "",
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

    source_date = _source_date()
    location_record = _location_record_from_daily_cache(county_name, source_date)
    if isinstance(location_record, dict):
        group_message = _build_weather_message(
            county_name=county_name,
            query_text=query_text,
            time_text=time_text,
            location_record=location_record,
        )
        payload = {
            "provider": CWA_PROVIDER,
            "county_name": county_name,
            "source_date": source_date,
            "query_text": query_text,
            "group_message": group_message,
            "results": [],
        }
        _debug_print(
            "CWA weather daily cache hit:",
            {
                "line_group_id": line_group_id,
                "county_name": county_name,
                "source_date": source_date,
                "query_text": query_text,
            },
        )
        return payload

    raw = _fetch_cwa_weather(county_name)
    locations = _locations_by_county(raw)
    location_record = locations.get(county_name)
    if not location_record:
        raise RuntimeError(f"CWA weather returned no location data for {county_name}.")

    group_message = _build_weather_message(
        county_name=county_name,
        query_text=query_text,
        time_text=time_text,
        location_record=location_record,
    )
    payload = {
        "provider": CWA_PROVIDER,
        "county_name": county_name,
        "source_date": source_date,
        "query_text": query_text,
        "group_message": group_message,
        "results": [],
    }
    _save_daily_location_record(
        county_name=county_name,
        source_date=source_date,
        location_record=location_record,
    )
    _debug_print(
        "CWA weather daily cache seed:",
        {
            "line_group_id": line_group_id,
            "county_name": county_name,
            "source_date": source_date,
            "query_text": query_text,
            "ttl_seconds": CWA_DAILY_CACHE_TTL_SECONDS,
        },
    )
    return payload
