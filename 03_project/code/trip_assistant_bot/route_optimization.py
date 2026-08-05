from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any, Callable
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover - production requirements include requests
    requests = None


GOOGLE_PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
MAX_EXACT_SPOTS = 10
EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class RouteSpot:
    name: str
    latitude: float
    longitude: float
    address: str = ""


def _valid_location_names(analysis_result: dict[str, Any]) -> list[str]:
    extracted = analysis_result.get("extracted_info") or {}
    raw_locations = extracted.get("location") or []
    if not isinstance(raw_locations, list):
        raw_locations = [raw_locations]
    ignored = {"", "目前位置", "附近", "未指定", "不確定"}
    locations: list[str] = []
    for value in raw_locations:
        name = str(value).strip()
        if name in ignored or name in locations:
            continue
        locations.append(name)
    return locations


def should_optimize_route(analysis_result: dict[str, Any]) -> bool:
    return str(analysis_result.get("scenario_code") or "").strip() == "劇本五" and len(_valid_location_names(analysis_result)) >= 2


def haversine_km(first: RouteSpot, second: RouteSpot) -> float:
    lat1, lat2 = math.radians(first.latitude), math.radians(second.latitude)
    delta_lat = lat2 - lat1
    delta_lng = math.radians(second.longitude - first.longitude)
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(value)))


def route_distance_km(spots: list[RouteSpot]) -> float:
    return sum(haversine_km(spots[index - 1], spots[index]) for index in range(1, len(spots)))


def _optimize_exact(spots: list[RouteSpot]) -> list[RouteSpot]:
    size = len(spots)
    full_mask = (1 << size) - 1
    costs: dict[tuple[int, int], float] = {}
    previous: dict[tuple[int, int], int] = {}
    for end in range(size):
        costs[(1 << end, end)] = 0.0

    for mask in range(1, full_mask + 1):
        for end in range(size):
            if not mask & (1 << end):
                continue
            prior_mask = mask ^ (1 << end)
            if not prior_mask:
                continue
            candidates = (
                (costs[(prior_mask, prior)] + haversine_km(spots[prior], spots[end]), prior)
                for prior in range(size)
                if prior_mask & (1 << prior) and (prior_mask, prior) in costs
            )
            best_cost, best_prior = min(candidates, default=(math.inf, -1))
            costs[(mask, end)] = best_cost
            previous[(mask, end)] = best_prior

    end = min(range(size), key=lambda index: costs[(full_mask, index)])
    order: list[int] = []
    mask = full_mask
    while end >= 0:
        order.append(end)
        prior = previous.get((mask, end), -1)
        mask ^= 1 << end
        end = prior
    return [spots[index] for index in reversed(order)]


def _optimize_greedy(spots: list[RouteSpot]) -> list[RouteSpot]:
    best_route: list[RouteSpot] | None = None
    best_distance = math.inf
    for first in spots:
        remaining = [spot for spot in spots if spot is not first]
        route = [first]
        while remaining:
            next_spot = min(remaining, key=lambda spot: haversine_km(route[-1], spot))
            route.append(next_spot)
            remaining.remove(next_spot)
        distance = route_distance_km(route)
        if distance < best_distance:
            best_route, best_distance = route, distance
    return best_route or list(spots)


def optimize_spots(spots: list[RouteSpot]) -> list[RouteSpot]:
    if len(spots) < 2:
        return list(spots)
    return _optimize_exact(spots) if len(spots) <= MAX_EXACT_SPOTS else _optimize_greedy(spots)


def geocode_place(name: str, *, session: Any = None) -> RouteSpot | None:
    session = session or requests
    if session is None:
        raise RuntimeError("requests is not installed")
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured")
    response = session.post(
        GOOGLE_PLACES_TEXT_SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
        },
        json={"textQuery": f"{name} 台灣", "languageCode": "zh-TW", "regionCode": "TW", "maxResultCount": 1},
        timeout=10,
    )
    response.raise_for_status()
    places = response.json().get("places") or []
    if not places:
        return None
    place = places[0]
    coordinate = place.get("location") or {}
    latitude, longitude = coordinate.get("latitude"), coordinate.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    display_name = place.get("displayName") or {}
    resolved_name = str(display_name.get("text") or name).strip()
    return RouteSpot(resolved_name, float(latitude), float(longitude), str(place.get("formattedAddress") or "").strip())


def build_optimized_route_reply(
    analysis_result: dict[str, Any],
    *,
    geocoder: Callable[[str], RouteSpot | None] = geocode_place,
) -> str | None:
    if not should_optimize_route(analysis_result):
        return None
    names = _valid_location_names(analysis_result)
    resolved: list[RouteSpot] = []
    missing: list[str] = []
    for name in names:
        spot = geocoder(name)
        if spot is None:
            missing.append(name)
        else:
            resolved.append(spot)
    if len(resolved) < 2:
        return "我有看到你們想排路線，但目前至少要有 2 個能辨識的景點名稱。可以再補上完整店名或景點名嗎？"

    route = optimize_spots(resolved)
    distance = route_distance_km(route)
    lines = ["我幫你們把景點排成較順的順序："]
    lines.extend(f"{index}. {spot.name}" for index, spot in enumerate(route, start=1))
    lines.append(f"景點間直線距離合計約 {distance:.1f} 公里。")
    if missing:
        lines.append(f"尚未辨識：{'、'.join(missing)}；補上更完整名稱後我可以重排。")
    waypoints = "/".join(quote(spot.name, safe="") for spot in route)
    lines.append(f"Google 地圖路線：https://www.google.com/maps/dir/{waypoints}")
    lines.append("實際時間仍會受道路、交通方式和營業時間影響。")
    return "\n".join(lines)
