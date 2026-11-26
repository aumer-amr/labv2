#!/usr/bin/env python3
"""Exercise bootstrap recipes with local fixtures."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
FLUX_RELEASE = Path("kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml")
COMMAND_FIXTURE = """#!/usr/bin/env bash
set -euo pipefail
case "${0##*/}" in
    gum)
        [[ "$6" != fatal ]] || { printf '%s\n' "$7" >&2; exit 1; }
        ;;
    kubectl)
        if [[ "$1" == get ]]; then
            [[ "$2" == nodes && "$3" == --output=name && "$4" == --request-timeout=* ]]
            calls=$(wc -l < "$FIXTURE_DIR/node-queries")
            printf 'get nodes\n' >> "$FIXTURE_DIR/node-queries"
            if [[ "$NODE_FIXTURE" == mixed ]] && (( calls > 0 )); then
                # A Ready node and a NotReady node are both registered.
                printf 'node/ready\nnode/not-ready\n'
            fi
        else
            [[ "$*" == "--namespace flux-system apply --server-side --filename -" ]]
            cat >/dev/null
        fi
        ;;
    sops)
        [[ "$1" == decrypt ]]
        printf '%s\n' "${2##*/}" >> "$FIXTURE_DIR/decryptions"
        cat "$2"
        ;;
    topf)
        case "$*" in
            talosconfig|'kubeconfig --validity 8760h') ;;
            *) exit 99 ;;
        esac
        printf 'generated %s\n' "$1"
        exit "$TOPF_EXIT_CODE"
        ;;
esac
"""


def main():
    with tempfile.TemporaryDirectory() as temp:
        fixture = Path(temp)
        for path in (
            Path("justfile"),
            Path("bootstrap/mod.just"),
            Path("scripts/write-admin-config.py"),
        ):
            (fixture / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, fixture / path)

        bin_dir = fixture / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "command-fixture"
        stub.write_text(COMMAND_FIXTURE)
        stub.chmod(0o755)
        for command in ("gum", "kubectl", "sops", "topf"):
            (bin_dir / command).symlink_to(stub.name)

        clock = fixture / "clock"
        clock.write_text('sleep() { SECONDS=$((SECONDS + $1)); }\n')

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BASH_ENV": str(clock),
            "XDG_RUNTIME_DIR": str(fixture),
            "FIXTURE_DIR": str(fixture),
        }

        def run(recipe, **variables):
            return subprocess.run(
                ["just", f"bootstrap::{recipe}"],
                cwd=fixture,
                env={**env, **variables},
                capture_output=True,
                text=True,
                timeout=15,
            )

        (fixture / "talos").mkdir()
        for name, directory in (("talosconfig", fixture / "talos"), ("kubeconfig", fixture)):
            config = directory / name
            config.write_text("previous configuration\n")
            config.chmod(0o644)

            result = run(f"talos-{name}", TOPF_EXIT_CODE="42")
            assert result.returncode == 42, result.stderr
            assert config.read_text() == "previous configuration\n"
            assert not list(directory.glob(f".{name}.*"))

            result = run(f"talos-{name}", TOPF_EXIT_CODE="0")
            assert result.returncode == 0, result.stderr
            assert config.read_text() == f"generated {name}\n"
            assert stat.S_IMODE(config.stat().st_mode) == 0o600
            assert not list(directory.glob(f".{name}.*"))

        queries = fixture / "node-queries"
        queries.write_text("")
        result = run("apps-ready", NODE_FIXTURE="mixed")
        assert result.returncode == 0, result.stderr
        assert queries.read_text().splitlines() == ["get nodes", "get nodes"]

        queries.write_text("")
        result = run("apps-ready", NODE_FIXTURE="empty")
        assert result.returncode != 0, result.stderr
        assert "Timed out waiting for a node to register" in result.stderr
        assert len(queries.read_text().splitlines()) > 1

        release = fixture / FLUX_RELEASE
        release.parent.mkdir(parents=True)
        for path in (
            "bootstrap/sops-age.sops.yaml",
            "kubernetes/components/sops/cluster-secrets.sops.yaml",
        ):
            secret = fixture / path
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text("fake fixture contents\n")

        decryptions = fixture / "decryptions"
        regular_secrets = ["sops-age.sops.yaml", "cluster-secrets.sops.yaml"]

        def run_secrets(url):
            release.write_text(f"spec: {{values: {{instance: {{sync: {{url: {url}}}}}}}}}\n")
            decryptions.write_text("")
            result = run("apps-secrets")
            return result, decryptions.read_text().splitlines()

        result, attempted = run_secrets("https://example.test/repo.git")
        assert result.returncode == 0, result.stderr
        assert attempted == regular_secrets, attempted

        ssh_url = "ssh://git@example.test/repo.git"
        result, attempted = run_secrets(ssh_url)
        assert result.returncode != 0, result.stderr
        assert "Failed to apply secret" in result.stderr
        assert attempted == ["deploy-key.sops.yaml"], attempted

        (fixture / "bootstrap/deploy-key.sops.yaml").write_text("fake fixture key\n")
        result, attempted = run_secrets(ssh_url)
        assert result.returncode == 0, result.stderr
        assert attempted == ["deploy-key.sops.yaml", *regular_secrets], attempted

    print("Bootstrap checks passed")


if __name__ == "__main__":
    main()
