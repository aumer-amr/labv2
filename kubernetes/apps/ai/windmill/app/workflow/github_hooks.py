# requirements:
# Only the Python standard library is used; do not install packages at runtime.
"""Keep the scoped Cloudflare GitHub webhook exception current and notify Alertmanager."""

import hashlib
import ipaddress
import json
import os
import re
import time
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


CONFIG_PATH = "/etc/github-hooks/config.json"
SUFFIX = "})"


@cache
def config():
    value = json.loads(Path(CONFIG_PATH).read_text())
    required = {"RULESET_URL", "RULE", "STATE", "TOKEN", "TICKET_TOKEN", "MCP", "VIKUNJA_SERVER",
                "ALERTMANAGER", "PREFIX", "PROJECT_NAME", "TRACKING_URL", "VIKUNJA_URL", "JOB_URL", "GITHUB_META_URL"}
    if (not isinstance(value, dict) or not required.issubset(value)
            or not all(isinstance(v, str) and v for v in value.values())):
        raise ValueError("Invalid GitHub webhook configuration")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect)


def http(url, body=None, token=None, method=None, json_response=True):
    headers = {"Content-Type": "application/json", "User-Agent": "GitHub-Hooks-Sync"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers, method=method)
    try:
        with HTTP.open(request, timeout=20) as response:
            data = response.read(1_000_001)
    except HTTPError as exc:
        # Never include upstream response bodies or headers: they may contain credentials.
        with exc:
            code = exc.code
        raise RuntimeError("Upstream HTTP " + str(code)) from None
    if len(data) > 1_000_000:
        raise ValueError("Upstream response exceeded size limit")
    return json.loads(data) if data and json_response else None


def wm(path, body=None, method=None, json_response=True):
    return http(os.environ["BASE_INTERNAL_URL"].rstrip("/") + "/api/w/"
                + os.environ["WM_WORKSPACE"] + path, body, os.environ["WM_TOKEN"], method, json_response)


def save(state):
    wm("/resources/update_value/" + config()["STATE"], {"value": state}, "POST", json_response=False)


def vikunja(name, arguments):
    token = wm("/variables/get_value/" + config()["TICKET_TOKEN"])
    if not isinstance(token, str) or not token:
        raise ValueError("Vikunja gateway key is not configured")
    response = http(config()["MCP"], {"server_id": config()["VIKUNJA_SERVER"], "name": name, "arguments": arguments}, token)
    if response.get("isError"):
        raise ValueError("Vikunja MCP operation failed")
    result = response.get("structuredContent")
    if result is None:
        result = json.loads(next(c["text"] for c in response.get("content", []) if c.get("type") == "text"))
    if not isinstance(result, dict):
        raise ValueError("Invalid Vikunja MCP response")
    return result


def ticket(event):
    if event.get("ticket_id"):
        return
    projects = vikunja("projects_list", {"q": config()["PROJECT_NAME"], "format": "markdown"})
    matches = [p for p in projects["items"] if p["title"] == config()["PROJECT_NAME"] and not p.get("is_archived")]
    if len(matches) != 1 or projects.get("total_pages", 1) > 1:
        raise ValueError("Expected exactly one configured active project")
    project = matches[0]
    title = "GitHub webhook ranges changed [" + event["id"] + "]"
    tasks = vikunja("tasks_list", {"q": event["id"], "filter": "project = " + str(project["id"]),
                                  "format": "markdown"})
    matches = [t for t in tasks["items"] if t["project_id"] == project["id"] and t["title"] == title]
    if len(matches) > 1 or tasks.get("total_pages", 1) > 1:
        raise ValueError("Ambiguous change-ticket lookup; refusing duplicate creation")
    if matches:
        result = matches[0]
    else:
        added = sorted(set(event["new"]) - set(event["old"]))
        removed = sorted(set(event["old"]) - set(event["new"]))
        payload = {"project": project["id"], "title": title, "done": False, "format": "markdown",
                   "description": "GitHub webhook IP ranges changed at " + stamp(event["at"]) + ".\n\n"
                   + "Added: " + (", ".join(added) or "none") + "\n\nRemoved: " + (", ".join(removed) or "none")
                   + "\n\nCloudflare's Konflate webhook exception was synchronized and read back successfully. "
                   "The exact POST /hooks scope, webhook signature verification, and US country block remain in place. "
                   "Review fresh GitHub webhook deliveries.\n\nSource: " + config()["GITHUB_META_URL"] + "\n\n"
                   + "Cloudflare rule: `" + config()["RULE"] + "`\n\nTracking: " + config()["TRACKING_URL"]}
        board = next((v for v in project.get("views", []) if v.get("view_kind") == "kanban"), {})
        if board.get("default_bucket_id"):
            payload["bucket_id"] = board["default_bucket_id"]
        result = vikunja("tasks_create", payload)
    if not isinstance(result.get("id"), int) or result["id"] <= 0 or result.get("project_id") != project["id"]:
        raise ValueError("Vikunja returned an invalid change ticket")
    event["ticket_id"] = result["id"]


