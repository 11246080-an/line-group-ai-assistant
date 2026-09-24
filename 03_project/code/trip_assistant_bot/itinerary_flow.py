"""Private itinerary confirmation and post-trip sharing consent flow.

The database functions used here are intentionally loaded lazily.  This keeps the
existing application importable while the database owner implements the contract
documented in ``資料庫交接文件_行程分享與公開網站.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import inspect
import os
import re
import secrets
from typing import Any

from ai_linebot_core.app.llm_judge import LLMJudgeError, select_best_itinerary_reason
from ai_linebot_core.app.models import ItineraryDraft
from expense_flow import (
    ActionSpec,
    DatabaseFeatureUnavailable,
    FlowResult,
    _book_id,
    _db_function,
    database_contract_ready,
    database_unavailable_result,
    ensure_book_from_itinerary,
)
from google_routes import estimate_route_durations, routes_api_configured
from location_flow import resolve_itinerary_spot_coordinates
from privacy_redaction import redact_sensitive_identifiers, redact_structure


_ITINERARY_DRAFT_TYPE = "itinerary_plan"
_RECOMMENDATION_REASON_DRAFT_TYPE = "itinerary_recommendation_reason"
_RECOMMENDATION_REASON_DB_CONTRACT = (
    "record_itinerary_recommendation_reason",
    "get_itinerary_recommendation_reasons",
    "set_published_itinerary_recommendation_reason",
)
_SHARE_DEADLINE_DAYS = max(1, int(os.getenv("ITINERARY_SHARE_DEADLINE_DAYS", "7")))
_ROUTE_MODE_LABELS = {
    "DRIVE": "開車",
    "WALK": "步行",
    "BICYCLE": "自行車",
    "TRANSIT": "大眾運輸",
    "TWO_WHEELER": "機車",
}


def _draft_type(draft_id: str) -> str:
    return f"{_ITINERARY_DRAFT_TYPE}:{draft_id}"


def _valid_draft_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,32}", str(value or "")))


def _accepts_keyword(function: Any, keyword: str) -> bool:
    """Allow callers to work before and after the DB owner adds optional fields."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def share_approval_required(eligible_count: int) -> int:
    """Return the at-least-half approval threshold."""
    count = max(0, int(eligible_count))
    return (count + 1) // 2 if count else 0


def evaluate_share_votes(
    eligible_voter_keys: list[str],
    consent_documents: list[dict[str, Any]],
) -> dict[str, int | str]:
    """Evaluate unique eligible decisions using an at-least-half rule."""
    eligible = {str(value) for value in eligible_voter_keys if str(value)}
    latest_decisions: dict[str, str] = {}
    for document in consent_documents:
        voter_key = str(document.get("voter_key") or "")
        decision = str(document.get("decision") or "")
        if voter_key in eligible and decision in {"approve", "decline"}:
            latest_decisions[voter_key] = decision

    approvals = sum(value == "approve" for value in latest_decisions.values())
    declines = sum(value == "decline" for value in latest_decisions.values())
    pending = max(0, len(eligible) - approvals - declines)
    required = share_approval_required(len(eligible))

    if required and approvals >= required:
        status = "approved"
    elif required and approvals + pending < required:
        status = "declined"
    else:
        status = "pending"

    return {
        "status": status,
        "eligible": len(eligible),
        "required": required,
        "approvals": approvals,
        "declines": declines,
        "pending": pending,
    }


def _anonymization_secret() -> str:
    value = (
        os.getenv("ITINERARY_SHARE_SECRET", "").strip()
        or os.getenv("VOTE_ANONYMIZATION_SECRET", "").strip()
        or os.getenv("INTERNAL_TASK_SECRET", "").strip()
    )
    if len(value) < 32 or value.startswith("replace_with_"):
        return ""
    return value


def _consent_voter_key(*, consent_salt: str, line_user_id: str) -> str:
    secret = _anonymization_secret()
    if not secret:
        raise ValueError("尚未設定行程分享匿名化密鑰")
    message = f"itinerary-share:{consent_salt}:{line_user_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _normalize_draft(value: Any) -> dict[str, Any] | None:
    if isinstance(value, ItineraryDraft):
        draft = value
    elif isinstance(value, dict):
        draft = ItineraryDraft.from_dict(value)
    else:
        draft = None
    if draft is None:
        return None

    payload = draft.to_dict()
    spots = []
    for index, spot in enumerate(payload.get("spots") or [], start=1):
        normalized_spot = dict(spot)
        normalized_spot["sequence"] = index
        normalized_spot["spot_id"] = f"spot-{index:03d}"
        spots.append(normalized_spot)
    payload["spots"] = spots
    return redact_structure(payload)


def _coerce_ticket_price(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = round(float(value))
    except (TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def _spot_attraction_id(spot: dict[str, Any]) -> str:
    for key in ("attraction_id", "tourism_attraction_id"):
        value = str(spot.get(key) or "").strip()
        if value:
            return value
    return ""


def _match_attraction_id_by_name(*, name: str, region: str) -> str:
    if not name or not database_contract_ready(("get_tourism_attractions",)):
        return ""
    get_attractions = _db_function("get_tourism_attractions")
    candidates: list[dict[str, Any]] = []
    city = region.strip() if region.strip().endswith(("市", "縣")) else ""
    for kwargs in (
        {"city": city, "keyword": name, "limit": 10} if city else None,
        {"keyword": name, "limit": 10},
    ):
        if not kwargs:
            continue
        try:
            candidates.extend(
                item for item in get_attractions(**kwargs) or [] if isinstance(item, dict)
            )
        except Exception:
            continue
        if candidates:
            break

    normalized_name = re.sub(r"\s+", "", name).casefold()
    for candidate in candidates:
        candidate_name = re.sub(r"\s+", "", str(candidate.get("name") or "")).casefold()
        if candidate_name == normalized_name:
            return str(candidate.get("attraction_id") or "").strip()
    for candidate in candidates:
        candidate_name = re.sub(r"\s+", "", str(candidate.get("name") or "")).casefold()
        if normalized_name and (
            normalized_name in candidate_name or candidate_name in normalized_name
        ):
            return str(candidate.get("attraction_id") or "").strip()
    return str((candidates[0] or {}).get("attraction_id") or "").strip() if candidates else ""


_TAIWAN_CITY_ALIASES = {
    "台北": "臺北市",
    "臺北": "臺北市",
    "新北": "新北市",
    "桃園": "桃園市",
    "台中": "臺中市",
    "臺中": "臺中市",
    "台南": "臺南市",
    "臺南": "臺南市",
    "高雄": "高雄市",
    "基隆": "基隆市",
    "新竹": "新竹縣",
    "苗栗": "苗栗縣",
    "彰化": "彰化縣",
    "南投": "南投縣",
    "雲林": "雲林縣",
    "嘉義": "嘉義縣",
    "屏東": "屏東縣",
    "宜蘭": "宜蘭縣",
    "花蓮": "花蓮縣",
    "台東": "臺東縣",
    "臺東": "臺東縣",
    "澎湖": "澎湖縣",
    "金門": "金門縣",
    "連江": "連江縣",
    "馬祖": "連江縣",
}


def _normalize_itinerary_tourism_city(region: str) -> str:
    text = str(region or "").strip()
    if not text:
        return ""
    if text.endswith(("市", "縣")):
        return text.replace("台", "臺")
    compact = text.replace("出發", "").replace("附近", "").replace("周邊", "").strip()
    compact = compact.replace("台", "臺")
    for keyword, city in _TAIWAN_CITY_ALIASES.items():
        if keyword.replace("台", "臺") in compact:
            return city
    return ""


def _fetch_tourism_attraction_candidates(*, region: str, limit: int = 120) -> list[dict[str, Any]]:
    if not database_contract_ready(("get_tourism_attractions",)):
        return []
    city = _normalize_itinerary_tourism_city(region)
    try:
        items = _db_function("get_tourism_attractions")(city=city or None, limit=limit)
    except Exception:
        return []
    return [item for item in items or [] if isinstance(item, dict)]


def _compact_text(value: Any, max_length: int = 72) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 1)].rstrip() + "…"


