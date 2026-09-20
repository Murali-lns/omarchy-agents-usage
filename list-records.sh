#!/bin/bash
# Bounded provider-record listing for the AI Agents Usage plugin.
#
# Enumerates candidate record names under producer-side bounds: the directory
# is streamed one entry at a time (null-delimited, so odd names cannot smuggle
# extra lines) and this consumer stops after at most max_files kept names or
# max_entries_scanned entries — whichever comes first — so neither this
# helper nor the caller ever materializes the whole directory. The emitted
# document is bounded by construction:
#
#   max_files * (max_name_length + 1) + meta  <  68 KiB
#
# Symlinked, non-regular, and newline-named entries are skipped, and a final
# `list-meta` line reports kept/skipped/truncated counts so the consumer can
# surface a capped scan. Started by Main.qml through supervised-run.sh; PATH
# is never consulted and the single external tool is pinned absolute.
set -u

dir=${1:-}
[[ -n "$dir" && -d "$dir" ]] || exit 0

max_files=256
max_name_length=255
max_entries_scanned=1024

find_bin=/usr/bin/find

kept=0
skipped=0
truncated=0
scanned=0

while IFS= read -r -d '' f; do
  scanned=$((scanned + 1))
  if (( scanned > max_entries_scanned )); then
    truncated=1
    break
  fi
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
done < <("$find_bin" -- "$dir" -maxdepth 1 -name '*.json' -print0 2>/dev/null)

printf 'list-meta {"kept":%d,"skipped":%d,"truncated":%d}\n' "$kept" "$skipped" "$truncated"
