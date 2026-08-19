# Application Valkey Caches

Use the shared Valkey component to create one isolated Valkey instance in the
same namespace as each application. Every consumer gets its own HelmRelease,
Deployment, Service, credentials, and network policies. The chart artifact is
downloaded once through the shared `OCIRepository/valkey` in the `database`
namespace.

The defaults are intended for reconstructable caches. Each instance is a
singleton with ephemeral storage: a Pod replacement causes downtime and loses
all cached data. Do not use this component for durable queues, sessions, locks,
or primary data without redesigning persistence and availability first.

## Prerequisites

Before adding manifests:

- Add `VALKEY_PASSWORD` to the application's 1Password item. The item name must
  exactly match the application name.
- Ensure the application Pods have the label
  `app.kubernetes.io/name: <app>`. Both sides of the NetworkPolicy use this
  label to permit Valkey traffic.
- Confirm the application can use authenticated standalone Valkey on port
  `6379`.

Never put the password in Git or print the generated Secret value.

## Add the component

In `kubernetes/apps/<namespace>/<app>/app/kustomization.yaml`, add the Valkey
component:

```yaml
components:
    - ../../../../components/valkey
```

In `kubernetes/apps/<namespace>/<app>/ks.yaml`, add both direct dependencies.
The `valkey` dependency is required because the HelmRelease references the
shared OCIRepository in the `database` namespace.

```yaml
spec:
    dependsOn:
        - name: onepassword-connect
          namespace: external-secrets
        - name: valkey
          namespace: database
    postBuild:
        substitute:
            APP: example
            NAMESPACE: apps
    prune: true
```

Replace `example` and `apps`. Preserve any existing dependencies and
substitutions in the Flux Kustomization.

The component defaults are:

| Setting                   | Default       | Purpose                         |
| ------------------------- | ------------- | ------------------------------- |
| `VALKEY_CPU_REQUEST`      | `10m`         | Reserved CPU                    |
| `VALKEY_CPU_LIMIT`        | `500m`        | Maximum CPU                     |
| `VALKEY_MEMORY_REQUEST`   | `64Mi`        | Reserved memory                 |
| `VALKEY_MEMORY_LIMIT`     | `256Mi`       | Container memory limit          |
| `VALKEY_MAXMEMORY`        | `192mb`       | Valkey-managed cache memory     |
| `VALKEY_MAXMEMORY_POLICY` | `allkeys-lru` | Evict least-recently-used keys  |
| Replica mode              | Disabled      | One standalone Pod              |
| Persistence               | Disabled      | Cache is lost with the Pod      |
| Deployment strategy       | `Recreate`    | Avoid two divergent cache nodes |
| Metrics and PDB           | Disabled      | Avoid singleton-only overhead   |

Override resource settings through `postBuild.substitute` when needed:

```yaml
postBuild:
    substitute:
        APP: example
        NAMESPACE: apps
        VALKEY_MEMORY_LIMIT: 512Mi
        VALKEY_MAXMEMORY: 384mb
```

Keep `VALKEY_MAXMEMORY` below `VALKEY_MEMORY_LIMIT` so Valkey has memory for
connections, buffers, allocator overhead, and the process itself.

## Connect the application

The component creates this endpoint:

```text
${APP}-valkey.${NAMESPACE}.svc.cluster.local:6379
```

Authentication uses the `default` Valkey ACL user. Secret `${APP}-valkey`
contains the password in key `VALKEY_PASSWORD`. Map that key to the environment
variable expected by the application; do not assume `envFrom` uses the correct
name.

For applications expecting standard Redis settings, the equivalent values are:

```text
REDIS_HOST=${APP}-valkey.${NAMESPACE}.svc.cluster.local
REDIS_PORT=6379
REDIS_USERNAME=default
REDIS_PASSWORD=<value from APP-valkey/VALKEY_PASSWORD>
REDIS_DB=0
```

Prefer separate host, port, username, and password settings. If an application
requires a URL, construct it at runtime and URL-encode the password rather than
placing credentials in a manifest.

Only application Pods matching `app.kubernetes.io/name: ${APP}` can reach the
Valkey Pods through the component policies. If an application uses different
labels, change the workload labels instead of broadening the policy.

## Validate before rollout

From the repository root, build the application and the Flux result with strict
substitution:

```sh
namespace=<namespace>
app=<app>
app_dir="kubernetes/apps/$namespace/$app"
rendered="$(mktemp)"

mise exec -- kustomize build \
  --load-restrictor=LoadRestrictionsNone \
  "$app_dir/app"

mise exec -- flux build kustomization "$app" \
  --namespace "$namespace" \
  --path "$app_dir/app" \
  --kustomization-file "$app_dir/ks.yaml" \
  --strict-substitute >"$rendered"

mise exec -- kubectl apply \
  --server-side \
  --dry-run=server \
  --namespace "$namespace" \
  --filename "$rendered"

rm -f -- "$rendered"
```

Inspect the rendered `HelmRelease`, `ExternalSecret`, and NetworkPolicies. Verify
that `spec.chartRef` points to `OCIRepository/valkey` in namespace `database`.
Commit and push only when requested; Flux cannot deploy a local commit.

## Verify after rollout

Do not treat a Running Pod as sufficient proof. Check the shared OCI source,
application Flux child, Helm release, credentials, workload, Service, policies,
and recent logs:

```sh
mise exec -- flux get sources oci --namespace database
mise exec -- flux get kustomization <app> --namespace <namespace>
mise exec -- kubectl get helmrelease <app>-valkey --namespace <namespace>
mise exec -- kubectl get externalsecret <app>-valkey --namespace <namespace>
mise exec -- kubectl get deployment,pod,service --namespace <namespace> \
  --selector app.kubernetes.io/instance=<app>-valkey
mise exec -- kubectl get networkpolicy --namespace <namespace>
mise exec -- kubectl logs deployment/<app>-valkey --namespace <namespace> \
  --since=10m
```

Verify the application can authenticate, write a disposable cache key, and read
it back. After an authorized restart or natural Pod replacement, verify the
application reconstructs the lost cache. Inspect Hubble for denied traffic
before considering the network policy verified.

## Upgrade Valkey

The chart version is pinned by `OCIRepository/valkey` in
`kubernetes/apps/database/valkey/app/ocirepository.yaml`. Because every
application HelmRelease references this source, changing its tag upgrades all
Valkey consumers. Inventory and render every consumer before changing the tag.

The instances remain independent at runtime: a failed application Valkey does
not affect another application's data plane. The shared OCIRepository only
centralizes chart retrieval and version selection.

## Remove or restore

Removing the component with pruning enabled deletes the application's
HelmRelease and ephemeral Valkey workload. There is no restore path because
persistence and backups are disabled. The application must rebuild its cache
from its authoritative data source.
