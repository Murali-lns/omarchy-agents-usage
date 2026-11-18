#!/usr/bin/env python3
"""Review gates from marketplace issue #14383 (HANCORE, 2026-11-14).

Each test maps to one gate the maintainer left open at 9a15e32:
  - Antigravity's captured CLI output is bounded while it is read
  - Grok's ACP RPC replies are read without readline() and with byte caps
  - Hermes' SQLite usage result is streamed with a row cap and cut text columns
  - every collector's whole-file reads are capped on the bytes actually read
  - the QML never loads a usage record, the model-history sidecar or a synced
    snapshot through FileView; only the bounded sync-scan.sh reader does
Behavioural tests run the real code against disposable fixtures; structural
tests pin the safe construct so a later commit cannot silently revert it.
"""

from datetime import datetime
import importlib.util
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
COLLECTORS = (
    "antigravity-collector.py",
    "grok-collector.py",
    "hermes-collector.py",
    "model-history-collector.py",
    "opencode-go-collector.py",
)


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(filename: str) -> str:
    return (ROOT / filename).read_text(encoding="utf-8")


class TestAntigravityBoundedCapture(unittest.TestCase):
    def setUp(self):
        self.collector = load("antigravity_collector_gate", "antigravity-collector.py")

    def test_flooding_child_is_killed_and_its_output_is_not_kept(self):
        flood = [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"]
        started = time.monotonic()
        result = self.collector.bounded_run(flood, timeout=20, limit=64 * 1024)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result.returncode, -1)
        self.assertEqual(result.stdout, "")

    def test_small_output_and_exit_status_pass_through(self):
        result = self.collector.bounded_run([sys.executable, "-c", "print('ok'); raise SystemExit(3)"], timeout=20)
        self.assertEqual((result.returncode, result.stdout), (3, "ok\n"))

    def test_hung_child_hits_the_deadline(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.collector.bounded_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)

    def test_cli_runs_through_the_bounded_runner_by_default(self):
        text = source("antigravity-collector.py")
        self.assertIn("runner: Callable[..., Any] = bounded_run", text)
        self.assertNotIn("= subprocess.run", text)
        self.assertIn("os.read(fd, min(65536, limit + 1 - size))", text)


