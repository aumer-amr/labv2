# Deployment

Use this runbook to rebuild the cluster after Talos nodes have been prepared or reset into maintenance mode.

Commands assume Mise is activated in WSL. Otherwise run them through `mise exec --`.

## Prerequisites

- Confirm `talos/topf.yaml` and its patches describe intended nodes, disks, network, Talos version, and Kubernetes version.
- Confirm repository contains desired Kubernetes state and is available to Flux.
- Confirm encrypted bootstrap and Talos secret files are present.
- Confirm nodes are reachable on Talos API.

## Bootstrap Talos

```sh
just bootstrap talos
```

This applies Talos configuration, bootstraps cluster, writes `talos/talosconfig`, and fetches repository `kubeconfig`.

## Bootstrap Kubernetes Applications

```sh
just bootstrap apps
```

This installs bootstrap CRDs and charts, supplies Flux secrets, and starts Flux synchronization from Git. Temporary readiness failures are expected before Cilium becomes healthy.

Watch rollout:

```sh
kubectl get pods --all-namespaces --watch
```

Do not rerun a partially completed Talos bootstrap blindly. Inspect node state first; use the [reset runbook](reset.md) only when a clean rebuild is required.

## Verify Deployment

Check Talos and Kubernetes nodes:

```sh
just talos nodes
kubectl get nodes -o wide
```

Check Cilium and Hubble:

```sh
kubectl -n kube-system exec ds/cilium --container cilium-agent -- cilium status
kubectl -n kube-system get deploy hubble-relay hubble-ui
```

Check Flux sources and reconciliations:

```sh
flux check
flux get sources git -A
flux get ks -A
flux get hr -A
```

Check Envoy Gateway and routes:

```sh
kubectl -n network get gateways.gateway.networking.k8s.io
kubectl get httproutes.gateway.networking.k8s.io -A
```

Both `envoy-external` and `envoy-internal` must report `Accepted=True` and `Programmed=True`. Affected HTTPRoutes must report `Accepted=True` and `ResolvedRefs=True`.

Check certificates and Gatus:

```sh
kubectl -n network get certificates
kubectl -n observability get ks,hr,deploy,pods
```

Continue with [GitOps operations](gitops.md) after cluster is healthy.
