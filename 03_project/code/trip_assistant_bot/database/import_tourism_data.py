"""
把觀光署開放資料（TDX 格式）的 AttractionList.json / EventList.json 匯入
MongoDB 的 tourism_attractions / tourism_events collection。

這次只處理主資料檔，AttractionFeeList.json / AttractionServiceTimeList.json
先不處理（照交接文件範圍）。

用法：
    python import_tourism_data.py
    python import_tourism_data.py --attraction-file path/to/AttractionList.json --event-file path/to/EventList.json
    python import_tourism_data.py --skip-events        # 只匯景點
    python import_tourism_data.py --skip-attractions   # 只匯活動

預設檔案路徑（相對於這支 script 所在資料夾）：
    Attraction-json/AttractionList.json
    Event-json/EventList.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ATTRACTION_FILE = SCRIPT_DIR / "Attraction-json" / "AttractionList.json"
DEFAULT_EVENT_FILE = SCRIPT_DIR / "Event-json" / "EventList.json"


def _get_nested(source: dict, dotted_path: str, default: Any = None) -> Any:
    """依 'PostalAddress.City' 這種點號路徑取巢狀欄位，中間任一層不存在就回傳 default。"""
    node: Any = source
    for part in dotted_path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def _first_image_url(images: Any) -> str:
    """Images[0].URL；沒有圖片（陣列是空的或不存在）就回傳空字串。"""
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            return first.get("URL") or ""
    return ""


def transform_attraction(raw: dict, dataset_meta: dict, fetched_at: datetime) -> dict:
    """把一筆 TDX 原始景點 JSON 轉成 tourism_attractions 的目標欄位格式。"""
    images = raw.get("Images")
    return {
        "attraction_id": raw.get("AttractionID"),
        "name": raw.get("AttractionName"),
        "alternate_names": raw.get("AlternateNames"),
        "description": raw.get("Description"),
        "latitude": raw.get("PositionLat"),
        "longitude": raw.get("PositionLon"),
        "attraction_classes": raw.get("AttractionClasses"),
        "city": _get_nested(raw, "PostalAddress.City"),
        "city_code": _get_nested(raw, "PostalAddress.CityCode"),
        "town": _get_nested(raw, "PostalAddress.Town"),
        "town_code": _get_nested(raw, "PostalAddress.TownCode"),
        "zip_code": _get_nested(raw, "PostalAddress.ZipCode"),
        "address": _get_nested(raw, "PostalAddress.StreetAddress"),
        "phones": raw.get("Telephones"),
        "image_url": _first_image_url(images),
        "images": images,
        "organizations": raw.get("Organizations"),
        "service_time_info": raw.get("ServiceTimeInfo"),
        "traffic_info": raw.get("TrafficInfo"),
        "parking_info": raw.get("ParkingInfo"),
        "facilities": raw.get("Facilities"),
        "service_status": raw.get("ServiceStatus"),
        "is_public_access": raw.get("IsPublicAccess"),
        "is_accessible_for_free": raw.get("IsAccessibleForFree"),
        "fee_info": raw.get("FeeInfo"),
        "payment_methods": raw.get("PaymentMethods"),
        "located_cities": raw.get("LocatedCities"),
        "website_url": raw.get("WebsiteURL"),
        "reservation_urls": raw.get("ReservationURLs"),
        "map_urls": raw.get("MapURLs"),
        "same_as_urls": raw.get("SameAsURLs"),
        "social_media_urls": raw.get("SocialMediaURLs"),
        "visit_duration": raw.get("VisitDuration"),
        "assets_class": raw.get("AssetsClass"),
        "sub_attractions": raw.get("SubAttractions"),
        "remarks": raw.get("Remarks"),
        "source_update_time": raw.get("UpdateTime"),
        "dataset_update_time": dataset_meta.get("UpdateTime"),
        "dataset_update_interval": dataset_meta.get("UpdateInterval"),
        "language": dataset_meta.get("Language"),
        "provider_id": dataset_meta.get("ProviderID"),
        "fetched_at": fetched_at,
        "raw_payload": raw,
    }


def transform_event(raw: dict, dataset_meta: dict, fetched_at: datetime) -> dict:
    """把一筆 TDX 原始活動 JSON 轉成 tourism_events 的目標欄位格式。"""
    images = raw.get("Images")
    return {
        "event_id": raw.get("EventID"),
        "name": raw.get("EventName"),
        "alternate_names": raw.get("AlternateNames"),
        "description": raw.get("Description"),
        "latitude": raw.get("PositionLat"),
        "longitude": raw.get("PositionLon"),
        "event_classes": raw.get("EventClasses"),
        "city": _get_nested(raw, "PostalAddress.City"),
        "city_code": _get_nested(raw, "PostalAddress.CityCode"),
        "town": _get_nested(raw, "PostalAddress.Town"),
        "town_code": _get_nested(raw, "PostalAddress.TownCode"),
        "zip_code": _get_nested(raw, "PostalAddress.ZipCode"),
        "address": _get_nested(raw, "PostalAddress.StreetAddress"),
        "phones": raw.get("Telephones"),
        "image_url": _first_image_url(images),
        "images": images,
        "organizations": raw.get("Organizations"),
        "traffic_info": raw.get("TrafficInfo"),
        "parking_info": raw.get("ParkingInfo"),
        "facilities": raw.get("Facilities"),
        "is_accessible_for_free": raw.get("IsAccessibleForFree"),
        "fee_info": raw.get("FeeInfo"),
        "payment_methods": raw.get("PaymentMethods"),
        "located_cities": raw.get("LocatedCities"),
        "website_url": raw.get("WebsiteURL"),
        "reservation_urls": raw.get("ReservationURLs"),
        "map_urls": raw.get("MapURLs"),
        "same_as_urls": raw.get("SameAsURLs"),
        "social_media_urls": raw.get("SocialMediaURLs"),
        "participant": raw.get("Participant"),
        "start_time": raw.get("StartDateTime"),
        "end_time": raw.get("EndDateTime"),
        "event_status": raw.get("EventStatus"),
        "previous_start_dates": raw.get("PreviousStartDates"),
        "calendar_urls": raw.get("CalendarURLs"),
        "sub_events": raw.get("SubEvents"),
        "remarks": raw.get("Remarks"),
        "source_update_time": raw.get("UpdateTime"),
        "dataset_update_time": dataset_meta.get("UpdateTime"),
        "dataset_update_interval": dataset_meta.get("UpdateInterval"),
        "language": dataset_meta.get("Language"),
        "provider_id": dataset_meta.get("ProviderID"),
        "fetched_at": fetched_at,
        "raw_payload": raw,
    }


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def import_attractions(path: Path) -> dict:
    print(f"讀取景點資料：{path}")
    data = _load_json(path)
    raw_items = data.get("Attractions") or []
    fetched_at = datetime.now(timezone.utc)
    items = [transform_attraction(item, data, fetched_at) for item in raw_items]
    print(f"  解析出 {len(items)} 筆景點，開始 upsert 匯入...")
    result = db.save_tourism_attractions(items)
    print(f"  完成：{result}")
    return result


def import_events(path: Path) -> dict:
    print(f"讀取活動資料：{path}")
    data = _load_json(path)
    raw_items = data.get("Events") or []
    fetched_at = datetime.now(timezone.utc)
    items = [transform_event(item, data, fetched_at) for item in raw_items]
    print(f"  解析出 {len(items)} 筆活動，開始 upsert 匯入...")
    result = db.save_tourism_events(items)
    print(f"  完成：{result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attraction-file", type=Path, default=DEFAULT_ATTRACTION_FILE)
    parser.add_argument("--event-file", type=Path, default=DEFAULT_EVENT_FILE)
    parser.add_argument("--skip-attractions", action="store_true", help="只匯活動，不匯景點")
    parser.add_argument("--skip-events", action="store_true", help="只匯景點，不匯活動")
    args = parser.parse_args()

    try:
        db.ensure_indexes()
        print("索引已確認建立。")
    except Exception as exc:
        print(f"建立索引失敗（仍會嘗試繼續匯入）：{exc}")

    if not args.skip_attractions:
        if args.attraction_file.exists():
            import_attractions(args.attraction_file)
        else:
            print(f"找不到景點資料檔，略過：{args.attraction_file}")

    if not args.skip_events:
        if args.event_file.exists():
            import_events(args.event_file)
        else:
            print(f"找不到活動資料檔，略過：{args.event_file}")


if __name__ == "__main__":
    main()
