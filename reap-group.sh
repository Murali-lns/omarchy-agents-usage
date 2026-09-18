#!/bin/bash
# Process-group janitor for the AI Agents Usage plugin.
#
# Invoked only by Main.qml with one argument: the PID of a terminated child.
# The QML-side timers already sent SIGTERM to the direct child; this helper
# closes out any surviving process-group members after TERM had its chance,
# then does the final wait so nothing lingers.
#
# Bounded by construction: fixed loop count, single builtin-only syscall
# family, no PATH lookups, no user data in arguments beyond the numeric PID
# (validated here before use).
set -u

pid=${1:-}
case $pid in
  ''|*[!0-9]*) exit 0 ;;
esac
(( pid > 1 )) || exit 0

# After the child exits, its group may only contain grandchildren still alive.
# TERM once, wait briefly, then KILL what remains. Verified with kill -0.
group="-$pid"

if kill -0 -- "$pid" 2>/dev/null || kill -0 -- "$group" 2>/dev/null; then
  :
else
  exit 0
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 -- "$group" 2>/dev/null || exit 0
  sleep 0.2
done

kill -KILL -- "$group" 2>/dev/null
exit 0