def _brief_spot_description(value: Any, max_length: int = 42) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？!?])", text)
        if sentence.strip()
    ]
    if sentences:
        text = "".join(sentences[:2]).strip()
    return _compact_text(text, max_length)


def _tourism_candidate_score(candidate: dict[str, Any], preference_text: str) -> int:
    searchable = " ".join(
        str(candidate.get(key) or "")
        for key in ("name", "description", "city", "town", "address")
    )
    score = 0
    for token in ("自然", "森林", "步道", "公園", "風景", "生態", "老街", "文化", "拍照", "散步"):
        if token in preference_text and token in searchable:
            score += 4
        elif token in searchable:
            score += 1
    if any(token in preference_text for token in ("不要逛街", "不逛街", "自然")) and "百貨" in searchable:
        score -= 8
    if candidate.get("latitude") is not None and candidate.get("longitude") is not None:
        score += 2
    if str(candidate.get("description") or "").strip():
        score += 1
    return score


def _candidate_name_key(candidate: dict[str, Any]) -> str:
    return re.sub(r"\s+", "", str(candidate.get("name") or "").casefold())


def _candidate_to_itinerary_spot(candidate: dict[str, Any], *, sequence: int) -> dict[str, Any]:
    name = str(candidate.get("name") or "景點").strip()
    description = _brief_spot_description(candidate.get("description"), 42)
    if not description:
        town = str(candidate.get("town") or "").strip()
        city = str(candidate.get("city") or "").strip()
        description = _compact_text(" ".join(part for part in (city, town, "觀光署景點資料") if part), 42)
    return {
        "name": name,
        "sequence": sequence,
        "spot_id": f"spot-{sequence:03d}",
        "description": description,
        "address": str(candidate.get("address") or "").strip(),
        "latitude": candidate.get("latitude"),
        "longitude": candidate.get("longitude"),
        "attraction_id": str(candidate.get("attraction_id") or "").strip(),
        "recommendation_source": "tourism_open_data",
        "coordinate_source": "tourism_open_data",
    }


def _constrain_itinerary_to_tourism_attractions(itinerary: dict[str, Any]) -> dict[str, Any]:
    """Enrich generated itinerary spots with Tourism Administration data when possible.

    The original LLM draft may contain user-selected places.  Keep those places
    instead of replacing unmatched items with unrelated open-data attractions.
    """
    candidates = _fetch_tourism_attraction_candidates(region=str(itinerary.get("region") or ""))
    if not candidates:
        return itinerary

    preference_text = " ".join(
        str(itinerary.get(key) or "")
        for key in ("title", "summary", "type", "best_for", "region", "duration")
    )
    ranked_candidates = sorted(
        candidates,
        key=lambda item: _tourism_candidate_score(item, preference_text),
        reverse=True,
    )
    by_name = {_candidate_name_key(candidate): candidate for candidate in ranked_candidates}

    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    raw_spots = [spot for spot in itinerary.get("spots") or [] if isinstance(spot, dict)]
    if raw_spots:
        matched_count = 0
        for index, spot in enumerate(raw_spots, start=1):
            name_key = re.sub(r"\s+", "", str(spot.get("name") or "").casefold())
            candidate = by_name.get(name_key)
            if candidate is None:
                for option in ranked_candidates:
                    option_key = _candidate_name_key(option)
                    if name_key and (name_key in option_key or option_key in name_key):
                        candidate = option
                        break
            if candidate is None:
                preserved = {**spot, "sequence": index}
                preserved.setdefault("recommendation_source", "ai_generated")
                selected.append(preserved)
                continue
            attraction_id = str(candidate.get("attraction_id") or "").strip()
            if attraction_id and attraction_id in seen_ids:
                preserved = {**spot, "sequence": index}
                preserved.setdefault("recommendation_source", "ai_generated")
                selected.append(preserved)
                continue
            if attraction_id:
                seen_ids.add(attraction_id)
            enriched = _candidate_to_itinerary_spot(candidate, sequence=index)
            if str(spot.get("description") or "").strip():
                enriched["description"] = str(spot.get("description") or "").strip()
            selected.append(enriched)
            matched_count += 1

        notice = {
            "label": "部分景點資訊已參考觀光署資料庫",
            "provider": "tourism_open_data_partial",
            "matched_count": matched_count,
            "total_spots": len(selected),
            "candidate_count": len(candidates),
            "selection_policy": "preserve_generated_spots",
        }
        return {**itinerary, "spots": selected, "recommendation_source_notice": notice}

    target_count = 3

    for spot in raw_spots:
        name_key = re.sub(r"\s+", "", str(spot.get("name") or "").casefold())
        candidate = by_name.get(name_key)
        if candidate is None:
            for option in ranked_candidates:
                option_key = _candidate_name_key(option)
                if name_key and (name_key in option_key or option_key in name_key):
                    candidate = option
                    break
        if candidate is None:
            continue
        attraction_id = str(candidate.get("attraction_id") or "").strip()
        if attraction_id and attraction_id in seen_ids:
            continue
        if attraction_id:
            seen_ids.add(attraction_id)
        selected.append(candidate)
        if len(selected) >= target_count:
            break

    for candidate in ranked_candidates:
        if len(selected) >= target_count:
            break
        attraction_id = str(candidate.get("attraction_id") or "").strip()
        if attraction_id and attraction_id in seen_ids:
            continue
        if attraction_id:
            seen_ids.add(attraction_id)
        selected.append(candidate)

    if len(selected) < 2:
        return itinerary

    spots = [
        _candidate_to_itinerary_spot(candidate, sequence=index)
        for index, candidate in enumerate(selected, start=1)
    ]
    transport = [
        {
            "from_sequence": index,
            "to_sequence": index + 1,
            "mode": "開車",
            "estimated_minutes": None,
            "note": "",
        }
        for index in range(1, len(spots))
    ]
    return {
        **itinerary,
        "spots": spots,
        "transport": transport,
        "recommendation_source_notice": {
            "label": "景點推薦來源：觀光署資料庫",
            "provider": "tourism_open_data",
            "matched_count": len(spots),
            "total_spots": len(spots),
            "candidate_count": len(candidates),
            "selection_policy": "tourism_candidates_first",
        },
    }


