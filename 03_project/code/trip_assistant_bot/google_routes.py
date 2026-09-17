from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - production requirements include requests
    requests = None


GOOGLE_ROUTES_COMPUTE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_ROUTES_FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
DEFAULT_ROUTES_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class RouteDurationEstimate:
    duration_minutes: int
    distance_meters: int | None
    travel_mode: str
    routing_preference: str
    source: str = "google_routes"


def _api_key() -> str:
    return (
        os.getenv("GOOGLE_ROUTES_API_KEY", "").strip()
        or os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    )


def routes_api_configured() -> bool:
    return bool(_api_key())


def _coerce_coordinate(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(coordinate):
        return None
    return coordinate


def _duration_to_minutes(value: Any) -> int | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", text)
    if not match:
        return None
    seconds = float(match.group(1))
    return max(1, round(seconds / 60))


def _travel_mode(value: str | None = None) -> str:
    value = (value or os.getenv("GOOGLE_ROUTES_TRAVEL_MODE", "DRIVE")).strip().upper()
    return value if value in {"DRIVE", "WALK", "BICYCLE", "TRANSIT", "TWO_WHEELER"} else "DRIVE"


def _routing_preference(travel_mode: str) -> str:
    if travel_mode != "DRIVE":
        return ""
    value = os.getenv("GOOGLE_ROUTES_ROUTING_PREFERENCE", "TRAFFIC_AWARE").strip().upper()
    return value if value in {"TRAFFIC_UNAWARE", "TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL"} else "TRAFFIC_AWARE"


def _timeout_seconds() -> int:
    try:
        return max(1, int(os.getenv("GOOGLE_ROUTES_TIMEOUT_SECONDS", str(DEFAULT_ROUTES_TIMEOUT_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_ROUTES_TIMEOUT_SECONDS


def _lat_lng(latitude: float, longitude: float) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": latitude, "longitude": longitude}}}


def estimate_route_duration(
    *,
    origin_latitude: Any,
    origin_longitude: Any,
    destination_latitude: Any,
    destination_longitude: Any,
    travel_mode: str | None = None,
    session: Any = None,
) -> RouteDurationEstimate | None:
    """Return a Google Routes duration estimate for one route leg.

    The caller treats ``None`` as "keep the existing local/AI estimate".
    """
    key = _api_key()
    if not key:
        return None
    http = session or requests
    if http is None:
        return None

    origin_lat = _coerce_coordinate(origin_latitude)
    origin_lng = _coerce_coordinate(origin_longitude)
    destination_lat = _coerce_coordinate(destination_latitude)
    destination_lng = _coerce_coordinate(destination_longitude)
    if None in {origin_lat, origin_lng, destination_lat, destination_lng}:
        return None

    travel_mode = _travel_mode(travel_mode)
    routing_preference = _routing_preference(travel_mode)
    payload: dict[str, Any] = {
        "origin": _lat_lng(origin_lat, origin_lng),
        "destination": _lat_lng(destination_lat, destination_lng),
        "travelMode": travel_mode,
        "computeAlternativeRoutes": False,
        "languageCode": "zh-TW",
        "units": "METRIC",
    }
    if routing_preference:
        payload["routingPreference"] = routing_preference

    response = http.post(
        GOOGLE_ROUTES_COMPUTE_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": GOOGLE_ROUTES_FIELD_MASK,
        },
        json=payload,
        timeout=_timeout_seconds(),
    )
    response.raise_for_status()
    routes = response.json().get("routes") or []
    if not routes:
        return None
    route = routes[0]
    minutes = _duration_to_minutes(route.get("duration"))
    if minutes is None:
        return None
    distance = route.get("distanceMeters")
    try:
        distance_meters = int(distance) if distance is not None else None
    except (TypeError, ValueError):
        distance_meters = None
    return RouteDurationEstimate(
        duration_minutes=minutes,
        distance_meters=distance_meters,
        travel_mode=travel_mode,
        routing_preference=routing_preference,
    )


def configured_travel_modes() -> list[str]:
    raw = os.getenv("GOOGLE_ROUTES_TRAVEL_MODES", "").strip()
    if not raw:
        raw = os.getenv("GOOGLE_ROUTES_TRAVEL_MODE", "DRIVE").strip()
    modes: list[str] = []
    for value in re.split(r"[,，\s]+", raw):
        if not value:
            continue
        mode = _travel_mode(value)
        if mode not in modes:
            modes.append(mode)
    return modes or ["DRIVE"]


def estimate_route_durations(
    *,
    origin_latitude: Any,
    origin_longitude: Any,
    destination_latitude: Any,
    destination_longitude: Any,
    travel_modes: list[str] | None = None,
    session: Any = None,
) -> list[RouteDurationEstimate]:
    estimates: list[RouteDurationEstimate] = []
    for mode in travel_modes or configured_travel_modes():
        try:
            estimate = estimate_route_duration(
                origin_latitude=origin_latitude,
                origin_longitude=origin_longitude,
                destination_latitude=destination_latitude,
                destination_longitude=destination_longitude,
                travel_mode=mode,
                session=session,
            )
        except Exception:
            estimate = None
        if estimate is not None:
            estimates.append(estimate)
    return estimates
