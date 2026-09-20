#!/bin/bash
# Bounded provider-record listing for the AI Agents Usage plugin.
#
# Replaces an unbounded `find` walk with a producer-side bounded listing:
# at most max_files candidate names are emitted, one per line, and a final
# `list-meta` line reports kept/skipped/truncated counts so the consumer can
# surface a capped scan. Symlinked, non-regular, and newline-named entries
# are skipped. No external tools are used at all — bash builtins and path
# expansion only — and the emitted document is bounded by construction:
#
#   max_files * (max_name_length + 1) + meta  <  68 KiB
#
# Started by Main.qml through supervised-run.sh; PATH is never consulted.
set -u

dir=${1:-}
[[ -n "$dir" && -d "$dir" ]] || exit 0

max_files=256
max_name_length=255

shopt -s nullglob
kept=0
skipped=0
truncated=0

for f in "$dir"/*.json; do
  name=${f##*/}
  case $name in
    .model-history.json) continue ;;
    *$'\n'*) skipped=$((skipped + 1)); continue ;;
  esac
  if (( ${#name} > max_name_length )); then
    skipped=$((skipped + 1))
    continue
  fi
  if (( kept >= max_files )); then
    truncated=1
    break
  fi
  if [[ -L "$f" || ! -f "$f" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  kept=$((kept + 1))
  printf '%s\n' "$name"
done

printf 'list-meta {"kept":%d,"skipped":%d,"truncated":%d}\n' "$kept" "$skipped" "$truncated"
