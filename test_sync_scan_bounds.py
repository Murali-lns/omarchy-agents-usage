"""Bounds checks for the cross-device sync scan path.

`sync-scan.sh` produces the bounded document that Main.qml's syncScanProcess
consumes, so these tests execute the real helper against disposable fixtures
and pin the parse-side limits that must also hold in the QML consumer.
"""

from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parent
SCRIPT = ROOT / "sync-scan.sh"
MAIN_PATH = ROOT / "Main.qml"

MAX_FILES = 64
MAX_FILE_BYTES = 262144
MAX_TOTAL_BYTES = 2097152


def run_scan(directory) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), str(directory)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"sync-scan.sh exited {result.returncode}: {result.stderr}")
    return result.stdout


def parse_blocks(output: str) -> dict:
    blocks = {}
    current = None
    body: list = []
    for line in output.split("\n"):
        header = re.fullmatch(r"===(.+)===", line)
        if header and line != "=== EOM ===":
            current = header.group(1)
            body = []
            continue
        if line == "=== EOM ===":
            if current is not None:
                blocks[current] = "\n".join(body)
            current = None
            body = []
            continue
        if current is not None:
            body.append(line)
    return blocks


def snapshot_paths(output: str):
    return [path for path in parse_blocks(output) if path != "sync-meta"]


def meta_of(output: str) -> dict:
    return json.loads(parse_blocks(output)["sync-meta"])


def write_snapshot(path: Path, device: str = "device-a") -> None:
    path.write_text(
        json.dumps({"schemaVersion": 1, "deviceId": device, "providers": {}}),
        encoding="utf-8",
    )


