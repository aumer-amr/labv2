#!/usr/bin/env python3
"""Publish a human-approved Paseo commit and merge its checked LabV2 pull request."""

import json
import os
import re
import subprocess
import sys

REPO = "aumer-amr/labv2"
REMOTE = f"https://github.com/{REPO}.git"
ACTIONS = 15368
REQUIRED = {"Talos Compatibility", "Flate - Success", "PR Hygiene"}


class Pending(Exception):
    """Ask Playbooks to poll again."""


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def api(path, *args):
    return json.loads(run("gh", "api", "--hostname", "github.com", f"repos/{REPO}/{path}", *args))


def validate_pr(pr, branch, sha):
    if (pr["base"]["repo"]["full_name"], pr["base"]["ref"]) != (REPO, "main"):
        raise ValueError("The PR target changed.")
    head = pr["head"]
    if (head["repo"]["full_name"], head["ref"], head["sha"]) != (REPO, branch, sha):
        raise ValueError("The PR source changed. Review and approve the new revision.")
    if pr["draft"] or (pr["state"] != "open" and not pr.get("merged_at")):
        raise ValueError("The PR is a draft or was closed without merging.")


def open_pr(card, branch, sha):
    existing = api(f"pulls?state=all&head=aumer-amr:{branch}&per_page=2")
    if len(existing) > 1:
        raise ValueError("More than one PR uses the run branch.")
    if existing:
        pr = existing[0]
    else:
        # Use the same GitHub identity for the push and the PR. Keep configured Git hooks.
        git = ("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")
        ref = f"refs/heads/{branch}"
        remote_ref = run(*git, "ls-remote", REMOTE, ref)
        if remote_ref and remote_ref.split()[0] != sha:
            raise ValueError("The remote run branch points to a different commit.")
        if not remote_ref:
            run(*git, "push", "--no-follow-tags", REMOTE, f"{sha}:{ref}")
        title = run("git", "show", "-s", "--format=%s", sha)
        body = (f"## Summary\n\n{title}\n\n## Evidence\n\n"
                f"Astra reviewed `{sha}` for Paseo task `{card}`. The operator approved this revision.\n\n"
                "## Merge\n\nPaseo waits for the required GitHub checks before merging into `main`.")
        pr = api("pulls", "--method", "POST", "-f", "base=main", "-f", f"head={branch}",
                 "-f", f"title={title}", "-f", f"body={body}")
    validate_pr(pr, branch, sha)
    return {"pr_number": str(pr["number"]), "pr_url": pr["html_url"]}


def check_results(sha):
    rules = api("rules/branches/main")
    required = {(name, ACTIONS) for name in REQUIRED}
    for rule in rules:
        if rule["type"] == "required_status_checks":
            for check in rule["parameters"]["required_status_checks"]:
                required.add((check["context"], check.get("integration_id")))
    pages = api(f"commits/{sha}/check-runs?filter=latest&per_page=100", "--paginate", "--slurp")
    runs = [r for page in pages for r in page["check_runs"] if r["head_sha"] == sha]
    waiting = set()
    for name, app in required:
        matches = [r for r in runs if r["name"] == name and (app in (None, -1) or r["app"]["id"] == app)]
        for r in matches:
            if r["status"] == "completed" and r["conclusion"] != "success":
                raise ValueError(f"{name} finished with {r['conclusion']}.")
        if not matches or any(r["status"] != "completed" for r in matches):
            waiting.add(name)
    if waiting:
        raise Pending("Waiting for " + ", ".join(sorted(waiting)))


def merge_pr(number, branch, sha):
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise ValueError("Invalid pull request number.")
    path = f"pulls/{number}"
    pr = api(path)
    validate_pr(pr, branch, sha)
    check_results(sha)
    if not pr.get("merged_at"):
        pr = api(path)
        validate_pr(pr, branch, sha)
    if pr.get("merged_at"):
        return {"merge_sha": pr["merge_commit_sha"]}
    state = pr["mergeable_state"]
    if state in {"unknown", "blocked"}:
        raise Pending(f"GitHub reports that PR #{number} is {state}.")
    if state != "clean" or pr["mergeable"] is not True:
        raise ValueError(f"GitHub reports that PR #{number} is {state}.")
    result = api(f"{path}/merge", "--method", "PUT", "-f", "merge_method=merge", "-f", f"sha={sha}")
    if not result["merged"]:
        raise ValueError(f"GitHub did not merge PR #{number}.")
    return {"merge_sha": result["sha"]}


def publish(env):
    card = env["PLAYBOOK_INPUT_TASK_KEY"]
    sha = env["PLAYBOOK_INPUT_REVIEWED_SHA"]
    if not re.fullmatch(r"LABV2-[1-9][0-9]*", card) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Invalid LabV2 task key or reviewed commit ID.")
    if env["PLAYBOOK_INPUT_CANDIDATE_SHA"] != sha or run("git", "rev-parse", "HEAD") != sha:
        raise ValueError("The local commit differs from the revision approved for publication.")
    if run("git", "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("The run worktree changed after review.")
    branch = f"paseo/{card.lower()}-{sha}"
    if env["PLAYBOOK_STEP_ID"] == "open_pr":
        return open_pr(card, branch, sha)
    if env["PLAYBOOK_STEP_ID"] == "merge_pr":
        return merge_pr(env["PLAYBOOK_INPUT_PR_NUMBER"], branch, sha)
    raise ValueError("Unexpected playbook step.")


if __name__ == "__main__":
    try:
        outputs = publish(os.environ)
        print(json.dumps({"summary": outputs.get("pr_url") or f"Merged {outputs['merge_sha']}",
                          "outputs": outputs}))
    except Pending as exc:
        print(exc)
        sys.exit(75)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"{exc.cmd[0]} failed with exit code {exc.returncode}.")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        sys.exit(str(exc))
