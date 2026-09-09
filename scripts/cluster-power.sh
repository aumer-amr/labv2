#!/usr/bin/env bash
set -euo pipefail

readonly STATE_FILE=".private/cluster-power-state.json"
readonly ROOK_NAMESPACE="rook-ceph"

log() {
  gum log -t rfc3339 -s -l info "$1"
}

fail() {
  gum log -t rfc3339 -s -l error "$1"
  exit 1
}

set_phase() {
  local phase="$1" temporary
  temporary="$(mktemp .private/cluster-power-state.XXXXXX)"
  jq --arg phase "$phase" '.phase = $phase' "$STATE_FILE" >"$temporary"
  mv -- "$temporary" "$STATE_FILE"
}

wait_for_no_ceph_clients() {
  local deadline=$((SECONDS + 600)) clients

  while ((SECONDS < deadline)); do
    clients="$(kubectl get pods --all-namespaces --output json | jq -r '
      [.items[]
        | select(.metadata.namespace != "rook-ceph")
        | select(any(.spec.volumes[]?; .persistentVolumeClaim != null))
        | [.metadata.namespace, .metadata.name] | @tsv] | .[]')"
    [[ -z "$clients" ]] && return
    sleep 5
  done

  printf '%s\n' "$clients" >&2
  fail "Ceph client pods remain"
}

scale_applications_to_zero() {
  local mode="${1:-all}" namespace name

  while IFS=$'\t' read -r namespace name; do
    [[ -n "$namespace" ]] || continue
    kubectl scale "deployment/$name" --namespace "$namespace" --replicas 0
  done < <(jq -r --arg mode "$mode" '.workloads.deployment[] | select($mode == "all" or .namespace != "database") | [.namespace, .name] | @tsv' "$STATE_FILE")
  while IFS=$'\t' read -r namespace name; do
    [[ -n "$namespace" ]] || continue
    kubectl scale "statefulset/$name" --namespace "$namespace" --replicas 0
  done < <(jq -r --arg mode "$mode" '.workloads.statefulset[] | select($mode == "all" or .namespace != "database") | [.namespace, .name] | @tsv' "$STATE_FILE")
}

wait_for_cnpg_hibernation() {
  local namespace="$1" name="$2" deadline=$((SECONDS + 2100))

  while ((SECONDS < deadline)); do
    if [[ "$(kubectl get pods --namespace "$namespace" --selector "cnpg.io/cluster=$name" --output json | jq '.items | length')" == 0 ]]; then
      return
    fi
    sleep 5
  done

  fail "Timed out hibernating $namespace/$name"
}

wait_for_cnpg_ready() {
  local namespace="$1" name="$2" deadline=$((SECONDS + 900)) cluster

  while ((SECONDS < deadline)); do
    cluster="$(kubectl get cluster.postgresql.cnpg.io "$name" --namespace "$namespace" --output json)"
    if jq -e '
      .status.readyInstances == .spec.instances and
      any(.status.conditions[]?; .type == "Ready" and .status == "True")
    ' <<<"$cluster" >/dev/null; then
      return
    fi
    sleep 5
  done

  fail "Timed out resuming $namespace/$name"
}

wait_for_ceph() {
  local deadline=$((SECONDS + 900)) status

  while ((SECONDS < deadline)); do
    if status="$(kubectl exec --namespace "$ROOK_NAMESPACE" deploy/rook-ceph-tools -- ceph status --format json 2>/dev/null)" &&
      jq -e '.health.status == "HEALTH_OK" and all(.pgmap.pgs_by_state[]; .state_name == "active+clean")' <<<"$status" >/dev/null; then
      return
    fi
    sleep 10
  done

  fail "Timed out waiting for Ceph HEALTH_OK with all PGs active+clean"
}

wait_for_ceph_with_noout() {
  local deadline=$((SECONDS + 900)) status

  while ((SECONDS < deadline)); do
    if status="$(kubectl exec --namespace "$ROOK_NAMESPACE" deploy/rook-ceph-tools -- ceph status --format json 2>/dev/null)" &&
      jq -e '
        .pgmap.num_pgs > 0 and
        all(.pgmap.pgs_by_state[]; .state_name == "active+clean") and
        ((.health.checks // {}) | keys | all(
          . == "OSDMAP_FLAGS" or
          . == "AUTH_INSECURE_CLIENT_KEY_TYPE" or
          . == "AUTH_INSECURE_KEYS_ALLOWED" or
          . == "AUTH_INSECURE_KEYS_CREATABLE" or
          . == "AUTH_INSECURE_ROTATING_SERVICE_KEY_TYPE"
        ))
      ' <<<"$status" >/dev/null; then
      return
    fi
    sleep 10
  done

  fail "Timed out waiting for Ceph to recover with only the expected noout warning"
}

