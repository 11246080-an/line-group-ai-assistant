from __future__ import annotations

import base64
from functools import lru_cache
import json
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any


ITINERARY_IMPORT_MARKER = "#TRIP_IMPORT_V1"
SPOT_IMPORT_MARKER = "#TRIP_SPOT_IMPORT_V1"
ITINERARY_IMPORT_HEADER = "【Trip Assistant 行程匯入】"
SPOT_IMPORT_HEADER = "【Trip Assistant 景點同步】"

NEXT_STOP_KEYWORDS = (
    "\u4e0b\u4e00\u7ad9",
    "\u4e0b\u4e00\u500b\u666f\u9ede",
    "\u63a5\u4e0b\u4f86\u53bb\u54ea",
    "\u63a5\u8457\u53bb\u54ea",
    "\u4e4b\u5f8c\u53bb\u54ea",
    "next stop",
    "where next",
)
ROUTE_KEYWORDS = (
    "\u666f\u9ede\u9806\u5e8f",
    "\u884c\u7a0b\u9806\u5e8f",
    "\u8def\u7dda\u9806\u5e8f",
    "\u8def\u7dda\u600e\u9ebc\u6392",
    "\u884c\u7a0b\u600e\u9ebc\u6392",
    "route order",
    "itinerary order",
)
BUDGET_KEYWORDS = (
    "\u9810\u7b97",
    "\u82b1\u8cbb",
    "\u591a\u5c11\u9322",
    "\u591a\u5c11\u5143",
    "budget",
    "cost",
)
TRANSPORT_KEYWORDS = (
    "\u4ea4\u901a",
    "\u600e\u9ebc\u53bb",
    "\u642d\u4ec0\u9ebc",
    "\u600e\u9ebc\u642d",
    "transport",
    "how to get there",
)
BEST_FOR_KEYWORDS = (
    "\u9069\u5408\u8ab0",
    "\u9069\u5408\u4ec0\u9ebc\u4eba",
    "\u8ab0\u9069\u5408",
    "best for",
    "suitable for",
)
REASON_KEYWORDS = (
    "\u63a8\u85a6\u7406\u7531",
    "\u70ba\u4ec0\u9ebc\u63a8\u85a6",
    "\u70ba\u4ec0\u9ebc\u662f\u9019\u689d",
    "why this plan",
    "why recommend",
)


class LineImportError(ValueError):
    """當 LINE 分享匯入訊息無法解析時拋出。"""


@dataclass(frozen=True)
class LineImportCommand:
    marker: str
    payload: dict[str, Any]

    @property
    def is_itinerary(self) -> bool:
        return self.marker == ITINERARY_IMPORT_MARKER

    @property
    def is_spot(self) -> bool:
        return self.marker == SPOT_IMPORT_MARKER


def extract_line_import_command(text: str) -> LineImportCommand | None:
    message = text.strip()
    if not message:
        return None

    for marker in (ITINERARY_IMPORT_MARKER, SPOT_IMPORT_MARKER):
        if marker not in message:
            continue

        marker_index = message.index(marker)
        summary_text = message[:marker_index].strip()
        payload_text = message[marker_index + len(marker) :].strip()

        return LineImportCommand(
            marker=marker,
            payload=_decode_shared_payload(
                marker=marker,
                summary_text=summary_text,
                payload_text=payload_text,
            ),
        )

    inferred_marker = _infer_marker_from_summary(message)
    if inferred_marker is None:
        return None

    return LineImportCommand(
        marker=inferred_marker,
        payload=_decode_shared_payload(
            marker=inferred_marker,
            summary_text=message,
            payload_text="",
        ),
    )

    return None


def _infer_marker_from_summary(message: str) -> str | None:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return None

    first_line = lines[0]
    if first_line == SPOT_IMPORT_HEADER:
        return SPOT_IMPORT_MARKER
    if first_line == ITINERARY_IMPORT_HEADER:
        return ITINERARY_IMPORT_MARKER
    return None


