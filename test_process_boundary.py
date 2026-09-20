"""Behavioural and structural checks for the supervised process boundary.

`supervised-run.sh` owns a dedicated process group per command (PID == PGID
== SID, verified from /proc before the command runs) and caps both streams at
the pipe boundary before they can reach the long-lived shell;
`list-records.sh` bounds record discovery without any external tools;
`reap-group.sh` validates group ownership before its TERM/KILL escalation.
These tests execute the real helpers against disposable fixtures and pin the
QML wiring that routes every spawn through the supervisor.
"""

from pathlib import Path
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
SUPERVISOR = ROOT / "supervised-run.sh"
LISTER = ROOT / "list-records.sh"
REAPER = ROOT / "reap-group.sh"
MAIN_PATH = ROOT / "Main.qml"

MAX_FILES = 256


def run_supervised(out_cap, err_cap, command, timeout=30):
    return subprocess.run(
        ["bash", str(SUPERVISOR), str(out_cap), str(err_cap), *command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def group_members(pgid):
    """PIDs whose process group equals `pgid` (Linux /proc scan)."""
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) >= 4 and fields[2] == str(pgid):
            members.append(int(entry.name))
    return members


def proc_stat(pid):
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = stat.rsplit(")", 1)[-1].split()
    return {
        "state": fields[0],
        "ppid": int(fields[1]),
        "pgrp": int(fields[2]),
        "sid": int(fields[3]),
    }


class TestSupervisedRunHelper(unittest.TestCase):
    def test_helper_is_executable_and_pins_absolute_tools(self):
        self.assertTrue(SUPERVISOR.is_file())
        self.assertTrue(os.access(SUPERVISOR, os.X_OK))
        text = SUPERVISOR.read_text(encoding="utf-8")
        self.assertIn("#!/bin/bash", text)
        self.assertIn("exec /usr/bin/setsid /usr/bin/bash", text)
        self.assertIn("/usr/bin/head -c", text)
        self.assertNotIn("PATH=", text)

    def test_argument_validation_fails_closed(self):
        no_args = subprocess.run(["bash", str(SUPERVISOR)], capture_output=True)
        self.assertEqual(no_args.returncode, 2)
        bad_cap = subprocess.run(
            ["bash", str(SUPERVISOR), "abc", "100", "/usr/bin/true"],
            capture_output=True,
        )
        self.assertEqual(bad_cap.returncode, 2)
        zero_cap = subprocess.run(
            ["bash", str(SUPERVISOR), "0", "100", "/usr/bin/true"],
            capture_output=True,
        )
        self.assertEqual(zero_cap.returncode, 2)
        relative = subprocess.run(
            ["bash", "supervised-run.sh", "100", "100", "/usr/bin/true"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(relative.returncode, 3)
        self.assertIn("absolute", relative.stderr)

    def test_stdout_is_capped_at_the_producer(self):
        flood = (
            "n=0; while (( n < 3000 )); do "
            "printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n'; ((n++)); done"
        )
        result = run_supervised(4096, 4096, ["/usr/bin/bash", "-c", flood])
        self.assertLessEqual(len(result.stdout.encode()), 4096)

    def test_stderr_is_capped_at_the_producer(self):
        flood = (
            "n=0; while (( n < 3000 )); do "
            "printf 'bbbbbbbbbbbbbbbb\\n' 1>&2; ((n++)); done"
        )
        result = run_supervised(2048, 2048, ["/usr/bin/bash", "-c", flood])
        self.assertLessEqual(len(result.stderr.encode()), 2048)

    def test_exit_status_propagates_with_bounded_output(self):
        result = run_supervised(
            4096,
            4096,
            ["/usr/bin/bash", "-c", "printf 'done\\n'; printf 'note\\n' 1>&2; exit 7"],
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "done\n")
        self.assertEqual(result.stderr, "note\n")

    def test_direct_child_leads_its_own_group(self):
        proc = subprocess.Popen(
            ["bash", str(SUPERVISOR), "4096", "4096", "/usr/bin/sleep", "1.0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.3)
            stat = proc_stat(proc.pid)
            self.assertEqual(stat["pgrp"], proc.pid)
            self.assertEqual(stat["sid"], proc.pid)
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_term_then_reaper_clears_the_whole_group(self):
        proc = subprocess.Popen(
            ["bash", str(SUPERVISOR), "4096", "4096", "/usr/bin/bash", "-c",
             "/usr/bin/sleep 20 & /usr/bin/sleep 20 & wait"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.4)
            self.assertGreaterEqual(len(group_members(proc.pid)), 3)
            os.kill(proc.pid, signal.SIGTERM)
            reaped = subprocess.run(
                ["bash", str(REAPER), str(proc.pid)],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(reaped.returncode, 0)
            proc.wait(timeout=10)
            deadline = time.time() + 5
            while time.time() < deadline and group_members(proc.pid):
                time.sleep(0.1)
            self.assertEqual(group_members(proc.pid), [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_reaper_clears_stragglers_after_the_leader_is_reaped(self):
        # The straggler parks its stdio so the supervisor's pipes close with
        # the foreground command; it must still be group-reaped afterwards.
        proc = subprocess.Popen(
            ["bash", str(SUPERVISOR), "4096", "4096", "/usr/bin/bash", "-c",
             "/usr/bin/sleep 20 >/dev/null 2>&1 & exit 0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=10)
            self.assertFalse(Path(f"/proc/{proc.pid}/stat").exists())
            self.assertGreaterEqual(len(group_members(proc.pid)), 1)
            reaped = subprocess.run(
                ["bash", str(REAPER), str(proc.pid)],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(reaped.returncode, 0)
            deadline = time.time() + 5
            while time.time() < deadline and group_members(proc.pid):
                time.sleep(0.1)
            self.assertEqual(group_members(proc.pid), [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_reaper_refuses_a_pid_that_does_not_lead_its_group(self):
        victim = subprocess.Popen(["/usr/bin/bash", "-c", "/usr/bin/sleep 20"])
        try:
            time.sleep(0.3)
            stat = proc_stat(victim.pid)
            if stat["pgrp"] == victim.pid:
                self.skipTest("victim unexpectedly leads its own group")
            reaped = subprocess.run(
                ["bash", str(REAPER), str(victim.pid)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(reaped.returncode, 0)
            self.assertIn("refusing", reaped.stderr)
            self.assertIsNone(victim.poll())
        finally:
            if victim.poll() is None:
                victim.terminate()
                victim.wait(timeout=10)


class TestListRecordsHelper(unittest.TestCase):
    def test_helper_declares_bounds_and_uses_no_external_tools(self):
        self.assertTrue(LISTER.is_file())
        self.assertTrue(os.access(LISTER, os.X_OK))
        text = LISTER.read_text(encoding="utf-8")
        self.assertIn("max_files=256", text)
        self.assertIn("max_name_length=255", text)
        self.assertNotIn("/usr/bin/", text)

    @staticmethod
    def run_lister(directory):
        return subprocess.run(
            ["bash", str(LISTER), str(directory)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_records_are_listed_with_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "claude.json").write_text("{}", encoding="utf-8")
            (directory / "codex.json").write_text("{}", encoding="utf-8")
            (directory / ".model-history.json").write_text("{}", encoding="utf-8")
            result = self.run_lister(directory)
        self.assertEqual(result.returncode, 0)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:-1], ["claude.json", "codex.json"])
        meta = json.loads(lines[-1].split(" ", 1)[1])
        self.assertEqual((meta["kept"], meta["skipped"], meta["truncated"]), (2, 0, 0))

    def test_symlinks_directories_and_newline_names_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            directory = base / "usage"
            directory.mkdir()
            (directory / "real.json").write_text("{}", encoding="utf-8")
            outside = base / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            (directory / "link.json").symlink_to(outside)
            (directory / "dir.json").mkdir()
            (directory / "a\nb.json").write_text("{}", encoding="utf-8")
            result = self.run_lister(directory)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:-1], ["real.json"])
        meta = json.loads(lines[-1].split(" ", 1)[1])
        self.assertEqual((meta["kept"], meta["skipped"], meta["truncated"]), (1, 3, 0))

    def test_file_count_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(MAX_FILES + 10):
                (directory / f"dev-{i:03d}.json").write_text("{}", encoding="utf-8")
            result = self.run_lister(directory)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines) - 1, MAX_FILES)
        meta = json.loads(lines[-1].split(" ", 1)[1])
        self.assertEqual((meta["kept"], meta["truncated"]), (MAX_FILES, 1))
        self.assertNotIn("dev-260.json", result.stdout)

    def test_missing_directory_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_lister(Path(tmp) / "nope")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class TestQmlSupervisorWiring(unittest.TestCase):
    """Pins the marketplace process-boundary requirements in Main.qml."""

    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_PATH.read_text(encoding="utf-8")

    def test_every_spawn_is_routed_through_the_supervisor(self):
        self.assertGreaterEqual(self.main.count("root.supervisedCommand("), 8)
        for needle in (
            "command: root.supervisedCommand([root.binBash, root.listRecordsScriptPath",
            "reaperProcess.command = root.supervisedCommand([root.binBash, root.reaperScriptPath",
            "syncMkdirProcess.command = root.supervisedCommand([root.binMkdir",
        ):
            self.assertIn(needle, self.main)
        # No process may be assigned a raw argv; every spawn flows through the
        # supervisor. (The local argv builders are plain `var`, not spawns.)
        for forbidden in (
            ".command = [root.binPython3",
            ".command = [root.binMkdir",
            ".command = [root.omarchyUsageUpdatePath",
            ".command = [root.binBash",
            "command: [root.binBash",
        ):
            self.assertNotIn(forbidden, self.main)

    def test_script_paths_resolve_portably(self):
        self.assertIn('Qt.resolvedUrl("supervised-run.sh")', self.main)
        self.assertIn('Qt.resolvedUrl("list-records.sh")', self.main)
        self.assertIn('Qt.resolvedUrl("reap-group.sh")', self.main)

    def test_stderr_is_bounded_producer_side_not_post_hoc(self):
        self.assertIn("readonly property int consoleMessageCapChars:", self.main)
        self.assertGreaterEqual(self.main.count("root.consoleMessageCapChars"), 5)
        self.assertNotIn("substring(0, root.streamCapBytes)", self.main)
        self.assertIn("producer-side", self.main)

    def test_listing_meta_is_consumed(self):
        self.assertIn('name.indexOf("list-meta ") === 0', self.main)
        self.assertIn("function reportListMeta", self.main)

    def test_sync_scan_uses_the_wider_supervised_cap(self):
        self.assertIn("readonly property int syncScanStdoutCapBytes:", self.main)
        self.assertIn("root.syncScanStdoutCapBytes)", self.main)

    def test_supervisor_and_lister_are_referenced_by_the_qml(self):
        self.assertIn("supervisedRunScriptPath", self.main)
        self.assertIn("listRecordsScriptPath", self.main)


if __name__ == "__main__":
    unittest.main()