class TestSyncScanHelper(unittest.TestCase):
    def test_helper_is_executable_and_declares_hard_bounds(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("max_files=64", text)
        self.assertIn("max_file_bytes=262144", text)
        self.assertIn("max_total_bytes=2097152", text)
        self.assertIn('[[ -L "$f" || ! -f "$f" ]]', text)
        self.assertIn('head_bin=/usr/bin/head', text)
        self.assertIn('"$head_bin" -c "$size"', text)

    def test_small_files_are_kept_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_snapshot(directory / "a.json", "alpha")
            write_snapshot(directory / "b.json", "beta")
            out = run_scan(directory)
        self.assertEqual(sorted(Path(p).name for p in snapshot_paths(out)), ["a.json", "b.json"])
        meta = meta_of(out)
        self.assertEqual((meta["kept"], meta["skipped"], meta["truncated"]), (2, 0, 0))
        self.assertGreater(meta["bytes"], 0)

    def test_oversized_and_empty_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_snapshot(directory / "good.json")
            (directory / "huge.json").write_bytes(b"x" * (MAX_FILE_BYTES + 4096))
            (directory / "empty.json").write_bytes(b"")
            out = run_scan(directory)
        self.assertEqual([Path(p).name for p in snapshot_paths(out)], ["good.json"])
        meta = meta_of(out)
        self.assertEqual((meta["kept"], meta["skipped"], meta["truncated"]), (1, 2, 0))
        self.assertNotIn("huge.json", out)
        self.assertLess(len(out), MAX_FILE_BYTES)

    def test_symlinks_and_non_regular_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            directory = base / "sync"
            directory.mkdir()
            write_snapshot(directory / "real.json")
            target = base / "outside.json"
            write_snapshot(target, "outside")
            (directory / "link.json").symlink_to(target)
            (directory / "dir.json").mkdir()
            out = run_scan(directory)
        names = sorted(Path(p).name for p in snapshot_paths(out))
        self.assertEqual(names, ["real.json"])
        meta = meta_of(out)
        self.assertEqual(meta["skipped"], 2)
        self.assertNotIn("link.json===", out)

    def test_newline_filenames_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_snapshot(directory / "normal.json")
            write_snapshot(directory / "a\nb.json", "sneaky")
            out = run_scan(directory)
        meta = meta_of(out)
        self.assertEqual((meta["kept"], meta["skipped"]), (1, 1))

    def test_file_count_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(MAX_FILES + 6):
                write_snapshot(directory / f"dev-{i:03d}.json", f"dev{i}")
            out = run_scan(directory)
        meta = meta_of(out)
        self.assertEqual(meta["kept"], MAX_FILES)
        self.assertEqual(meta["truncated"], 1)
        self.assertEqual(len(snapshot_paths(out)), MAX_FILES)

    def test_aggregate_budget_bounds_total_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for i in range(12):
                (directory / f"bulk-{i:02d}.json").write_bytes(b"y" * 200000)
            out = run_scan(directory)
        meta = meta_of(out)
        self.assertEqual(meta["truncated"], 1)
        self.assertLessEqual(meta["bytes"], MAX_TOTAL_BYTES)
        self.assertEqual(meta["kept"], meta["bytes"] // 200000)
        self.assertLess(len(out), MAX_TOTAL_BYTES + 65536)
        self.assertNotIn("bulk-10.json", out)
        self.assertNotIn("bulk-11.json", out)

    def test_missing_and_empty_directories_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_missing = run_scan(Path(tmp) / "does-not-exist")
            self.assertEqual(out_missing, "")
            out_empty = run_scan(Path(tmp))
            meta = meta_of(out_empty)
        self.assertEqual((meta["kept"], meta["skipped"], meta["truncated"], meta["bytes"]), (0, 0, 0, 0))


class TestQmlProcessBoundary(unittest.TestCase):
    """Pins the marketplace process-boundary requirements.

    Absolute trusted binaries, cleared child environments, capped
    stdout/stderr, hard deadlines with TERM-then-KILL via a bounded reaper.
    """

    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_PATH.read_text(encoding="utf-8")

    def test_no_path_resolved_commands(self):
        for needle in (
            'command: ["find"',
            '["bash", root',
            '["mkdir"',
            '["omarchy-agent-usage-update"]',
            "[root.hermesCollectorPath]",
            "[root.grokCollectorPath]",
            "[root.modelHistoryCollectorPath]",
        ):
            self.assertNotIn(needle, self.main)

    def test_absolute_trusted_binaries(self):
        for needle in (
            'readonly property string binBash: "/usr/bin/bash"',
            'readonly property string binMkdir: "/usr/bin/mkdir"',
            'readonly property string binPython3: "/usr/bin/python3"',
            'omarchyUsageUpdatePath: "/usr/share/omarchy/bin/omarchy-agent-usage-update"',
            'readonly property string supervisedRunScriptPath: {',
            'readonly property string listRecordsScriptPath: {',
            'readonly property string reaperScriptPath: {',
        ):
            self.assertIn(needle, self.main)
        # The unbounded find walk is gone; discovery runs through the bounded
        # listing helper instead.
        self.assertNotIn("binFind", self.main)
        self.assertNotIn('"/usr/bin/find"', self.main)

    def test_collectors_started_through_absolute_interpreter(self):
        self.assertIn("[root.binPython3, root.hermesCollectorPath]", self.main)
        self.assertIn("[root.binPython3, root.grokCollectorPath]", self.main)
        self.assertIn("[root.binPython3, root.modelHistoryCollectorPath]", self.main)
        self.assertIn("syncScanProcess.command = root.supervisedCommand([root.binBash, root.syncScanScriptPath", self.main)

    def test_minimal_explicit_environment(self):
        self.assertIn("clearEnvironment: true", self.main)
        self.assertIn('minimalChildEnv: ({ "HOME": root.home })', self.main)
        spawns = self.main.count("Process {")
        cleared = self.main.count("clearEnvironment: true")
        self.assertEqual(cleared, spawns)

    def test_stream_output_is_capped(self):
        # Every spawn carries producer-side caps through the supervisor;
        # console trimming is display hygiene only.
        self.assertIn("readonly property int streamCapBytes:", self.main)
        self.assertGreaterEqual(self.main.count("root.supervisedCommand("), 8)
        self.assertIn("String(stdoutCapBytes === undefined ? root.streamCapBytes : stdoutCapBytes)", self.main)
        self.assertIn("String(root.streamCapBytes)]", self.main)
        self.assertGreaterEqual(self.main.count("root.consoleMessageCapChars"), 5)
        self.assertNotIn("substring(0, root.streamCapBytes)", self.main)

    def test_deadlines_and_group_reaper_exist(self):
        self.assertIn("readonly property int collectorDeadlineMs:", self.main)
        self.assertIn("readonly property int discoveryDeadlineMs:", self.main)
        self.assertIn("readonly property int syncDeadlineMs:", self.main)
        self.assertIn("function killLeaked(processObject, guard)", self.main)
        for name in ("updateProcess", "hermesProcess", "grokProcess",
                     "modelHistoryProcess", "listProcess", "syncMkdirProcess",
                     "syncScanProcess"):
            self.assertIn("killLeaked(" + name + ",", self.main)

    def test_reaper_helper_is_bounded_and_absolute(self):
        helper = ROOT / "reap-group.sh"
        text = helper.read_text(encoding="utf-8")
        self.assertIn("#!/bin/bash", text)
        self.assertIn("kill -0", text)
        self.assertIn("kill -KILL", text)
        self.assertIn("sleep_bin=/usr/bin/sleep", text)
        self.assertIn('"/proc/$pid/stat"', text)
        self.assertNotIn("PATH=", text)
        self.assertTrue(os.access(helper, os.X_OK))

    def test_sync_scan_helper_avoids_path_resolution(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("stat_bin=/usr/bin/stat", text)
        self.assertIn("head_bin=/usr/bin/head", text)
        self.assertLessEqual(text.count("/usr/bin/"), 3)
        self.assertNotIn("$(stat ", text)

class TestQmlSyncConsumers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = MAIN_PATH.read_text(encoding="utf-8")

    @staticmethod
    def block(source, start_marker, end_marker):
        start = source.index(start_marker)
        end = source.index(end_marker, start)
        return source[start:end]

    def test_scan_runs_the_bounded_helper_not_an_inline_cat(self):
        self.assertIn("sync-scan.sh", self.main)
        self.assertIn(
            "syncScanProcess.command = root.supervisedCommand([root.binBash, root.syncScanScriptPath, root.syncEffectiveDir], root.syncScanStdoutCapBytes)",
            self.main,
        )
        self.assertNotIn('cat \\"$f\\"', self.main)

    def test_parse_side_caps_bound_the_scan_document(self):
        parse = self.block(self.main, "function parseSyncScanOutput(output)", "\n  function syncInputNotice")
        self.assertIn("text.substring(0, root.syncMaxScanChars)", parse)
        self.assertIn("snapshots.length >= root.syncMaxSnapshots", parse)
        self.assertIn('=== "sync-meta"', parse)
        notice = self.block(self.main, "function syncInputNotice", "\n  function cloneValue")
        self.assertIn("root.syncMaxSnapshots", notice)

    def test_aggregate_ingestion_is_bounded(self):
        aggregate = self.block(self.main, "function aggregateSnapshots(snapshots)", "\n  // Snapshots keep")
        self.assertIn("root.syncMaxProvidersPerSnapshot", aggregate)
        self.assertIn("root.syncMaxKeysPerMap", aggregate)
        self.assertIn("root.syncMaxActiveDates", aggregate)
        self.assertIn("root.syncMaxStringLength", aggregate)
        combiner = self.block(self.main, "function combineObjectNumbers", "function aggregateSnapshots")
        self.assertIn("root.syncMaxKeysPerMap", combiner)
        self.assertIn("root.syncMaxKeyLength", combiner)

    def test_numeric_values_are_clamped(self):
        bounded = self.block(self.main, "function boundedNumber", "function numberValue")
        number = self.block(self.main, "function numberValue", "\n  function dateString")
        self.assertIn("root.maxCountValue", bounded)
        self.assertIn("boundedNumber", number)
        total = self.block(self.main, "function modelUsageValueTotal", "\n  function modelUsageByPeriodHasData")
        self.assertIn("root.maxCountValue", total)


if __name__ == "__main__":
    unittest.main()