def _apply_tourism_source_notice_to_itinerary(itinerary: dict[str, Any]) -> dict[str, Any]:
    spots = [spot for spot in itinerary.get("spots") or [] if isinstance(spot, dict)]
    total_spots = len(spots)
    if not total_spots:
        return itinerary

    region = str(itinerary.get("region") or "").strip()
    matched_count = 0
    for spot in spots:
        attraction_id = _spot_attraction_id(spot)
        if not attraction_id:
            attraction_id = _match_attraction_id_by_name(
                name=str(spot.get("name") or "").strip(),
                region=region,
            )
            if attraction_id:
                spot["attraction_id"] = attraction_id
        if attraction_id:
            matched_count += 1
            spot.setdefault("recommendation_source", "tourism_open_data")

    itinerary["recommendation_source_notice"] = {
        "label": "景點推薦來源：觀光署資料庫",
        "provider": "tourism_open_data",
        "matched_count": matched_count,
        "total_spots": total_spots,
    }
    return itinerary


def _estimate_ticket_budget(
    spots: list[dict[str, Any]],
    *,
    region: str,
) -> dict[str, Any]:
    total_spots = len([spot for spot in spots if isinstance(spot, dict)])
    if not total_spots or not database_contract_ready(("get_tourism_attraction_fees_by_ids",)):
        return {
            "currency": "TWD",
            "estimated_total": None,
            "priced_count": 0,
            "total_spots": total_spots,
            "items": [],
            "missing_names": [],
        }

    spot_pairs: list[tuple[dict[str, Any], str]] = []
    for spot in spots:
        if not isinstance(spot, dict):
            continue
        attraction_id = _spot_attraction_id(spot)
        if not attraction_id:
            attraction_id = _match_attraction_id_by_name(
                name=str(spot.get("name") or "").strip(),
                region=region,
            )
            if attraction_id:
                spot["attraction_id"] = attraction_id
        spot_pairs.append((spot, attraction_id))

    attraction_ids = [attraction_id for _spot, attraction_id in spot_pairs if attraction_id]
    try:
        fee_documents = _db_function("get_tourism_attraction_fees_by_ids")(attraction_ids)
    except Exception:
        fee_documents = []
    fee_by_id = {
        str(item.get("attraction_id") or ""): item
        for item in fee_documents or []
        if isinstance(item, dict)
    }

    items: list[dict[str, Any]] = []
    missing_names: list[str] = []
    total = 0
    for spot, attraction_id in spot_pairs:
        name = str(spot.get("name") or "未命名景點").strip()
        fee = fee_by_id.get(attraction_id)
        price = _coerce_ticket_price((fee or {}).get("default_price")) if fee else None
        if price is None:
            missing_names.append(name)
            continue
        fee_name = ""
        for fee_item in (fee.get("fees") if isinstance(fee, dict) else []) or []:
            if not isinstance(fee_item, dict):
                continue
            if _coerce_ticket_price(fee_item.get("price")) == price:
                fee_name = str(fee_item.get("name") or "").strip()
                break
        spot["ticket_price"] = price
        spot["ticket_price_source"] = "tourism_attraction_fees"
        items.append(
            {
                "name": name,
                "attraction_id": attraction_id,
                "price": price,
                "fee_name": fee_name,
            }
        )
        total += price

    return {
        "currency": "TWD",
        "estimated_total": total if items else None,
        "priced_count": len(items),
        "total_spots": total_spots,
        "items": items,
        "missing_names": missing_names,
    }


_SERVICE_DAY_LABELS = {
    "Monday": "一",
    "Tuesday": "二",
    "Wednesday": "三",
    "Thursday": "四",
    "Friday": "五",
    "Saturday": "六",
    "Sunday": "日",
}


def _format_service_days(days: Any) -> str:
    if not isinstance(days, list) or not days:
        return ""
    labels = [_SERVICE_DAY_LABELS.get(str(day), str(day)) for day in days if str(day)]
    if not labels:
        return ""
    weekdays = ["一", "二", "三", "四", "五"]
    weekend = ["六", "日"]
    if labels == weekdays:
        return "週一至週五"
    if labels == weekend:
        return "週六至週日"
    if labels == weekdays + weekend:
        return "每日"
    return "週" + "、".join(labels)


def _format_service_clock(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{1,2}):(\d{2})", text)
    if not match:
        return text
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _format_service_time_entry(entry: dict[str, Any]) -> str:
    day_text = _format_service_days(entry.get("ServiceDays"))
    start = _format_service_clock(entry.get("StartTime"))
    end = _format_service_clock(entry.get("EndTime"))
    time_text = f"{start}-{end}" if start and end else start or end
    name = str(entry.get("Name") or "").strip()
    description = str(entry.get("Description") or "").strip()
    parts = [part for part in (day_text, time_text, name or description) if part]
    return " ".join(parts)


def _service_time_summary_from_doc(document: dict[str, Any]) -> str:
    entries = document.get("service_time")
    if not isinstance(entries, list):
        entries = []
    lines = [
        _format_service_time_entry(entry)
        for entry in entries
        if isinstance(entry, dict)
    ]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return "；".join(lines[:2])


