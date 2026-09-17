import unittest
from unittest.mock import patch
import sys
from types import SimpleNamespace

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
import itinerary_flow


class ItineraryTicketBudgetTests(unittest.TestCase):
    def test_estimate_ticket_budget_sums_known_prices_and_marks_missing(self):
        def fake_ready(names):
            return True

        def fake_db_function(name):
            if name == "get_tourism_attraction_fees_by_ids":
                return lambda ids: [
                    {
                        "attraction_id": "Attraction_376480000A_000253",
                        "default_price": 200,
                        "fees": [{"name": "全票", "price": 200}],
                    }
                ]
            raise AssertionError(f"unexpected db function: {name}")

        spots = [
            {"name": "清境農場", "attraction_id": "Attraction_376480000A_000253"},
            {"name": "沒有票價的景點", "attraction_id": "missing"},
        ]
        with patch.object(itinerary_flow, "database_contract_ready", fake_ready), patch.object(
            itinerary_flow, "_db_function", fake_db_function
        ):
            result = itinerary_flow._estimate_ticket_budget(spots, region="南投縣")

        self.assertEqual(result["estimated_total"], 200)
        self.assertEqual(result["priced_count"], 1)
        self.assertEqual(result["total_spots"], 2)
        self.assertEqual(result["missing_names"], ["沒有票價的景點"])
        self.assertEqual(spots[0]["ticket_price"], 200)

    def test_apply_ticket_budget_updates_estimated_budget_when_price_exists(self):
        def fake_ready(names):
            return True

        def fake_db_function(name):
            if name == "get_tourism_attraction_fees_by_ids":
                return lambda ids: [
                    {
                        "attraction_id": "Attraction_376480000A_000253",
                        "default_price": 200,
                        "fees": [{"name": "全票", "price": 200}],
                    }
                ]
            raise AssertionError(f"unexpected db function: {name}")

        itinerary = {
            "region": "南投縣",
            "estimated_budget": 9999,
            "spots": [
                {"name": "清境農場", "attraction_id": "Attraction_376480000A_000253"},
            ],
        }
        with patch.object(itinerary_flow, "database_contract_ready", fake_ready), patch.object(
            itinerary_flow, "_db_function", fake_db_function
        ):
            updated = itinerary_flow._apply_ticket_budget_to_itinerary(itinerary)

        self.assertEqual(updated["estimated_budget"], 200)
        self.assertEqual(updated["ticket_budget"]["items"][0]["fee_name"], "全票")

    def test_service_time_notice_formats_known_service_hours(self):
        def fake_ready(names):
            return True

        def fake_db_function(name):
            if name == "get_tourism_attraction_service_times_by_ids":
                return lambda ids: [
                    {
                        "attraction_id": "spot-1",
                        "service_time": [
                            {
                                "Name": "開放時間",
                                "ServiceDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
                                "StartTime": "08:00:00",
                                "EndTime": "17:00:00",
                            }
                        ],
                    }
                ]
            raise AssertionError(f"unexpected db function: {name}")

        itinerary = {
            "region": "南投縣",
            "spots": [
                {"name": "有營業時間", "attraction_id": "spot-1"},
                {"name": "沒有營業時間", "attraction_id": "missing"},
            ],
        }
        with patch.object(itinerary_flow, "database_contract_ready", fake_ready), patch.object(
            itinerary_flow, "_db_function", fake_db_function
        ):
            updated = itinerary_flow._apply_service_time_notice_to_itinerary(itinerary)

        self.assertEqual(updated["service_time_notice"]["covered_count"], 1)
        self.assertEqual(updated["service_time_notice"]["missing_names"], ["沒有營業時間"])
        self.assertIn("週一至週五 08:00-17:00", updated["spots"][0]["service_time_summary"])
        self.assertIn("營業時間提醒", itinerary_flow._service_time_summary_text(updated["service_time_notice"]))

    def test_route_duration_estimates_replace_ai_transport_minutes(self):
        fake_estimates = [
            SimpleNamespace(
                duration_minutes=18,
                distance_meters=6800,
                travel_mode="DRIVE",
                routing_preference="TRAFFIC_AWARE",
                source="google_routes",
            ),
            SimpleNamespace(
                duration_minutes=52,
                distance_meters=5100,
                travel_mode="WALK",
                routing_preference="",
                source="google_routes",
            ),
        ]
        itinerary = {
            "spots": [
                {"sequence": 1, "name": "A", "latitude": 23.1, "longitude": 120.1},
                {"sequence": 2, "name": "B", "latitude": 23.2, "longitude": 120.2},
            ],
            "transport": [
                {"from_sequence": 1, "to_sequence": 2, "mode": "步行", "estimated_minutes": 5}
            ],
        }
        with patch.object(itinerary_flow, "routes_api_configured", lambda: True), patch.object(
            itinerary_flow, "estimate_route_durations", lambda **kwargs: fake_estimates
        ):
            updated = itinerary_flow._apply_route_duration_estimates_to_itinerary(itinerary)

        leg = updated["transport"][0]
        self.assertEqual(leg["estimated_minutes"], 18)
        self.assertEqual(leg["mode"], "開車")
        self.assertEqual(leg["route_duration_source"], "google_routes")
        self.assertEqual(len(leg["route_duration_options"]), 2)
        self.assertEqual(leg["route_duration_options"][1]["mode"], "步行")
        self.assertEqual(updated["route_duration_notice"]["updated_legs"], 1)


if __name__ == "__main__":
    unittest.main()
