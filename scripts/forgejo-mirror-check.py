#!/usr/bin/env python3
"""Check the LabV2 pull mirror without changing it or logging credentials."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


def validate(repository, now):
    if not isinstance(repository, dict):
        raise ValueError("The mirror API response is invalid")
    if repository.get("mirror") is not True:
        raise ValueError("LabV2 is not a pull mirror")
    if repository.get("original_url") != "https://github.com/aumer-amr/labv2.git":
        raise ValueError("The mirror source does not match LabV2")
    if repository.get("private") is not True:
        raise ValueError("The mirror is not private")
    if repository.get("has_actions") is not True:
        raise ValueError("Forgejo Actions are disabled")
    if repository.get("default_branch") != "main":
        raise ValueError("The mirror default branch is not main")
    try:
        timestamp = repository["mirror_updated"]
        if not isinstance(timestamp, str):
            raise ValueError("Invalid timestamp")
        updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = (now - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise ValueError("The mirror update timestamp is invalid") from None
    if not -60 <= age <= 1800:
        raise ValueError("The mirror has no successful update within 30 minutes")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("The mirror API returned a redirect")


def main():
    token = Path("/credentials/token").read_text().strip()
    if not token:
        raise ValueError("The mirror monitor token is empty")
    request = urllib.request.Request(
        "http://forgejo-http.developer-tools.svc.cluster.local:3000/api/v1/repos/"
        + os.environ["FORGEJO_REPOSITORY"],
        headers={"Authorization": "token " + token, "Accept": "application/json"},
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        repository = json.load(response)
    validate(repository, datetime.now(timezone.utc))
    print("LabV2 mirror is private, enabled, and synchronized within 30 minutes")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        # API response bodies and request headers can contain credentials.
        print("LabV2 mirror check failed. Inspect synchronization and monitor credentials.", file=sys.stderr)
        sys.exit(1)