def _lookup_service_time_notice(
    spots: list[dict[str, Any]],
    *,
    region: str,
) -> dict[str, Any]:
    total_spots = len([spot for spot in spots if isinstance(spot, dict)])
    required = ("get_tourism_attraction_service_times_by_ids",)
    if not total_spots or not database_contract_ready(required):
        return {"items": [], "missing_names": [], "covered_count": 0, "total_spots": total_spots}

    spot_pairs: list[tuple[dict[str, Any], str]] = []
    for spot in spots:
        if not isinstance(spot, dict):
            continue
        attraction_id = _spot_attraction_id(spot)
        if not attraction_id:
            attraction_id = _match_attraction_id_by_name(
                name=str(spot.get("name") or "").strip(),
                region=region,
            )
            if attraction_id:
                spot["attraction_id"] = attraction_id
        spot_pairs.append((spot, attraction_id))

    attraction_ids = [attraction_id for _spot, attraction_id in spot_pairs if attraction_id]
    try:
        documents = _db_function("get_tourism_attraction_service_times_by_ids")(attraction_ids)
    except Exception:
        documents = []
    service_by_id = {
        str(item.get("attraction_id") or ""): item
        for item in documents or []
        if isinstance(item, dict)
    }

    items: list[dict[str, Any]] = []
    missing_names: list[str] = []
    for spot, attraction_id in spot_pairs:
        name = str(spot.get("name") or "未命名景點").strip()
        document = service_by_id.get(attraction_id)
        summary = _service_time_summary_from_doc(document) if document else ""
        if not summary:
            missing_names.append(name)
            continue
        spot["service_time_summary"] = summary
        spot["service_time_source"] = "tourism_attraction_service_times"
        items.append({"name": name, "attraction_id": attraction_id, "summary": summary})

    return {
        "items": items,
        "missing_names": missing_names,
        "covered_count": len(items),
        "total_spots": total_spots,
    }


def _apply_ticket_budget_to_itinerary(itinerary: dict[str, Any]) -> dict[str, Any]:
    spots = [spot for spot in itinerary.get("spots") or [] if isinstance(spot, dict)]
    ticket_budget = _estimate_ticket_budget(
        spots,
        region=str(itinerary.get("region") or ""),
    )
    updated = {**itinerary, "spots": spots, "ticket_budget": ticket_budget}
    if ticket_budget.get("estimated_total") is not None:
        updated["estimated_budget"] = ticket_budget["estimated_total"]
        updated["currency"] = ticket_budget.get("currency") or "TWD"
    return updated


def _apply_service_time_notice_to_itinerary(itinerary: dict[str, Any]) -> dict[str, Any]:
    spots = [spot for spot in itinerary.get("spots") or [] if isinstance(spot, dict)]
    notice = _lookup_service_time_notice(
        spots,
        region=str(itinerary.get("region") or ""),
    )
    return {**itinerary, "spots": spots, "service_time_notice": notice}


def _spot_sequence(spot: dict[str, Any], fallback: int) -> int:
    try:
        return max(1, int(spot.get("sequence") or fallback))
    except (TypeError, ValueError):
        return fallback


def _find_transport_leg(
    transport: list[dict[str, Any]],
    *,
    from_sequence: int,
    to_sequence: int,
    fallback_index: int,
) -> dict[str, Any]:
    for leg in transport:
        if not isinstance(leg, dict):
            continue
        try:
            leg_from = int(leg.get("from_sequence") or 0)
            leg_to = int(leg.get("to_sequence") or 0)
        except (TypeError, ValueError):
            continue
        if leg_from == from_sequence and leg_to == to_sequence:
            return leg
    if 0 <= fallback_index < len(transport) and isinstance(transport[fallback_index], dict):
        return transport[fallback_index]
    leg = {"from_sequence": from_sequence, "to_sequence": to_sequence}
    transport.append(leg)
    return leg


def _has_coordinates(spot: dict[str, Any]) -> bool:
    return spot.get("latitude") is not None and spot.get("longitude") is not None


def _route_duration_option(estimate: Any) -> dict[str, Any]:
    return {
        "mode": _ROUTE_MODE_LABELS.get(estimate.travel_mode, estimate.travel_mode),
        "travel_mode": estimate.travel_mode,
        "minutes": estimate.duration_minutes,
        "distance_meters": estimate.distance_meters,
        "routing_preference": estimate.routing_preference,
        "source": estimate.source,
    }


def _primary_route_estimate(estimates: list[Any]) -> Any | None:
    if not estimates:
        return None
    for preferred_mode in ("DRIVE", "TRANSIT", "WALK"):
        for estimate in estimates:
            if estimate.travel_mode == preferred_mode:
                return estimate
    return estimates[0]


def _apply_route_duration_estimates_to_itinerary(itinerary: dict[str, Any]) -> dict[str, Any]:
    """Use Google Routes API to replace local/AI travel-time guesses when possible."""
    spots = [spot for spot in itinerary.get("spots") or [] if isinstance(spot, dict)]
    transport = [
        dict(leg) for leg in itinerary.get("transport") or [] if isinstance(leg, dict)
    ]
    if len(spots) < 2 or not routes_api_configured():
        return {**itinerary, "spots": spots, "transport": transport}

    updated_count = 0
    for index in range(len(spots) - 1):
        origin = spots[index]
        destination = spots[index + 1]
        if not _has_coordinates(origin) or not _has_coordinates(destination):
            continue
        estimates: list[Any] = []
        try:
            estimates = estimate_route_durations(
                origin_latitude=origin.get("latitude"),
                origin_longitude=origin.get("longitude"),
                destination_latitude=destination.get("latitude"),
                destination_longitude=destination.get("longitude"),
            )
        except Exception:
            estimates = []
        estimate = _primary_route_estimate(estimates)
        if estimate is None:
            continue
        from_sequence = _spot_sequence(origin, index + 1)
        to_sequence = _spot_sequence(destination, index + 2)
        leg = _find_transport_leg(
            transport,
            from_sequence=from_sequence,
            to_sequence=to_sequence,
            fallback_index=index,
        )
        leg["from_sequence"] = from_sequence
        leg["to_sequence"] = to_sequence
        leg["estimated_minutes"] = estimate.duration_minutes
        leg["mode"] = _ROUTE_MODE_LABELS.get(estimate.travel_mode, estimate.travel_mode)
        leg["note"] = "Google Routes API 交通時間"
        leg["route_duration_source"] = estimate.source
        leg["route_duration_options"] = [_route_duration_option(item) for item in estimates]
        leg["route_travel_mode"] = estimate.travel_mode
        leg["route_routing_preference"] = estimate.routing_preference
        leg["distance_meters"] = estimate.distance_meters
        updated_count += 1

    if updated_count:
        route_notice = {
            "source": "google_routes",
            "updated_legs": updated_count,
            "total_legs": max(0, len(spots) - 1),
            "note": "交通時間由 Google Routes API 估算，仍可能受即時路況影響。",
        }
        return {**itinerary, "spots": spots, "transport": transport, "route_duration_notice": route_notice}
    return {**itinerary, "spots": spots, "transport": transport}


