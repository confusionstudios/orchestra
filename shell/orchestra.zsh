# Optional zsh helpers for running Orchestra from a work repository.
#
# Source this file from ~/.zshrc after ORCHESTRA_DIR is set:
#
#   source "$ORCHESTRA_DIR/shell/orchestra.zsh"
#
# The commands below intentionally require the current directory to be the root
# of a git repository. Orchestra uses the launch directory as the work repo, so
# refusing nested directories avoids accidentally supervising the wrong project.

_orchestra_physical_path() {
  emulate -L zsh
  (builtin cd -P -- "$1" 2>/dev/null && pwd -P)
}

_orchestra_require_git_root() {
  emulate -L zsh

  local git_root physical_root physical_pwd

  git_root="$(command git rev-parse --show-toplevel 2>/dev/null)" || {
    print -u2 "orchestra: run this from the root of a git work repository"
    return 2
  }

  physical_root="$(_orchestra_physical_path "$git_root")" || {
    print -u2 "orchestra: could not resolve git repository root: $git_root"
    return 2
  }
  physical_pwd="$(pwd -P)"

  if [[ "$physical_pwd" != "$physical_root" ]]; then
    print -u2 "orchestra: run this from the git repository root:"
    print -u2 "  $physical_root"
    return 2
  fi
}

_orchestra_run_from_git_root() {
  emulate -L zsh

  local command_name="$1"
  shift

  _orchestra_require_git_root || return

  if [[ -z "${ORCHESTRA_DIR:-}" ]]; then
    print -u2 "orchestra: ORCHESTRA_DIR is not set"
    return 2
  fi

  if [[ ! -x "$ORCHESTRA_DIR/bin/$command_name" ]]; then
    print -u2 "orchestra: expected executable not found: $ORCHESTRA_DIR/bin/$command_name"
    return 2
  fi

  "$ORCHESTRA_DIR/bin/$command_name" "$@"
}

orchestra() {
  _orchestra_run_from_git_root ko-orchestrator "$@"
}

orchestra-dashboard() {
  _orchestra_run_from_git_root ko-dashboard "$@"
}

orchestra-fleet() {
  if [[ -z "${ORCHESTRA_DIR:-}" ]]; then
    print -u2 "orchestra: ORCHESTRA_DIR is not set"
    return 2
  fi
  "$ORCHESTRA_DIR/bin/ko-fleet" "$@"
}

orchestra-status() {
  _orchestra_run_from_git_root ko-get-update "$@"
}

orchestra-start() {
  print -u2 "orchestra-start is deprecated; use 'orchestra' from the repo root."
  _orchestra_run_from_git_root ko-orchestrator "$@"
}

orchestra-stop() {
  print -u2 "orchestra-stop is deprecated; stop the foreground orchestrator with Ctrl-C, touch KANBAN_ORCHESTRATOR_STOP_AFTER_TASK in the repo, or use 'orchestra-fleet stop <repo>'."
  return 2
}

orchestra-break() {
  print -u2 "orchestra-break was removed with the process-manager UI; inspect the blocked task and use ko-task directly."
  return 2
}

alias odash='orchestra-dashboard'
alias ofleet='orchestra-fleet'
alias ostatus='orchestra-status'