wait_for_rook_group() {
  local pattern="$1" target="$2" deadline=$((SECONDS + 600)) name replicas current all_ready

  while ((SECONDS < deadline)); do
    all_ready=1
    while IFS=$'\t' read -r name replicas; do
      [[ -n "$name" ]] || continue
      if [[ "$target" == zero ]]; then
        current="$(kubectl get "deployment/$name" --namespace "$ROOK_NAMESPACE" --output json | jq '.status.replicas // 0')"
        [[ "$current" == 0 ]] || all_ready=0
      else
        current="$(kubectl get "deployment/$name" --namespace "$ROOK_NAMESPACE" --output json | jq '.status.readyReplicas // 0')"
        [[ "$current" == "$replicas" ]] || all_ready=0
      fi
    done < <(jq -r --arg pattern "$pattern" '.rookDeployments[] | select(.name | test($pattern)) | [.name, (.replicas | tostring)] | @tsv' "$STATE_FILE")
    ((all_ready)) && return
    sleep 5
  done

  fail "Timed out waiting for Rook group $pattern to reach $target"
}

scale_rook_group_to_zero() {
  local pattern="$1" name

  while read -r name; do
    [[ -n "$name" ]] || continue
    kubectl scale "deployment/$name" --namespace "$ROOK_NAMESPACE" --replicas 0
  done < <(jq -r --arg pattern "$pattern" '.rookDeployments[] | select(.name | test($pattern)) | .name' "$STATE_FILE")
  wait_for_rook_group "$pattern" zero
}

scale_from_state() {
  local section="$1" namespace name replicas

  while IFS=$'\t' read -r namespace name replicas; do
    [[ -n "$namespace" ]] || continue
    kubectl scale "$section/$name" --namespace "$namespace" --replicas "$replicas"
  done < <(jq -r --arg section "$section" '.workloads[$section][] | [.namespace, .name, (.replicas | tostring)] | @tsv' "$STATE_FILE")
}

patch_flux() {
  local section="$1" suspend="$2" namespace name resource
  [[ "$section" == kustomizations ]] && resource=kustomization.kustomize.toolkit.fluxcd.io || resource=helmrelease.helm.toolkit.fluxcd.io

  while IFS=$'\t' read -r namespace name; do
    [[ -n "$namespace" ]] || continue
    kubectl patch "$resource" "$name" --namespace "$namespace" --type merge --patch "{\"spec\":{\"suspend\":$suspend}}" >/dev/null
  done < <(jq -r --arg section "$section" '.flux[$section][] | [.namespace, .name] | @tsv' "$STATE_FILE")
}

preflight() {
  local ceph clusters backups

  kubectl get nodes >/dev/null
  [[ "$(kubectl get nodes --output json | jq '[.items[] | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))] | length')" == 3 ]] ||
    fail "Expected three Ready nodes"

  ceph="$(kubectl exec --namespace "$ROOK_NAMESPACE" deploy/rook-ceph-tools -- ceph status --format json)"
  jq -e '.health.status == "HEALTH_OK" and all(.pgmap.pgs_by_state[]; .state_name == "active+clean")' <<<"$ceph" >/dev/null ||
    fail "Ceph must be HEALTH_OK with all PGs active+clean"

  clusters="$(kubectl get clusters.postgresql.cnpg.io --all-namespaces --output json)"
  jq -e 'all(.items[];
    .status.readyInstances == .spec.instances and
    any(.status.conditions[]?; .type == "Ready" and .status == "True")
  )' <<<"$clusters" >/dev/null || fail "Every CloudNativePG cluster must be healthy"

  backups="$(kubectl get backups.postgresql.cnpg.io --all-namespaces --output json)"
  jq -e 'all(.items[]; (.status.phase | ascii_downcase) == "completed" or (.status.phase | ascii_downcase) == "failed")' <<<"$backups" >/dev/null ||
    fail "A CloudNativePG backup is still running"
}

