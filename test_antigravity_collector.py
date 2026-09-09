#!/usr/bin/env python3
"""Focused tests for the guarded Antigravity CLI usage adapter."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest


MODULE_PATH = Path(__file__).parent / "antigravity-collector.py"


class TestAntigravityCollector(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("antigravity_collector", MODULE_PATH)
        assert spec is not None and spec.loader is not None
        self.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.collector)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_home = Path(self.temp_dir.name) / "state"
        self.state_home.mkdir(parents=True)
        self.omarchy_root = Path(self.temp_dir.name) / "omarchy"
        (self.omarchy_root / "bin").mkdir(parents=True)
        self.old_state = os.environ.get("XDG_STATE_HOME")
        self.old_omarchy = os.environ.get("OMARCHY_PATH")
        os.environ["XDG_STATE_HOME"] = str(self.state_home)
        os.environ["OMARCHY_PATH"] = str(self.omarchy_root)

    def tearDown(self):
        if self.old_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.old_state
        if self.old_omarchy is None:
            os.environ.pop("OMARCHY_PATH", None)
        else:
            os.environ["OMARCHY_PATH"] = self.old_omarchy
        self.temp_dir.cleanup()

    def test_usage_rows_convert_remaining_percent_to_used_meter(self):
        output = (
            "Gemini 3.5 Flash (High)\tRequests (5-hour)\t40%\t2026-09-09T16:00:00Z\n"
            "Gemini 3.5 Flash (High)\tTokens (weekly)\t100%\t2026-09-14T16:00:00Z\n"
            "informational line that is not a record\n"
        )

        limits = self.collector.parse_usage_output(output)

        self.assertEqual(len(limits), 2)
        self.assertEqual(limits[0]["title"], "Gemini 3.5 Flash (High) · Requests (5-hour)")
        self.assertAlmostEqual(limits[0]["percent"], 0.60)
        self.assertEqual(limits[0]["resetAt"], "2026-09-09T16:00:00Z")
        self.assertEqual(limits[0]["status"], "40% remaining")
        self.assertNotIn("used", limits[0])
        self.assertNotIn("limit", limits[0])
        self.assertEqual(limits[1]["percent"], 0.0)

    def test_credits_row_is_exposed_as_count_without_inventing_a_cap(self):
        output = "Remaining credits\t42\nStatus\tAvailable\n"

        remaining = self.collector.parse_credits_output(output)

        self.assertEqual(remaining, 42)
        record = self.collector.build_record("", output)
        self.assertTrue(record["ready"])
        self.assertEqual(record["limits"][0]["title"], "AI credits")
        self.assertEqual(record["limits"][0]["remaining"], 42)
        self.assertNotIn("limit", record["limits"][0])

    def test_invalid_rows_and_secret_shaped_labels_are_ignored(self):
        output = (
            "Model\tWindow\t101%\t2026-09-09T16:00:00Z\n"
            "Model\tWindow\t50%\tnot-a-date\n"
            "Model\tWindow\t50%\t2026-09-09T16:00:00Z\n"
            "access token\tWindow\t25%\t2026-09-09T16:00:00Z\n"
        )

        limits = self.collector.parse_usage_output(output)

        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0]["title"], "Model · Window")

    def test_empty_cli_output_is_explicitly_unavailable(self):
        record = self.collector.build_record("", "")

        self.assertFalse(record["ready"])
        self.assertEqual(record["limits"], [])
        self.assertIn("unavailable", record["usageStatusText"].lower())
        self.assertEqual(record["id"], "antigravity")
        self.assertFalse(record["hasLocalStats"])

    def test_cli_runner_receives_no_shell_and_only_expected_slash_commands(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[2] == "/usage":
                stdout = "Model\tWindow\t40%\t2026-09-09T16:00:00Z\n"
            else:
                stdout = "Credits remaining\t42\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        record = self.collector.collect_record("/usr/bin/agy", runner=runner)

        self.assertTrue(record["ready"])
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[0][2] for call in calls], ["/usage", "/credits"])
        for _, kwargs in calls:
            self.assertFalse(kwargs.get("shell", False))
            self.assertTrue(kwargs["stdin"] is not None)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("capture_output", kwargs)

    def test_native_collector_presence_causes_deferral(self):
        native = self.omarchy_root / "bin" / "omarchy-agent-usage-antigravity"
        native.write_text("native placeholder\n", encoding="utf-8")

        self.assertTrue(self.collector.native_collector_path())

        target = self.state_home / "omarchy" / "agents" / "usage" / "antigravity.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"sentinel": True}), encoding="utf-8")
        self.assertEqual(self.collector.main([]), 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"sentinel": True})

    def test_recent_record_is_cached_but_stale_record_is_refreshable(self):
        target = self.state_home / "omarchy" / "agents" / "usage" / "antigravity.json"
        record = self.collector.build_record(
            "Model\tWindow\t40%\t2026-09-09T16:00:00Z\n",
            "Remaining credits\t42\n",
            now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        )
        self.collector.write_record(record, target)

        self.assertTrue(
            self.collector.record_is_fresh(
                target,
                now=datetime(2026, 9, 9, 12, 1, tzinfo=timezone.utc),
            )
        )
        self.assertFalse(
            self.collector.record_is_fresh(
                target,
                now=datetime(2026, 9, 9, 12, 3, tzinfo=timezone.utc),
            )
        )

    def test_main_uses_cache_unless_force_is_requested(self):
        calls = []
        original_resolve = self.collector.resolve_cli_path
        original_collect = self.collector.collect_record
        try:
            setattr(self.collector, "resolve_cli_path", lambda: "/usr/bin/agy")

            def fake_collect(path):
                calls.append(path)
                return self.collector.build_record(
                    "Model\tWindow\t40%\t2026-09-09T16:00:00Z\n",
                    "Remaining credits\t42\n",
                )

            setattr(self.collector, "collect_record", fake_collect)
            self.assertEqual(self.collector.main([]), 0)
            self.assertEqual(self.collector.main([]), 0)
            self.assertEqual(self.collector.main(["--force"]), 0)
        finally:
            setattr(self.collector, "resolve_cli_path", original_resolve)
            setattr(self.collector, "collect_record", original_collect)

        self.assertEqual(calls, ["/usr/bin/agy", "/usr/bin/agy"])

    def test_qml_invokes_guarded_fallback_collector(self):
        qml = (MODULE_PATH.parent / "Main.qml").read_text(encoding="utf-8")

        for marker in (
            "antigravity-collector.py",
            "antigravityCollectorPath",
            "antigravityProcess",
            "function antigravityWanted",
            "function antigravityCommand",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, qml)
        self.assertIn("!antigravityProcess.running", qml)
        self.assertIn("antigravityWanted(agentIds)", qml)

    def test_manifest_publishes_antigravity_fallback(self):
        manifest = json.loads((MODULE_PATH.parent / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["version"], "1.3.0")
        self.assertTrue((MODULE_PATH.parent / "antigravity-collector.py").is_file())
        self.assertTrue(manifest["barWidget"]["defaults"]["providers"]["antigravity"]["enabled"])

    def test_atomic_record_write_creates_only_final_json(self):
        target = self.state_home / "omarchy" / "agents" / "usage" / "antigravity.json"
        record = self.collector.build_record(
            "Model\tWindow\t40%\t2026-09-09T16:00:00Z\n",
            "Credits remaining\t42\n",
        )

        self.collector.write_record(record, target)

        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["id"], "antigravity")
        self.assertEqual(list(target.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
