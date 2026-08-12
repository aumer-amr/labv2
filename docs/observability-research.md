# Observability Research

Research snapshot: 2026-08-12. Upstream references are pinned to
[`onedr0p/home-ops@b71f396`](https://github.com/onedr0p/home-ops/tree/b71f396275fdde54e25a218bb82269cb18d55c7b),
which was `main` when inspected. This report compares only applications present in this repository.

## Recommended Work

### 1. Cert-manager dashboard and alerts

Add the upstream `GrafanaDashboard` and `PrometheusRule`, then include both in the app Kustomization.
No Helm change is needed locally: `prometheus.enabled` and `prometheus.servicemonitor.enabled` are
already enabled.

The dashboard imports Grafana dashboard `20842` revision `3`. The rules alert when cert-manager is
absent, a certificate is not ready, or the ACME client receives HTTP 429 responses.

- [Upstream dashboard](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/cert-manager/cert-manager/app/grafanadashboard.yaml)
- [Upstream rules](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/cert-manager/cert-manager/app/prometheusrule.yaml)
- [Upstream Helm metrics settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/cert-manager/cert-manager/app/helmrelease.yaml)

### 2. Flux dashboards, component scraping, and reconciliation alerts

Add the four upstream dashboards, the controller `PodMonitor`, and the `PrometheusRule`. Local
`flux-operator` already creates its ServiceMonitor, and local kube-state-metrics already exports
`gotk_resource_info`, so the rules' data sources are present. The PodMonitor adds direct controller
metrics from `source-controller`, `kustomize-controller`, `helm-controller`, and
`notification-controller`.

The rules cover a missing or unready Flux instance and unready HelmReleases or Kustomizations. The
dashboards cover Flux Kubernetes API performance, controller performance, cluster reconciliation,
and control-plane health.

- [Upstream dashboards](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/flux-system/flux-instance/app/grafanadashboard.yaml)
- [Upstream PodMonitor](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/flux-system/flux-instance/app/podmonitor.yaml)
- [Upstream rules](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/flux-system/flux-instance/app/prometheusrule.yaml)
- [Upstream operator ServiceMonitor setting](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/flux-system/flux-operator/app/helmrelease.yaml)

Before rollout, verify the local `flux-operator` version emits `flux_instance_info` with the labels
used by the pinned rule. Upstream uses chart `0.58.0`; this repository currently uses `0.57.0`.

### 3. Envoy Gateway control-plane monitor and dashboards

Keep the existing local Envoy proxy `PodMonitor`; it matches the proxy half of upstream's combined
observability file. Add only the upstream control-plane `ServiceMonitor` and three dashboards to
avoid duplicate proxy scraping. Local `EnvoyProxy` already enables Prometheus telemetry.

The dashboards cover Envoy overview, upstream traffic, and downstream traffic.

- [Upstream dashboards](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/envoy-gateway/app/grafanadashboard.yaml)
- [Upstream PodMonitor and ServiceMonitor](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/envoy-gateway/app/o11y.yaml)
- [Upstream Envoy Prometheus telemetry](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/envoy-gateway/app/envoy.yaml)

### 4. Rook-Ceph dashboards

Add the three upstream dashboards for Ceph cluster, OSD, and pool health. Local Rook monitoring and
cluster-generated Prometheus rules are already enabled, so no metrics toggle is needed.

- [Upstream dashboards](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/rook-ceph/rook-ceph/app/grafanadashboard.yaml)
- [Upstream cluster monitoring and rules settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/rook-ceph/rook-ceph/cluster/helmrelease.yaml)
- [Upstream operator monitoring setting](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/rook-ceph/rook-ceph/app/helmrelease.yaml)

Upstream also points the Ceph dashboard at Prometheus. Treat that as a separate optional change:
first verify the exact local Prometheus Service name, then use the `observability` namespace rather
than copying upstream's `o11y` URL.

### 5. Import chart-generated Cilium and external-secrets dashboards

Both charts already generate dashboard ConfigMaps locally, but no local `GrafanaDashboard` refers
to them. Add the small upstream bridge resources:

- Cilium: import `cilium-dashboard` and `cilium-operator-dashboard`. Local Cilium agent, operator,
  and Hubble metrics plus ServiceMonitors are already enabled.
- External Secrets: import `external-secrets-dashboard`. Local `serviceMonitor.enabled` and
  `grafanaDashboard.enabled` are already enabled.

Sources:

- [Upstream Cilium dashboard imports](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/cilium/app/grafanadashboard.yaml)
- [Upstream Cilium metrics and dashboard settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/cilium/app/helmrelease.yaml)
- [Upstream External Secrets dashboard import](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/external-secrets/external-secrets/app/grafanadashboard.yaml)
- [Upstream External Secrets settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/external-secrets/external-secrets/app/helmrelease.yaml)

### 6. Enable chart-native Kopiur and Spegel observability

Use chart values rather than adding hand-written resources.

- Kopiur: retain `monitoring.serviceMonitor.enabled`, then enable
  `monitoring.prometheusRule.enabled`, `monitoring.dashboards.enabled`, and
  `monitoring.dashboards.grafanaOperator.enabled` with `dashboards: grafana` as the instance label.
  These values exist in the locally installed `0.9.3` chart; no Kopiur upgrade is required solely
  for this work. Upstream currently uses `0.10.1`.
- Spegel: enable its Grafana Operator dashboard mode alongside the already-enabled ServiceMonitor.
  Local and upstream both use chart `0.7.4`.

Sources:

- [Upstream Kopiur settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kopiur-system/kopiur/app/helmrelease.yaml)
- [Official Kopiur 0.9.3 values](https://github.com/home-operations/kopiur/blob/0.9.3/deploy/helm/kopiur/values.yaml)
- [Official Kopiur 0.9.3 observability example](https://github.com/home-operations/kopiur/blob/0.9.3/deploy/observability-values.yaml)
- [Upstream Spegel settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/spegel/app/helmrelease.yaml)

Kopiur also supports a separate HTTPS webhook ServiceMonitor. Upstream home-ops does not enable it;
skip it until webhook metrics are needed because it adds a second TLS scrape path.

### 7. Add external-dns stale-sync alerting once

Add one copy of upstream's `ExternalDNSStale` rule. It alerts when
`external_dns_controller_last_sync_timestamp_seconds` is over 60 seconds old for five minutes and
preserves the `job` label, so one cluster-wide rule covers both local external-dns releases. Both
`cloudflare-dns` and `unifi-dns` already enable their ServiceMonitors.

- [Upstream rule](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/cloudflare-dns/app/prometheusrule.yaml)
- [Upstream Cloudflare DNS settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/cloudflare-dns/app/helmrelease.yaml)
- [Upstream UniFi DNS settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/unifi-dns/app/helmrelease.yaml)

### 8. Add the Cloudflare Tunnel dashboard

Add upstream's Cloudflare Tunnel dashboard. Local cloudflared already exposes metrics on port 8080
and has an app-template ServiceMonitor, so no Helm metrics change is needed.

- [Upstream dashboard](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/cloudflare-tunnel/app/grafanadashboard.yaml)
- [Upstream metrics and ServiceMonitor settings](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/network/cloudflare-tunnel/app/helmrelease.yaml)

## Already Covered

These local applications already match the relevant upstream metrics or monitor setting. No new
resource from upstream is needed:

| Application            | Current coverage                                                           | Upstream source                                                                                                                                                            |
| ---------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| onepassword-connect    | API ServiceMonitor enabled                                                 | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/external-secrets/onepassword-connect/app/helmrelease.yaml) |
| flux-operator          | ServiceMonitor created                                                     | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/flux-system/flux-operator/app/helmrelease.yaml)            |
| metrics-server         | Metrics endpoint and ServiceMonitor enabled                                | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/metrics-server/app/helmrelease.yaml)           |
| ocharted               | ServiceMonitor enabled                                                     | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/ocharted/app/helmrelease.yaml)                 |
| reloader               | PodMonitor enabled                                                         | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/reloader/app/helmrelease.yaml)                 |
| snapshot-controller    | ServiceMonitor enabled                                                     | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/snapshot-controller/app/helmrelease.yaml)      |
| VictoriaLogs server    | Chart dashboards, Grafana Operator integration, and ServiceMonitor enabled | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/o11y/victoria-logs/app/helmrelease.yaml)                   |
| VictoriaLogs collector | PodMonitor enabled                                                         | [HelmRelease](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/o11y/victoria-logs/collector/helmrelease.yaml)             |

