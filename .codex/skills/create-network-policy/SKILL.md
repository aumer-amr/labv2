---
name: create-network-policy
description: Research and author least-privilege Kubernetes NetworkPolicy or CiliumNetworkPolicy manifests for a workload. Use when adding a new application policy, preparing a namespace for default-deny, translating Hubble traffic into rules, choosing between standard and Cilium policy features, or documenting validation and rollout gates.
---

# Create Network Policy

Create the smallest policy supported by configuration and reproduced traffic. Produce rollout gates with the manifest; a valid YAML document alone is not a finished policy.

## Research the workload

1. Read the nearest `AGENTS.md`, owning Flux Kustomization or HelmRelease, rendered workload, Services, probes, routes, hooks, Jobs, and existing namespace policies.
2. Resolve stable pod labels. Avoid generated labels, pod names, and controller revision hashes.
3. List required flows as exact source, destination, protocol, and port tuples. Include DNS, API, probes, metrics, reconciliation, failover, and cleanup.
4. Observe fresh flows with Hubble on every Cilium agent. Exercise cold starts and real operations; idle traffic is incomplete evidence.
5. Record whether each rule is proven by configuration, architecture, audit traffic, or enforced traffic.

## Choose the policy type

Use standard `NetworkPolicy` for namespace and pod selectors, IP blocks, and L3/L4 ports. Use `CiliumNetworkPolicy` only when a required rule needs Cilium features such as entities, FQDN DNS proxying, or other unsupported semantics. Keep ordinary pod-to-pod rules in standard policy when practical.

Account for Cilium identity aliases on node-facing paths: `host`, `remote-node`, and `kube-apiserver` may classify the same physical node differently. Add only identities reproduced by required traffic.

## Author least privilege

- Place workload-specific policy beside the application. Keep namespace-wide default-deny in the shared namespace network-policy directory.
- Select one intended workload with stable labels.
- Separate unrelated peers, ports, protocols, and evidence tuples. Avoid accidental Cartesian products.
- Add both source egress and destination ingress when both endpoints are isolated.
- Split DNS UDP/53 and TCP/53.
- Exclude Pod and Service CIDRs from intentionally broad LAN or internet access.
- Do not mistake `loadBalancerSourceRanges` for NodePort protection; prove the external boundary separately.
- Preserve documented exceptions such as host-network workloads and state their residual trust.

## Validate before rollout

1. Format and build every touched Kustomize directory.
2. Run Flux strict substitution without printing secret values.
3. Inspect rendered kinds, names, namespaces, selectors, peers, protocols, and ports.
4. Run server-side dry-run with an explicit namespace.
5. Confirm selectors match the expected live endpoints.
6. Define audit-mode exercises, enforcement gates, kubectl `/readyz` checks, and exact rollback steps.
7. Request a separate security review before enforcement.

Do not claim a flow was observed unless the evidence exists. Mark untested failover, recovery, redirect, transient-job, and host-network paths as rollout blockers or explicit residuals.
