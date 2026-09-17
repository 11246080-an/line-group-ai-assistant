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


if __name__ == "__main__":
    unittest.main()
