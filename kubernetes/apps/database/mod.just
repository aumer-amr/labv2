set quiet
set shell := ['bash', '-euo', 'pipefail', '-c']
set script-interpreter := ['bash', '-euo', 'pipefail']
set default-list
set default-script

[doc('Upgrade one CloudNativePG cluster to a reviewed PostgreSQL major version')]
[group('cnpg')]
upgrade namespace app target:
    #!/usr/bin/env bash
    namespace={{ quote(namespace) }}
    app={{ quote(app) }}
    target={{ quote(target) }}

    fail() {
      gum log -t rfc3339 -s -l error "$1"
      exit 1
    }

    log() {
      gum log -t rfc3339 -s -l info "$1"
    }

    wait_backup() {
      local backup="$1"
      local phase
      local deadline=$((SECONDS + 1800))

      while (( SECONDS < deadline )); do
        phase="$(kubectl get backup.postgresql.cnpg.io "$backup" --namespace "$namespace" --output jsonpath='{.status.phase}')"
        case "${phase,,}" in
          completed)
            log "Backup $namespace/$backup completed"
            return
            ;;
          failed)
            fail "Backup $namespace/$backup failed"
            ;;
        esac
        sleep 10
      done

      fail "Timed out waiting for backup $namespace/$backup"
    }

    create_backup() {
      local purpose="$1"
      local backup="upgrade-${purpose}-$(date -u +%Y%m%d%H%M%S)"

      kubectl create --namespace "$namespace" --filename - <<EOF
    apiVersion: postgresql.cnpg.io/v1
    kind: Backup
    metadata:
      name: ${backup}
      namespace: ${namespace}
    spec:
      cluster:
        name: ${app}
      method: plugin
      pluginConfiguration:
        name: barman-cloud.cloudnative-pg.io
    EOF
      wait_backup "$backup"
    }

    (( ${#namespace} <= 63 )) && [[ "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "Invalid namespace"
    (( ${#app} <= 63 )) && [[ "$app" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "Invalid app"
    [[ "$target" =~ ^[1-9][0-9]*$ ]] || fail "PostgreSQL major must be numeric"

    repo_root="$(git rev-parse --show-toplevel)"
    cd "$repo_root"
    [[ "$(git branch --show-current)" == main ]] || fail "Run from the main branch"
    [[ -z "$(git status --porcelain)" ]] || fail "The working tree must be clean"

    log "Checking origin/main"
    git fetch origin main
    [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "Local main must equal origin/main"

    app_dir="kubernetes/apps/$namespace/$app"
    ks="$app_dir/ks.yaml"
    catalog="kubernetes/apps/database/cloudnative-pg/app/clusterimagecatalog.yaml"
    [[ -f "$ks" ]] || fail "Missing $ks"
    [[ -f "$app_dir/app/kustomization.yaml" ]] || fail "Missing $app_dir/app/kustomization.yaml"

    current="$(yq -r '.spec.postBuild.substitute.PG_MAJOR // ""' "$ks")"
    [[ "$current" =~ ^[0-9]+$ ]] || fail "$ks must explicitly pin spec.postBuild.substitute.PG_MAJOR"
    [[ "$(yq -r '.spec.prune' "$ks")" == false ]] || fail "$ks must disable Flux pruning for the database bundle"
    (( target > current )) || fail "Target major must be greater than current major $current"

    current_image="$(yq -r ".spec.images[] | select(.major == $current) | .image" "$catalog")"
    target_image="$(yq -r ".spec.images[] | select(.major == $target) | .image" "$catalog")"
    [[ -n "$current_image" && "$current_image" != null ]] || fail "PostgreSQL $current is absent from the reviewed catalog"
    [[ -n "$target_image" && "$target_image" != null ]] || fail "PostgreSQL $target is absent from the reviewed catalog"
    [[ "$current_image" == *-minimal-trixie@sha256:* && "$target_image" == *-minimal-trixie@sha256:* ]] || fail "Catalog images must use minimal-trixie and immutable digests"

    sslmode="$(yq -r '.spec.postBuild.substitute.PG_SSLMODE // "verify-full"' "$ks")"
    case "$sslmode" in
      verify-full|verify-ca|require) ;;
      *) fail "PG_SSLMODE must be verify-full, verify-ca, or require" ;;
    esac

    log "Checking all CloudNativePG clusters"
    clusters="$(kubectl get clusters.postgresql.cnpg.io --all-namespaces --output json)"
    jq -e 'all(.items[];
      .status.readyInstances == .spec.instances and
      any(.status.conditions[]?; .type == "Ready" and .status == "True") and
      (.status.targetPgDataImageInfo == null)
    )' <<<"$clusters" >/dev/null || fail "Another CloudNativePG cluster is unhealthy or upgrading"

    cluster="$(kubectl get cluster.postgresql.cnpg.io "$app" --namespace "$namespace" --output json)"
    live_major="$(jq -r '.status.pgDataImageInfo.majorVersion // empty' <<<"$cluster")"
    [[ "$live_major" == "$current" ]] || fail "Live major $live_major does not match Git major $current"

    primary="$(kubectl get pods --namespace "$namespace" --selector "cnpg.io/cluster=$app,cnpg.io/instanceRole=primary" --output jsonpath='{.items[0].metadata.name}')"
    [[ -n "$primary" ]] || fail "Primary pod not found"

    extensions="$(kubectl exec --namespace "$namespace" "$primary" --container postgres -- psql --quiet --tuples-only --no-align --dbname postgres --command "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1")"
    [[ -z "$extensions" ]] || fail "Non-default extensions require manual compatibility review: $extensions"

    read -r blocks used available capacity mountpoint < <(kubectl exec --namespace "$namespace" "$primary" --container postgres -- df -Pk /var/lib/postgresql/data | awk 'NR == 2 {print $2, $3, $4, $5, $6}')
    usage="${capacity%%%}"
    (( usage < 80 )) || fail "PostgreSQL data volume is ${capacity} full"
    (( available >= 1048576 )) || fail "PostgreSQL data volume has less than 1 GiB free"

    log "Creating pre-upgrade backup"
    create_backup pre

    yq -i ".spec.postBuild.substitute.PG_MAJOR = \"$target\"" "$ks"
    mise exec node@24.19.0 oxfmt@0.63.0 -- oxfmt "$ks"
    git diff --check -- "$ks"

    rendered="$(mktemp)"
    pushed=0
    cleanup() {
      rm -f -- "$rendered"
    }
    report_failure() {
      if (( pushed )); then
        gum log -t rfc3339 -s -l error "Upgrade failed after push; no automatic rollback was attempted"
      fi
    }
    trap cleanup EXIT
    trap report_failure ERR

    log "Rendering and server-side dry-running the upgrade"
    flux build kustomization "$app" \
      --namespace "$namespace" \
      --path "$app_dir/app" \
      --kustomization-file "$ks" \
      --strict-substitute >"$rendered"
    yq -r 'select(.kind != null) | [.kind, .metadata.namespace, .metadata.name] | @tsv' "$rendered"
    kubectl apply --server-side --dry-run=server --namespace "$namespace" --filename "$rendered" >/dev/null

    phrase="upgrade $namespace/$app from $current to $target"
    confirmation="$(gum input --prompt "Type '$phrase' to continue: ")"
    [[ "$confirmation" == "$phrase" ]] || fail "Confirmation did not match"

    git add -- "$ks"
    git diff --cached --check
    git commit -m "chore($namespace): upgrade $app to PostgreSQL $target"
    git push origin main
    pushed=1

    log "Reconciling $namespace/$app"
    flux reconcile kustomization "$app" --namespace "$namespace" --with-source

    deadline=$((SECONDS + 3600))
    observed_upgrade=0
    while (( SECONDS < deadline )); do
      cluster="$(kubectl get cluster.postgresql.cnpg.io "$app" --namespace "$namespace" --output json)"
      live_major="$(jq -r '.status.pgDataImageInfo.majorVersion // empty' <<<"$cluster")"
      target_status="$(jq -r '.status.targetPgDataImageInfo.majorVersion // empty' <<<"$cluster")"
      [[ -n "$target_status" || "$live_major" == "$target" ]] && observed_upgrade=1

      if (( observed_upgrade )) && [[ "$live_major" == "$target" ]] && \
        jq -e '.status.targetPgDataImageInfo == null and
          .status.readyInstances == .spec.instances and
          any(.status.conditions[]?; .type == "Ready" and .status == "True")' <<<"$cluster" >/dev/null; then
        break
      fi
      sleep 10
    done
    (( observed_upgrade )) || fail "The operator never observed the requested upgrade"
    [[ "$live_major" == "$target" ]] || fail "Timed out waiting for PostgreSQL $target"

    kubectl wait pods --namespace "$namespace" --selector "app.kubernetes.io/name=$app,!cnpg.io/cluster" --for condition=Ready --timeout 10m
    kubectl wait kustomization.kustomize.toolkit.fluxcd.io "$app" --namespace "$namespace" --for condition=Ready --timeout 10m

    log "Creating post-upgrade backup"
    create_backup post
    log "Upgrade complete at $(git rev-parse HEAD)"
