import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("mirror", ROOT / "scripts/forgejo-mirror-check.py")
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, tzinfo=timezone.utc)
        self.repository = {
            "mirror": True,
            "original_url": "https://github.com/aumer-amr/labv2.git",
            "private": True,
            "has_actions": True,
            "default_branch": "main",
            "mirror_updated": self.now.isoformat(),
        }

    def test_recent_success_and_clock_skew(self):
        for seconds in (-60, 0, 1800):
            with self.subTest(seconds=seconds):
                self.repository["mirror_updated"] = (self.now - timedelta(seconds=seconds)).isoformat()
                mirror.validate(self.repository, self.now)

    def test_stale_future_missing_and_naive_timestamps_fail(self):
        for value in (None, "bad", "2026-11-17T00:00:00", "0001-01-01T00:00:00Z",
                      (self.now - timedelta(seconds=1801)).isoformat(),
                      (self.now + timedelta(seconds=61)).isoformat()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mirror.validate(dict(self.repository, mirror_updated=value), self.now)
        del self.repository["mirror_updated"]
        with self.assertRaises(ValueError):
            mirror.validate(self.repository, self.now)

    def test_wrong_source_visibility_mode_or_branch_fails(self):
        for key, value in (("original_url", "https://example.com/repo.git"),
                           ("private", False), ("mirror", False), ("has_actions", False),
                           ("default_branch", "other")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                mirror.validate(dict(self.repository, **{key: value}), self.now)

    def test_redirect_never_forwards_token(self):
        with self.assertRaises(ValueError):
            mirror.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com")


if __name__ == "__main__":
    unittest.main()