class TestGrokBoundedRpc(unittest.TestCase):
    def setUp(self):
        self.collector = load("grok_collector_gate", "grok-collector.py")

    def spawn(self, script: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def finish(self, proc):
        proc.kill()
        proc.wait(timeout=5)
        proc.stdin.close()
        proc.stdout.close()

    def test_reply_without_newline_is_cut_at_the_line_cap(self):
        proc = self.spawn("import sys\nsys.stdin.readline()\nwhile True: sys.stdout.write('a' * 65536); sys.stdout.flush()")
        try:
            with self.assertRaises(self.collector.RpcOverflow):
                self.collector.rpc_request(proc, 1, "initialize", timeout=20)
        finally:
            self.finish(proc)

    def test_many_short_lines_are_cut_at_the_request_cap(self):
        proc = self.spawn("import sys\nsys.stdin.readline()\nwhile True: sys.stdout.write('{}\\n' * 20000); sys.stdout.flush()")
        try:
            with self.assertRaises(self.collector.RpcOverflow):
                self.collector.rpc_request(proc, 1, "initialize", timeout=20)
        finally:
            self.finish(proc)

    def test_replies_split_across_reads_and_requests_still_match(self):
        script = (
            "import json, sys\n"
            "for _ in range(2):\n"
            "    msg = json.loads(sys.stdin.readline())\n"
            "    if msg['id'] == 1:\n"
            "        sys.stdout.write('{\"note\": 1}\\n{\"id\": 1, \"result\": {}}\\n{\"id\": 2, ')\n"
            "    else:\n"
            "        sys.stdout.write('\"result\": {\"ok\": true}}\\n')\n"
            "    sys.stdout.flush()\n"
        )
        proc = self.spawn(script)
        try:
            self.assertEqual(self.collector.rpc_request(proc, 1, "initialize", timeout=20)["id"], 1)
            self.assertEqual(self.collector.rpc_request(proc, 2, "_x.ai/billing", timeout=20)["result"], {"ok": True})
        finally:
            self.finish(proc)

    def test_no_readline_on_the_acp_pipe(self):
        text = source("grok-collector.py")
        self.assertNotIn(".readline()", text)
        self.assertNotIn("text=True", text)
        self.assertIn("MAX_RPC_LINE_BYTES", text)
        self.assertIn("MAX_RPC_READ_BYTES", text)


class TestHermesBoundedQuery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        home = Path(self.temp.name) / ".hermes"
        home.mkdir()
        self.saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "XDG_STATE_HOME")}
        os.environ["HERMES_HOME"] = str(home)
        os.environ["XDG_STATE_HOME"] = str(Path(self.temp.name) / "state")
        conn = sqlite3.connect(home / "state.db")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL)")
        conn.execute(
            "CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL,"
            " api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
            " cache_read_tokens INTEGER, cache_write_tokens INTEGER, first_seen REAL)"
        )
        now = datetime.now().timestamp()
        for i in range(12):
            conn.execute("INSERT INTO sessions VALUES (?, 'tui', ?)", (f"s{i}", now))
            model = "m" * 5000 if i == 11 else f"model-{i}"
            conn.execute(
                "INSERT INTO session_model_usage VALUES (?, ?, 1, 10, 10, 0, 0, ?)",
                (f"s{i}", model, now),
            )
        conn.commit()
        conn.close()
        self.collector = load("hermes_collector_gate", "hermes-collector.py")

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_rows_stop_at_the_cap_newest_first_and_say_so(self):
        self.collector.MAX_USAGE_ROWS = 5
        record = self.collector.collect_usage()
        self.assertEqual(record["totalPrompts"], 5)
        self.assertIn("newest 5 usage rows", record["usageStatusText"])
        self.assertTrue(any(len(model) == self.collector.MAX_MODEL_CHARS for model in record["modelUsage"]))

    def test_under_the_cap_nothing_is_flagged(self):
        record = self.collector.collect_usage()
        self.assertEqual(record["totalPrompts"], 12)
        self.assertEqual(record["usageStatusText"], "")
        self.assertTrue(all(len(model) <= self.collector.MAX_MODEL_CHARS for model in record["modelUsage"]))

    def test_usage_query_is_streamed_not_fetched_whole(self):
        text = source("hermes-collector.py")
        self.assertNotIn("rows = cur.fetchall()", text)
        self.assertIn("for index, row in enumerate(cur):", text)
        self.assertIn("LIMIT ?", text)
        self.assertIn("substr(smu.model, 1, {MAX_MODEL_CHARS})", text)


class TestCappedFileReads(unittest.TestCase):
    def test_every_collector_caps_whole_file_reads(self):
        for filename in COLLECTORS:
            with self.subTest(filename=filename):
                text = source(filename)
                self.assertNotRegex(text, r"\.read_text\(")
                self.assertNotRegex(text, r"(?<!limit \+ 1)\)\.read\(\)")

    def test_capped_reader_rejects_a_file_over_the_limit(self):
        module = load("grok_collector_cap", "grok-collector.py")
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "usage.json"
            big.write_bytes(b"{" + b" " * 4096 + b"}")
            with self.assertRaises(ValueError):
                module.read_text_capped(big, 1024)
            self.assertEqual(module.read_text_capped(big, 8192)[0], "{")


class TestQmlNeverLoadsRecordsWhole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = source("Main.qml")
        cls.agent = source("Agent.qml")
        cls.panel = source("Panel.qml")

    def test_agent_records_have_no_file_view(self):
        self.assertNotIn("FileView", self.agent)
        self.assertNotIn("Quickshell.Io", self.agent)
        self.assertIn("onRawTextChanged: parse(rawText)", self.agent)

    def test_only_write_only_and_system_file_views_remain(self):
        views = re.findall(r"FileView \{\n    id: (\w+)", self.main)
        self.assertEqual(sorted(views), ["hostnameFile", "syncSnapshotFile"])
        self.assertNotIn("FileView", self.panel)
        snapshot = self.main[self.main.index("id: syncSnapshotFile"):]
        snapshot = snapshot[:snapshot.index("}")]
        self.assertIn("preload: false", snapshot)
        self.assertNotIn("onLoaded", snapshot)

    def test_records_and_sidecar_come_from_the_bounded_reader(self):
        self.assertIn(
            "command: root.supervisedCommand([root.binBash, root.syncScanScriptPath, root.usageDir], root.syncScanStdoutCapBytes)",
            self.main,
        )
        self.assertIn('rawText: root.recordTexts[modelData] || ""', self.main)
        self.assertNotIn("text()", self.main.replace("String(text() || \"\").trim()", ""))


if __name__ == "__main__":
    unittest.main()
