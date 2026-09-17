"""
一次性 migration，對應「資料庫交接文件：近期行程功能補強」P0-5 / P1-1 / §10。

預設 dry-run，只印出會被影響的筆數，不會真的寫入；加 --apply 才會真的執行。

三件事：
1. 清理 §8 指定的 3 個不可靠座標 spot（只清這 3 個，不做全庫批次更新）。
2. 既有 published_itineraries 補上 status="published"（新文件已經由
   publish_itinerary_snapshot() 自動帶上，這裡只補歷史資料）。
3. 修正 §10 發現的重複 expense_book_id：把「較晚建立、被誤連到已關閉帳本」
   的那份行程解除連結（expense_book_id 設回 None），讓 expense_book_id 的
   unique 索引可以順利建立。

用法：
    python migrate_itinerary_enhancements.py            # dry-run，只看數字
    python migrate_itinerary_enhancements.py --apply    # 真的寫入
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

# 資料庫交接文件 §8.2 指定的三個目標（只動這三個，不用名稱批次比對）。
_BAD_COORDINATE_SPOTS = [
    {"source_itinerary_id": "trip_jYaMS-CLicTgamGZ", "spot_id": "trip_jYaMS-CLicTgamGZ_s2"},
    {"source_itinerary_id": "trip_lVi51hJBO4Hrm-yZ", "spot_id": "trip_lVi51hJBO4Hrm-yZ_s2"},
    {"source_itinerary_id": "trip_lVi51hJBO4Hrm-yZ", "spot_id": "trip_lVi51hJBO4Hrm-yZ_s3"},
]

_CLEARED_SPOT_FIELDS = {
    "address": "",
    "latitude": None,
    "longitude": None,
    "coordinate_source": None,
}


def clean_bad_coordinates(mongo_db, apply: bool) -> None:
    print("\n=== 1. 清理 §8 指定的 3 個不可靠座標 spot ===")
    for target in _BAD_COORDINATE_SPOTS:
        itinerary_id = target["source_itinerary_id"]
        spot_id = target["spot_id"]

        # dry-run：先確認這個 spot 目前存在，列出目前的值
        priv = mongo_db.itineraries.find_one(
            {"itinerary_id": itinerary_id, "spots.spot_id": spot_id},
            {"spots.$": 1},
        )
        pub = mongo_db.published_itineraries.find_one(
            {"source_itinerary_id": itinerary_id, "spots.spot_id": spot_id},
            {"spots.$": 1},
        )
        priv_hit = 1 if priv else 0
        pub_hit = 1 if pub else 0
        print(f"  {spot_id}: private matched={priv_hit}, published matched={pub_hit}")
        if priv:
            print(f"    private 現值: {priv['spots'][0]}")
        if pub:
            print(f"    published 現值: {pub['spots'][0]}")

        if not apply:
            continue

        array_filters = [{"elem.spot_id": spot_id}]
        set_fields = {f"spots.$[elem].{k}": v for k, v in _CLEARED_SPOT_FIELDS.items()}

        priv_result = mongo_db.itineraries.update_one(
            {"itinerary_id": itinerary_id},
            {"$set": {**set_fields, "updated_at": db._utc_now()}},
            array_filters=array_filters,
        )
        pub_result = mongo_db.published_itineraries.update_one(
            {"source_itinerary_id": itinerary_id},
            {"$set": {**set_fields, "updated_at": db._utc_now()}},
            array_filters=array_filters,
        )
        print(
            f"    APPLY -> private matched={priv_result.matched_count} modified={priv_result.modified_count}, "
            f"published matched={pub_result.matched_count} modified={pub_result.modified_count}"
        )


def backfill_published_status(mongo_db, apply: bool) -> None:
    print("\n=== 2. published_itineraries 補 status=\"published\" ===")
    query = {"status": {"$exists": False}}
    matched = mongo_db.published_itineraries.count_documents(query)
    print(f"  目前沒有 status 欄位的筆數: {matched}")
    if not apply:
        return
    result = mongo_db.published_itineraries.update_many(
        query, {"$set": {"status": "published", "updated_at": db._utc_now()}}
    )
    print(f"  APPLY -> matched={result.matched_count} modified={result.modified_count}")


def report_duplicate_expense_book(mongo_db) -> None:
    """
    只回報，不自動修——重複的 expense_book_id 不一定是誤連，可能是產品真的
    允許一本帳本連多份行程，這是需要人判斷的事，不是能用「保留最早那筆」
    這種通用規則自動處理的事（這支 script 曾經因為重跑 --apply 而誤觸這個
    邏輯，把 4 份彼此標題完全不同、看起來都合法的行程解除連結，所以拿掉了
    自動修正，只留報告）。看到重複時，先去確認這些行程是不是真的該共用同一本
    帳本，再決定要不要手動處理、或要不要把 expense_book_id 改成 unique 索引。
    """
    print("\n=== 3. 重複的 expense_book_id（只報告，不會自動修改） ===")
    pipeline = [
        {"$match": {"expense_book_id": {"$ne": None}}},
        {"$group": {
            "_id": "$expense_book_id",
            "count": {"$sum": 1},
            "docs": {"$push": {"itinerary_id": "$itinerary_id", "created_at": "$created_at",
                                "status": "$status", "title": "$title"}},
        }},
        {"$match": {"count": {"$gt": 1}}},
    ]
    dupes = list(mongo_db.itineraries.aggregate(pipeline))
    print(f"  重複的 expense_book_id 組數: {len(dupes)}")
    for group in dupes:
        docs = sorted(group["docs"], key=lambda d: d["created_at"])
        print(f"  book={group['_id']}：")
        for d in docs:
            print(f"    - {d['itinerary_id']}｜{d['title']!r}｜status={d['status']}｜created_at={d['created_at']}")
    if dupes:
        print("  ↑ 這些需要人工確認是不是真的該共用帳本，這支 script 不會自動處理。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真的寫入；不加只會 dry-run 印出數字")
    args = parser.parse_args()

    mongo_db = db.get_db()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"執行模式：{mode}")

    clean_bad_coordinates(mongo_db, args.apply)
    backfill_published_status(mongo_db, args.apply)
    report_duplicate_expense_book(mongo_db)  # 只報告；--apply 不影響這一步

    if not args.apply:
        print("\n這是 dry-run，沒有寫入任何資料。確認數字正確後加 --apply 重跑。")


if __name__ == "__main__":
    main()
