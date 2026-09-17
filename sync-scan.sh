#!/usr/bin/env bash
# Bounded cross-device sync scan for the AI Agents Usage plugin.
#
# Emits one already-bounded stream: per-file, aggregate, and file-count
# limits are enforced before any data leaves this helper, so the caller
# only ever buffers a bounded document. Oversized, empty, non-regular, and
# symlinked entries are skipped, and a trailing sync-meta block reports
# kept/skipped/truncated counts. Reads are capped with `head -c` at the
# checked size, so a file that grows after the size check cannot exceed
# its budget either.
set -u

dir=${1:-}
[[ -n "$dir" && -d "$dir" ]] || exit 0

max_files=64
max_file_bytes=262144   # 256 KiB per snapshot file
max_total_bytes=2097152 # 2 MiB aggregate budget per scan

shopt -s nullglob
kept=0
skipped=0
total=0
truncated=0

for f in "$dir"/*.json; do
  case $f in *$'\n'*) skipped=$((skipped + 1)); continue ;; esac
  if [[ -L "$f" || ! -f "$f" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  size=$(stat -c %s -- "$f" 2>/dev/null) || { skipped=$((skipped + 1)); continue; }
  case $size in ''|*[!0-9]*) skipped=$((skipped + 1)); continue ;; esac
  if (( size == 0 || size > max_file_bytes )); then
    skipped=$((skipped + 1))
    continue
  fi
  if (( kept >= max_files )); then
    truncated=1
    break
  fi
  if (( total + size > max_total_bytes )); then
    truncated=1
    break
  fi
  total=$((total + size))
  kept=$((kept + 1))
  printf '===%s===\n' "$f"
  head -c "$size" -- "$f"
  printf '\n=== EOM ===\n'
done

printf '===sync-meta===\n'
printf '{"kept":%d,"skipped":%d,"truncated":%d,"bytes":%d}\n' "$kept" "$skipped" "$truncated" "$total"
printf '=== EOM ===\n'
