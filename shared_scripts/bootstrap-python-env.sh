#!/usr/bin/env bash
# Create or update an Orchestra checkout-local Python environment.
#
# Run this in any Orchestra checkout. A shared path such as
# /Users/Shared/orchestra is optional.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
orchestra_dir="${ORCHESTRA_DIR:-$(cd -- "$script_dir/.." && pwd)}"
python_bin="${PYTHON:-python3}"
venv_dir="$orchestra_dir/.venv"
requirements="$orchestra_dir/requirements.txt"

if [[ ! -f "$requirements" ]]; then
  echo "error: requirements file not found: $requirements" >&2
  exit 2
fi

if [[ ! -x "$venv_dir/bin/python" ]]; then
  echo "+ $python_bin -m venv $venv_dir"
  "$python_bin" -m venv "$venv_dir"
fi

echo "+ $venv_dir/bin/python -m pip install --upgrade pip"
"$venv_dir/bin/python" -m pip install --disable-pip-version-check --no-cache-dir --quiet --upgrade pip

echo "+ $venv_dir/bin/python -m pip install -r $requirements"
"$venv_dir/bin/python" -m pip install --disable-pip-version-check --no-cache-dir --quiet -r "$requirements"

# In /Users/Shared deployments, keep the environment usable by a shared group.
# Skip for single-user installs where opening group write is unnecessary.
case "$venv_dir" in
  /Users/Shared/*) chmod -R g+rwX "$venv_dir" 2>/dev/null || true ;;
esac

echo "Orchestra Python environment ready: $venv_dir"
