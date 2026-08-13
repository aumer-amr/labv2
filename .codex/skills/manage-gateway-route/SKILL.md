---
name: manage-gateway-route
description: Configure, secure, troubleshoot, and verify Kubernetes Gateway API HTTPRoutes served by Envoy Gateway. Use when adding or changing internal or external routes, choosing Kanidm protection, configuring native OAuth2/OIDC or LDAP, adding an Envoy SecurityPolicy, or diagnosing route Accepted, ResolvedRefs, or Gateway Programmed conditions.
---

# Manage Gateway Route

## Inspect

1. Read `AGENTS.md`, owning Flux objects, existing Gateway, HTTPRoute, backend Service, policies, and nearby route patterns.
2. Confirm intended exposure: internal routes use `network/envoy-internal`; external routes use `network/envoy-external`.
3. Resolve listener, hostname, parent references, backend namespace, port, ReferenceGrant needs, and existing policy attachment.

## Choose protection

For an external route not classified as `external-tlb`, ask whether Kanidm must protect it before editing.

When protection is required:

1. Check whether the application supports OAuth 2.0, OpenID Connect, or LDAP natively.
2. Prefer native integration and, after permission, create a dedicated Kanidm client for the application.
3. If no suitable native integration exists, attach an Envoy Gateway OIDC `SecurityPolicy` and create a dedicated Kanidm OAuth2 client.
4. Never share clients between applications. Store credentials through the existing ExternalSecret provider and expose no values.

## Validate

1. Render and strictly substitute manifests, then inspect names and namespaces.
2. Confirm both Gateways report `Accepted=True` and `Programmed=True` after Envoy changes.
3. Confirm affected HTTPRoutes report `Accepted=True` and `ResolvedRefs=True` at their current generation.
4. Test intended hostname, TLS behavior, backend response, and authentication boundary from relevant network location.
5. Inspect Envoy and application error logs when conditions or behavior disagree.

Do not enable Cilium Envoy or Gateway API as part of Envoy Gateway work unless the user explicitly requests that architecture change.
