set no-exit-message
set quiet
set shell := ['bash', '-euo', 'pipefail', '-c']
set script-interpreter := ['bash', '-euo', 'pipefail']
set default-list
set default-script

kubernetes_dir := justfile_dir() + '/kubernetes'

[doc('Bootstrap the Talos cluster')]
[group('bootstrap')]
talos: talos-secret talos-apply talos-talosconfig talos-kubeconfig

[doc('Bootstrap apps into the Talos cluster')]
[group('bootstrap')]
apps: apps-ready apps-namespaces apps-secrets apps-secrets-op apps-crds apps-helm
    just log info "Cluster is bootstrapped — Flux will start syncing the Git repository"

# No sops call or existence guard is needed: the bundle is stored already
# encrypted with the repo's age recipient, and existing bundles are left
# untouched.
[private]
[working-directory('../talos')]
talos-secret:
    just log info "Generating secrets" stage "{{ recipe_name() }}"
    topf secrets --confirm=false > /dev/null

[private]
[working-directory('../talos')]
talos-apply:
    just log info "Applying talos config and bootstrapping" stage "{{ recipe_name() }}"
    topf apply --auto-bootstrap --confirm=false

[private]
[working-directory('../talos')]
talos-talosconfig:
    just log info "Generating talosconfig" stage "{{ recipe_name() }}"
    topf talosconfig > talosconfig

# topf issues short-lived admin certs by default; 8760h keeps the
# kubeconfig usable long-term.
[private]
[working-directory('../talos')]
talos-kubeconfig:
    just log info "Fetching kubeconfig" stage "{{ recipe_name() }}"
    topf kubeconfig --validity 8760h > "{{ justfile_dir() }}/kubeconfig"

[private]
apps-crds:
    just log info "Applying CRDs" stage "{{ recipe_name() }}"
    if ! helmfile --file "{{ source_directory() }}/helmfile/crds.yaml" template --quiet | yq eval-all --exit-status 'select(.kind == "CustomResourceDefinition")' | kubectl apply --server-side --force-conflicts --filename -; then
        just log fatal "Failed to apply crds"
    fi

[private]
apps-helm:
    just log info "Syncing helmfile" stage "{{ recipe_name() }}"
    if ! helmfile --file "{{ source_directory() }}/helmfile/apps.yaml" sync --hide-notes; then
        just log fatal "Failed to sync helmfile"
    fi

[private]
apps-namespaces:
    just log info "Applying namespaces for apps" stage "{{ recipe_name() }}"
    for app in "{{ kubernetes_dir }}/apps"/*/; do
        ns="$(basename "$app")"
        if kubectl create namespace "$ns" --dry-run=client -o yaml \
                | kubectl apply --server-side --filename - &>/dev/null; then
            just log info "Namespace applied" namespace "$ns"
        else
            just log fatal "Failed to apply namespace" namespace "$ns"
        fi
    done

[private]
apps-secrets:
    just log info "Applying secrets for apps" stage "{{ recipe_name() }}"
    sync_url="$(yq --exit-status '.spec.values.instance.sync.url' "{{ kubernetes_dir }}/apps/flux-system/flux-instance/app/helmrelease.yaml")"
    secrets=()
    case "$sync_url" in
        ssh://*) secrets+=("{{ source_directory() }}/deploy-key.sops.yaml") ;;
        https://*|http://*) ;;
        *) just log fatal "Unsupported Flux Git source URL scheme" ;;
    esac
    secrets+=(
        "{{ source_directory() }}/sops-age.sops.yaml"
        "{{ kubernetes_dir }}/components/sops/cluster-secrets.sops.yaml"
    )
    for secret in "${secrets[@]}"; do
        name="$(basename "$secret" .sops.yaml)"
        if sops decrypt "$secret" \
                | kubectl --namespace flux-system apply --server-side --filename - &>/dev/null; then
            just log info "Secret applied" resource "$name"
        else
            just log fatal "Failed to apply secret" resource "$name"
        fi
    done

[private]
apps-secrets-op:
    just log info "Injecting 1Password secrets for apps" stage "{{ recipe_name() }}"
    if ! kustomize build {{ source_directory() }}/kustomize | just kustomize - | kubectl apply --server-side --force-conflicts -f -; then
        just log fatal "Failed to apply kustomize manifests" "stage" "{{ recipe_name() }}"
    fi

# Nodes can register before the CNI makes them Ready.
[private]
apps-ready:
    just log info "Waiting for a node to register" stage "{{ recipe_name() }}"
    deadline=$((SECONDS + 600))
    while (( (remaining = deadline - SECONDS) > 0 )); do
        if nodes="$(kubectl get nodes --output=name --request-timeout="$((remaining < 10 ? remaining : 10))s" 2>/dev/null)" && [[ -n "$nodes" ]]; then
            exit 0
        fi
        remaining=$((deadline - SECONDS))
        if (( remaining > 0 )); then
            sleep "$((remaining < 5 ? remaining : 5))"
        fi
    done
    just log fatal "Timed out waiting for a node to register"
