<div align="center">

<img src="https://raw.githubusercontent.com/aumer-amr/labv2/refs/heads/main/raw/amr.png" align="center" width="144px" height="144px"/>

</div>

# labv2

Personal Talos Kubernetes cluster managed through Flux GitOps. Core services include Cilium, Hubble, Envoy Gateway, cert-manager, External Secrets, Spegel, Reloader, and Gatus.

This repository contains rendered cluster configuration. Initial template setup is complete; documentation now focuses on operating and redeploying the cluster.

## Runbooks

### Deploy and change the cluster

- [Bootstrap Talos and deploy applications](docs/deployment.md)
- [Change applications and network routes with Flux](docs/gitops.md)

### Operate and recover the cluster

- [Maintain Talos and Kubernetes or troubleshoot failures](docs/maintenance.md)
- [Investigate cluster alerts with AI monitoring](docs/ai-cluster-monitoring.md)
- Read the [power preconditions](docs/power.md#preconditions) before you [shut down](docs/power.md#shutdown) or [start the cluster](docs/power.md#startup).
- Resetting removes Kubernetes state. Read the [reset warning](docs/reset.md) before you [reset one node](docs/reset.md#reset-one-node) or [reset all nodes](docs/reset.md#reset-all-nodes).

### Manage application data

- [Set up, upgrade, or recover PostgreSQL databases](docs/database.md)
- [Set up, upgrade, or restore Valkey caches](docs/valkey.md)

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

For a fresh deployment, follow the [Talos bootstrap prerequisites](docs/deployment.md#bootstrap-talos).

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
