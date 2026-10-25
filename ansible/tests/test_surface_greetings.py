#!/usr/bin/env python3
"""Exercise reporter recovery and the actual HA greeting templates offline."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ansible/files/surface-dashboard"))
import reporter

SPEC = json.loads((ROOT / "raw/home-assistant/front-door-greetings.json").read_text())
ACTIONS = SPEC["automations"]["surface_greetings"]["actions"]
MESSAGE = next(step["variables"]["message"] for step in ACTIONS
               if "message" in step.get("variables", {}))
TZ = ZoneInfo("Europe/Amsterdam")


class ReporterTests(unittest.TestCase):
    def test_startup_and_restart_do_not_manufacture_wakes(self):
        for on in (True, False):
            r = reporter.Reporter()
            r.observe(on, 1000)
            payload = r.payload(1000, 1)
            self.assertEqual(payload, {"display_on": on, "wake_at": 0})
            r.acknowledge(payload, 1)
            r.observe(on, 1001)
            self.assertIsNone(r.payload(1001, 2))

    def test_real_wake_published_once_and_heartbeat_does_not_repeat_it(self):
        r = reporter.Reporter()
        r.observe(False, 1000)
        r.acknowledge(r.payload(1000, 0), 0)
        r.observe(True, 1001)
        payload = r.payload(1001, 1)
        self.assertEqual(payload["wake_at"], 1001)
        r.acknowledge(payload, 1)
        self.assertIsNone(r.payload(1002, 2))
        self.assertEqual(r.payload(1031, 31), {"display_on": True, "wake_at": 0})

    def test_network_failure_throttles_retries_and_drops_expired_wake(self):
        r = reporter.Reporter()
        r.observe(False, 1000)
        r.acknowledge(r.payload(1000, 0), 0)
        r.observe(True, 1001)
        r.failed(1)
        self.assertIsNone(r.payload(1003, 3))
        self.assertEqual(r.payload(1006, 6)["wake_at"], 1001)
        r.failed(6)
        payload = r.payload(1020, 20)
        self.assertEqual(payload, {"display_on": True, "wake_at": 0})
        r.acknowledge(payload, 20)
        self.assertIsNone(r.payload(1021, 21))

    def test_token_requires_private_regular_file_and_never_browser_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ha-token"
            path.write_text("test-only-token\n")
            path.chmod(0o600)
            self.assertEqual(reporter.token(path), "test-only-token")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                reporter.token(path)
            path.chmod(0o600)
            alias = Path(directory) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(ValueError):
                reporter.token(alias)

    def test_credential_cannot_be_redirected(self):
        self.assertIsNone(reporter.NoRedirect().redirect_request(None, None, 302, "", {},
                                                               "https://other.test/"))
        self.assertEqual(reporter.endpoint({"dashboard_url": "https://hass.test/front-door/home"}),
                         "https://hass.test/api/services/script/surface_report_display")
        for url in ("http://hass.test", "https://user:pass@hass.test", "https://hass.test?token=x"):
            with self.assertRaises(ValueError):
                reporter.endpoint({"dashboard_url": url})

    def test_browser_or_display_startup_wake_is_suppressed(self):
        r = reporter.Reporter()
        r.observe(False, 1000)
        r.acknowledge(r.payload(1000, 0), 0)
        r.observe(True, 1001, allow_wake=False)
        self.assertEqual(r.payload(1001, 1)["wake_at"], 0)
        r.observe(False, 1020)
        r.observe(True, 1021)
        self.assertEqual(r.payload(1021, 21)["wake_at"], 1021)
        for times, expected in [("100000000\n\n110000000\n", False),
                                ("100000000\n\n90000000\n", True), ("0\n0\n", False)]:
            with patch.object(reporter.subprocess, "run", return_value=SimpleNamespace(stdout=times)):
                self.assertEqual(reporter.controllers_settled(120), expected)


class GreetingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 13, 7, 0, tzinfo=TZ)
        self.states = {}
        self.attributes = {}
        self.env = Environment(undefined=StrictUndefined)
        self.env.globals.update(now=lambda: self.now, timedelta=timedelta,
                                states=lambda entity: self.states.get(entity, "unknown"),
                                state_attr=lambda entity, key: self.attributes.get((entity, key)),
                                is_number=lambda value: self.is_number(value),
                                as_timestamp=self.timestamp)
        self.env.filters["timestamp_custom"] = lambda value, fmt, local=True: datetime.fromtimestamp(
            float(value), TZ).strftime(fmt)

    @staticmethod
    def is_number(value):
        try:
            float(value)
            return value is not None
        except (ValueError, TypeError):
            return False

    @staticmethod
    def timestamp(value, default=None):
        try:
            if isinstance(value, (float, int)):
                return value
            dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
            return dt.replace(tzinfo=TZ).timestamp() if dt.tzinfo is None else dt.timestamp()
        except (ValueError, TypeError):
            return default

    def render(self, **values):
        variables = dict(morning=True, welcome=False, personal_events={}, birthday_events={},
                         holiday_events={})
        variables.update(values)
        return self.env.from_string(MESSAGE).render(**variables).strip()

    def test_welcome_alone_and_combined_greeting(self):
        self.assertEqual(self.render(morning=False, welcome=True), "Welcome home, Sem.")
        self.assertEqual(self.render(welcome=True), "Welcome home, Sem. Good morning, Sem.")

    def test_agenda_orders_events_and_omits_finished_events(self):
        events = [{"start": "2026-10-13T09:00:00+02:00", "end": "2026-10-13T10:00:00+02:00",
                   "summary": "Appointment"},
                  {"start": "2026-10-13T06:00:00+02:00", "end": "2026-10-13T06:30:00+02:00",
                   "summary": "Finished"},
                  {"start": "2026-10-13T06:30:00+02:00", "end": "2026-10-13T08:00:00+02:00",
                   "summary": "Ongoing"}]
        birthdays = [{"start": "2026-10-13", "end": "2026-10-14", "summary": "Birthday"}]
        message = self.render(personal_events={"personal": {"events": events}},
                              birthday_events={"birthdays": {"events": birthdays}})
        self.assertNotIn("Finished", message)
        self.assertLess(message.index("All day, Birthday"), message.index("06:30, Ongoing"))
        self.assertLess(message.index("06:30, Ongoing"), message.index("09:00, Appointment"))

    def test_missing_car_data_never_becomes_zero_or_unknown_speech(self):
        self.assertEqual(self.render(), "Good morning, Sem.")
        self.states.update({"sensor.garage_audi_battery_level": "80",
                            "sensor.garage_audi_range": "204",
                            "sensor.garage_audi_charging_status": "unknown"})
        self.attributes[("sensor.garage_audi_range", "unit_of_measurement")] = "km"
        message = self.render()
        self.assertIn("80 percent", message)
        self.assertIn("204 kilometres", message)
        self.assertNotIn("Charging status", message)

    def test_bins_today_tomorrow_stale_and_missing(self):
        for stream, days, date in [("gft", 0, 20261013), ("papier", 1, 20261014),
                                   ("pmd", 1, 20261001)]:
            entity = "sensor.afvalbeheer_circulus_circulus_" + stream
            self.states[entity] = "collection date"
            self.attributes[(entity, "Days_until")] = days
            self.attributes[(entity, "Sort_date")] = date
        message = self.render()
        self.assertIn("food and garden waste bin is collected today", message)
        self.assertIn("paper bin is collected tomorrow", message)
        self.assertNotIn("drink cartons", message)
        self.assertNotIn("residual waste", message)

    def test_morning_cutoff_and_persistent_daily_deduplication(self):
        variables = next(step["variables"] for step in ACTIONS
                         if "morning" in step.get("variables", {}))
        template = self.env.from_string(variables["morning"])
        self.states["input_datetime.surface_last_morning"] = "2026-10-12"
        for hour, minute, expected in [(3, 59, "False"), (4, 0, "True"),
                                       (11, 59, "True"), (12, 0, "False"),
                                       (14, 0, "False"), (16, 50, "False")]:
            wake = self.now.replace(hour=hour, minute=minute)
            trigger = SimpleNamespace(to_state=SimpleNamespace(attributes={"timestamp": wake.timestamp()}))
            self.assertEqual(template.render(trigger=trigger), expected)
        self.states["input_datetime.surface_last_morning"] = "2026-10-13"
        self.assertEqual(template.render(trigger=trigger), "False")

    def test_morning_wake_cannot_become_a_noon_briefing(self):
        variables = next(step["variables"] for step in ACTIONS
                         if "morning" in step.get("variables", {}))
        self.now = self.now.replace(hour=12)
        wake = self.now - timedelta(seconds=20)
        trigger = SimpleNamespace(to_state=SimpleNamespace(attributes={"timestamp": wake.timestamp()}))
        self.assertEqual(self.env.from_string(variables["morning"]).render(trigger=trigger), "False")

    def test_audio_recovery_waits_through_unavailable_until_ready(self):
        wait = ACTIONS[2]["then"][0]
        wake = self.now
        trigger = SimpleNamespace(to_state=SimpleNamespace(attributes={"timestamp": wake.timestamp()}))
        timeout = self.env.from_string(wait["timeout"]["seconds"])
        budget = float(timeout.render(trigger=trigger, deadline=wake.timestamp() + 15))
        self.assertGreater(budget, 21)
        self.assertLessEqual(budget, 45)
        self.assertFalse(wait["continue_on_timeout"])
        ready = self.env.from_string(wait["wait_template"])
        for elapsed, state, expected in [(0, "unknown", "False"),
                                          (20, "unavailable", "False"),
                                          (21, "off", "True"), (22, "idle", "True")]:
            self.now = wake + timedelta(seconds=elapsed)
            self.states["media_player.surface"] = state
            self.assertEqual(ready.render(), expected)
        # The wake, rather than each reconnect, bounds the recovery window.
        self.now = wake + timedelta(seconds=46)
        self.assertEqual(float(timeout.render(trigger=trigger)), 0)

    def test_arrival_requires_confirmed_absence_and_ignores_restored_presence(self):
        presence = SPEC["automations"]["surface_presence"]
        guard = self.env.from_string(presence["conditions"][0]["value_template"])
        for before, after, expected in [(None, "home", "False"), ("unknown", "home", "False"),
                                        ("unavailable", "home", "False"), ("work", "home", "True")]:
            trigger = SimpleNamespace(from_state=None if before is None else SimpleNamespace(state=before),
                                      to_state=SimpleNamespace(state=after))
            self.assertEqual(guard.render(trigger=trigger), expected)
        condition = presence["actions"][0]["choose"][1]["sequence"][0]["if"][0]["value_template"]
        template = self.env.from_string(condition)
        key = ("input_datetime.surface_away_since", "timestamp")
        for elapsed, expected in [(299, "False"), (300, "True"), (600, "True")]:
            self.attributes[key] = self.now.timestamp() - elapsed
            self.assertEqual(template.render(), expected)
        self.attributes[key] = 0
        self.assertEqual(template.render(), "False")

    def test_previous_day_delayed_wake_is_not_a_new_days_briefing(self):
        condition = SPEC["automations"]["surface_greetings"]["conditions"][-1]["value_template"]
        template = self.env.from_string(condition)
        self.now = datetime(2026, 10, 14, 0, 0, 5, tzinfo=TZ)
        yesterday = self.now - timedelta(seconds=10)
        trigger = SimpleNamespace(to_state=SimpleNamespace(attributes={"timestamp": yesterday.timestamp()}))
        self.assertEqual(template.render(trigger=trigger), "False")


if __name__ == "__main__":
    unittest.main()
