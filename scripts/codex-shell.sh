codex() {
  local repo_root remote
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  remote="$(git config --get remote.origin.url 2>/dev/null || true)"

  if [[ -n "$repo_root" && -x "$repo_root/scripts/codex-private" &&
    ( "$remote" == "https://github.com/aumer-amr/labv2.git" || "$remote" == "git@github.com:aumer-amr/labv2.git" ) ]]; then
    "$repo_root/scripts/codex-private" "$@"
  else
    command codex "$@"
  fi
}
