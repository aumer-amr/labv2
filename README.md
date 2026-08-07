<div align="center">

<img src="https://raw.githubusercontent.com/aumer-amr/labv2/refs/heads/main/raw/amr.png" align="center" width="144px" height="144px"/>

</div>

# labv2

Personal Talos Kubernetes cluster managed through Flux GitOps. Core services include Cilium, Hubble, Envoy Gateway, cert-manager, External Secrets, Spegel, Reloader, and Gatus.

This repository contains rendered cluster configuration. Initial template setup is complete; documentation now focuses on operating and redeploying the cluster.

## Runbooks

- [Deployment](docs/deployment.md) — bootstrap Talos and applications, then verify the rollout.
- [GitOps](docs/gitops.md) — change applications, reconcile Flux, and operate network routes.
- [Reset](docs/reset.md) — return one or all Talos nodes to maintenance mode.
- [Maintenance](docs/maintenance.md) — apply and upgrade Talos, add nodes, maintain Kubernetes, and troubleshoot failures.

## Quick Reference

Run commands from repository root in WSL Bash with Mise activated:

```sh
mise trust
mise install
just --list
```

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
