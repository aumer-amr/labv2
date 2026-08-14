set quiet
set minimum-version := '1.55.1'
set default-list
set default-script
set shell := ['bash', '-euo', 'pipefail', '-c']
set script-interpreter := ['bash', '-euo', 'pipefail']

[group('bootstrap')]
mod? bootstrap 'bootstrap'

[group('kubernetes')]
mod? kube 'kubernetes'

[group('database')]
mod? cnpg 'kubernetes/apps/database'

[group('talos')]
mod? talos 'talos'

[private]
log lvl msg *args:
    gum log -t rfc3339 -s -l "{{ lvl }}" "{{ msg }}" {{ args }}

[private]
kustomize file *args:
    minijinja-cli "{{ file }}" {{ args }} | op inject