def normalize_itinerary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("kind") != "travel_itinerary_import":
        raise LineImportError("這不是有效的行程匯入資料。")

    itinerary_id = _as_text(payload.get("itinerary_id"))
    title = _as_text(payload.get("title")) or itinerary_id
    if not itinerary_id:
        raise LineImportError("行程資料缺少行程編號。")
    if not title:
        raise LineImportError("行程資料缺少標題。")

    return {
        "kind": "travel_itinerary_import",
        "version": _as_int(payload.get("version"), default=1),
        "itinerary_id": itinerary_id,
        "title": title,
        "region": _as_text(payload.get("region")),
        "budget": _as_text(payload.get("budget")),
        "distance": _as_text(payload.get("distance")),
        "type": _as_text(payload.get("type")),
        "transport": _as_text(payload.get("transport")),
        "duration": _as_text(payload.get("duration")),
        "summary": _as_text(payload.get("summary")),
        "description": _as_text(payload.get("description")),
        "best_for": _as_text(payload.get("bestFor") or payload.get("best_for")),
        "comment": _as_text(payload.get("comment")),
        "spots": _normalize_spots(payload.get("spots", [])),
    }


def normalize_spot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("kind") != "travel_spot_import":
        raise LineImportError("這不是有效的景點匯入資料。")

    itinerary_id = _as_text(payload.get("itinerary_id"))
    spot_name = _as_text(payload.get("spot_name"))
    if not itinerary_id:
        raise LineImportError("景點資料缺少行程編號。")
    if not spot_name:
        raise LineImportError("景點資料缺少景點名稱。")

    return {
        "kind": "travel_spot_import",
        "version": _as_int(payload.get("version"), default=1),
        "itinerary_id": itinerary_id,
        "itinerary_title": _as_text(payload.get("itinerary_title")),
        "spot_id": _as_text(payload.get("spot_id")),
        "sequence": _as_int(payload.get("sequence"), default=0),
        "spot_name": spot_name,
        "spot_description": _as_text(payload.get("spot_description")),
        "next_prompt": _as_text(payload.get("next_prompt")),
    }


def find_itinerary_spot(
    itinerary: dict[str, Any],
    *,
    spot_id: str = "",
    spot_name: str = "",
    sequence: int = 0,
) -> dict[str, Any] | None:
    spots = itinerary.get("spots") or []
    for spot in spots:
        if spot_id and spot.get("spot_id") == spot_id:
            return spot
        if sequence and spot.get("sequence") == sequence:
            return spot
        if spot_name and spot.get("name") == spot_name:
            return spot
    return None


def build_itinerary_import_reply(itinerary: dict[str, Any]) -> str:
    route_preview = _format_route_preview(itinerary.get("spots") or [])
    detail_bits = [
        bit
        for bit in (
            itinerary.get("region"),
            itinerary.get("duration"),
            itinerary.get("transport"),
            itinerary.get("budget"),
        )
        if bit
    ]

    lines = [
        f'已收到行程「{itinerary["title"]}」，我會依照這份規劃協助群組討論。',
    ]
    if detail_bits:
        lines.append(" / ".join(detail_bits))
    if itinerary.get("summary"):
        lines.append(f'摘要：{itinerary["summary"]}')
    if route_preview:
        lines.append(f"路線：{route_preview}")
    if itinerary.get("comment"):
        lines.append(f'快速建議：{itinerary["comment"]}')
    lines.append("接下來可以直接問我下一站、預算、路線順序，或下雨時的備案。")
    return "\n".join(lines)


def build_spot_import_reply(
    itinerary: dict[str, Any],
    focused_spot: dict[str, Any],
) -> str:
    next_spot = _next_spot(itinerary, focused_spot)
    lines = [
        f'目前焦點已切換到「{focused_spot["name"]}」。',
        f'這是行程「{itinerary["title"]}」的第 {focused_spot["sequence"]} 站。',
    ]
    if focused_spot.get("description"):
        lines.append(f'景點說明：{focused_spot["description"]}')
    if next_spot:
        lines.append(f'下一站建議可以安排「{next_spot["name"]}」。')
    else:
        lines.append("這一站目前已是匯入路線中的最後一站。")
    return "\n".join(lines)


