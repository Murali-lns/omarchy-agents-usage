#!/usr/bin/env python3
"""Synthetic fixture tests for the local Grok CLI usage collector."""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest


class TestGrokCollector(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.grok_home = root / ".grok"
        self.sessions = self.grok_home / "sessions" / "workspace"
        self.sessions.mkdir(parents=True)
        self.xdg_state_home = root / ".local" / "state"
        self.xdg_state_home.mkdir(parents=True)
        self.omarchy_path = root / "omarchy"
        (self.omarchy_path / "bin").mkdir(parents=True)

        self.orig = {
            "GROK_HOME": os.environ.get("GROK_HOME"),
            "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
            "OMARCHY_PATH": os.environ.get("OMARCHY_PATH"),
        }
        os.environ["GROK_HOME"] = str(self.grok_home)
        os.environ["XDG_STATE_HOME"] = str(self.xdg_state_home)
        os.environ["OMARCHY_PATH"] = str(self.omarchy_path)

        spec = importlib.util.spec_from_file_location(
            "grok_collector",
            Path(__file__).parent / "grok-collector.py",
        )
        self.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.collector)

    def tearDown(self):
        for key, value in self.orig.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def _write_session(self, session_id, session, turns, session_kind=None, extra_files=None):
        folder = self.sessions / session_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessionId": session_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "session": session,
            "turns": turns,
        }
        (folder / "usage.json").write_text(json.dumps(payload), encoding="utf-8")
        summary = {"info": {"id": session_id, "cwd": "/tmp"}}
        if session_kind is not None:
            summary["session_kind"] = session_kind
        (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        for name, content in (extra_files or {}).items():
            (folder / name).write_text(content, encoding="utf-8")
        return folder

    @staticmethod
    def _iso(days_ago=0, hour=12):
        moment = datetime.now().astimezone().replace(hour=hour, minute=0, second=0, microsecond=0)
        moment = moment - timedelta(days=days_ago)
        return moment.isoformat()

    def test_empty_home_is_not_ready(self):
        record = self.collector.collect_usage()
        self.assertEqual(record["id"], "grok")
        self.assertEqual(record["name"], "Grok")
        self.assertFalse(record["ready"])
        self.assertEqual(record["limits"], [])
        self.assertEqual(record["tierLabel"], "")
        self.assertIn("usage not found", record["usageStatusText"].lower())

    def test_usage_json_cache_split_and_periods(self):
        self._write_session(
            "parent",
            {
                "inputTokens": 130,
                "outputTokens": 20,
                "cachedReadTokens": 30,
                "cacheCreationTokens": 5,
                "reasoningTokens": 8,
                "totalTokens": 150,
                "turnCount": 2,
                "primaryModelId": "grok-4.6-build",
                "modelUsage": {
                    "grok-4.6-build": {
                        "inputTokens": 130,
                        "outputTokens": 20,
                        "cachedReadTokens": 30,
                        "cacheCreationTokens": 5,
                        "reasoningTokens": 8,
                    }
                },
            },
            [
                {
                    "endedAt": self._iso(0),
                    "inputTokens": 80,
                    "outputTokens": 10,
                    "cachedReadTokens": 20,
                    "cacheCreationTokens": 5,
                    "primaryModelId": "grok-4.6-build",
                    "modelUsage": {
                        "grok-4.6-build": {
                            "inputTokens": 80,
                            "outputTokens": 10,
                            "cachedReadTokens": 20,
                            "cacheCreationTokens": 5,
                        }
                    },
                },
                {
                    "endedAt": self._iso(3),
                    "inputTokens": 50,
                    "outputTokens": 10,
                    "cachedReadTokens": 10,
                    "cacheCreationTokens": 0,
                    "primaryModelId": "grok-4.6-build",
                    "modelUsage": {
                        "grok-4.6-build": {
                            "inputTokens": 50,
                            "outputTokens": 10,
                            "cachedReadTokens": 10,
                            "cacheCreationTokens": 0,
                        }
                    },
                },
            ],
        )

        record = self.collector.collect_usage()
        self.assertTrue(record["ready"])
        self.assertEqual(record["totalSessions"], 1)
        self.assertEqual(record["todaySessions"], 1)
        self.assertEqual(record["totalPrompts"], 2)
        self.assertEqual(record["todayPrompts"], 1)
        self.assertEqual(record["limits"], [])
        self.assertEqual(record["usageStatusText"], "Limit unavailable / usage-only")

        all_usage = record["modelUsage"]["grok-4.6-build"]
        self.assertEqual(all_usage["inputTokens"], 100)
        self.assertEqual(all_usage["outputTokens"], 20)
        self.assertEqual(all_usage["cacheReadInputTokens"], 30)
        self.assertEqual(all_usage["cacheCreationInputTokens"], 5)

        today = record["modelUsageByPeriod"]["today"]["grok-4.6-build"]
        self.assertEqual(today["inputTokens"], 60)
        self.assertEqual(today["outputTokens"], 10)
        self.assertEqual(today["cacheReadInputTokens"], 20)
        self.assertEqual(today["cacheCreationInputTokens"], 5)
        self.assertEqual(record["todayTotalTokens"], 95)

        week = record["modelUsageByPeriod"]["7d"]["grok-4.6-build"]
        self.assertEqual(week["inputTokens"], 100)
        self.assertEqual(week["outputTokens"], 20)

    def test_subagent_sessions_are_skipped(self):
        self._write_session(
            "parent",
            {
                "inputTokens": 10,
                "outputTokens": 2,
                "cachedReadTokens": 0,
                "cacheCreationTokens": 0,
                "turnCount": 1,
                "primaryModelId": "grok-4.6-build",
                "modelUsage": {"grok-4.6-build": {"inputTokens": 10, "outputTokens": 2}},
            },
            [
                {
                    "endedAt": self._iso(0),
                    "inputTokens": 10,
                    "outputTokens": 2,
                    "primaryModelId": "grok-4.6-build",
                    "modelUsage": {"grok-4.6-build": {"inputTokens": 10, "outputTokens": 2}},
                }
            ],
        )
        self._write_session(
            "child",
            {
                "inputTokens": 999,
                "outputTokens": 999,
                "turnCount": 9,
                "primaryModelId": "grok-4.6-build",
                "modelUsage": {"grok-4.6-build": {"inputTokens": 999, "outputTokens": 999}},
            },
            [
                {
                    "endedAt": self._iso(0),
                    "inputTokens": 999,
                    "outputTokens": 999,
                    "primaryModelId": "grok-4.6-build",
                    "modelUsage": {"grok-4.6-build": {"inputTokens": 999, "outputTokens": 999}},
                }
            ],
            session_kind="subagent_fork",
        )

        record = self.collector.collect_usage()
        self.assertEqual(record["totalSessions"], 1)
        self.assertEqual(record["totalPrompts"], 1)
        self.assertEqual(record["modelUsage"]["grok-4.6-build"]["inputTokens"], 10)
        self.assertNotIn("unattributed", record["modelUsage"])

    def test_does_not_infer_subscription_from_model_name_or_read_private_files(self):
        folder = self._write_session(
            "parent",
            {
                "inputTokens": 4,
                "outputTokens": 1,
                "turnCount": 1,
                "primaryModelId": "grok-4.6-build",
                "modelUsage": {"grok-4.6-build": {"inputTokens": 4, "outputTokens": 1}},
            },
            [
                {
                    "endedAt": self._iso(0),
                    "inputTokens": 4,
                    "outputTokens": 1,
                    "primaryModelId": "grok-4.6-build",
                    "modelUsage": {"grok-4.6-build": {"inputTokens": 4, "outputTokens": 1}},
                }
            ],
            extra_files={
                "chat_history.jsonl": '{"prompt":"do not read this"}\n',
                "events.jsonl": '{"content":"secret transcript"}\n',
            },
        )
        (self.grok_home / "auth.json").write_text('{"access_token":"do-not-read"}', encoding="utf-8")

        opened = []
        original_read_text = Path.read_text

        def tracking_read_text(path, *args, **kwargs):
            opened.append(Path(path).name)
            return original_read_text(path, *args, **kwargs)

        Path.read_text = tracking_read_text
        try:
            record = self.collector.collect_usage()
        finally:
            Path.read_text = original_read_text

        self.assertTrue(record["ready"])
        self.assertEqual(record["tierLabel"], "")
        self.assertEqual(record["limits"], [])
        self.assertNotIn("auth.json", opened)
        self.assertNotIn("chat_history.jsonl", opened)
        self.assertNotIn("events.jsonl", opened)
        self.assertTrue((folder / "chat_history.jsonl").exists())

    def test_packaged_collector_is_deferred_and_does_not_write(self):
        official = self.omarchy_path / "bin" / "omarchy-agent-usage-grok"
        official.write_text("#!/bin/sh\necho official\n", encoding="utf-8")
        official.chmod(official.stat().st_mode | stat.S_IXUSR)
        self._write_session(
            "parent",
            {
                "inputTokens": 4,
                "outputTokens": 1,
                "turnCount": 1,
                "primaryModelId": "grok-4.6-build",
                "modelUsage": {"grok-4.6-build": {"inputTokens": 4, "outputTokens": 1}},
            },
            [
                {
                    "endedAt": self._iso(0),
                    "inputTokens": 4,
                    "outputTokens": 1,
                    "primaryModelId": "grok-4.6-build",
                    "modelUsage": {"grok-4.6-build": {"inputTokens": 4, "outputTokens": 1}},
                }
            ],
        )

        self.assertIsNotNone(self.collector.packaged_grok_collector())
        usage_dir = self.xdg_state_home / "omarchy" / "agents" / "usage"
        usage_dir.mkdir(parents=True)
        marker = usage_dir / "grok.json"
        marker.write_text('{"id":"official-marker"}\n', encoding="utf-8")

        self.assertEqual(self.collector.main([]), 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"id":"official-marker"}\n')

    def test_atomic_write_uses_standard_record_id(self):
        record = self.collector.empty_record("fixture")
        path = self.collector.atomic_write_record(
            self.xdg_state_home / "omarchy" / "agents" / "usage",
            record,
        )
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, "grok.json")
        self.assertEqual(saved["id"], "grok")
        self.assertEqual(saved["name"], "Grok")

    def test_grok_icon_assets_exist_for_runtime_panel(self):
        assets = Path(__file__).parent / "assets"
        self.assertTrue((assets / "grok.svg").is_file())
        self.assertTrue((assets / "grok-light.svg").is_file())


if __name__ == "__main__":
    unittest.main()
