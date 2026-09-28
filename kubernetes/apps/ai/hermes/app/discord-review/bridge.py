"""Discord review cards and human-authorized, SHA-bound GitHub merges."""

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = "aumer-amr/labv2"
WORKFLOW = ".github/workflows/ai-pr-review.yaml"
MARKER = re.compile(r"<!--\s*ai-pr-reviewer:\s*(?=\{)")
SHA = re.compile(r"[0-9a-f]{40}")
LOG = logging.getLogger("discord-review")


class Blocked(Exception):
    """A safe, user-facing reason to refuse a merge."""


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, method="GET", data=None):
        req = Request(
            f"https://api.github.com/repos/{REPO}/{path}",
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            raise Blocked(f"GitHub refused the request (HTTP {exc.code}).") from None

    def pages(self, path, key=None):
        result = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            data = self.request(f"{path}{separator}per_page=100&page={page}")
            items = data[key] if key else data
            result.extend(items)
            if len(items) < 100:
                return result
        raise Blocked("Too many results to verify safely.")


def review_metadata(comment):
    if (
        comment.get("user", {}).get("id") != 41898282
        or (comment.get("performed_via_github_app") or {}).get("id") != 15368
    ):
        return None
    body = comment.get("body", "")
    match = MARKER.search(body)
    if not match:
        return None
    try:
        data, end = json.JSONDecoder().raw_decode(body, match.end())
    except ValueError:
        return None
    if not isinstance(data, dict) or not body[end:].lstrip().startswith("-->"):
        return None
    if data.get("version") != 1:
        return None
    for name in ("head_sha", "base_sha"):
        if not isinstance(data.get(name), str) or not SHA.fullmatch(data[name]):
            return None
    data["approved"] = (
        data.get("review_result") == "clean"
        and "**Automated recommendation: APPROVE**" in body
    )
    return data


def snapshot(api, number, on_merge=None):
    pr = api.request(f"pulls/{number}")
    state = {
        "number": number, "title": pr["title"], "head": pr["head"]["sha"],
        "base": pr["base"]["sha"], "comment": None, "recommendation": "Awaiting review",
        "eligible": False, "reason": "Awaiting a review of the current commit.",
    }
    if pr["state"] != "open":
        state["reason"] = "Merged." if pr.get("merged") else "Closed."
        if pr.get("merged") and pr["base"]["ref"] == "main" and on_merge:
            state.update(on_merge(pr))
        return state
    comments = api.pages(f"issues/{number}/comments")
    # The latest authentic reviewer comment wins, including a newer negative verdict.
    reviews = [(c, review_metadata(c)) for c in comments]
    reviews = [(c, m) for c, m in reviews if m]
    if not reviews:
        return state
    comment, metadata = max(reviews, key=lambda pair: (pair[0]["updated_at"], pair[0]["id"]))
    state["comment"] = comment["id"]
    state["recommendation"] = "Awaiting fresh review"
    if metadata["head_sha"] != state["head"]:
        state["reason"] = "The PR branch commit changed since this review (including a rebase); a fresh review is required."
        return state
    if metadata["base_sha"] != state["base"]:
        state["reason"] = "The target branch advanced since this review; a fresh review against the current base is required."
        return state
    state["recommendation"] = "Approve" if metadata["approved"] else "Needs attention"
    if not metadata["approved"]:
        state["reason"] = "The AI has not recommended approval."
        return state
    if (
        pr["draft"] or pr["base"]["ref"] != "main"
        or (pr["head"].get("repo") or {}).get("full_name") != REPO
        or pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean"
    ):
        state["reason"] = "GitHub has not marked this same-repository PR ready to merge."
        return state
    checks = api.pages(f"commits/{state['head']}/check-runs?filter=latest", "check_runs")
    statuses = api.pages(f"commits/{state['head']}/statuses")
    latest_statuses = {}
    for status in statuses:  # GitHub returns newest first.
        latest_statuses.setdefault(status["context"], status)
    required = {"review", "Talos Compatibility", "Flate - Success"}
    passed = {c["name"] for c in checks if c["conclusion"] == "success" and c["app"]["id"] == 15368}
    if (
        not required <= passed
        or any(c["status"] != "completed" or c["conclusion"] not in {"success", "skipped", "neutral"} for c in checks)
        or any(s["state"] != "success" for s in latest_statuses.values())
    ):
        state["reason"] = "Waiting for all checks to finish successfully."
        return state
    review_check = next(c for c in checks if c["name"] == "review" and c["app"]["id"] == 15368)
    run_match = re.fullmatch(
        rf"https://github\.com/{re.escape(REPO)}/actions/runs/(\d+)/job/\d+", review_check.get("details_url", "")
    )
    if not run_match:
        raise Blocked("Cannot verify the review workflow's provenance.")
    run = api.request(f"actions/runs/{run_match[1]}")
    if not (
        run["path"] == WORKFLOW and run["event"] == "pull_request_target"
        and run["head_sha"] == state["head"] and run["status"] == "completed"
        and run["conclusion"] == "success"
    ):
        state["reason"] = "The matching review workflow has not succeeded."
        return state
    latest_reviews = {}
    for review in api.pages(f"pulls/{number}/reviews"):
        if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest_reviews[review["user"]["id"]] = review["state"]
    if "CHANGES_REQUESTED" in latest_reviews.values():
        state["reason"] = "A reviewer has requested changes."
        return state
    state.update(eligible=True, reason="Checks passed. Your approval will merge this commit.")
    return state


def merge(api, expected):
    current = snapshot(api, expected["number"])
    if not current["eligible"]:
        raise Blocked(current["reason"])
    if any(current[k] != expected[k] for k in ("head", "base", "comment")):
        raise Blocked("This button is stale. Wait for the updated review card.")
    result = api.request(f"pulls/{current['number']}/merge", "PUT", {
        "sha": current["head"], "merge_method": "merge",
    })
    if result.get("merged") is not True:
        raise Blocked("GitHub did not merge the PR. Refresh its status before retrying.")
    commented = False
    try:
        api.request(f"issues/{current['number']}/comments", "POST", {"body": "/merge"})
        commented = True
    except Exception as exc:
        LOG.warning("PR %s merged, but /merge comment failed: %s", current["number"], type(exc).__name__)
    return result["sha"], commented


def user_ids(value):
    parts = [p for p in re.split(r"[,;\s]+", value) if p]
    if not parts or not all(p.isdecimal() for p in parts):
        raise ValueError("DISCORD_ALLOWED_USERS must contain explicit numeric user IDs")
    return {int(p) for p in parts}


def authorized_card(interaction, cards, allowed, channel_id, bot_id):
    if interaction.user.id not in allowed or interaction.channel_id != channel_id:
        raise Blocked("You are not authorized to merge PRs here.")
    token = (interaction.data or {}).get("custom_id", "").removeprefix("prmerge:")
    card = next((c for c in cards.values() if c["token"] == token), None)
    if (
        not card or not interaction.message or card["message"] != interaction.message.id
        or interaction.message.author.id != bot_id or not card["state"]["eligible"]
    ):
        raise Blocked("This button is stale or unavailable. Wait for the updated card.")
    return card


def main():
    # discord.py ships in the pinned Hermes image; no extra package or bot is needed.
    import discord
    import importlib.util
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    # -I omits the script directory from sys.path; load only the mounted trusted module.
    spec = importlib.util.spec_from_file_location("rollout", Path(__file__).with_name("rollout.py"))
    rollout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rollout)
    kube = rollout.Kubernetes()

    api = GitHub(os.environ["GH_TOKEN"])
    allowed = user_ids(os.environ["DISCORD_ALLOWED_USERS"])
    channel_id = int(os.environ["DISCORD_REVIEWS_CHANNEL"])
    db = sqlite3.connect("/var/lib/hermes-broker/discord-reviews.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS cards (number INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    cards = {number: json.loads(data) for number, data in db.execute("SELECT number, data FROM cards")}
    lock = asyncio.Lock()

    def refresh(number):
        previous = cards.get(number, {}).get("state", {}).get("rollout")
        return snapshot(api, number, lambda pr: rollout.feedback(api, kube, pr, previous))

    def save(number, card):
        db.execute("INSERT OR REPLACE INTO cards VALUES (?, ?)", (number, json.dumps(card)))
        db.commit()
        cards[number] = card

    class Client(discord.Client):
        async def setup_hook(self):
            self.poll_task = asyncio.create_task(self.poll())

        async def on_ready(self):
            LOG.info("Discord review handler connected; %s stored cards", len(cards))

        async def show(self, state):
            number = state["number"]
            previous = cards.get(number)
            if previous and state["reason"] in {"Merged.", "Closed."} and "rollout" not in state:
                state = {**previous["state"], "eligible": False, "reason": state["reason"]}
            token = secrets.token_urlsafe(18)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="View PR", url=f"https://github.com/{REPO}/pull/{number}"))
            if state["comment"]:
                view.add_item(discord.ui.Button(label="Read review", url=f"https://github.com/{REPO}/pull/{number}#issuecomment-{state['comment']}"))
            view.add_item(discord.ui.Button(
                label="Approve & merge", style=discord.ButtonStyle.success,
                custom_id=f"prmerge:{token}", disabled=not state["eligible"],
            ))
            embed = discord.Embed(
                title=f"#{number} {state['title']}"[:256],
                url=f"https://github.com/{REPO}/pull/{number}",
                description=("" if "rollout" in state else f"AI recommendation: **{state['recommendation']}**\n\n")
                + f"{state['reason']}\n\nCommit: `{state['head']}`",
                color=0x2ECC71 if state["eligible"] else 0xF1C40F,
            )
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            message = None
            if previous:
                try:
                    message = await channel.fetch_message(previous["message"])
                    if previous["state"] == state:
                        return
                    await message.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    message = None
            if message is None:
                message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            save(number, {"token": token, "message": message.id, "state": state})
            LOG.info("PR %s card updated; merge enabled=%s", number, state["eligible"])

        async def poll(self):
            await self.wait_until_ready()
            while not self.is_closed():
                try:
                    async with lock:
                        opened = await asyncio.to_thread(api.pages, "pulls?state=open")
                        # ponytail: latest 100 closures; paginate if this repo exceeds 100 per day.
                        closed = await asyncio.to_thread(api.request, "pulls?state=closed&sort=updated&direction=desc&per_page=100")
                        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                        merged = {p["number"] for p in closed if p.get("merged_at")
                                  and p["base"]["ref"] == "main"
                                  and datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00")) >= cutoff
                                  and not cards.get(p["number"], {}).get("state", {}).get("rollout_done")}
                        numbers = {p["number"] for p in opened} | {
                            n for n, c in cards.items() if c["state"]["reason"] not in {"Merged.", "Closed."}
                            and not c["state"].get("rollout_done")
                        } | merged
                        for number in sorted(numbers):
                            try:
                                state = await asyncio.to_thread(refresh, number)
                                if state["comment"] or number in cards or "rollout" in state:
                                    await self.show(state)
                            except Exception as exc:
                                LOG.warning("PR %s refresh failed: %s", number, type(exc).__name__)
                except Exception as exc:
                    LOG.warning("Review polling failed: %s", type(exc).__name__)
                await asyncio.sleep(60)

        async def on_interaction(self, interaction):
            custom_id = (interaction.data or {}).get("custom_id", "")
            if not custom_id.startswith("prmerge:"):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with lock:
                try:
                    match = authorized_card(interaction, cards, allowed, channel_id, self.user.id)
                except Blocked as exc:
                    await interaction.followup.send(str(exc), ephemeral=True)
                    return
                number = match["state"]["number"]
                try:
                    merged_sha, commented = await asyncio.to_thread(merge, api, match["state"])
                    LOG.info("PR %s merged by Discord user %s at %s", number, interaction.user.id, merged_sha)
                    notice = "" if commented else " GitHub rejected or could not confirm the `/merge` comment; check the PR."
                    await interaction.followup.send(f"Merged PR #{number} at `{merged_sha[:12]}`.{notice}", ephemeral=True)
                    await self.show(await asyncio.to_thread(refresh, number))
                except Blocked as exc:
                    await interaction.followup.send(f"Not merged: {exc}", ephemeral=True)
                except Exception as exc:
                    LOG.warning("PR %s merge outcome requires checking: %s", number, type(exc).__name__)
                    await interaction.followup.send("Could not confirm the result. Check GitHub before retrying.", ephemeral=True)

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("discord").setLevel(logging.WARNING)
    Client(intents=discord.Intents.none()).run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
