#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
topf_file="${1:-${repo_root}/talos/topf.yaml}"
talos_upgrade_file="${2:-${repo_root}/kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml}"
kubernetes_upgrade_file="${3:-${repo_root}/kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetesupgrade.yaml}"

read_topf_version() {
  awk -v key="$2" '$1 == key ":" { print $2; exit }' "$1"
}

read_upgrade_version() {
  awk '$1 == "version:" { print $2; exit }' "$1"
}

fail() {
  echo "Talos/Kubernetes compatibility check failed: $*" >&2
  exit 1
}

talos_version="$(read_topf_version "$topf_file" talosVersion)"
kubernetes_version="$(read_topf_version "$topf_file" kubernetesVersion)"
talos_upgrade_version="$(read_upgrade_version "$talos_upgrade_file")"
kubernetes_upgrade_version="$(read_upgrade_version "$kubernetes_upgrade_file")"

[[ -n "$talos_version" && -n "$kubernetes_version" ]] || fail "missing version in ${topf_file}"
[[ "$talos_version" == "$talos_upgrade_version" ]] || fail "Talos versions differ: ${talos_version} != ${talos_upgrade_version}"
[[ "$kubernetes_version" == "$kubernetes_upgrade_version" ]] || fail "Kubernetes versions differ: ${kubernetes_version} != ${kubernetes_upgrade_version}"

IFS=. read -r talos_major talos_minor _ <<<"${talos_version#v}"
IFS=. read -r kubernetes_major kubernetes_minor _ <<<"${kubernetes_version#v}"
[[ "$talos_major" =~ ^[0-9]+$ && "$talos_minor" =~ ^[0-9]+$ ]] || fail "invalid Talos version ${talos_version}"
[[ "$kubernetes_major" =~ ^[0-9]+$ && "$kubernetes_minor" =~ ^[0-9]+$ ]] || fail "invalid Kubernetes version ${kubernetes_version}"

# Add one entry per Talos minor after checking:
# https://docs.siderolabs.com/talos/vX.Y/getting-started/support-matrix
case "${talos_major}.${talos_minor}" in
  1.13)
    kubernetes_min_minor=31
    kubernetes_max_minor=36
    ;;
  *)
    fail "no matrix entry for Talos ${talos_major}.${talos_minor}; verify the official support matrix, then add its Kubernetes minor range and update Renovate's ceiling"
    ;;
esac

if ((kubernetes_major != 1 || kubernetes_minor < kubernetes_min_minor || kubernetes_minor > kubernetes_max_minor)); then
  fail "Talos ${talos_major}.${talos_minor} supports Kubernetes 1.${kubernetes_min_minor}-1.${kubernetes_max_minor}, got ${kubernetes_version}"
fi

echo "Talos ${talos_version} and Kubernetes ${kubernetes_version} are compatible"
