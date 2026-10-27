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
API_TOKEN = None


def api(path, data=None, method=None, missing=False):
    request = Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + (API_TOKEN or os.environ["SUPERADMIN_SECRET"]),
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


def github_hooks(prefix):
    path = "f/cluster/github-hooks-sync"
    state = "f/cluster/github-hooks-state"
    token_path = "f/cluster/github-hooks-token"
    source = Path(__file__).with_name("github_hooks.py").read_text()
    current = api(prefix + "/scripts/get/p/" + path, missing=True)
    if current is None or current.get("content") != source:
        payload = {"path": path, "summary": "Synchronize GitHub webhook ranges with Cloudflare",
                   "content": source, "language": "python3", "lock": "", "timeout": 240,
                   "concurrent_limit": 1, "description": "Repository-owned; alerts through Alertmanager"}
        if current:
            payload["parent_hash"] = current["hash"]
        api(prefix + "/scripts/create", payload)
    if api(prefix + "/resources/get_value/" + state, missing=True) is None:
        api(prefix + "/resources/create", {"path": state, "resource_type": "state",
                                          "value": {"version": 1}, "description": "Webhook range baseline and pending notifications"})
    tokens = {token_path: os.environ.get("CLOUDFLARE_WAF_API_TOKEN"),
              "f/cluster/github-hooks-litellm-key": os.environ.get("GITHUB_HOOKS_LITELLM_KEY")}
    # Missing optional credentials must not activate a broken schedule or affect cluster monitoring.
    if not all(tokens.values()):
        return
    for variable_path, token in tokens.items():
        current_token = api(prefix + "/variables/get_value/" + variable_path, missing=True)
        if current_token is None:
            api(prefix + "/variables/create", {"path": variable_path, "value": token, "is_secret": True,
                                              "description": "GitHub webhook sync credential; repository-owned"})
        elif current_token != token:
            api(prefix + "/variables/update/" + variable_path, {"value": token, "is_secret": True}, "POST")
    if api(prefix + "/schedules/get/" + path, missing=True) is None:
        api(prefix + "/schedules/create", {"path": path, "script_path": path, "is_flow": False,
                                          "schedule": "0 17 * * * *", "timezone": "Europe/Amsterdam",
                                          "enabled": True, "args": {}})


def main():
    global API_TOKEN
    API_TOKEN = None
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
    # Schedules must run as a real user, not the reserved internal superadmin identity.
    API_TOKEN = api("/auth/login", {"email": email, "password": os.environ["ADMIN_PASSWORD"]})
    if not isinstance(API_TOKEN, str) or not API_TOKEN:
        raise ValueError("Administrator login did not return a session token")
    prefix = "/w/" + WORKSPACE
    if not api("/workspaces/exists", {"id": WORKSPACE}):
        api("/workspaces/create", {"id": WORKSPACE, "name": "Cluster monitoring"})
    users = api(prefix + "/users/list")
    if not any(user["email"] == email for user in users):
        api(prefix + "/workspaces/add_user", {"email": email, "is_admin": True, "operator": False})
    owner = "u/" + api(prefix + "/users/whoami")["username"]
    folder = api(prefix + "/folders/get/cluster", missing=True)
    if folder is None:
        api(prefix + "/folders/create", {"name": "cluster", "summary": "Repository-owned shadow monitoring",
                                         "owners": [owner], "extra_perms": {owner: True}})
    elif owner not in folder.get("owners", []) or not folder.get("extra_perms", {}).get(owner):
        api(prefix + "/folders/update/cluster", {"owners": sorted(set(folder.get("owners", [])) | {owner}),
                                                 "extra_perms": {**folder.get("extra_perms", {}), owner: True}})
    if api(prefix + "/resources/get_value/f/cluster/monitor-state", missing=True) is None:
        legacy = api(prefix + "/variables/get_value/f/cluster/observations", missing=True)
        state = json.loads(legacy) if legacy is not None else {"version": 1}
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Cannot migrate unknown observation state")
        api(prefix + "/resources/create", {"path": "f/cluster/monitor-state", "resource_type": "state", "value": state,
                                          "description": "Persistent shadow observations and investigation budget"})
    if api(prefix + "/resources/get_value/f/cluster/monitor-publication", missing=True) is None:
        api(prefix + "/resources/create", {
            "path": "f/cluster/monitor-publication", "resource_type": "state",
            "value": {"mode": "shadow", "paused": True, "reviewer": owner.removeprefix("u/")},
            "description": "Select shadow or approval_required; paused stops publication without stopping collection",
        })
    source = Path(__file__).with_name("monitor.py").read_text()
    digest = hashlib.sha256(source.encode()).hexdigest()
    current = api(prefix + "/scripts/get/p/" + PATH, missing=True)
    if current is None or current.get("content") != source:
        payload = {"path": PATH, "summary": "Cluster monitoring and approved Vikunja publication", "content": source,
                   "language": "python3", "lock": "", "timeout": 600, "concurrent_limit": 1,
                   "description": "Repository source SHA256: " + digest,
                   "schema": {"type": "object", "properties": {
                       "action": {"type": "string", "enum": ["collect", "drafts", "approve", "reject"], "default": "collect"},
                       "draft_id": {"type": "string", "default": ""},
                       "draft_hash": {"type": "string", "default": ""},
                       "approve": {"type": "boolean", "default": False,
                                   "description": "I reviewed this exact draft and approve publishing it to LabV2"},
                   }}}
        if current:
            payload["parent_hash"] = current["hash"]
        api(prefix + "/scripts/create", payload)
    flow = {"path": PATH, "summary": "Collect, investigate and trace without external writes",
            "description": "Shadow only. Persistent state is f/cluster/monitor-state.",
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
    github_hooks(prefix)
    print("Windmill shadow workflow registered; existing schedule state preserved")


if __name__ == "__main__":
    main()
