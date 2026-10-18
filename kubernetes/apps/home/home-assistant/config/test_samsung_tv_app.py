import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

import samsung_tv_app as sensor


class CurrentAppTests(unittest.TestCase):
    def poll(self, apps):
        with patch.object(sensor, "APP_IDS", tuple(apps)), patch.object(
            sensor, "read_app", side_effect=apps.get
        ):
            return sensor.current_app()

    def test_foreground_app_ignores_background_running_apps(self):
        self.assertEqual(
            self.poll({
                "youtube": {"name": "YouTube", "visible": True, "running": True},
                "spotify": {"name": "Spotify", "visible": False, "running": True},
                "missing": None,
            }),
            {"app": "YouTube", "available": True},
        )

    def test_tv_or_hdmi_does_not_invent_an_app(self):
        self.assertEqual(
            self.poll({"youtube": {"name": "YouTube", "visible": False}}),
            {"app": "unknown", "available": True},
        )

    def test_unreachable_tv_is_unavailable(self):
        self.assertEqual(
            self.poll({"youtube": None}),
            {"app": "unknown", "available": False},
        )

    def test_ambiguous_visibility_is_unknown(self):
        self.assertEqual(
            self.poll({
                "youtube": {"name": "YouTube", "visible": True},
                "spotify": {"name": "Spotify", "visible": True},
            }),
            {"app": "unknown", "available": True},
        )

    def test_rejects_malformed_responses_and_network_errors(self):
        for payload in [b"not json", b"[]", b'{"name":"YouTube","visible":"false"}']:
            with self.subTest(payload=payload), patch.object(sensor, "build_opener") as opener:
                opener.return_value.open.return_value.__enter__.return_value = BytesIO(payload)
                self.assertIsNone(sensor.read_app("youtube"))
        with patch.object(sensor, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError("unreachable")
            self.assertIsNone(sensor.read_app("youtube"))


if __name__ == "__main__":
    unittest.main()
