"""Keep the UI unavailable until the demo account is disabled and the owner exists."""

import json
import os
from urllib.request import Request, urlopen


def ready(users, owner):
    return bool(owner) and any(user["email"] == owner and not user.get("disabled", False) for user in users) and all(
        user.get("disabled", False) for user in users if user["email"] == "admin@windmill.dev"
    )


if __name__ == "__main__":
    request = Request("http://127.0.0.1:8000/api/users/list_as_super_admin?per_page=100",
                      headers={"Authorization": "Bearer " + os.environ["SUPERADMIN_SECRET"]})
    with urlopen(request, timeout=3) as response:
        users = json.load(response)
    raise SystemExit(0 if ready(users, os.environ["ADMIN_EMAIL"]) else 1)
