#!/bin/bash
# Process-group janitor for the AI Agents Usage plugin.
#
# Invoked by Main.qml (through supervised-run.sh) with one argument: the PID
# of a supervised child that has just been TERMed. Supervised children lead
# their own process group (PID == PGID == SID, enforced and re-verified by
# supervised-run.sh before any command runs), so the group ID equals this
# PID and -PID names exactly that command's group.
#
# Ownership proof before any group signal:
#   * If /proc/<pid>/stat is still readable (the leader may be a zombie),
#     pgrp must equal pid; otherwise this helper refuses to signal anything.
#   * If the leader has already been reaped, the numeric PGID cannot have
#     been recycled while the group still has members — the kernel keeps the
#     group's pid referenced — so a surviving -PID group can only be the
#     supervised child's own group.
#
# Then the escalation: TERM the group, give it a short grace window, KILL
# what remains. Bounded by construction: fixed loop count, builtin-only
# syscalls, pinned absolute sleep, numeric-PID validation, and no user data
# in arguments.
set -u

pid=${1:-}
case $pid in
  ''|*[!0-9]*) exit 0 ;;
esac
(( pid > 1 )) || exit 0

# Group-ownership validation while the leader is still visible.
if [ -r "/proc/$pid/stat" ]; then
  read -r line < "/proc/$pid/stat" || exit 0
  rest=${line##*)}      # drop "pid (comm)": comm may contain spaces or )
  set -- $rest          # state ppid pgrp sid ...
  if [ "${3:-}" != "$pid" ]; then
    printf 'reap-group: refusing unverified group for pid %s\n' "$pid" >&2
    exit 0
  fi
fi

group="-$pid"

if kill -0 -- "$pid" 2>/dev/null || kill -0 -- "$group" 2>/dev/null; then
  :
else
  exit 0
fi

sleep_bin=/usr/bin/sleep

# TERM first (the caller's TERM reached only the direct child), then a short
# grace window, then KILL what remains. Verified with kill -0 throughout.
kill -TERM -- "$group" 2>/dev/null

for _ in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 -- "$group" 2>/dev/null || exit 0
  "$sleep_bin" 0.2
done

kill -KILL -- "$group" 2>/dev/null
exit 0