def _ticket_budget_summary_text(ticket_budget: dict[str, Any]) -> str:
    amount = _coerce_ticket_price(ticket_budget.get("estimated_total"))
    if amount is None:
        return ""
    priced_count = int(ticket_budget.get("priced_count") or 0)
    total_spots = int(ticket_budget.get("total_spots") or 0)
    missing_count = max(0, total_spots - priced_count)
    text = f"門票預估：NT${amount:,}（已估 {priced_count} 個景點"
    if missing_count:
        text += f"，{missing_count} 個景點票價未提供"
    text += "）。"
    return text


def _service_time_summary_text(service_time_notice: dict[str, Any]) -> str:
    items = [
        item for item in service_time_notice.get("items") or []
        if isinstance(item, dict) and str(item.get("summary") or "").strip()
    ]
    if not items:
        return ""
    covered_count = int(service_time_notice.get("covered_count") or len(items))
    total_spots = int(service_time_notice.get("total_spots") or covered_count)
    missing_count = max(0, total_spots - covered_count)
    lines = [f"營業時間提醒：已查到 {covered_count} 個景點"]
    if missing_count:
        lines[0] += f"，{missing_count} 個景點未提供營業時間"
    lines[0] += "。"
    for item in items[:3]:
        lines.append(f"- {item.get('name')}：{item.get('summary')}")
    lines.append("實際營業仍可能受天候、活動或現場公告影響。")
    return "\n".join(lines)


def stage_generated_itinerary(
    *,
    line_group_id: str,
    line_user_id: str,
    itinerary_draft: Any,
    reply_text: str,
) -> FlowResult:
    """Save an AI-generated private draft and ask its creator to confirm it."""
    if not line_group_id:
        return FlowResult(False)
    normalized = _normalize_draft(itinerary_draft)
    if normalized is None:
        return FlowResult(False)
    if not database_contract_ready(("save_feature_draft",)):
        return database_unavailable_result()
    normalized = _constrain_itinerary_to_tourism_attractions(normalized)
    resolved_spots, _coordinate_summary = resolve_itinerary_spot_coordinates(
        list(normalized.get("spots") or []),
        region=str(normalized.get("region") or ""),
        line_group_id=line_group_id,
    )
    normalized = {**normalized, "spots": resolved_spots}
    normalized = _apply_tourism_source_notice_to_itinerary(normalized)
    normalized = _apply_route_duration_estimates_to_itinerary(normalized)
    normalized = _apply_ticket_budget_to_itinerary(normalized)
    normalized = _apply_service_time_notice_to_itinerary(normalized)

    draft_id = secrets.token_urlsafe(9)
    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="",
        draft_type=_draft_type(draft_id),
        payload={"itinerary": normalized, "created_by": line_user_id, "draft_id": draft_id},
    )
    text = str(reply_text or "").strip()
    ticket_summary = _ticket_budget_summary_text(normalized.get("ticket_budget") or {})
    service_time_summary = _service_time_summary_text(normalized.get("service_time_notice") or {})
    if text:
        text += "\n\n"
    if ticket_summary:
        text += ticket_summary + "\n\n"
    if service_time_summary:
        text += service_time_summary + "\n\n"
    text += "這是行程草稿。確認後才會保存為群組的私人行程。"
    return FlowResult(
        True,
        text,
        actions=[
            ActionSpec("確認行程", "postback", f"itinerary|confirm|{draft_id}"),
            ActionSpec("取消草稿", "postback", f"itinerary|cancel|{draft_id}"),
        ],
        data={
            "itinerary_draft_staged": True,
            "itinerary_draft": normalized,
            "itinerary_draft_id": draft_id,
        },
    )


def _draft_payload(
    *,
    line_group_id: str,
    line_user_id: str,
    draft_id: str = "",
) -> dict[str, Any] | None:
    if draft_id and not _valid_draft_id(draft_id):
        return None
    document = _db_function("get_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="" if draft_id else line_user_id,
        draft_type=_draft_type(draft_id) if draft_id else _ITINERARY_DRAFT_TYPE,
    )
    if not isinstance(document, dict):
        return None
    payload = document.get("payload") if isinstance(document.get("payload"), dict) else document
    return payload if isinstance(payload, dict) else None


def _cancel_draft(*, line_group_id: str, line_user_id: str, draft_id: str = "") -> FlowResult:
    if not database_contract_ready(("delete_feature_draft",)):
        return database_unavailable_result()
    payload = _draft_payload(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_id=draft_id,
    )
    if payload is None:
        return FlowResult(True, "這份行程草稿不存在或已經處理過了。")
    created_by = str(payload.get("created_by") or line_user_id)
    if draft_id and created_by and created_by != line_user_id:
        return FlowResult(True, "只有建立這份草稿的成員可以取消；同群組成員仍可按確認行程。")
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="" if draft_id else line_user_id,
        draft_type=_draft_type(draft_id) if draft_id else _ITINERARY_DRAFT_TYPE,
    )
    return FlowResult(True, "已取消這份行程草稿。")


