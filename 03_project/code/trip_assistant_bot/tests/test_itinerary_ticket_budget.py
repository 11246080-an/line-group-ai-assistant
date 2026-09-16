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


if __name__ == "__main__":
    unittest.main()
