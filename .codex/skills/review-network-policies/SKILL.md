---
name: review-network-policies
description: Review Kubernetes NetworkPolicy and CiliumNetworkPolicy designs against repository configuration and live traffic. Use for netpol plans, policy manifests, default-deny migrations, least-privilege reviews, selector and port validation, Hubble evidence analysis, or deciding whether policies are safe to enforce.
---

# Review Network Policies

Review the desired policy, the owning workload, and live behavior together. Do not approve from manifest shape alone.

## Establish the real boundary

1. Read the nearest `AGENTS.md`, the owning Flux objects, and any netpol design or prior QA document.
2. Resolve every selected workload, stable label, namespace, probe, controller, Service port, hook, transient Job, and `hostNetwork` exception.
3. Express each allowance as one tuple: source, destination, protocol, and port. Check source egress and destination ingress when both endpoints are isolated.
4. Evaluate the additive union of every policy selecting an endpoint. Include chart-owned policies and namespace default-deny.

## Demand evidence

- Prefer fresh connections and Hubble observations from every Cilium agent. Established connections do not prove a new connection will pass.
- Distinguish `AUDIT` evidence from enforced `ALLOWED`, `FORWARDED`, and `DROPPED` evidence.
- Cite architectural requirements as architecture, never as live evidence.
- Add an identity, CIDR, FQDN, or port only when configuration or a reproduced flow requires it.
- Treat `host`, `remote-node`, and `kube-apiserver` as possible aliases for node-facing traffic. Preserve only the identities proven necessary in this cluster.

## Inspect common misses

- DNS over UDP and TCP, API access, kubelet probes, webhooks, reconciliation, failover, and cleanup paths.
- Redirect, CDN, and resolved IP behavior behind FQDN rules.
- LoadBalancer source ranges, NodePort bypass, and external positive and negative tests. In-cluster probes do not prove an external boundary.
- CSI controller traffic to MON, MGR, MDS, and OSD messenger ports. Namespaced policies do not isolate host-network CSI node plugins.
- Controller-owned or Helm-owned permissive policies that keep strict policies from being meaningful.
- Selectors for completed or short-lived Jobs that will be recreated after enforcement.

## Return a verdict

List findings by severity with file and line, evidence, minimal fix, and required retest. End with one explicit verdict:

- `APPROVED` only when no material correctness, availability, recovery, or security blocker remains.
- `NOT APPROVED` when a blocker remains, followed by the smallest safe next action.

State residual trust and untested disruptive scenarios separately. Never turn an audit-stage success into an enforcement approval.
