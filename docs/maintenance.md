# Maintenance

## Talos

### Inspect Nodes and Pending Configuration

```sh
just talos nodes
just talos diff
```

Render machine configurations for review:

```sh
just talos render
```

Rendered files are written to `talos/rendered`.

### Apply Configuration

Apply to all nodes after reviewing prompted diff:

```sh
just talos apply
```

Apply to one node:

```sh
just talos apply-node <node>
```

Optional modes:

```sh
just talos apply-node <node> auto
just talos apply-node <node> reboot
just talos apply-node <node> no-reboot
just talos apply-node <node> staged
just talos apply-node <node> try
```

### Upgrade Talos

Update `talosVersion` in `talos/topf.yaml` and
`kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml`, review the
diff, then upgrade one node at a time:

```sh
just talos upgrade-node <node>
```

Upgrade every node sequentially only after validating rollout:

```sh
just talos upgrade
```

Confirm node health between upgrades.

For Talos patch releases, no compatibility-gate change is needed. Before a
Talos minor release:

1. Check the official Talos support matrix for that minor release.
2. Add its supported Kubernetes minor range to
   `scripts/check-talos-kubernetes-compatibility.sh`.
3. Raise the exclusive `ghcr.io/siderolabs/kubelet` `allowedVersions` ceiling
   in `.renovaterc.json5`. For a maximum supported Kubernetes version of
   `1.37`, use `<1.38.0`.
4. Run the compatibility check:

    ```sh
    bash scripts/check-talos-kubernetes-compatibility.sh
    ```

Unknown Talos minors fail the check until this review is completed. Do not
derive support from release numbering alone.

### Upgrade Kubernetes

Confirm the target Kubernetes minor is listed in the current Talos support
matrix. Update `kubernetesVersion` in `talos/topf.yaml` and the version in
`kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetesupgrade.yaml`, then
run:

```sh
bash scripts/check-talos-kubernetes-compatibility.sh
```

After the check passes, run:

```sh
just talos upgrade-k8s
```

Verify nodes, Cilium, Flux controllers, and workloads after upgrade.

### Add a Node

1. Boot new node into Talos maintenance mode.
2. Inspect disks and network links:

    ```sh
    talosctl get disks --nodes <ip> --insecure
    talosctl get links --nodes <ip> --insecure
    ```

3. Add node and required patches to `talos/topf.yaml`.
4. Review rendered configuration:

    ```sh
    just talos render
    just talos diff
    ```

5. Apply only to new node:

    ```sh
    just talos apply-node <node>
    ```

6. Confirm it joins and becomes ready:

    ```sh
    just talos nodes
    kubectl get nodes -o wide
    ```

Keep an odd number of control-plane nodes for etcd quorum.

## Kubernetes and Flux

### Reconcile and Inspect Flux

```sh
just kube reconcile
flux get sources git -A
flux get ks -A
flux get hr -A
```

Check for suspended or failed Kustomizations before assuming workloads are current. When resuming networking changes, resume `network/envoy-gateway` first and wait for `Ready=True` before its dependents.

### Inspect Workloads

```sh
kubectl -n <namespace> get deploy,sts,ds,pods -o wide
kubectl -n <namespace> get events --sort-by='.metadata.creationTimestamp'
kubectl -n <namespace> describe <resource> <name>
kubectl -n <namespace> logs <pod> --since=10m
```

For multi-container pods, add `--container <container>`.

### Diagnose Flux Resources

```sh
flux get ks -A
flux get hr -A
kubectl -n <namespace> describe kustomization <name>
kubectl -n <namespace> describe helmrelease <name>
```

Inspect source-controller for OCI or Git artifact failures:

```sh
kubectl -n flux-system logs deploy/source-controller --since=10m
```

An HTTP `401` means registry was reached but authentication or network bypass policy rejected request. An HTTP `503` or reset before headers points to DNS, routing, TLS, proxy, or upstream connectivity.

### Diagnose Routes

```sh
kubectl -n network get gateways.gateway.networking.k8s.io
kubectl get httproutes.gateway.networking.k8s.io -A
kubectl -n <namespace> describe httproute <name>
```

Check Gateway `Accepted` and `Programmed` conditions, then HTTPRoute `Accepted` and `ResolvedRefs`. Verify backend Service and endpoints before restarting or recreating anything.

Use [GitOps workflow](gitops.md) for durable application changes and [reset runbook](reset.md) only for intentional destructive rebuilds.
