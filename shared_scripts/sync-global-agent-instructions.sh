#!/usr/bin/env bash
# Render the canonical global instructions into each agent's target file.

set -euo pipefail

script_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -P -- "$script_dir/.." && pwd)"
source_path="$repo_root/global-agent-instructions.md"

if [[ ! -f "$source_path" ]]; then
  echo "error: canonical source not found: $source_path" >&2
  exit 2
fi
if [[ "$(head -n 1 "$source_path")" != '# {{TITLE}}' ]]; then
  echo "error: canonical source must begin with: # {{TITLE}}" >&2
  exit 2
fi

mode="write"
case "${1:-}" in
  --dry-run) mode="dry-run" ;;
  --check) mode="check" ;;
  "") ;;
  *) echo "error: unknown argument: $1 (expected --dry-run or --check)" >&2; exit 2 ;;
esac

targets=(
  "$HOME/.claude/CLAUDE.md|Global Claude Code Instructions"
  "$HOME/.codex/AGENTS.md|Global Codex Instructions"
  "$HOME/.config/kilo/AGENTS.md|Global Kilo Instructions"
  "$HOME/.gemini/GEMINI.md|Global Gemini Instructions"
)

render() {
  local title="$1"
  awk -v title="$title" '
    NR == 1 {
      print "# " title
      print ""
      print "<!--"
      print "Generated from the canonical source:"
      print "  $ORCHESTRA_DIR/global-agent-instructions.md"
      print ""
      print "Do not edit this generated file directly. Edit the canonical source, then run:"
      print "  \"$ORCHESTRA_DIR\"/shared_scripts/sync-global-agent-instructions.sh"
      print "-->"
      next
    }
    { print }
  ' "$source_path"
}

out_of_sync=0
for entry in "${targets[@]}"; do
  target="${entry%%|*}"
  title="${entry#*|}"
  rendered="$(render "$title")"

  if [[ -f "$target" ]] && [[ "$rendered" == "$(<"$target")" ]]; then
    echo "= $target (up to date)"
    continue
  fi

  case "$mode" in
    check)
      out_of_sync=1
      echo "! $target (out of sync)"
      ;;
    dry-run)
      echo "~ $target (would update)"
      if [[ -f "$target" ]]; then
        diff -u "$target" <(printf '%s\n' "$rendered") || true
      else
        echo "  target does not exist; would create"
      fi
      ;;
    write)
      mkdir -p "$(dirname -- "$target")"
      temp_path="$(mktemp "${target}.tmp.XXXXXX")"
      printf '%s\n' "$rendered" > "$temp_path"
      mv "$temp_path" "$target"
      echo "+ $target (updated)"
      ;;
  esac
done

if [[ "$mode" == "check" && "$out_of_sync" -ne 0 ]]; then
  exit 1
fi
