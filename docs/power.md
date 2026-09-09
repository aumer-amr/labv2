# Cluster Power Runbook

Use this runbook for a planned full power outage of all three Talos nodes. Each
node is both a Kubernetes control-plane member and a Rook Ceph storage host, so
ordinary sequential node drains are not a safe full-cluster shutdown strategy.

The workflow stores recovery data in `.private/cluster-power-state.json`. Keep
that file until startup completes. It contains resource names and replica
counts, not Secret values, and is excluded from Git.

## Preconditions

- Run commands from WSL in `/mnt/e/code/private/labv2`.
- Ensure important data has a recent off-cluster backup.
- Ensure all three nodes are reachable and Ready.
- Ensure Ceph is `HEALTH_OK` with every placement group `active+clean`.
- Ensure every CloudNativePG cluster is healthy and no backup is running.
- Ensure `.private/cluster-power-state.json` does not exist from an unfinished
  shutdown.

Preview the checks and targets without changing live state:

```sh
mise exec -- just talos shutdown-plan
```

## Shutdown

```sh
mise exec -- just talos shutdown
```

The command requires confirmation, then:

1. Records unsuspended Flux resources, CloudNativePG clusters, workload
   replicas, and Rook deployment replicas.
2. Suspends Flux Kustomizations and HelmReleases.
3. Scales database clients down.
4. Hibernates CloudNativePG, then stops remaining application Deployments and
   StatefulSets.
5. Refuses to continue while any non-Rook pod still mounts a PVC.
6. Sets Ceph `noout` and scales down Rook components in controlled order.
7. Requests shutdown of all three Talos nodes together.

The final Talos call uses `--force` only to skip its redundant Kubernetes
cordon/drain phase. Applications and storage have already been stopped
gracefully; this is not an abrupt power cut.

Wait until every host is off before removing external power.

## Startup

Restore power to all three nodes. After Talos, etcd, the Kubernetes API, and
Cilium are available, run:

```sh
mise exec -- just talos startup
```

Startup restores Ceph monitors first, then managers and OSDs, remaining Ceph
components, the Rook operator, database operators and databases, application
workloads, and Flux. It waits for core Ceph health before starting the Rook
operator and for every database operator/plugin before resuming databases.
It clears Ceph `noout` only after every placement group is `active+clean`.
While `noout` is still set, the only accepted health warning is the expected
`OSDMAP_FLAGS` warning in addition to the auth warnings already muted in the
Rook configuration; full `HEALTH_OK` is required after clearing it.

Verify final health:

```sh
mise exec -- kubectl get nodes
mise exec -- kubectl --namespace rook-ceph exec deploy/rook-ceph-tools -- ceph status
mise exec -- flux get kustomizations --all-namespaces
mise exec -- kubectl get gateways.gateway.networking.k8s.io --namespace network
mise exec -- kubectl get httproutes.gateway.networking.k8s.io --all-namespaces
```

Inspect local recovery phase at any time:

```sh
mise exec -- just talos power-status
```

Do not remove the recovery state or manually clear Ceph flags after a partial
failure. Inspect the recorded phase and rerun the same command to resume, or
reverse the failed step.
