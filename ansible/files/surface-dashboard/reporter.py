#!/usr/bin/env python3
"""Report physical DPMS state to HA; never control the display or read browser storage."""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


CONFIG = Path.home() / ".config/surface-dashboard/config.json"
TOKEN = Path.home() / ".config/surface-dashboard/ha-token"


class Reporter:
    def __init__(self):
        self.previous = None
        self.wake = 0
        self.sent_wake = 0
        self.sent_display = None
        self.next_attempt = 0
        self.last_success = float("-inf")

    def observe(self, display_on, wall_time, allow_wake=True):
        # The first observation is a baseline, including after service restart.
        if self.previous is False and display_on and allow_wake:
            self.wake = int(wall_time)
        self.previous = display_on

    def payload(self, wall_time, monotonic_time):
        fresh_wake = (self.wake != self.sent_wake and 0 <= wall_time - self.wake < 15)
        changed = self.previous != self.sent_display
        if monotonic_time < self.next_attempt or not (changed or fresh_wake or
                                                     monotonic_time - self.last_success >= 30):
            return None
        return {"display_on": self.previous, "wake_at": self.wake if fresh_wake else 0}

    def acknowledge(self, payload, monotonic_time):
        self.sent_display = payload["display_on"]
        self.sent_wake = self.wake
        self.last_success = monotonic_time
        self.next_attempt = monotonic_time

    def failed(self, monotonic_time):
        self.next_attempt = monotonic_time + 5


def endpoint(config):
    url = urlsplit(config["dashboard_url"])
    if (url.scheme != "https" or not url.hostname or url.username is not None or
            url.password is not None or url.query or url.fragment):
        raise ValueError("Reporter requires the credential-free HTTPS dashboard URL")
    return f"{url.scheme}://{url.netloc}/api/services/script/surface_report_display"


def token(path=TOKEN):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("HA token must be an owner-only regular file")
    value = path.read_text().strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError("HA token file is empty or malformed")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def report(url, payload):
    request = Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + token(), "Content-Type": "application/json",
    })
    with build_opener(NoRedirect()).open(request, timeout=5) as response:
        response.read()


def controllers_settled(monotonic_time):
    # Firefox startup can reset X11 input idle and wake DPMS without a person.
    try:
        result = subprocess.run([
            "systemctl", "--user", "show", "surface-dashboard-browser.service",
            "surface-dashboard-display.service", "--property=ActiveEnterTimestampMonotonic", "--value",
        ], check=True, capture_output=True, text=True, timeout=2)
        started = [int(line) / 1_000_000 for line in result.stdout.splitlines() if line.strip()]
        return len(started) == 2 and all(value > 0 and monotonic_time - value >= 15 for value in started)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def main():
    url = endpoint(json.loads(CONFIG.read_text()))
    reporter = Reporter()
    failed = False
    while True:
        try:
            result = subprocess.run(["xset", "q"], check=True, capture_output=True,
                                    text=True, timeout=2)
            match = re.search(r"Monitor is (On|Off|Standby|Suspend)", result.stdout)
            if not match:
                raise RuntimeError("DPMS state unavailable")
            display_on = match.group(1) == "On"
            allowed = True
            if reporter.previous is False and display_on:
                allowed = controllers_settled(time.monotonic())
            reporter.observe(display_on, time.time(), allow_wake=allowed)
            payload = reporter.payload(time.time(), time.monotonic())
            if payload is not None:
                report(url, payload)
                reporter.acknowledge(payload, time.monotonic())
                if failed:
                    print("Home Assistant reporting recovered", flush=True)
                failed = False
        except Exception:
            # Neither HTTP responses, credentials nor exception details enter the journal.
            reporter.failed(time.monotonic())
            if not failed:
                print("Home Assistant reporting unavailable; display control is unaffected", flush=True)
            failed = True
        time.sleep(0.5)


if __name__ == "__main__":
    main()