def _confirm_draft(*, line_group_id: str, line_user_id: str, draft_id: str = "") -> FlowResult:
    required = (
        "get_feature_draft",
        "delete_feature_draft",
        "get_active_expense_book",
        "create_expense_book",
        "create_itinerary",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()

    payload = _draft_payload(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_id=draft_id,
    )
    itinerary = payload.get("itinerary") if isinstance(payload, dict) else None
    if not isinstance(itinerary, dict):
        return FlowResult(True, "找不到這份行程草稿，可能已確認、取消或過期。")

    created_by = str(payload.get("created_by") or line_user_id)

    itinerary = _constrain_itinerary_to_tourism_attractions(itinerary)
    resolved_spots, _coordinate_summary = resolve_itinerary_spot_coordinates(
        list(itinerary.get("spots") or []),
        region=str(itinerary.get("region") or ""),
        line_group_id=line_group_id,
    )
    itinerary = {**itinerary, "spots": resolved_spots}
    itinerary = _apply_tourism_source_notice_to_itinerary(itinerary)
    itinerary = _apply_route_duration_estimates_to_itinerary(itinerary)
    itinerary = _apply_ticket_budget_to_itinerary(itinerary)
    itinerary = _apply_service_time_notice_to_itinerary(itinerary)

    active_book = _db_function("get_active_expense_book")(line_group_id)
    had_active_book = isinstance(active_book, dict)
    if not had_active_book:
        active_book = ensure_book_from_itinerary(
            line_group_id=line_group_id,
            line_user_id=created_by or line_user_id,
            itinerary=itinerary,
        )
        if not isinstance(active_book, dict):
            active_book = _db_function("get_active_expense_book")(line_group_id)
        if not isinstance(active_book, dict):
            return FlowResult(True, "目前無法建立行程帳本，因此尚未確認行程，請稍後再試。")

    participant_user_ids = []
    for user_id in (created_by, line_user_id):
        if user_id and user_id not in participant_user_ids:
            participant_user_ids.append(user_id)
    expense_book_id = None
    if isinstance(active_book, dict):
        expense_book_id = _book_id(active_book)
        for member in active_book.get("members") or []:
            if not isinstance(member, dict) or member.get("type") != "line":
                continue
            user_id = str(member.get("line_user_id") or "")
            if user_id and user_id not in participant_user_ids:
                participant_user_ids.append(user_id)

    create_itinerary = _db_function("create_itinerary")
    create_kwargs: dict[str, Any] = dict(
        itinerary_id=f"trip_{secrets.token_urlsafe(12)}",
        line_group_id=line_group_id,
        title=str(itinerary.get("title") or "行程"),
        spots=list(itinerary.get("spots") or []),
        transport=list(itinerary.get("transport") or []),
        created_by=created_by or line_user_id,
        participant_user_ids=participant_user_ids,
        expense_book_id=expense_book_id,
        region=str(itinerary.get("region") or ""),
        summary=str(itinerary.get("summary") or ""),
        duration=str(itinerary.get("duration") or ""),
        budget={
            "currency": str(itinerary.get("currency") or "TWD"),
            "estimated_total": itinerary.get("estimated_budget"),
            "category_breakdown": (
                [
                    {
                        "category": "門票",
                        "amount": itinerary["ticket_budget"]["estimated_total"],
                    }
                ]
                if isinstance(itinerary.get("ticket_budget"), dict)
                and itinerary["ticket_budget"].get("estimated_total") is not None
                else []
            ),
        },
    )
    optional_fields = {
        "itinerary_type": str(itinerary.get("type") or ""),
        "best_for": str(itinerary.get("best_for") or ""),
        "recommendation_source_notice": itinerary.get("recommendation_source_notice"),
        "confirmed_by": line_user_id,
    }
    for key, value in optional_fields.items():
        if value and _accepts_keyword(create_itinerary, key):
            create_kwargs[key] = value
    created = create_itinerary(**create_kwargs)
    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id="" if draft_id else line_user_id,
        draft_type=_draft_type(draft_id) if draft_id else _ITINERARY_DRAFT_TYPE,
    )
    title = str((created or {}).get("title") or itinerary.get("title") or "行程")
    ledger_text = "已連接目前帳本" if had_active_book else "已同時建立行程帳本"
    ticket_summary = _ticket_budget_summary_text(itinerary.get("ticket_budget") or {})
    service_time_summary = _service_time_summary_text(itinerary.get("service_time_notice") or {})
    suffix_parts = [part for part in (ticket_summary, service_time_summary) if part]
    suffix = "\n" + "\n".join(suffix_parts) if suffix_parts else ""
    return FlowResult(
        True,
        f"已將「{redact_sensitive_identifiers(title)}」保存為群組私人行程，{ledger_text}。{suffix}",
    )


def _recommendation_reason_contract_ready() -> bool:
    return database_contract_ready(
        (
            "get_itinerary",
            "get_itinerary_share_consents",
            "save_feature_draft",
            "get_feature_draft",
            "delete_feature_draft",
            *_RECOMMENDATION_REASON_DB_CONTRACT,
        )
    )


def _eligible_voter_key(
    itinerary: dict[str, Any],
    *,
    line_user_id: str,
) -> str:
    consent_salt = str(itinerary.get("consent_salt") or "")
    if not consent_salt or not line_user_id:
        return ""
    voter_key = _consent_voter_key(
        consent_salt=consent_salt,
        line_user_id=line_user_id,
    )
    eligible_keys = {str(value) for value in itinerary.get("eligible_consent_keys") or []}
    return voter_key if voter_key in eligible_keys else ""


def _voter_approved_sharing(*, itinerary_id: str, voter_key: str) -> bool:
    latest_decision = ""
    for document in _db_function("get_itinerary_share_consents")(
        itinerary_id=itinerary_id
    ) or []:
        if isinstance(document, dict) and str(document.get("voter_key") or "") == voter_key:
            latest_decision = str(document.get("decision") or "")
    return latest_decision == "approve"


def _start_recommendation_reason(
    *,
    itinerary_id: str,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not _recommendation_reason_contract_ready():
        return database_unavailable_result()
    itinerary = _db_function("get_itinerary")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
    )
    if not isinstance(itinerary, dict):
        return FlowResult(True, "找不到這筆已分享的行程。")
    voter_key = _eligible_voter_key(itinerary, line_user_id=line_user_id)
    if not voter_key or not _voter_approved_sharing(
        itinerary_id=itinerary_id,
        voter_key=voter_key,
    ):
        return FlowResult(True, "只有同意公開這次行程的成員可以提供推薦理由。")
    _db_function("save_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
        payload={"itinerary_id": itinerary_id},
    )
    return FlowResult(
        True,
        "請直接輸入推薦這份行程的理由（8～240 字）。AI 會比較所有成員提供的內容，選出最具體、最符合行程的一筆公開。",
    )


def _skip_recommendation_reason(*, line_group_id: str, line_user_id: str) -> FlowResult:
    if database_contract_ready(("delete_feature_draft",)):
        _db_function("delete_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
        )
    return FlowResult(True, "好的，這次不填推薦理由；網頁不會顯示空白的推薦理由區塊。")


def _valid_recommendation_reason(value: str) -> bool:
    reason = str(value or "").strip()
    if not 8 <= len(reason) <= 240:
        return False
    if re.search(r"https?://|www\.", reason, flags=re.I):
        return False
    meaningful = re.sub(r"[\W_]+", "", reason, flags=re.UNICODE)
    return len(set(meaningful)) >= 4


