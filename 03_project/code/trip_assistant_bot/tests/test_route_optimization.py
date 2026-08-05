import unittest

from route_optimization import RouteSpot, build_optimized_route_reply, optimize_spots, route_distance_km, should_optimize_route


class RouteOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.a = RouteSpot("A", 25.0, 121.0)
        self.b = RouteSpot("B", 25.0, 121.1)
        self.c = RouteSpot("C", 25.0, 121.2)

    def test_exact_optimizer_shortens_open_route(self):
        original = [self.a, self.c, self.b]
        optimized = optimize_spots(original)
        self.assertLess(route_distance_km(optimized), route_distance_km(original))
        self.assertIn([spot.name for spot in optimized], (["A", "B", "C"], ["C", "B", "A"]))

    def test_only_scenario_five_with_two_locations_runs(self):
        result = {"scenario_code": "劇本五", "extracted_info": {"location": ["台北101", "故宮"]}}
        self.assertTrue(should_optimize_route(result))
        result["scenario_code"] = "劇本四"
        self.assertFalse(should_optimize_route(result))

    def test_reply_uses_all_resolved_locations(self):
        lookup = {"台北101": self.a, "故宮": self.b, "士林夜市": self.c}
        result = {"scenario_code": "劇本五", "extracted_info": {"location": list(lookup)}}
        reply = build_optimized_route_reply(result, geocoder=lookup.get)
        self.assertIn("1.", reply)
        self.assertIn("Google 地圖路線", reply)
        for name in ("A", "B", "C"):
            self.assertIn(name, reply)


if __name__ == "__main__":
    unittest.main()
