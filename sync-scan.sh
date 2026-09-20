#!/bin/bash
# Bounded cross-device sync scan for the AI Agents Usage plugin.
#
# Emits one already-bounded stream: per-file, aggregate, and file-count
# limits are enforced before any data leaves this helper, so the caller
# only ever buffers a bounded document. The directory is streamed entry by
# entry (null-delimited) and this consumer stops after at most max_files
# kept files or max_entries_scanned entries — whichever comes first — so
# neither side ever materializes the whole directory. Oversized, empty,
# non-regular, and symlinked entries are skipped, and a trailing sync-meta
# block reports kept/skipped/truncated counts. Reads are capped with
# `head -c` at the checked size, so a file that grows after the size check
# cannot exceed its budget either.
#
# Started by Main.qml through supervised-run.sh with a cleared environment;
# the shebang is never consulted. PATH is not trusted here — every external
# tool this helper needs is pinned to an absolute path.
set -u

dir=${1:-}
[[ -n "$dir" && -d "$dir" ]] || exit 0

max_files=64
max_file_bytes=262144   # 256 KiB per snapshot file
max_total_bytes=2097152 # 2 MiB aggregate budget per scan
max_entries_scanned=1024

kept=0
skipped=0
total=0
truncated=0
scanned=0

stat_bin=/usr/bin/stat
head_bin=/usr/bin/head
find_bin=/usr/bin/find

while IFS= read -r -d '' f; do
  scanned=$((scanned + 1))
  if (( scanned > max_entries_scanned )); then
    truncated=1
    break
  fi
  case $f in *$'\n'*) skipped=$((skipped + 1)); continue ;; esac
  if [[ -L "$f" || ! -f "$f" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  size=$("$stat_bin" -c %s -- "$f" 2>/dev/null) || { skipped=$((skipped + 1)); continue; }
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
  # Skip the size re-check if head fails: a missing file raced a delete.
  if ! "$head_bin" -c "$size" -- "$f" 2>/dev/null; then
    skipped=$((skipped + 1))
    kept=$((kept - 1))
    total=$((total - size))
    continue
  fi
  printf '\n=== EOM ===\n'
done < <("$find_bin" -- "$dir" -maxdepth 1 -name '*.json' -print0 2>/dev/null)

printf '===sync-meta===\n'
printf '{"kept":%d,"skipped":%d,"truncated":%d,"bytes":%d}\n' "$kept" "$skipped" "$truncated" "$total"
printf '=== EOM ===\n'