def _capture_recommendation_reason(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if not database_contract_ready(("get_feature_draft",)):
        return FlowResult(False)
    try:
        document = _db_function("get_feature_draft")(
            line_group_id=line_group_id,
            line_user_id=line_user_id,
            draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
        )
    except Exception:
        # Ordinary conversation must continue even when the optional reason
        # collection store is temporarily unavailable.
        return FlowResult(False)
    if not isinstance(document, dict):
        return FlowResult(False)
    if not _recommendation_reason_contract_ready():
        return database_unavailable_result()

    payload = document.get("payload") if isinstance(document.get("payload"), dict) else document
    itinerary_id = str((payload or {}).get("itinerary_id") or "")
    itinerary = _db_function("get_itinerary")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
    )
    if not isinstance(itinerary, dict):
        return FlowResult(True, "這筆行程已不存在，請重新操作。")
    voter_key = _eligible_voter_key(itinerary, line_user_id=line_user_id)
    if not voter_key or not _voter_approved_sharing(
        itinerary_id=itinerary_id,
        voter_key=voter_key,
    ):
        return FlowResult(True, "只有同意公開這次行程的成員可以提供推薦理由。")

    reason = redact_sensitive_identifiers(str(text or "").strip())[:240]
    if not _valid_recommendation_reason(reason):
        return FlowResult(
            True,
            "這段理由太短、像亂碼或包含連結。請用 8～240 字說明這個行程值得推薦的具體原因。",
        )

    now = datetime.now(timezone.utc)
    _db_function("record_itinerary_recommendation_reason")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
        voter_key=voter_key,
        reason=reason,
        submitted_at=now,
    )
    candidates = list(
        _db_function("get_itinerary_recommendation_reasons")(itinerary_id=itinerary_id) or []
    )
    try:
        selected_reason = select_best_itinerary_reason(itinerary, candidates)
    except LLMJudgeError:
        selected_reason = ""

    if selected_reason:
        selected_voter_key = next(
            (
                str(candidate.get("voter_key") or "")
                for candidate in candidates
                if isinstance(candidate, dict)
                and str(candidate.get("reason") or "").strip() == selected_reason
            ),
            "",
        )
        _db_function("set_published_itinerary_recommendation_reason")(
            itinerary_id=itinerary_id,
            line_group_id=line_group_id,
            reason=selected_reason,
            selected_voter_key=selected_voter_key,
            selected_at=now,
        )

    _db_function("delete_feature_draft")(
        line_group_id=line_group_id,
        line_user_id=line_user_id,
        draft_type=_RECOMMENDATION_REASON_DRAFT_TYPE,
    )
    if not selected_reason:
        return FlowResult(True, "已收到。AI 判斷目前候選內容尚不適合公開，因此網頁暫時不顯示推薦理由。")
    if selected_reason == reason:
        return FlowResult(True, "已收到。AI 比較候選內容後，選用這筆作為公開推薦理由。")
    return FlowResult(True, "已收到。AI 比較後保留了另一筆更符合行程內容的推薦理由。")


