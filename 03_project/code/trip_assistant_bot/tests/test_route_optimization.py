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

    def test_route_signal_can_trigger_without_scenario_five(self):
        result = {"scenario_code": "劇本四", "extracted_info": {"location": ["台北101", "故宮"]}}
        self.assertTrue(should_optimize_route(result, user_text="台北101、故宮怎麼排比較順"))

    def test_direct_text_locations_can_trigger_route_optimization(self):
        lookup = {"阿里山": self.a, "奮起湖": self.b, "檜意森活村": self.c}
        result = {"scenario_code": "no_reply", "extracted_info": {"location": []}}
        reply = build_optimized_route_reply(
            result,
            user_text="阿里山、奮起湖、檜意森活村怎麼排比較順",
            geocoder=lookup.get,
        )
        self.assertIn("基礎路線最佳化", reply)
        for name in ("A", "B", "C"):
            self.assertIn(name, reply)

    def test_route_prefers_options_over_generic_locations(self):
        lookup = {
            "阿里山": RouteSpot("阿里山", 23.51, 120.80),
            "奮起湖": RouteSpot("奮起湖", 23.50, 120.69),
            "檜意森活村": RouteSpot("檜意森活村", 23.48, 120.45),
            "嘉義": RouteSpot("嘉義市", 23.48, 120.45),
        }
        result = {
            "scenario_code": "劇本五",
            "extracted_info": {
                "location": ["嘉義", "阿里山", "奮起湖", "檜意森活村"],
                "options": ["阿里山", "奮起湖", "檜意森活村"],
            },
        }
        reply = build_optimized_route_reply(
            result,
            user_text="那我們從嘉義出發，這幾個景點要怎麼走會比較順？",
            geocoder=lookup.get,
        )
        self.assertIn("阿里山", reply)
        self.assertIn("奮起湖", reply)
        self.assertIn("檜意森活村", reply)
        self.assertNotIn("嘉義市", reply)

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
