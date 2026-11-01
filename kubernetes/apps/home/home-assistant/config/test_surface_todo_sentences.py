import json
from pathlib import Path
import unittest

from hassil import Intents, recognize


CONFIG = json.loads(Path(__file__).with_name("surface_todo_voice.json").read_text())


class ReminderSentenceTests(unittest.TestCase):
    def setUp(self):
        self.intents = Intents.from_dict({
            "language": "en",
            "intents": {"SaveReminder": {"data": [{
                "sentences": CONFIG["automation"]["triggers"][0]["command"],
            }]}},
            "lists": {"reminder": {"wildcard": True}},
        })

    def test_reminder_wording_captures_only_the_task(self):
        for prefix in ("I need to", "Remind me to", "Remind me of", "Remind me about", "Remember to"):
            with self.subTest(prefix=prefix):
                match = recognize(f"{prefix} get groceries tomorrow", self.intents)
                self.assertIsNotNone(match)
                self.assertEqual(match.entities["reminder"].value, "get groceries tomorrow")

    def test_device_commands_do_not_match(self):
        for sentence in ("turn on the lights", "open the garage", "what is the temperature"):
            with self.subTest(sentence=sentence):
                self.assertIsNone(recognize(sentence, self.intents))


if __name__ == "__main__":
    unittest.main()