def handle_itinerary_text(
    text: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    normalized = str(text or "").strip()
    reason_result = _capture_recommendation_reason(
        normalized,
        line_group_id=line_group_id,
        line_user_id=line_user_id,
    )
    if reason_result.handled:
        return reason_result
    if normalized == "確認行程":
        try:
            return _confirm_draft(line_group_id=line_group_id, line_user_id=line_user_id)
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法確認行程，請稍後再試。")
    if normalized in {"取消行程草稿", "放棄行程草稿"}:
        try:
            return _cancel_draft(line_group_id=line_group_id, line_user_id=line_user_id)
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法取消行程草稿，請稍後再試。")
    return FlowResult(False)


def _budget_summary(book: dict[str, Any], expenses: list[dict[str, Any]]) -> dict[str, Any]:
    confirmed = [item for item in expenses if str(item.get("status") or "confirmed") == "confirmed"]
    currencies = {str(item.get("currency") or "TWD").upper() for item in confirmed}
    members = list(book.get("members") or [])
    participant_count = len(members) or None

    if len(currencies) != 1:
        return {
            "currency": "MULTI" if currencies else "TWD",
            "actual_total": None,
            "participant_count": participant_count,
            "per_person": None,
            "category_breakdown": [],
        }

    currency = next(iter(currencies))
    actual_total = sum(int(item.get("amount") or 0) for item in confirmed)
    categories: dict[str, int] = {}
    for item in confirmed:
        category = str(item.get("category") or "其他").strip() or "其他"
        categories[category] = categories.get(category, 0) + int(item.get("amount") or 0)
    return {
        "currency": currency,
        "actual_total": actual_total,
        "participant_count": participant_count,
        "per_person": (
            round(actual_total / participant_count) if participant_count else None
        ),
        "category_breakdown": [
            {"category": category, "amount": amount}
            for category, amount in sorted(categories.items())
        ],
    }


def prepare_share_prompt_for_closed_book(
    *,
    book: dict[str, Any],
    expenses: list[dict[str, Any]],
    now: datetime | None = None,
) -> FlowResult | None:
    """Complete the linked itinerary and create one idempotent share request."""
    required = (
        "get_itinerary_by_expense_book",
        "mark_itinerary_completed",
        "create_itinerary_share_request",
    )
    if not database_contract_ready(required):
        return None

    current = now or datetime.now(timezone.utc)
    book_id = _book_id(book)
    itinerary = _db_function("get_itinerary_by_expense_book")(expense_book_id=book_id)
    if not isinstance(itinerary, dict):
        return None

    completed = _db_function("mark_itinerary_completed")(
        expense_book_id=book_id,
        completed_at=current,
        budget_summary=_budget_summary(book, expenses),
    )
    if isinstance(completed, dict):
        itinerary = completed

    user_ids = [str(value) for value in itinerary.get("participant_user_ids") or [] if str(value)]
    if not user_ids:
        for member in book.get("members") or []:
            if isinstance(member, dict) and member.get("type") == "line":
                user_id = str(member.get("line_user_id") or "")
                if user_id and user_id not in user_ids:
                    user_ids.append(user_id)
    created_by = str(itinerary.get("created_by") or "")
    if created_by and created_by not in user_ids:
        user_ids.append(created_by)
    if not user_ids:
        return None

    consent_salt = str(itinerary.get("consent_salt") or secrets.token_hex(16))
    eligible_keys = [
        _consent_voter_key(consent_salt=consent_salt, line_user_id=user_id)
        for user_id in user_ids
    ]
    request_result = _db_function("create_itinerary_share_request")(
        itinerary_id=str(itinerary.get("itinerary_id") or ""),
        line_group_id=str(itinerary.get("line_group_id") or book.get("line_group_id") or ""),
        eligible_consent_keys=eligible_keys,
        consent_salt=consent_salt,
        requested_at=current,
        deadline_at=current + timedelta(days=_SHARE_DEADLINE_DAYS),
    )
    created_now = bool((request_result or {}).get("created_now")) if isinstance(request_result, dict) else False
    if not created_now:
        return None

    request_itinerary = request_result.get("itinerary")
    if isinstance(request_itinerary, dict):
        itinerary = request_itinerary
    itinerary_id = str(itinerary.get("itinerary_id") or "")
    title = redact_sensitive_identifiers(str(itinerary.get("title") or book.get("name") or "這次行程"))
    required_count = share_approval_required(len(eligible_keys))
    deadline_at = current + timedelta(days=_SHARE_DEADLINE_DAYS)
    return FlowResult(
        True,
        (
            f"「{title}」已經結束。\n\n"
            "是否同意將景點順序、交通方式與彙總預算匿名分享到公開行程網站？\n"
            "不會公開群組名稱、成員名稱、付款人或聊天內容。\n\n"
            f"共 {len(eligible_keys)} 位符合資格的成員，需要 {required_count} 位同意才會公開。"
        ),
        actions=[
            ActionSpec("同意匿名分享", "postback", f"itinerary_share|approve|{itinerary_id}"),
            ActionSpec("不同意分享", "postback", f"itinerary_share|decline|{itinerary_id}"),
        ],
        data={
            "itinerary_share_request": {
                "itinerary_id": itinerary_id,
                "title": title,
                "eligible_count": len(eligible_keys),
                "required_count": required_count,
                "deadline_at": deadline_at,
            }
        },
    )


def _handle_share_decision(
    *,
    itinerary_id: str,
    decision: str,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    required = (
        "get_itinerary",
        "record_itinerary_share_consent",
        "get_itinerary_share_consents",
        "publish_itinerary_snapshot",
        "reject_itinerary_sharing",
    )
    if not database_contract_ready(required):
        return database_unavailable_result()

    itinerary = _db_function("get_itinerary")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
    )
    if not isinstance(itinerary, dict):
        return FlowResult(True, "找不到這筆行程，或你所在的群組不符。")

    consent_salt = str(itinerary.get("consent_salt") or "")
    voter_key = _consent_voter_key(
        consent_salt=consent_salt,
        line_user_id=line_user_id,
    )
    eligible_keys = [str(value) for value in itinerary.get("eligible_consent_keys") or []]
    if voter_key not in eligible_keys:
        return FlowResult(True, "你不在這次行程的分享同意名單中。")

    now = datetime.now(timezone.utc)
    _db_function("record_itinerary_share_consent")(
        itinerary_id=itinerary_id,
        line_group_id=line_group_id,
        voter_key=voter_key,
        decision=decision,
        responded_at=now,
    )
    documents = list(
        _db_function("get_itinerary_share_consents")(itinerary_id=itinerary_id) or []
    )
    outcome = evaluate_share_votes(eligible_keys, documents)

    if outcome["status"] == "approved":
        _db_function("publish_itinerary_snapshot")(
            itinerary_id=itinerary_id,
            published_at=now,
        )
        reason_actions: list[ActionSpec] = []
        reason_suffix = ""
        if _recommendation_reason_contract_ready():
            reason_actions = [
                ActionSpec("填寫推薦理由", "postback", f"itinerary_reason|write|{itinerary_id}"),
                ActionSpec("略過", "postback", f"itinerary_reason|skip|{itinerary_id}"),
            ]
            reason_suffix = "\n\n願意補充推薦理由嗎？多人填寫時會由 AI 選出最符合行程、最有參考價值的一筆。"
        return FlowResult(
            True,
            f"已達半數同意門檻（{outcome['approvals']}/{outcome['eligible']}），行程已匿名公開。{reason_suffix}",
            actions=reason_actions,
        )

    if outcome["status"] == "declined":
        _db_function("reject_itinerary_sharing")(
            itinerary_id=itinerary_id,
            line_group_id=line_group_id,
            resolved_at=now,
        )
        return FlowResult(True, "目前已不可能達到半數同意門檻，這份行程不會公開。")

    choice_text = "同意" if decision == "approve" else "不同意"
    return FlowResult(
        True,
        (
            f"已記錄你的選擇：{choice_text}。"
            f"目前 {outcome['approvals']}/{outcome['eligible']} 位同意，"
            f"需要 {outcome['required']} 位同意才會公開。"
        ),
    )


def handle_itinerary_postback(
    data: str,
    *,
    line_group_id: str,
    line_user_id: str,
) -> FlowResult:
    if data == "itinerary|confirm" or data.startswith("itinerary|confirm|"):
        draft_id = data.split("|", 2)[2] if data.count("|") == 2 else ""
        try:
            return _confirm_draft(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_id=draft_id,
            )
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法確認行程，請稍後再試。")
    if data == "itinerary|cancel" or data.startswith("itinerary|cancel|"):
        draft_id = data.split("|", 2)[2] if data.count("|") == 2 else ""
        try:
            return _cancel_draft(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
                draft_id=draft_id,
            )
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except Exception:
            return FlowResult(True, "目前無法取消行程草稿，請稍後再試。")
    if data.startswith("itinerary_reason|"):
        parts = data.split("|", 2)
        if len(parts) != 3 or parts[1] not in {"write", "skip"}:
            return FlowResult(True, "這個推薦理由操作無效或已過期。")
        if parts[1] == "skip":
            return _skip_recommendation_reason(
                line_group_id=line_group_id,
                line_user_id=line_user_id,
            )
        try:
            return _start_recommendation_reason(
                itinerary_id=parts[2],
                line_group_id=line_group_id,
                line_user_id=line_user_id,
            )
        except DatabaseFeatureUnavailable:
            return database_unavailable_result()
        except ValueError as exc:
            return FlowResult(True, redact_sensitive_identifiers(str(exc)))
        except Exception:
            return FlowResult(True, "目前無法開始填寫推薦理由，請稍後再試。")

    if not data.startswith("itinerary_share|"):
        return FlowResult(False)

    parts = data.split("|", 2)
    if len(parts) != 3 or parts[1] not in {"approve", "decline"}:
        return FlowResult(True, "這個分享操作無效或已過期。")
    if not line_group_id or not line_user_id:
        return FlowResult(True, "行程分享同意目前只支援 LINE 群組成員。")
    try:
        return _handle_share_decision(
            itinerary_id=parts[2],
            decision=parts[1],
            line_group_id=line_group_id,
            line_user_id=line_user_id,
        )
    except DatabaseFeatureUnavailable:
        return database_unavailable_result()
    except ValueError as exc:
        return FlowResult(True, redact_sensitive_identifiers(str(exc)))
    except Exception:
        return FlowResult(True, "目前無法記錄分享決定，請稍後再試。")