CoreDNS also exposes Prometheus metrics through its `prometheus 0.0.0.0:9153` plugin, matching
upstream. Upstream adds no standalone monitor or dashboard for it, so confirm the existing
kube-prometheus-stack CoreDNS target before adding another scraper.

- [Upstream CoreDNS configuration](https://github.com/onedr0p/home-ops/blob/b71f396275fdde54e25a218bb82269cb18d55c7b/kubernetes/apps/kube-system/coredns/app/helmrelease.yaml)

## No Applicable Upstream Example

Current upstream has no requested dashboard, rule, ServiceMonitor, PodMonitor, or metrics toggle for
the local `intel-gpu-resource-driver` or Home Assistant apps. Current upstream has no corresponding
app path for local `k8s-gateway`, LiteLLM, or LiteLLM Operator. Local LiteLLM already has a standalone
ServiceMonitor. Do not invent resources for these apps without first checking their official metric
surface and observing a concrete need.

## Suggested Delivery Order

1. Cert-manager, external-dns rules, and Flux rules/PodMonitor: direct failure detection.
2. Envoy control-plane ServiceMonitor: closes an existing scrape gap.
3. Cert-manager, Flux, Envoy, and Rook dashboards: high operational value with metrics already
   available.
4. Cilium and External Secrets dashboard imports: small bridge resources for already-generated
   ConfigMaps.
5. Kopiur and Spegel chart-native dashboards/rules.
6. Cloudflare Tunnel dashboard.

Validate each namespace separately. Render touched Kustomizations, run Flux strict substitution,
then confirm every new monitor has active Prometheus targets and every dashboard reaches `Ready=True`
before treating the work as complete.