def build_itinerary_context(
    itinerary: dict[str, Any],
    focused_spot: dict[str, Any] | None,
) -> str:
    lines = [
        "[匯入行程脈絡]",
        f'行程名稱：{itinerary.get("title", "")}',
    ]

    detail_bits = [
        bit
        for bit in (
            itinerary.get("region"),
            itinerary.get("duration"),
            itinerary.get("transport"),
            itinerary.get("budget"),
        )
        if bit
    ]
    if detail_bits:
        lines.append("行程資訊：" + " / ".join(detail_bits))
    if itinerary.get("summary"):
        lines.append(f'摘要：{itinerary["summary"]}')
    if itinerary.get("comment"):
        lines.append(f'補充建議：{itinerary["comment"]}')
    if focused_spot:
        lines.append(f'目前焦點景點：{focused_spot.get("name", "")}')

    spots = itinerary.get("spots") or []
    if spots:
        lines.append("景點順序：")
        for spot in spots:
            description = spot.get("description")
            if description:
                lines.append(f'{spot["sequence"]}. {spot["name"]} - {description}')
            else:
                lines.append(f'{spot["sequence"]}. {spot["name"]}')

    return "\n".join(line for line in lines if line.strip())


def build_itinerary_followup_reply(
    message: str,
    itinerary: dict[str, Any],
    focused_spot: dict[str, Any] | None,
) -> str | None:
    text = message.strip()
    if not text:
        return None

    if _contains_any(text, NEXT_STOP_KEYWORDS):
        return _build_next_stop_reply(itinerary, focused_spot)

    if _contains_any(text, ROUTE_KEYWORDS):
        route_preview = _format_route_preview(itinerary.get("spots") or [], numbered=True)
        if route_preview:
            return f'目前「{itinerary["title"]}」的行程順序如下：\n{route_preview}'
        return f'我有收到「{itinerary["title"]}」，但裡面還沒有景點順序資料。'

    if _contains_any(text, BUDGET_KEYWORDS) and itinerary.get("budget"):
        return f'目前「{itinerary["title"]}」的預算是：{itinerary["budget"]}。'

    if _contains_any(text, TRANSPORT_KEYWORDS) and itinerary.get("transport"):
        return f'目前這條路線建議的交通方式是「{itinerary["transport"]}」。'

    if _contains_any(text, BEST_FOR_KEYWORDS) and itinerary.get("best_for"):
        return f'這條路線特別適合：{itinerary["best_for"]}'

    if _contains_any(text, REASON_KEYWORDS) and itinerary.get("comment"):
        return f'推薦補充說明：{itinerary["comment"]}'

    return None


def create_focus_spot_from_import(spot_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "spot_id": spot_payload.get("spot_id") or "shared-spot",
        "sequence": spot_payload.get("sequence") or 1,
        "name": spot_payload["spot_name"],
        "description": spot_payload.get("spot_description", ""),
    }


def create_placeholder_itinerary_from_spot(spot_payload: dict[str, Any]) -> dict[str, Any]:
    focused_spot = create_focus_spot_from_import(spot_payload)
    return {
        "kind": "travel_itinerary_import",
        "version": spot_payload.get("version", 1),
        "itinerary_id": spot_payload["itinerary_id"],
        "title": spot_payload.get("itinerary_title") or spot_payload["spot_name"],
        "region": "",
        "budget": "",
        "distance": "",
        "type": "",
        "transport": "",
        "duration": "",
        "summary": "",
        "description": "",
        "best_for": "",
        "comment": "",
        "spots": [focused_spot],
    }


