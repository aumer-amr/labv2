<div align="center">

<img src="https://raw.githubusercontent.com/aumer-amr/labv2/refs/heads/main/raw/amr.png" align="center" width="144px" height="144px"/>

</div>

# labv2

Personal Talos Kubernetes cluster managed through Flux GitOps. Core services include Cilium, Hubble, Envoy Gateway, cert-manager, External Secrets, Spegel, Reloader, and Gatus.

This repository contains rendered cluster configuration. Initial template setup is complete; documentation now focuses on operating and redeploying the cluster.

## Runbooks

- [Deployment](docs/deployment.md) — bootstrap Talos and applications, then verify the rollout.
- [GitOps](docs/gitops.md) — change applications, reconcile Flux, and operate network routes.
- [Application databases](docs/database.md) — add, connect, verify, upgrade, and recover per-application PostgreSQL clusters.
- [Application Valkey caches](docs/valkey.md) — add, connect, verify, and upgrade isolated per-application Valkey instances.
- [Reset](docs/reset.md) — return one or all Talos nodes to maintenance mode.
- [Maintenance](docs/maintenance.md) — apply and upgrade Talos, add nodes, maintain Kubernetes, and troubleshoot failures.

## Quick Reference

Run commands from repository root in WSL Bash with Mise activated:

```sh
mise trust
mise install
just --list
```

### Private Codex instructions

Codex must be launched through the repository's private-instruction wrapper. Add this to WSL `~/.bashrc` on every workstation:

```bash
if [[ -f /mnt/e/code/private/labv2/scripts/codex-shell.sh ]]; then
  source /mnt/e/code/private/labv2/scripts/codex-shell.sh
fi
```

Reload the shell with `source ~/.bashrc`. Running `codex` anywhere inside this repository then uses `scripts/codex-private`; elsewhere it uses the normal Codex executable.

Edit `.codex-private/AGENTS.private.sops.yaml` through the VS Code SOPS extension or with `mise exec -- sops .codex-private/AGENTS.private.sops.yaml`. SOPS uses the repository's ignored `age.key` for decryption.

Fresh deployment:

```sh
just bootstrap talos
just bootstrap apps
kubectl get pods --all-namespaces --watch
```

Routine status:

```sh
just talos nodes
kubectl get nodes -o wide
flux get ks -A
flux get hr -A
```

Force Flux to pull current Git state:

```sh
just kube reconcile
```

Never print or commit decrypted credentials, age keys, kubeconfigs, Talos secrets, or provider tokens.
