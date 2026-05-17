#!/usr/bin/env bash
set -euo pipefail

# Optional helper for shared mirror deployments. Override the example path with
# ORCHESTRA_MIRROR_DIR when your mirror lives elsewhere.
mirror_dir="${ORCHESTRA_MIRROR_DIR:-/Users/Shared/orchestra}"
skip_python=0

for arg in "$@"; do
  case "$arg" in
    --skip-python) skip_python=1 ;;
    -h|--help)
      echo "Usage: ORCHESTRA_MIRROR_DIR=/path/to/orchestra pull_orchestra_mirror.sh [--skip-python]"
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -d "$mirror_dir/.git" ]]; then
  echo "Mirror repo not found at $mirror_dir" >&2
  exit 1
fi

git -C "$mirror_dir" pull --ff-only

if [[ "$skip_python" -eq 0 ]]; then
  ORCHESTRA_DIR="$mirror_dir" "$mirror_dir/shared_scripts/bootstrap-python-env.sh"
fi
