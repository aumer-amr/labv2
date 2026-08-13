codex() {
  local repo_root
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"

  if [[ "$repo_root" == "/mnt/e/code/private/labv2" ]]; then
    "$repo_root/scripts/codex-private" "$@"
  else
    command codex "$@"
  fi
}
