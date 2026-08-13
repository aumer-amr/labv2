---
name: manage-gatus-monitoring
description: Configure, troubleshoot, and verify repository Gatus monitoring and discovery. Use when adding or changing Gatus endpoint annotations, configuring internal or external checks, handling guarded endpoint exceptions, changing alert routing, disabling self-monitoring, or verifying generated Gatus configuration and endpoint behavior.
---

# Manage Gatus Monitoring

## Configure

1. Read `AGENTS.md`, Gatus configuration, discovery sidecar settings, target HTTPRoute or Service, and nearby monitored resources.
2. Opt resources in with `gatus.home-operations.com/endpoint` annotations.
3. Preserve repository defaults:
    - external Gateway: group `external` and configured public DNS resolver;
    - internal Gateway: group `internal`, `guarded: true`, and hidden hostname/URL;
    - alerts: Pushover by default, with `external-tlb` using `DISCORD_TLB_WEBHOOK_URL`.
4. Keep Gatus's own HTTPRoute monitoring disabled.

## Handle exceptions

Do not assume `guarded: false` overrides inherited `guarded: true`. For an unguarded exception such as Hubble, disable discovery on the HTTPRoute and define the complete endpoint on its backing Service.

Do not add another Discord webhook integration without explicit permission.

## Verify

1. Render and validate changed manifests.
2. Inspect generated Gatus configuration; annotations alone do not prove the resulting check.
3. Verify endpoint URL, group, resolver, guard settings, UI hiding, conditions, and alert provider.
4. Exercise endpoint behavior from Gatus's network context and inspect recent errors.
5. Inspect before restarting a sidecar or recreating an HTTPRoute; neither is automatically required.
