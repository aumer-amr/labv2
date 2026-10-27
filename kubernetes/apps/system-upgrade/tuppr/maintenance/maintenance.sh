#!/bin/sh
set -eu

fail() { echo "Minecraft maintenance failed: $*" >&2; exit 1; }
k() { kubectl --namespace home --request-timeout=20s --cache-dir=/tmp/kube-cache "$@"; }
selector='app.kubernetes.io/name=minecraft,app.kubernetes.io/instance=minecraft,app.kubernetes.io/controller=minecraft'
directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

read_deployment() {
    fields=$(k get deployment minecraft -o 'jsonpath={.metadata.uid}|{.metadata.resourceVersion}|{.spec.replicas}|{.metadata.annotations.maintenance\.home\.arpa/tuppr-run}|{.metadata.annotations.maintenance\.home\.arpa/tuppr-replicas}')
    IFS='|' read -r deployment_uid resource_version replicas saved_run saved_replicas <<EOF
$fields
EOF
    [ -n "$deployment_uid" ] && [ -n "$resource_version" ] || fail "missing deployment identity"
    case "$replicas" in 0|1) ;; *) fail "expected zero or one replica" ;; esac
    if [ -n "$saved_run" ]; then
        case "$saved_replicas" in 0|1) ;; *) fail "invalid replica recovery state" ;; esac
    else
        [ -z "$saved_replicas" ] || fail "incomplete recovery state"
    fi
}

wait_stopped() {
    attempt=0
    while [ "$attempt" -lt 180 ]; do
        pods=$(k get pods --selector "$selector" -o 'jsonpath={.items[*].metadata.uid}')
        [ -n "$pods" ] || return 0
        attempt=$((attempt + 1))
        sleep 1
    done
    fail "Minecraft pods did not terminate"
}

case "${1:-}" in pre|post) ;; *) fail "expected pre or post" ;; esac
# Tuppr can complete a no-op run without startedAt; recovery must still work.
run=$(kubectl --namespace system-upgrade --request-timeout=20s --cache-dir=/tmp/kube-cache get talosupgrade talos -o 'jsonpath={.metadata.uid}:{.metadata.generation}')
case "$run" in ''|*:|*[!a-zA-Z0-9:._-]*) fail "missing or invalid upgrade run identity" ;; esac
read_deployment
[ -z "$saved_run" ] || [ "$saved_run" = "$run" ] || fail "another upgrade owns recovery state"

if [ "$1" = pre ]; then
    if [ -n "$saved_run" ]; then
        [ "$replicas" = 0 ] || fail "Minecraft restarted during maintenance"
        wait_stopped
        exit 0
    fi
    original_uid=$deployment_uid
    original_version=$resource_version
    if [ "$replicas" = 1 ]; then
        # Check every matching pod; an extra or terminating pod must block the gate.
        pods=$(k get pods --selector "$selector" -o 'jsonpath={range .items[*]}{.metadata.name}|{.metadata.uid}|{.metadata.deletionTimestamp}|{.status.containerStatuses[?(@.name=="app")].ready}|{.status.containerStatuses[?(@.name=="app")].restartCount}{"\n"}{end}')
        [ "$(printf '%s\n' "$pods" | wc -l)" -eq 1 ] || fail "expected one Minecraft pod"
        IFS='|' read -r pod pod_uid deleting ready restarts <<EOF
$pods
EOF
        [ -n "$pod_uid" ] && [ -z "$deleting" ] && [ "$ready" = true ] || fail "Minecraft pod is not Ready"
        result=$(k exec "$pod" --container app -- timeout 90 sh -c "$(cat "$directory/save.sh")")
        [ "$result" = MINECRAFT_SAVE_CONFIRMED ] || fail "save completion not confirmed"
        current=$(k get pod "$pod" -o 'jsonpath={.metadata.uid}|{.metadata.deletionTimestamp}|{.status.containerStatuses[?(@.name=="app")].ready}|{.status.containerStatuses[?(@.name=="app")].restartCount}')
        [ "$current" = "$pod_uid||true|$restarts" ] || fail "Minecraft pod changed during save"
    else
        wait_stopped
    fi
    # Record recovery state and stop atomically; concurrent deployment changes fail closed.
    k patch deployment minecraft --type merge --patch "{\"metadata\":{\"resourceVersion\":\"$original_version\",\"annotations\":{\"maintenance.home.arpa/tuppr-run\":\"$run\",\"maintenance.home.arpa/tuppr-replicas\":\"$replicas\"}},\"spec\":{\"replicas\":0}}" >/dev/null
    wait_stopped
    read_deployment
    [ "$deployment_uid" = "$original_uid" ] && [ "$replicas" = 0 ] && [ "$saved_run" = "$run" ] || fail "deployment changed while stopping"
    echo "Minecraft saved and stopped; recovery state is on home/deployment/minecraft"
else
    [ -n "$saved_run" ] || exit 0
    case "$saved_replicas" in 0|1) ;; *) fail "invalid replica recovery state" ;; esac
    [ "$replicas" = 0 ] || [ "$replicas" = "$saved_replicas" ] || fail "replicas changed outside maintenance"
    k patch deployment minecraft --type merge --patch "{\"metadata\":{\"resourceVersion\":\"$resource_version\"},\"spec\":{\"replicas\":$saved_replicas}}" >/dev/null
    if [ "$saved_replicas" = 1 ]; then
        k rollout status deployment/minecraft --timeout=35m --request-timeout=36m
    else
        wait_stopped
    fi
    read_deployment
    [ "$saved_run" = "$run" ] && [ "$replicas" = "$saved_replicas" ] || fail "deployment changed while restoring"
    k patch deployment minecraft --type merge --patch "{\"metadata\":{\"resourceVersion\":\"$resource_version\",\"annotations\":{\"maintenance.home.arpa/tuppr-run\":null,\"maintenance.home.arpa/tuppr-replicas\":null}}}" >/dev/null
    echo "Minecraft restored"
fi
