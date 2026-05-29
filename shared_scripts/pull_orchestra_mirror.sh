#!/usr/bin/env bash
set -euo pipefail

# Optional helper for shared mirror deployments. Override the example path with
# ORCHESTRA_MIRROR_DIR when your mirror lives elsewhere. The shared mirror is a
# staging checkout and is always advanced from origin/develop, regardless of
# whichever branch happened to be checked out before this script ran.
mirror_dir="${ORCHESTRA_MIRROR_DIR:-/Users/Shared/orchestra}"
remote_name="origin"
target_branch="develop"
skip_python=0

for arg in "$@"; do
  case "$arg" in
    --skip-python) skip_python=1 ;;
    -h|--help)
      echo "Usage: ORCHESTRA_MIRROR_DIR=/path/to/orchestra pull_orchestra_mirror.sh [--skip-python]"
      echo
      echo "Fetches origin/develop, checks out local develop, tracks origin/develop,"
      echo "and fast-forwards the mirror from that branch."
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -d "$mirror_dir/.git" ]]; then
  echo "Mirror repo not found at $mirror_dir" >&2
  exit 1
fi

git -C "$mirror_dir" fetch "$remote_name" "+refs/heads/$target_branch:refs/remotes/$remote_name/$target_branch"

if git -C "$mirror_dir" show-ref --verify --quiet "refs/heads/$target_branch"; then
  git -C "$mirror_dir" checkout "$target_branch"
else
  git -C "$mirror_dir" checkout -b "$target_branch" --track "$remote_name/$target_branch"
fi

git -C "$mirror_dir" branch --set-upstream-to="$remote_name/$target_branch" "$target_branch"
git -C "$mirror_dir" merge --ff-only "$remote_name/$target_branch"

if [[ "$skip_python" -eq 0 ]]; then
  ORCHESTRA_DIR="$mirror_dir" "$mirror_dir/shared_scripts/bootstrap-python-env.sh"
fi
