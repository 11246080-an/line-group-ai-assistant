import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("requests", SimpleNamespace())
sys.modules.setdefault(
    "db",
    SimpleNamespace(
        get_api_query_cache=lambda *args, **kwargs: None,
        get_tourism_attractions=lambda *args, **kwargs: [],
        get_tourism_events=lambda *args, **kwargs: [],
        save_api_query_cache=lambda *args, **kwargs: None,
    ),
)

import location_flow


class TextLocationRecommendationTests(unittest.TestCase):
    def test_google_places_failure_falls_back_to_tourism_payload(self):
        tourism_payload = {
            "group_message": "觀光署景點結果",
            "results": [{"name": "宜蘭設治紀念館"}],
            "provider": "tourism_open_data",
            "tourism_kind": "attraction",
        }

        with patch.dict(os.environ, {"GOOGLE_PLACES_API_KEY": "test-key"}, clear=False), patch.object(
            location_flow,
            "_build_tourism_text_recommendation",
            return_value=tourism_payload,
        ), patch.object(
            location_flow,
            "_build_google_places_text_recommendation",
            side_effect=RuntimeError("HTTP 403"),
        ):
            result = location_flow.run_text_location_recommendation(
                query_text="推薦宜蘭景點",
                location_text="宜蘭",
                activity_types=["拍照", "散步"],
                line_group_id="group-1",
            )

        self.assertIs(result, tourism_payload)

    def test_tourism_preference_ranking_prioritizes_requested_styles(self):
        items = [
            {"name": "新竹市立動物園", "description": "親子景點", "town": "東區", "address": "新竹市"},
            {"name": "北埔老街", "description": "客家老街，適合散步拍照", "town": "北埔鄉", "address": "新竹縣"},
            {"name": "青草湖", "description": "湖邊風景與環湖步道", "town": "東區", "address": "新竹市"},
        ]

        ranked = location_flow._rank_tourism_items_by_area(
            items,
            area_text="",
            city="新竹市",
            query_text="想找老街、湖邊或自然景觀，可以拍照散步",
            activity_types=["老街", "湖邊", "自然景觀", "拍照", "散步"],
        )

        self.assertEqual(set(item["name"] for item in ranked[:2]), {"北埔老街", "青草湖"})

    def test_tourism_item_without_description_gets_brief_intro(self):
        result = location_flow._tourism_item_to_result(
            {
                "attraction_id": "a1",
                "name": "青草湖",
                "description": "",
                "city": "新竹市",
                "town": "東區",
                "address": "新竹市東區",
            },
            item_type="attraction",
        )

        self.assertIn("適合散步、拍照", result["description"])
        self.assertNotEqual(result["description"], result["address"])


if __name__ == "__main__":
    unittest.main()