capture_state() {
  local deployments statefulsets clusters kustomizations helmreleases temporary

  mkdir -p .private
  umask 077
  deployments="$(kubectl get deployments --all-namespaces --output json | jq '[
    .items[]
    | select(.metadata.namespace != "kube-system" and .metadata.namespace != "flux-system" and .metadata.namespace != "rook-ceph")
    | {namespace: .metadata.namespace, name: .metadata.name, replicas: (.spec.replicas // 1)}]')"
  statefulsets="$(kubectl get statefulsets --all-namespaces --output json | jq '[
    .items[]
    | select(.metadata.namespace != "kube-system" and .metadata.namespace != "flux-system" and .metadata.namespace != "rook-ceph")
    | {namespace: .metadata.namespace, name: .metadata.name, replicas: (.spec.replicas // 1)}]')"
  clusters="$(kubectl get clusters.postgresql.cnpg.io --all-namespaces --output json | jq '[
    .items[] | select(.metadata.annotations["cnpg.io/hibernation"] != "on")
    | {namespace: .metadata.namespace, name: .metadata.name}]')"
  kustomizations="$(kubectl get kustomizations.kustomize.toolkit.fluxcd.io --all-namespaces --output json | jq '[
    .items[] | select(.spec.suspend != true) | {namespace: .metadata.namespace, name: .metadata.name}]')"
  helmreleases="$(kubectl get helmreleases.helm.toolkit.fluxcd.io --all-namespaces --output json | jq '[
    .items[] | select(.spec.suspend != true) | {namespace: .metadata.namespace, name: .metadata.name}]')"

  temporary="$(mktemp .private/cluster-power-state.XXXXXX)"
  jq -n \
    --arg createdAt "$(date -u +%FT%TZ)" \
    --argjson deployments "$deployments" \
    --argjson statefulsets "$statefulsets" \
    --argjson clusters "$clusters" \
    --argjson kustomizations "$kustomizations" \
    --argjson helmreleases "$helmreleases" \
    '{
      version: 1,
      createdAt: $createdAt,
      phase: "captured",
      flux: {kustomizations: $kustomizations, helmreleases: $helmreleases},
      cnpgClusters: $clusters,
      workloads: {deployment: $deployments, statefulset: $statefulsets},
      rookDeployments: []
    }' >"$temporary"
  mv -- "$temporary" "$STATE_FILE"
}

