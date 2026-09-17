import os
import unittest
from unittest.mock import patch

from google_routes import configured_travel_modes, estimate_route_duration


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "routes": [
                {
                    "duration": "780s",
                    "distanceMeters": 5200,
                }
            ]
        }


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeResponse()


class GoogleRoutesTests(unittest.TestCase):
    def test_estimate_route_duration_parses_google_routes_response(self):
        session = _FakeSession()
        with patch.dict(os.environ, {"GOOGLE_ROUTES_API_KEY": "test-key"}, clear=False):
            result = estimate_route_duration(
                origin_latitude=23.1,
                origin_longitude=120.1,
                destination_latitude=23.2,
                destination_longitude=120.2,
                session=session,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.duration_minutes, 13)
        self.assertEqual(result.distance_meters, 5200)
        self.assertEqual(result.travel_mode, "DRIVE")
        self.assertEqual(result.routing_preference, "TRAFFIC_AWARE")
        headers = session.calls[0][1]["headers"]
        self.assertIn("routes.duration", headers["X-Goog-FieldMask"])

    def test_configured_travel_modes_supports_multiple_values(self):
        with patch.dict(os.environ, {"GOOGLE_ROUTES_TRAVEL_MODES": "DRIVE,WALK,TRANSIT"}, clear=False):
            self.assertEqual(configured_travel_modes(), ["DRIVE", "WALK", "TRANSIT"])


if __name__ == "__main__":
    unittest.main()
