"""Idempotently register the repository-owned shadow workflow in Windmill."""

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE = "http://windmill-bootstrap.ai.svc.cluster.local:8000/api"
WORKSPACE = "cluster"
PATH = "f/cluster/monitor"


def api(path, data=None, method=None, missing=False):
    request = Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + os.environ["SUPERADMIN_SECRET"],
                               "Content-Type": "application/json"}, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode()
    except HTTPError as exc:
        if missing and exc.code == 404:
            return None
        raise RuntimeError("Windmill bootstrap API failed: " + str(exc.code)) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def main():
    email = os.environ["ADMIN_EMAIL"]
    if not email or email == "admin@windmill.dev" or len(os.environ["ADMIN_PASSWORD"]) < 20:
        raise ValueError("Configure a dedicated administrator with a strong password")
    deadline = time.monotonic() + 600
    while True:
        try:
            api("/version")
            break
        except (OSError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Windmill did not become available within ten minutes") from None
            time.sleep(5)
    if not api("/users/exists/" + quote(email, safe="")):
        api("/users/create", {"email": email, "password": os.environ["ADMIN_PASSWORD"],
                              "super_admin": True, "name": "Lab administrator", "skip_email": True})
    # Disable the documented demo account before the app is made available to users.
    if api("/users/exists/admin%40windmill.dev"):
        api("/users/update/admin%40windmill.dev", {"disabled": True}, "POST")
    prefix = "/w/" + WORKSPACE
    if not api("/workspaces/exists", {"id": WORKSPACE}):
        api("/workspaces/create", {"id": WORKSPACE, "name": "Cluster monitoring", "username": "cluster"})
    users = api(prefix + "/users/list")
    if not any(user["email"] == email for user in users):
        api(prefix + "/workspaces/add_user", {"email": email, "username": "admin", "is_admin": True, "operator": False})
    if api(prefix + "/folders/get/cluster", missing=True) is None:
        api(prefix + "/folders/create", {"name": "cluster", "summary": "Repository-owned shadow monitoring"})
    if api(prefix + "/variables/get/f/cluster/observations", missing=True) is None:
        api(prefix + "/variables/create", {"path": "f/cluster/observations", "value": json.dumps({"version": 1}),
                                          "is_secret": False, "description": "Persistent shadow observations and investigation budget"})
    source = Path(__file__).with_name("monitor.py").read_text()
    digest = hashlib.sha256(source.encode()).hexdigest()
    current = api(prefix + "/scripts/get/p/" + PATH, missing=True)
    if current is None or current.get("content") != source:
        payload = {"path": PATH, "summary": "Proactive cluster monitoring (shadow)", "content": source,
                   "language": "python3", "lock": "", "timeout": 600, "concurrent_limit": 1,
                   "description": "Repository source SHA256: " + digest}
        if current:
            payload["parent_hash"] = current["hash"]
        api(prefix + "/scripts/create", payload)
    flow = {"path": PATH, "summary": "Collect, investigate and trace without external writes",
            "description": "Shadow only. Persistent state is f/cluster/observations.",
            "value": {"modules": [{"id": "monitor", "value": {"type": "script", "path": PATH,
                                                                             "input_transforms": {}}}]},
            "schema": {"type": "object", "properties": {}}}
    current_flow = api(prefix + "/flows/get/" + PATH, missing=True)
    if current_flow is None:
        api(prefix + "/flows/create", flow)
    elif current_flow.get("value") != flow["value"]:
        api(prefix + "/flows/update/" + PATH, flow)
    # Create once: later bootstrap runs must preserve an operator's schedule pause.
    if api(prefix + "/schedules/get/" + PATH, missing=True) is None:
        api(prefix + "/schedules/create", {"path": PATH, "script_path": PATH, "is_flow": True,
                                          "schedule": "0 */15 * * * *", "timezone": "Europe/Amsterdam",
                                          "enabled": True, "args": {}, "no_flow_overlap": True})
    print("Windmill shadow workflow registered; existing schedule state preserved")


if __name__ == "__main__":
    main()
