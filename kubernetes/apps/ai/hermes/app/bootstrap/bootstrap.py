#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

BOOTSTRAP = Path("/run/bootstrap")
DATA = Path("/opt/data")
TOOLS = Path("/opt/tools")
CRON_STORE = DATA / "cron/jobs.json"


def run(*args: str, input_text: str | None = None, env: dict | None = None) -> None:
    subprocess.run(args, check=True, input=input_text, text=True, env=env)


def output(*args: str) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True
    ).stdout.strip()


def load_yaml(name: str) -> dict:
    value = yaml.safe_load((BOOTSTRAP / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"Invalid bootstrap config: {name}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_tools(config: dict) -> None:
    for name in ("gh", "kubectl"):
        tool = config.get(name)
        if not isinstance(tool, dict):
            raise SystemExit(f"Missing tool config: {name}")
        if not isinstance(tool.get("version"), str):
            raise SystemExit(f"Invalid {name} version")
        if not str(tool.get("url", "")).startswith("https://"):
            raise SystemExit(f"Invalid {name} URL")
        if not re.fullmatch(r"[0-9a-f]{64}", str(tool.get("sha256", ""))):
            raise SystemExit(f"Invalid {name} checksum")
    if not isinstance(config["gh"].get("archive_member"), str):
        raise SystemExit("Invalid gh archive member")


def load_jobs() -> list[dict]:
    jobs = load_yaml("cron-jobs.yaml").get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise SystemExit("Cron config must contain jobs")
    names = [job.get("name") for job in jobs if isinstance(job, dict)]
    if len(names) != len(jobs) or len(names) != len(set(names)):
        raise SystemExit("Cron job names must be present and unique")
    for job in jobs:
        for key in ("name", "schedule", "deliver_env"):
            if not isinstance(job.get(key), str) or not job[key]:
                raise SystemExit(f"Invalid {key} for cron job {job.get('name')}")
        if not re.fullmatch(r"DISCORD_[A-Z_]+", job["deliver_env"]):
            raise SystemExit(f"Invalid delivery environment for {job['name']}")
        if "no_agent" in job and not isinstance(job["no_agent"], bool):
            raise SystemExit(f"Invalid no_agent for cron job {job['name']}")
        if job.get("no_agent") and not isinstance(job.get("script"), str):
            raise SystemExit(f"No-agent cron job requires a script: {job['name']}")
        for key in ("prompt", "script", "workdir"):
            if key in job and not isinstance(job[key], str):
                raise SystemExit(f"Invalid {key} for cron job {job['name']}")
        if not isinstance(job.get("skills", []), list) or not all(
            isinstance(skill, str) for skill in job.get("skills", [])
        ):
            raise SystemExit(f"Invalid skills for cron job {job['name']}")
    return jobs


def validate() -> tuple[dict, list[dict]]:
    tools = load_yaml("tools.yaml")
    validate_tools(tools)
    return tools, load_jobs()


def download(tool: str, config: dict) -> Path:
    cache = DATA / "tool-cache" / ("gh.tar.gz" if tool == "gh" else tool)
    if cache.is_file() and file_sha256(cache) == config["sha256"]:
        return cache
    temporary = Path("/tmp") / cache.name
    run(
        "curl",
        "--connect-timeout",
        "15",
        "--fail",
        "--location",
        "--max-time",
        "120",
        "--retry",
        "3",
        "--silent",
        "--show-error",
        config["url"],
        "--output",
        str(temporary),
    )
    if file_sha256(temporary) != config["sha256"]:
        raise SystemExit(f"Checksum mismatch for {tool}")
    shutil.copyfile(temporary, cache)
    cache.chmod(0o600)
    return cache


def install_tools(config: dict) -> None:
    gh_archive = download("gh", config["gh"])
    with tarfile.open(gh_archive, "r:gz") as archive:
        source = archive.extractfile(config["gh"]["archive_member"])
        if source is None:
            raise SystemExit("GitHub CLI binary missing from archive")
        with (TOOLS / "gh").open("wb") as destination:
            shutil.copyfileobj(source, destination)
    (TOOLS / "gh").chmod(0o755)

    shutil.copyfile(download("kubectl", config["kubectl"]), TOOLS / "kubectl")
    (TOOLS / "kubectl").chmod(0o755)
    if not output("gh", "--version").startswith(f"gh version {config['gh']['version']} "):
        raise SystemExit("Unexpected GitHub CLI version")
    if f"v{config['kubectl']['version']}" not in output("kubectl", "version", "--client"):
        raise SystemExit("Unexpected kubectl version")


def configure_github() -> None:
    config_dir = Path(os.environ["GH_CONFIG_DIR"])
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = os.environ.copy()
    token = environment.pop("GH_TOKEN")
    run(
        "gh",
        "auth",
        "login",
        "--hostname",
        "github.com",
        "--git-protocol",
        "https",
        "--with-token",
        input_text=f"{token}\n",
        env=environment,
    )
    (config_dir / "hosts.yml").chmod(0o600)


def prepare_workspace() -> None:
    workspace = DATA / "renovate-workspace"
    remote = "https://github.com/aumer-amr/labv2.git"
    if not (workspace / ".git").is_dir():
        run("git", "clone", "--quiet", remote, str(workspace))
    if output("git", "-C", str(workspace), "remote", "get-url", "origin") != remote:
        raise SystemExit("Unexpected Renovate workspace remote")
    run("git", "-C", str(workspace), "fetch", "--quiet", "--force", "--prune", "origin", "main")
    run("git", "-C", str(workspace), "checkout", "--quiet", "--detach", "--force", "origin/main")
    run("git", "-C", str(workspace), "clean", "-ffd")


def install_private_skills() -> None:
    source = Path("/run/private-skills")
    destination = DATA / "skills/private"
    prompts = DATA / "private-prompts"
    for bundle in sorted(source.glob("*.sops.yaml")):
        name = bundle.name.removesuffix(".sops.yaml")
        values = yaml.safe_load(bundle.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or not all(
            isinstance(values.get(key), str) for key in ("skill_md", "helper_py")
        ):
            raise SystemExit(f"Invalid private skill bundle: {name}")
        target = destination / name
        scripts = target / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        skill = target / "SKILL.md"
        helper = scripts / "assessment.py"
        skill.write_text(values["skill_md"], encoding="utf-8")
        helper.write_text(values["helper_py"], encoding="utf-8")
        skill.chmod(0o600)
        helper.chmod(0o700)
        if isinstance(values.get("instructions"), str):
            prompt = prompts / f"{name}.txt"
            prompt.parent.mkdir(parents=True, exist_ok=True)
            prompt.write_text(values["instructions"], encoding="utf-8")
            prompt.chmod(0o600)
        if isinstance(values.get("runner_sh"), str):
            runner = DATA / "scripts/private" / f"{name}.sh"
            runner.parent.mkdir(parents=True, exist_ok=True)
            runner.write_text(values["runner_sh"], encoding="utf-8")
            runner.chmod(0o700)


def stored_jobs() -> list[dict]:
    return json.loads(CRON_STORE.read_text()).get("jobs", []) if CRON_STORE.is_file() else []


def delivery(job: dict) -> str:
    channel = os.environ.get(job["deliver_env"], "")
    if not channel:
        raise SystemExit(f"Missing delivery environment: {job['deliver_env']}")
    return f"discord:{channel}"


def reconcile_cron(jobs: list[dict]) -> None:
    existing = stored_jobs()
    for job in jobs:
        matches = [item for item in existing if item.get("name") == job["name"]]
        if len(matches) > 1:
            raise SystemExit(f"Duplicate cron jobs named {job['name']}")
        prompt = job.get("prompt", "")
        common = [
            "--schedule",
            job["schedule"],
            "--prompt",
            prompt,
            "--deliver",
            delivery(job),
        ]
        if matches:
            command = ["hermes", "cron", "edit", matches[0]["id"], *common]
            command.extend(["--script", job.get("script", "")])
            command.append("--no-agent" if job.get("no_agent") else "--agent")
            command.extend(["--workdir", job.get("workdir", "")])
            skills = job.get("skills", [])
            if skills:
                for skill in skills:
                    command.extend(["--skill", skill])
            else:
                command.append("--clear-skills")
            run(*command)
            run("hermes", "cron", "resume", matches[0]["id"])
        else:
            command = [
                "hermes",
                "cron",
                "create",
                job["schedule"],
                prompt,
                "--name",
                job["name"],
                "--deliver",
                delivery(job),
            ]
            if job.get("script"):
                command.extend(["--script", job["script"]])
            if job.get("no_agent"):
                command.append("--no-agent")
            if job.get("workdir"):
                command.extend(["--workdir", job["workdir"]])
            for skill in job.get("skills", []):
                command.extend(["--skill", skill])
            run(*command)
        existing = stored_jobs()

    existing = stored_jobs()
    for job in jobs:
        matches = [item for item in existing if item.get("name") == job["name"]]
        if len(matches) != 1:
            raise SystemExit(f"Expected exactly one cron job named {job['name']}")
        expected = {
            "deliver": delivery(job),
            "enabled": True,
            "no_agent": bool(job.get("no_agent")),
            "prompt": job.get("prompt", ""),
            "schedule_display": job["schedule"],
            "script": job.get("script"),
            "skills": job.get("skills", []),
            "workdir": job.get("workdir"),
        }
        mismatches = [key for key, value in expected.items() if matches[0].get(key) != value]
        if mismatches:
            raise SystemExit(f"Cron job {job['name']} mismatch: {', '.join(mismatches)}")


def initialize() -> None:
    tools, jobs = validate()
    os.environ.update(
        HOME="/tmp/bootstrap-home",
        HERMES_HOME=str(DATA),
        PATH="/opt/tools:/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        GH_CONFIG_DIR="/run-shared/gh",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    for path in (Path(os.environ["HOME"]), DATA / "home", DATA / "tool-cache", TOOLS):
        path.mkdir(parents=True, exist_ok=True)
    for destination in (DATA / ".bash_profile", DATA / "home/.bash_profile"):
        shutil.copyfile(BOOTSTRAP / "profile", destination)
        destination.chmod(0o600)
    install_tools(tools)
    configure_github()
    prepare_workspace()
    install_private_skills()
    reconcile_cron(jobs)


def prepare_merge_broker() -> None:
    gh = Path("/run/broker/gh")
    shutil.copyfile(TOOLS / "gh", gh)
    gh.chmod(0o500)
    values = yaml.safe_load(
        Path("/run/private-skills/assess-renovate-prs.sops.yaml").read_text(encoding="utf-8")
    )
    helper = values.get("broker_py") if isinstance(values, dict) else None
    if not isinstance(helper, str):
        raise SystemExit("Invalid merge broker bundle")
    destination = Path("/run/broker/assessment.py")
    destination.write_text(helper, encoding="utf-8")
    destination.chmod(0o500)


def verify_read_token() -> None:
    request = Request(
        "https://api.github.com/repos/aumer-amr/labv2",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        repository = json.load(response)
    if repository.get("full_name") != "aumer-amr/labv2":
        raise SystemExit("Hermes GitHub token cannot read the target repository")
    for endpoint in ("contents", "pulls?per_page=1", "actions/runs?per_page=1"):
        with urlopen(
            Request(
                f"https://api.github.com/repos/aumer-amr/labv2/{endpoint}",
                headers=request.headers,
            ),
            timeout=30,
        ):
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("initialize", "prepare-merge-broker", "validate", "verify-read-token"),
    )
    command = parser.parse_args().command
    if command == "initialize":
        initialize()
    elif command == "prepare-merge-broker":
        prepare_merge_broker()
    elif command == "verify-read-token":
        verify_read_token()
    else:
        validate()


if __name__ == "__main__":
    main()
