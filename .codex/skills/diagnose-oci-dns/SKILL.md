---
name: diagnose-oci-dns
description: Diagnose Flux OCI artifact, container registry, DNS, routing, TLS, proxy, reachability, and authentication failures without changing live state. Use when OCIRepository or source-controller fetches fail, registry bypass rules appear ineffective, responses are 401 or 503, connections reset, or cluster and workstation results disagree.
---

# Diagnose OCI and DNS

Diagnose from the component performing the request, usually Flux source-controller. Do not change configuration until the failing layer is proven.

1. Read `AGENTS.md`, OCIRepository status, source-controller events and recent logs, registry configuration, DNS configuration, proxy settings, and relevant network policies.
2. Record endpoint, resolved addresses, response class, TLS stage, and observed source address without exposing credentials.
3. Reproduce DNS resolution and registry access from source-controller's network context. Workstation or unrelated pod success is not equivalent.
4. Classify failure:
    - `503` or reset before headers: investigate DNS, routing, TLS, proxy, or upstream reachability;
    - `401`: registry was reached; investigate authentication or bypass policy;
    - certificate or handshake failure: investigate trust, SNI, hostname, and interception before credentials.
5. Compare registry-observed source address with `bypassNetworks`. Pod CIDRs may be SNATed to node or egress addresses.
6. Check each layer with read-only evidence before proposing its smallest configuration change.

Return proven failing layer, evidence, competing explanations excluded, minimal proposed fix, and exact verification. Do not perform a live fix without explicit authorization.