def _decode_shared_payload(
    *,
    marker: str,
    summary_text: str,
    payload_text: str,
) -> dict[str, Any]:
    if not payload_text:
        return _decode_summary_payload(marker=marker, summary_text=summary_text)

    if "{" in payload_text and "}" in payload_text:
        return _load_json_object(payload_text)

    lookup_payload = _decode_lookup_payload(marker=marker, payload_text=payload_text)
    if lookup_payload is not None:
        return lookup_payload

    candidate = next((line.strip() for line in payload_text.splitlines() if line.strip()), "")
    if not candidate:
        raise LineImportError("分享訊息中沒有有效的資料內容。")

    padding = "=" * (-len(candidate) % 4)
    try:
        decoded = base64.urlsafe_b64decode(candidate + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise LineImportError("分享資料無法解碼。") from exc

    return _load_json_object(decoded)


def _decode_summary_payload(*, marker: str, summary_text: str) -> dict[str, Any]:
    lines = [line.strip() for line in summary_text.splitlines() if line.strip()]
    if marker == SPOT_IMPORT_MARKER:
        itinerary_title, spot_name = _extract_spot_titles(lines)
        return _lookup_spot_payload(
            itinerary_title=itinerary_title,
            spot_name=spot_name,
        )

    itinerary_title = _extract_itinerary_title(lines)
    return _lookup_itinerary_payload(itinerary_title=itinerary_title)


def _decode_lookup_payload(*, marker: str, payload_text: str) -> dict[str, Any] | None:
    fields, saw_lookup_field = _parse_lookup_fields(payload_text)
    if not saw_lookup_field:
        return None

    itinerary_id = fields.get("itinerary_id", "")
    if marker == SPOT_IMPORT_MARKER:
        if not itinerary_id:
            raise LineImportError("分享訊息缺少行程編號。")
        return _lookup_spot_payload(
            itinerary_id=itinerary_id,
            spot_id=fields.get("spot_id", ""),
            spot_name=fields.get("spot_name", ""),
            sequence=_as_int(fields.get("sequence"), default=0),
        )

    if not itinerary_id:
        raise LineImportError("分享訊息缺少行程編號。")
    return _lookup_itinerary_payload(itinerary_id=itinerary_id)


def _parse_lookup_fields(payload_text: str) -> tuple[dict[str, str], bool]:
    field_aliases = {
        "行程編號": "itinerary_id",
        "itinerary_id": "itinerary_id",
        "景點編號": "spot_id",
        "spot_id": "spot_id",
        "景點名稱": "spot_name",
        "spot_name": "spot_name",
        "站次": "sequence",
        "sequence": "sequence",
    }

    fields: dict[str, str] = {}
    saw_lookup_field = False

    for raw_line in payload_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        separator = "：" if "：" in line else ":"
        if separator not in line:
            continue

        key, value = line.split(separator, 1)
        normalized_key = field_aliases.get(key.strip().lower()) or field_aliases.get(key.strip())
        if not normalized_key:
            continue

        saw_lookup_field = True
        fields[normalized_key] = value.strip()

    return fields, saw_lookup_field


def _extract_itinerary_title(lines: list[str]) -> str:
    for line in lines:
        if line.startswith("【Trip Assistant"):
            continue
        if line.startswith("路線："):
            continue
        if line.startswith("請 AI "):
            continue
        return line

    raise LineImportError("分享訊息中找不到行程名稱。")


def _extract_spot_titles(lines: list[str]) -> tuple[str, str]:
    for line in lines:
        if line.startswith("【Trip Assistant"):
            continue
        if line.startswith("請把群組"):
            continue

        parts = re.split(r"\s*/\s*", line, maxsplit=1)
        if len(parts) != 2:
            continue

        itinerary_title, spot_name = (part.strip() for part in parts)
        if itinerary_title and spot_name:
            return itinerary_title, spot_name

    raise LineImportError("分享訊息中找不到景點對應資訊。")


@lru_cache(maxsize=1)
def _load_trip_website_lookup() -> dict[str, Any]:
    data_path = Path(__file__).resolve().parents[2] / "trip_website" / "src" / "data.js"

    try:
        raw_text = data_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LineImportError("找不到網站行程資料，無法還原分享內容。") from exc

    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise LineImportError("網站行程資料格式錯誤，無法還原分享內容。")

    try:
        raw_itineraries = json.loads(raw_text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LineImportError("網站行程資料格式錯誤，無法還原分享內容。") from exc

    if not isinstance(raw_itineraries, list):
        raise LineImportError("網站行程資料格式錯誤，無法還原分享內容。")

    by_id: dict[str, dict[str, Any]] = {}
    by_title: dict[str, list[dict[str, Any]]] = {}

    for raw_itinerary in raw_itineraries:
        if not isinstance(raw_itinerary, dict):
            continue

        itinerary_id = _as_text(raw_itinerary.get("id")) or _slugify(_as_text(raw_itinerary.get("title")))
        if not itinerary_id:
            continue

        title = _as_text(raw_itinerary.get("title")) or itinerary_id
        spots = []
        spots_by_id: dict[str, dict[str, Any]] = {}
        spots_by_name: dict[str, list[dict[str, Any]]] = {}

        for index, raw_spot in enumerate(raw_itinerary.get("spots") or [], start=1):
            if not isinstance(raw_spot, dict):
                continue

            spot_name = _as_text(raw_spot.get("name")) or _as_text(raw_spot.get("spot_name")) or f"第 {index} 站"
            spot = {
                "spot_id": _as_text(raw_spot.get("id")) or f"{itinerary_id}-spot-{index:02d}",
                "sequence": index,
                "name": spot_name,
                "description": _as_text(raw_spot.get("description") or raw_spot.get("spot_description")),
            }
            spots.append(spot)
            spots_by_id[spot["spot_id"]] = spot
            spots_by_name.setdefault(_normalize_lookup_key(spot_name), []).append(spot)

        itinerary_payload = {
            "kind": "travel_itinerary_import",
            "version": 1,
            "itinerary_id": itinerary_id,
            "title": title,
            "region": _as_text(raw_itinerary.get("region")),
            "budget": _as_text(raw_itinerary.get("budget")),
            "distance": _as_text(raw_itinerary.get("distance")),
            "type": _as_text(raw_itinerary.get("type")),
            "transport": _as_text(raw_itinerary.get("transport")),
            "duration": _as_text(raw_itinerary.get("duration")),
            "summary": _as_text(raw_itinerary.get("summary")),
            "description": _as_text(raw_itinerary.get("description")),
            "bestFor": _as_text(raw_itinerary.get("bestFor") or raw_itinerary.get("best_for")),
            "comment": _as_text(raw_itinerary.get("comment")),
            "spots": spots,
        }
        entry = {
            "itinerary": itinerary_payload,
            "spots_by_id": spots_by_id,
            "spots_by_name": spots_by_name,
        }
        by_id[itinerary_id] = entry
        by_title.setdefault(_normalize_lookup_key(title), []).append(entry)

    return {
        "by_id": by_id,
        "by_title": by_title,
    }


def _lookup_itinerary_payload(
    *,
    itinerary_id: str = "",
    itinerary_title: str = "",
) -> dict[str, Any]:
    lookup = _load_trip_website_lookup()

    if itinerary_id:
        entry = lookup["by_id"].get(itinerary_id)
        if entry is None:
            raise LineImportError(f"找不到行程編號「{itinerary_id}」對應的網站資料。")
        return entry["itinerary"]

    if not itinerary_title:
        raise LineImportError("分享訊息中找不到行程名稱。")

    matched_entries = lookup["by_title"].get(_normalize_lookup_key(itinerary_title), [])
    if not matched_entries:
        raise LineImportError(f"找不到行程「{itinerary_title}」對應的網站資料。")
    if len(matched_entries) > 1:
        raise LineImportError(f"找到多筆名稱同為「{itinerary_title}」的行程，無法自動判斷。")
    return matched_entries[0]["itinerary"]


def _lookup_spot_payload(
    *,
    itinerary_id: str = "",
    itinerary_title: str = "",
    spot_id: str = "",
    spot_name: str = "",
    sequence: int = 0,
) -> dict[str, Any]:
    itinerary = normalize_itinerary_payload(
        _lookup_itinerary_payload(
            itinerary_id=itinerary_id,
            itinerary_title=itinerary_title,
        )
    )
    focused_spot = find_itinerary_spot(
        itinerary,
        spot_id=spot_id,
        spot_name=spot_name,
        sequence=sequence,
    )
    if focused_spot is None:
        display_name = spot_name or spot_id or f"第 {sequence} 站"
        raise LineImportError(f"找不到景點「{display_name}」對應的網站資料。")

    return {
        "kind": "travel_spot_import",
        "version": 1,
        "itinerary_id": itinerary["itinerary_id"],
        "itinerary_title": itinerary["title"],
        "spot_id": focused_spot["spot_id"],
        "sequence": focused_spot["sequence"],
        "spot_name": focused_spot["name"],
        "spot_description": focused_spot.get("description", ""),
        "next_prompt": (
            f'下一站是 {focused_spot["name"]}，可以提醒使用者怎麼前往或附近有什麼可做。'
        ),
    }


def _normalize_lookup_key(value: str) -> str:
    return re.sub(r"\s+", "", _as_text(value)).lower()


def _slugify(value: str) -> str:
    text = _as_text(value).lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w-]+", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _load_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LineImportError("分享訊息中找不到有效的 JSON 物件。")

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LineImportError("分享資料不是有效的 JSON。") from exc

    if not isinstance(data, dict):
        raise LineImportError("分享資料解碼後必須是 JSON 物件。")
    return data


def _normalize_spots(raw_spots: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_spots, list):
        return []

    spots: list[dict[str, Any]] = []
    for index, raw_spot in enumerate(raw_spots, start=1):
        if not isinstance(raw_spot, dict):
            continue

        sequence = _as_int(raw_spot.get("sequence"), default=index)
        name = _as_text(raw_spot.get("name")) or _as_text(raw_spot.get("spot_name"))
        if not name:
            name = f"第 {sequence} 站"

        spots.append(
            {
                "spot_id": _as_text(raw_spot.get("spot_id") or raw_spot.get("id")) or f"spot-{sequence:02d}",
                "sequence": sequence,
                "name": name,
                "description": _as_text(raw_spot.get("description") or raw_spot.get("spot_description")),
            }
        )

    return sorted(spots, key=lambda spot: (spot["sequence"], spot["name"]))


def _build_next_stop_reply(
    itinerary: dict[str, Any],
    focused_spot: dict[str, Any] | None,
) -> str:
    spots = itinerary.get("spots") or []
    if not spots:
        return f'我有收到「{itinerary["title"]}」，但目前資料裡還沒有景點順序。'

    if focused_spot is None:
        current_spot = spots[0]
        next_spot = _next_spot(itinerary, current_spot)
        if next_spot:
            return (
                f'依照目前匯入的路線，建議先去「{current_spot["name"]}」，'
                f'接著再到「{next_spot["name"]}」。'
            )
        return f'這份匯入行程目前只有一站：「{current_spot["name"]}」。'

    next_spot = _next_spot(itinerary, focused_spot)
    if next_spot:
        return (
            f'目前焦點是「{focused_spot["name"]}」，'
            f'下一站建議前往「{next_spot["name"]}」。'
        )
    return (
        f'目前焦點是「{focused_spot["name"]}」，這裡已經是匯入路線的最後一站。'
    )


def _next_spot(
    itinerary: dict[str, Any],
    focused_spot: dict[str, Any],
) -> dict[str, Any] | None:
    spots = itinerary.get("spots") or []
    current_sequence = focused_spot.get("sequence", 0)
    for spot in spots:
        if spot.get("sequence", 0) > current_sequence:
            return spot
    return None


def _format_route_preview(
    spots: list[dict[str, Any]],
    *,
    numbered: bool = False,
) -> str:
    if not spots:
        return ""

    if numbered:
        return "\n".join(f'{spot["sequence"]}. {spot["name"]}' for spot in spots)

    names = [spot["name"] for spot in spots[:4]]
    route = " -> ".join(names)
    if len(spots) > 4:
        route += f" -> 另外還有 {len(spots) - 4} 站"
    return route


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
