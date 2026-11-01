import datetime
import json
from pathlib import Path
import re
import unittest

from jinja2 import Environment, StrictUndefined


CONFIG = json.loads(Path(__file__).with_name("surface_todo_voice.json").read_text())


def parse_date(value, pattern, default=None):
    try:
        return datetime.datetime.strptime(value, pattern)
    except (TypeError, ValueError):
        return default


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(undefined=StrictUndefined)
        self.env.filters["regex_match"] = lambda value, pattern: re.match(pattern, value) is not None
        self.env.filters["regex_search"] = lambda value, pattern: re.search(pattern, value) is not None
        self.env.filters["regex_replace"] = lambda value, pattern, replacement: re.sub(pattern, replacement, value)
        self.env.globals["strptime"] = parse_date
        self.env.globals["now"] = lambda: datetime.datetime(2026, 11, 1, 17, 0)
        self.env.globals["timedelta"] = datetime.timedelta

    def invalid(self, **values):
        return self.env.from_string(CONFIG["script"]["sequence"][0]["if"]).render(**values) == "True"

    def test_valid_title_and_optional_date(self):
        for values in ({"title": "Get groceries"}, {"title": "Get groceries", "due_date": "2026-11-02"},
                       {"title": "Get groceries", "due_date": "2028-02-29"},
                       {"title": "Get groceries", "due_date": ""}, {"title": "Get groceries", "due_date": None}):
            with self.subTest(values=values):
                self.assertFalse(self.invalid(**values))

    def test_invalid_title_is_rejected_before_save(self):
        for values in ({}, {"title": ""}, {"title": "   "}, {"title": None}, {"title": 1},
                       {"title": []}, {"title": "x" * 256}):
            with self.subTest(values=values):
                self.assertTrue(self.invalid(**values))

    def test_invalid_date_is_rejected_before_save(self):
        for value in ("tomorrow", "2026-02-29", "2026-13-01", "2026-1-1", "2026-11-02T08:00:00", 1, [], {}):
            with self.subTest(value=value):
                self.assertTrue(self.invalid(title="Get groceries", due_date=value))

    def test_save_targets_only_todo_and_success_follows_write(self):
        actions = CONFIG["script"]["sequence"]
        write = actions[3]["then"][0]
        self.assertEqual(actions[1]["action"], "todo.get_items")
        self.assertEqual(write["action"], "todo.add_item")
        self.assertEqual(write["target"], {"entity_id": "todo.todo"})
        self.assertNotIn("continue_on_error", write)
        self.assertEqual(actions[-1]["response_variable"], "saved_reminder")
        self.assertTrue(CONFIG["pipeline"]["prefer_local_intents"])
        self.assertEqual(CONFIG["pipeline"]["conversation_engine"], "conversation.home_assistant")

    def parse_reminder(self, reminder):
        values = {"trigger": {"slots": {"reminder": reminder}}}
        for action in CONFIG["automation"]["actions"]:
            for key, value in action.get("variables", {}).items():
                values[key] = self.env.from_string(value).render(**values)
        return values["reminder_title"], values["reminder_due"]

    def test_spoken_relative_dates_and_undated_reminders(self):
        self.assertEqual(self.parse_reminder("get groceries tomorrow."), ("get groceries", "2026-11-02"))
        self.assertEqual(self.parse_reminder("  collect my parcel TODAY! "), ("collect my parcel", "2026-11-01"))
        self.assertEqual(self.parse_reminder("get groceries"), ("get groceries", ""))
        self.assertEqual(self.parse_reminder("call Tomorrow Ltd"), ("call Tomorrow Ltd", ""))

    def test_failed_save_never_returns_success(self):
        final = CONFIG["automation"]["actions"][-1]
        template = self.env.from_string(final["if"])
        self.assertEqual(template.render(), "False")
        self.assertEqual(template.render(save_result={"saved": False}), "False")
        self.assertEqual(template.render(save_result={"saved": True}), "True")
        self.assertIn("could not confirm", final["else"][0]["set_conversation_response"])

    def test_identical_active_title_and_date_are_deduplicated(self):
        template = self.env.from_string(CONFIG["script"]["sequence"][2]["variables"]["reminder_exists"])
        def exists(items, **values):
            return template.render(existing_reminders={"todo.todo": {"items": items}}, **values) == "True"
        self.assertTrue(exists([{"summary": "Get groceries", "due": "2026-11-02"}], title=" get groceries ", due_date="2026-11-02"))
        self.assertTrue(exists([{"summary": "Get groceries", "due": "2026-11-02T00:00:00+01:00"}], title="Get groceries", due_date="2026-11-02"))
        self.assertTrue(exists([{"summary": "Get groceries"}], title="Get groceries"))
        self.assertFalse(exists([{"summary": "Get groceries", "due": "2026-11-03"}], title="Get groceries", due_date="2026-11-02"))
        self.assertFalse(exists([{"summary": "Get groceries"}], title="Get groceries", due_date="2026-11-02"))
        self.assertFalse(exists([], title="Get groceries", due_date="2026-11-02"))


if __name__ == "__main__":
    unittest.main()