def networks(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("Expected a bounded, nonempty hooks CIDR list")
    result = set()
    for value in values:
        if not isinstance(value, str) or "/" not in value:
            raise ValueError("Every hook range must be a CIDR")
        network = ipaddress.ip_network(value, strict=True)
        if (network.prefixlen < (8 if network.version == 4 else 16)
                or not network.network_address.is_global or not network.broadcast_address.is_global):
            raise ValueError("Refusing a broad or non-public hook range")
        result.add(str(network))
    if not any(":" not in value for value in result) or not any(":" in value for value in result):
        raise ValueError("Hook ranges must include both IPv4 and IPv6")
    return sorted(result)


def rule_ranges(rule):
    if (rule.get("action") != "skip" or rule.get("action_parameters") != {"ruleset": "current"}
            or rule.get("enabled") is not True or rule.get("logging") != {"enabled": True}):
        raise ValueError("Cloudflare webhook exception protection settings have drifted")
    expression = rule.get("expression", "")
    match = re.fullmatch(re.escape(config()["PREFIX"]) + r"([^{}]+)" + re.escape(SUFFIX), expression)
    if not match:
        raise ValueError("Cloudflare webhook exception scope has drifted")
    return networks(match[1].split())


def ruleset(token):
    response = http(config()["RULESET_URL"], token=token)
    if response.get("success") is not True:
        raise ValueError("Cloudflare ruleset read failed")
    result = response["result"]
    if result.get("phase") != "http_request_firewall_custom" or result.get("kind") != "zone":
        raise ValueError("Unexpected Cloudflare ruleset")
    return result["rules"]


def target(rules):
    matches = [rule for rule in rules if rule["id"] == config()["RULE"]]
    if len(matches) != 1:
        raise ValueError("Expected exactly one existing GitHub webhook exception")
    return matches[0]


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def alert(name, description, now, event=None, resolved=False):
    labels = {"alertname": name, "severity": "warning", "namespace": "ai", "job": "github-hooks-sync"}
    if event:
        labels["change"] = event["id"]
    http(config()["ALERTMANAGER"], [{
        "labels": labels,
        "annotations": {"summary": name, "description": description},
        "startsAt": stamp(event["at"] if event else now),
        "endsAt": stamp(now if resolved else event["until"] if event else now + 86400),
        "generatorURL": config()["JOB_URL"],
    }])


def synchronize(state, now):
    desired = networks(http(config()["GITHUB_META_URL"])["hooks"])
    token = wm("/variables/get_value/" + config()["TOKEN"])
    if not isinstance(token, str) or not token:
        raise ValueError("Cloudflare API token is not configured")
    before = ruleset(token)
    actual = rule_ranges(target(before))
    previous = networks(state.get("hooks", actual))
    if actual != desired or previous != desired:
        pending = state.get("pending")
        if not pending or pending["new"] != desired:
            state["pending"] = {"at": now, "old": previous, "new": desired,
                                "id": hashlib.sha256(json.dumps([now, previous, desired]).encode()).hexdigest()[:16]}
        # Persist notification intent before the external write, so retries cannot lose a change.
        save(state)
        if actual != desired:
            expression = config()["PREFIX"] + " ".join(desired) + SUFFIX
            if len(expression) > 4000:
                raise ValueError("GitHub ranges exceed Cloudflare expression limit")
            response = http(config()["RULESET_URL"] + "/rules/" + config()["RULE"], {"expression": expression}, token, "PATCH")
            if response.get("success") is not True:
                raise ValueError("Cloudflare rule update failed")
        after = ruleset(token)
        if rule_ranges(target(after)) != desired:
            raise ValueError("Cloudflare rule readback did not match GitHub")
        fields = ("id", "expression", "action", "action_parameters", "logging", "enabled", "description", "ref")
        def preserved(rules):
            return [{key: value for key, value in rule.items() if key in fields and not (rule["id"] == config()["RULE"] and key == "expression")}
                    for rule in rules]
        if preserved(before) != preserved(after):
            raise ValueError("Cloudflare rule order or other security settings changed during sync")
    if pending := state.get("pending"):
        if pending.get("until", 0) <= now:
            pending["until"] = now + 7200
    state["hooks"] = desired
    state["last_success"] = now
    save(state)
    event = state.get("pending") or state.get("last_change")
    if event and now < event["until"] + 300:
        added = sorted(set(event["new"]) - set(event["old"]))
        removed = sorted(set(event["old"]) - set(event["new"]))
        ticket_failed = False
        try:
            ticket(event)
            save(state)
        except Exception:
            ticket_failed = True
        ticket_link = " Ticket: " + config()["VIKUNJA_URL"] + "/tasks/" + str(event["ticket_id"]) if event.get("ticket_id") else " Ticket creation pending."
        alert("GitHubWebhookRangesChanged", "Cloudflare GitHub webhook exception synchronized. Added: "
              + (", ".join(added) or "none") + ". Removed: " + (", ".join(removed) or "none")
              + ". US country block and webhook scope preserved." + ticket_link, now, event)
        if ticket_failed:
            raise RuntimeError("Change notification sent; Vikunja ticket creation remains pending")
        state["last_change"] = event
        state.pop("pending", None)
        save(state)
    elif event:
        state.pop("last_change", None)
        save(state)
    if state.pop("failed_at", None) is not None:
        alert("GitHubWebhookSyncFailed", "GitHub webhook-range synchronization recovered.", now, resolved=True)
        save(state)
    return {"ranges": len(desired), "synchronized": True}


def main():
    now = int(time.time())
    state = None
    try:
        state = wm("/resources/get_value/" + config()["STATE"])
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("Unsupported GitHub webhook sync state")
        return synchronize(state, now)
    except Exception:
        # Alert content deliberately excludes exceptions, API responses, and secret values.
        if isinstance(state, dict) and state.get("version") == 1:
            state.setdefault("failed_at", now)
            try:
                save(state)
            except Exception:
                pass
        try:
            alert("GitHubWebhookSyncFailed", "GitHub webhook-range synchronization failed. Review the Windmill job; "
                  "the US country block was not intentionally modified.", now)
        finally:
            raise RuntimeError("GitHub webhook sync failed; inspect upstream availability and scoped configuration") from None