shutdown_cluster() {
  local namespace name rook_deployments nodes phase

  if [[ -e "$STATE_FILE" && "$(jq -r '.phase' "$STATE_FILE")" == complete ]]; then
    mv -- "$STATE_FILE" .private/cluster-power-state.last.json
  fi

  if [[ ! -e "$STATE_FILE" ]]; then
    preflight
    capture_state
    log "Recovery state saved to $STATE_FILE"
  fi

  phase="$(jq -r '.phase' "$STATE_FILE")"
  [[ "$phase" != complete && "$phase" != shutdown-requested ]] || fail "Cannot continue shutdown from phase $phase"

  if [[ "$phase" == captured ]]; then
    kubectl patch kustomization.kustomize.toolkit.fluxcd.io flux-system --namespace flux-system --type merge --patch '{"spec":{"suspend":true}}' >/dev/null
    patch_flux helmreleases true
    patch_flux kustomizations true
    set_phase flux-suspended
    phase=flux-suspended
  fi

  if [[ "$phase" == flux-suspended ]]; then
    scale_applications_to_zero clients
    sleep 10
    scale_applications_to_zero clients

    while IFS=$'\t' read -r namespace name; do
      [[ -n "$namespace" ]] || continue
      kubectl annotate cluster.postgresql.cnpg.io "$name" --namespace "$namespace" cnpg.io/hibernation=on --overwrite >/dev/null
      wait_for_cnpg_hibernation "$namespace" "$name"
    done < <(jq -r '.cnpgClusters[] | [.namespace, .name] | @tsv' "$STATE_FILE")

    scale_applications_to_zero
    sleep 10
    scale_applications_to_zero
    wait_for_no_ceph_clients
    set_phase applications-stopped
    phase=applications-stopped
  fi

  if [[ "$phase" == applications-stopped ]]; then
    kubectl exec --namespace "$ROOK_NAMESPACE" deploy/rook-ceph-tools -- ceph osd set noout
    rook_deployments="$(kubectl get deployments --namespace "$ROOK_NAMESPACE" --output json | jq '[
      .items[] | {namespace: .metadata.namespace, name: .metadata.name, replicas: (.spec.replicas // 1)}]')"
    jq --argjson deployments "$rook_deployments" '.rookDeployments = $deployments' "$STATE_FILE" >"$STATE_FILE.tmp"
    mv -- "$STATE_FILE.tmp" "$STATE_FILE"
    set_phase rook-captured
    phase=rook-captured
  fi

  if [[ "$phase" == rook-captured ]]; then
    scale_rook_group_to_zero '^rook-ceph-operator$'
    for pattern in \
      'rook-ceph.rbd.csi.ceph.com-ctrlplugin|rook-ceph.cephfs.csi.ceph.com-ctrlplugin|ceph-csi-controller-manager' \
      'rook-ceph-mds' \
      'rook-ceph-osd' \
      'rook-ceph-mon' \
      'rook-ceph-mgr' \
      'rook-ceph-exporter|rook-ceph-crashcollector|rook-ceph-tools'; do
      scale_rook_group_to_zero "$pattern"
    done
    set_phase ready-to-power-off
    phase=ready-to-power-off
  fi

  if [[ "$phase" == ready-to-power-off ]]; then
    flux get kustomizations --all-namespaces >/dev/null
    kubectl get gateways.gateway.networking.k8s.io --namespace network >/dev/null
    kubectl get httproutes.gateway.networking.k8s.io --all-namespaces >/dev/null

    nodes="$(yq -r '[.nodes[].ip] | join(",")' talos/topf.yaml)"
    set_phase shutdown-requested
    log "Requesting graceful Talos shutdown for $nodes"
    talosctl shutdown --nodes "$nodes" --force --wait=false
    return
  fi

  fail "Unsupported shutdown phase $phase"
}

restore_rook_group() {
  local pattern="$1" name replicas
  while IFS=$'\t' read -r name replicas; do
    [[ -n "$name" ]] || continue
    kubectl scale "deployment/$name" --namespace "$ROOK_NAMESPACE" --replicas "$replicas"
  done < <(jq -r --arg pattern "$pattern" '.rookDeployments[] | select(.name | test($pattern)) | [.name, (.replicas | tostring)] | @tsv' "$STATE_FILE")
  wait_for_rook_group "$pattern" ready
}

startup_cluster() {
  local namespace name

  [[ -f "$STATE_FILE" ]] || fail "Missing $STATE_FILE"
  kubectl get nodes >/dev/null || fail "Kubernetes API is not ready; power on all three nodes first"

  restore_rook_group 'rook-ceph-mon'
  restore_rook_group 'rook-ceph-mgr|rook-ceph-osd'
  restore_rook_group 'rook-ceph-mds'
  restore_rook_group 'rook-ceph.rbd.csi.ceph.com-ctrlplugin|rook-ceph.cephfs.csi.ceph.com-ctrlplugin|ceph-csi-controller-manager|rook-ceph-exporter|rook-ceph-crashcollector|rook-ceph-tools'
  wait_for_ceph_with_noout
  restore_rook_group '^rook-ceph-operator$'
  kubectl exec --namespace "$ROOK_NAMESPACE" deploy/rook-ceph-tools -- ceph osd unset noout
  wait_for_ceph
  set_phase ceph-restored

  while IFS=$'\t' read -r namespace name; do
    [[ -n "$namespace" ]] || continue
    if [[ "$namespace" == database ]]; then
      replicas="$(jq -r --arg namespace "$namespace" --arg name "$name" '.workloads.deployment[] | select(.namespace == $namespace and .name == $name) | .replicas' "$STATE_FILE")"
      kubectl scale "deployment/$name" --namespace "$namespace" --replicas "$replicas"
    fi
  done < <(jq -r '.workloads.deployment[] | [.namespace, .name] | @tsv' "$STATE_FILE")
  while IFS=$'\t' read -r name; do
    [[ -n "$name" ]] || continue
    kubectl rollout status "deployment/$name" --namespace database --timeout 5m
  done < <(jq -r '.workloads.deployment[] | select(.namespace == "database") | [.name] | @tsv' "$STATE_FILE")

  while IFS=$'\t' read -r namespace name; do
    [[ -n "$namespace" ]] || continue
    kubectl annotate cluster.postgresql.cnpg.io "$name" --namespace "$namespace" cnpg.io/hibernation- >/dev/null
    wait_for_cnpg_ready "$namespace" "$name"
  done < <(jq -r '.cnpgClusters[] | [.namespace, .name] | @tsv' "$STATE_FILE")

  scale_from_state deployment
  scale_from_state statefulset
  set_phase applications-restored

  patch_flux helmreleases false
  if jq -e '.flux.kustomizations[] | select(.namespace == "network" and .name == "envoy-gateway")' "$STATE_FILE" >/dev/null; then
    kubectl patch kustomization.kustomize.toolkit.fluxcd.io envoy-gateway --namespace network --type merge --patch '{"spec":{"suspend":false}}' >/dev/null
    kubectl wait kustomization.kustomize.toolkit.fluxcd.io/envoy-gateway --namespace network --for=condition=Ready --timeout 10m
  fi
  patch_flux kustomizations false
  set_phase complete
  log "Cluster state restored; recovery record retained at $STATE_FILE"
}

show_plan() {
  preflight
  log "Preflight passed"
  kubectl get nodes
  kubectl exec --namespace "$ROOK_NAMESPACE" deploy/rook-ceph-tools -- ceph status
  kubectl get clusters.postgresql.cnpg.io --all-namespaces
  printf 'State file: %s\n' "$STATE_FILE"
  printf 'Talos nodes: %s\n' "$(yq -r '[.nodes[].ip] | join(",")' talos/topf.yaml)"
}

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

case "${1:-}" in
  plan) show_plan ;;
  shutdown) shutdown_cluster ;;
  startup) startup_cluster ;;
  status)
    [[ -f "$STATE_FILE" ]] && jq '{createdAt, phase}' "$STATE_FILE" || printf 'No recovery state recorded.\n'
    ;;
  *) fail "Usage: $0 {plan|shutdown|startup|status}" ;;
esac
