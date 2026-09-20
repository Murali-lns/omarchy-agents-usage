#!/bin/bash
# Supervised command runner for the AI Agents Usage plugin.
#
# Started by Main.qml as:
#   /usr/bin/bash supervised-run.sh <stdout-cap> <stderr-cap> <cmd> [args...]
#
# It closes the process-boundary gaps the marketplace review requires:
#
# 1. A real process group per command. When this script does not already lead
#    its own group it re-execs itself through the pinned /usr/bin/setsid.
#    A non-leader's setsid() succeeds without forking, so the direct child
#    keeps its PID and that PID becomes the new session/group ID — the shell
#    can later signal exactly -PID. The stage after the re-exec re-verifies
#    group ownership from /proc/$$/stat and refuses to run anything unless
#    this process owns its group (PID == PGID).
#
# 2. Producer-side stream caps. stdout and stderr are each relayed through the
#    pinned /usr/bin/head -c <cap> before they can reach the long-lived shell,
#    so StdioCollector only ever buffers at most the cap for a stream, and a
#    flooding child is severed (SIGPIPE) once it overruns its budget.
#
# 3. Group-wide TERM on the deadline. The command pipeline runs in the
#    background and is collected with `wait`, because a trapped signal
#    interrupts `wait` immediately; the trap then disarms its own TERM
#    handler, TERMs the supervisor's entire group (the direct child leads
#    it) and exits 143 — so a deadline reaches every descendant even if no
#    external reaper is available. The reaper still completes the KILL
#    escalation of the same group.
#
# No PATH lookups and no shell evaluation of data: every external tool is
# pinned to an absolute path and the command runs directly from its argv.
# The only bytes this script emits on its own are fixed diagnostic strings.
set -u

[[ $# -ge 3 ]] || exit 2
out_cap=$1
err_cap=$2
shift 2

case $out_cap in ''|*[!0-9]*) exit 2 ;; esac
case $err_cap in ''|*[!0-9]*) exit 2 ;; esac
(( out_cap > 0 && err_cap > 0 )) || exit 2

# The re-exec below must never PATH-search, so the script path has to be
# absolute (Main.qml always passes one).
case $0 in
  /*) ;;
  *)
    printf '%s\n' "supervised-run: script path must be absolute" >&2
    exit 3
    ;;
esac

# Process-group ID of this shell, read from /proc with builtins only.
proc_group_id() {
  local line rest
  read -r line < "/proc/$$/stat" || return 1
  rest=${line##*)}      # drop "pid (comm)": comm may contain spaces or )
  set -- $rest          # state ppid pgrp sid ...
  printf '%s' "${3:-}"
}

pgid=$(proc_group_id)
if [ -z "$pgid" ]; then
  printf '%s\n' "supervised-run: cannot verify process group from /proc" >&2
  exit 3
fi

if [ "$pgid" != "$$" ]; then
  # Not a group leader, so this cannot fork: PID is preserved and the second
  # stage owns PGID == PID == SID.
  exec /usr/bin/setsid /usr/bin/bash "$0" "$out_cap" "$err_cap" "$@"
fi

# Group-ownership proof: PID must equal PGID. From here on the shell can hand
# -PID to the reaper knowing it names exactly this command's own group.
if [ "$(proc_group_id)" != "$$" ]; then
  printf '%s\n' "supervised-run: no dedicated process group, refusing to run" >&2
  exit 3
fi

# Bounded execution: each stream is capped before it reaches the caller. The
# pipeline is run in the background so the TERM trap can fire promptly (a
# trapped signal interrupts the wait builtin immediately); the trap first
# disarms its own TERM handler — so the group-wide signal cannot race the
# exit path — then TERMs the whole group and exits 143. That reaches every
# descendant even when no external reaper is available.
set -o pipefail
trap 'trap "" TERM; kill -TERM -- "-$$" 2>/dev/null; exit 143' TERM HUP INT

"$@" 2> >(/usr/bin/head -c "$err_cap" >&2) | /usr/bin/head -c "$out_cap" &
pipeline_pid=$!
wait "$pipeline_pid"
status=$?
wait 2>/dev/null
exit "$status"
