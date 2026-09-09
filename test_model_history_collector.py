#!/usr/bin/env python3
"""Synthetic, privacy-boundary tests for the rolling model-token history collector."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("model-history-collector.py")


def load_collector():
    spec = importlib.util.spec_from_file_location("model_history_collector", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load collector module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestModelHistoryCollector(unittest.TestCase):
    def setUp(self):
        self.collector = load_collector()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.usage_dir = self.root / "usage"
        self.usage_dir.mkdir()
        self.history_path = self.usage_dir / ".model-history.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def record(provider="codex", *, model_usage=None, today=None, routes=None):
        record = {
            "schemaVersion": 1,
            "id": provider,
            "updatedAt": "2026-09-15T12:00:00+00:00",
            "modelUsage": model_usage if model_usage is not None else {},
        }
        if today is not None:
            record["todayTokensByModel"] = today
        if routes is not None:
            record["routes"] = routes
        return record

    @staticmethod
    def bucket(input_tokens, output_tokens=0, cache_read=0, cache_creation=0):
        return {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadInputTokens": cache_read,
            "cacheCreationInputTokens": cache_creation,
        }

    def write_record(self, name, payload):
        (self.usage_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    def run_once(self, payload, *, now):
        self.write_record("codex.json", payload)
        return self.collector.collect_and_write(
            self.usage_dir,
            now=now,
            history_path=self.history_path,
        )

    def load_history(self):
        return json.loads(self.history_path.read_text(encoding="utf-8"))

    def test_today_and_all_use_standard_model_usage_and_today_map(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        self.write_record(
            "codex.json",
            self.record(
                model_usage={"gpt-5": self.bucket(100, 50, 25, 5)},
                today={"gpt-5": 42, "gpt-5-mini": 3},
            ),
        )

        result = self.collector.build_history(
            self.usage_dir,
            now=now,
            history_path=self.history_path,
        )
        provider = result["providers"]["codex"]
        periods = provider["modelUsageByPeriod"]

        self.assertEqual(result["schemaVersion"], 1)
        self.assertIn("updatedAt", result)
        self.assertEqual(periods["today"], {"gpt-5": 42, "gpt-5-mini": 3})
        self.assertEqual(periods["7d"], {"gpt-5": 42, "gpt-5-mini": 3})
        self.assertEqual(periods["month"], {"gpt-5": 42, "gpt-5-mini": 3})
        self.assertEqual(periods["all"], {"gpt-5": 180})
        self.assertEqual(set(periods), {"today", "7d", "month", "all"})
        self.assertLessEqual(len(provider["dailyTokenBuckets"]), 31)

    def test_seven_day_and_rolling_month_aggregation(self):
        observations = [
            (datetime(2026, 8, 15, 12, tzinfo=timezone.utc), 50, 50),
            (datetime(2026, 9, 8, 12, tzinfo=timezone.utc), 60, 10),
            (datetime(2026, 9, 10, 12, tzinfo=timezone.utc), 80, 20),
            (datetime(2026, 9, 14, 12, tzinfo=timezone.utc), 110, 30),
            (datetime(2026, 9, 15, 12, tzinfo=timezone.utc), 150, 40),
        ]
        for observation_now, all_tokens, today_tokens in observations:
            payload = self.record(
                model_usage={"model-a": self.bucket(all_tokens)},
                today={"model-a": today_tokens},
            )
            self.run_once(payload, now=observation_now)

        history = self.load_history()
        periods = history["providers"]["codex"]["modelUsageByPeriod"]
        self.assertEqual(periods["7d"], {"model-a": 90})
        self.assertEqual(periods["month"], {"model-a": 100})
        self.assertEqual(periods["all"], {"model-a": 150})
        self.assertEqual(periods["today"], {"model-a": 40})

    def test_daily_history_is_bounded_to_latest_31_buckets(self):
        start = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        for offset in range(33):
            observation_now = start + timedelta(days=offset)
            payload = self.record(
                model_usage={"model-a": self.bucket(offset + 1)},
                today={"model-a": 1},
            )
            self.run_once(payload, now=observation_now)

        buckets = self.load_history()["providers"]["codex"]["dailyTokenBuckets"]
        self.assertEqual(len(buckets), 31)
        self.assertEqual(min(buckets), "2026-08-16")
        self.assertEqual(max(buckets), "2026-09-15")

    def test_hermes_route_keys_are_provider_colon_route_id(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        routes = [
            {
                "id": "all",
                "isAll": True,
                "modelUsage": {"gpt-5": self.bucket(18)},
                "todayTokensByModel": {"gpt-5": 18},
            },
            {
                "id": "openai-codex",
                "modelUsage": {"gpt-5": self.bucket(7)},
                "todayTokensByModel": {"gpt-5": 7},
            },
            {
                "id": "opencode-go",
                "modelUsage": {"grok-4.6": self.bucket(11)},
                "todayTokensByModel": {"grok-4.6": 11},
            },
        ]
        self.write_record(
            "hermes.json",
            self.record(
                provider="hermes",
                model_usage={"gpt-5": self.bucket(18)},
                today={"gpt-5": 18},
                routes=routes,
            ),
        )

        result = self.collector.build_history(
            self.usage_dir,
            now=now,
            history_path=self.history_path,
        )
        self.assertIn("hermes", result["providers"])
        self.assertIn("hermes:all", result["providers"])
        self.assertIn("hermes:openai-codex", result["providers"])
        self.assertIn("hermes:opencode-go", result["providers"])
        self.assertNotIn("openai-codex", result["providers"])
        self.assertEqual(
            result["providers"]["hermes:openai-codex"]["modelUsageByPeriod"]["all"],
            {"gpt-5": 7},
        )

    def test_first_observation_never_attributes_historical_all_time_to_today(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        first = self.record(
            model_usage={"model-a": self.bucket(100)},
        )
        self.run_once(first, now=now)
        first_history = self.load_history()
        self.assertEqual(
            first_history["providers"]["codex"]["modelUsageByPeriod"]["today"],
            {},
        )
        self.assertEqual(
            first_history["providers"]["codex"]["modelUsageByPeriod"]["all"],
            {"model-a": 100},
        )

        second = self.record(
            model_usage={"model-a": self.bucket(130), "new-model": self.bucket(50)},
        )
        self.run_once(second, now=now)
        second_history = self.load_history()
        periods = second_history["providers"]["codex"]["modelUsageByPeriod"]
        self.assertEqual(periods["today"], {"model-a": 30})
        self.assertEqual(periods["all"], {"model-a": 130, "new-model": 50})

    def test_atomic_output_replaces_whole_sidecar_and_cleans_temp_file(self):
        first = {
            "schemaVersion": 1,
            "updatedAt": "2026-09-15T12:00:00+00:00",
            "providers": {},
        }
        second = {
            "schemaVersion": 1,
            "updatedAt": "2026-09-15T12:01:00+00:00",
            "providers": {"codex": {"modelUsageByPeriod": {"today": {}, "7d": {}, "month": {}, "all": {}}}},
        }
        self.collector.atomic_write_history(self.history_path, first)
        self.assertEqual(self.load_history(), first)
        self.collector.atomic_write_history(self.history_path, second)
        self.assertEqual(self.load_history(), second)
        self.assertFalse(list(self.usage_dir.glob(".model-history.json.*.tmp")))
        self.assertEqual(self.history_path.stat().st_mode & 0o777, 0o600)

        with mock.patch.object(self.collector.os, "replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                self.collector.atomic_write_history(self.history_path, first)
        self.assertEqual(self.load_history(), second)
        self.assertFalse(list(self.usage_dir.glob(".model-history.json.*.tmp")))

    def test_malformed_and_unsafe_usage_is_rejected_without_output(self):
        self.write_record(
            "unsafe.json",
            self.record(
                model_usage={"../secret": self.bucket(5)},
            ),
        )
        with self.assertRaises(self.collector.CollectorError):
            self.collector.collect_and_write(
                self.usage_dir,
                now=datetime(2026, 9, 15, tzinfo=timezone.utc),
                history_path=self.history_path,
            )
        self.assertFalse(self.history_path.exists())

        (self.usage_dir / "unsafe.json").unlink()
        (self.usage_dir / "malformed.json").write_text(
            '{"schemaVersion":1,"id":"codex","modelUsage":{"m":{"inputTokens":NaN}}}',
            encoding="utf-8",
        )
        with self.assertRaises(self.collector.CollectorError):
            self.collector.build_history(
                self.usage_dir,
                now=datetime(2026, 9, 15, tzinfo=timezone.utc),
                history_path=self.history_path,
            )

    def test_quota_fields_never_become_model_tokens(self):
        self.write_record(
            "antigravity.json",
            {
                **self.record(provider="antigravity"),
                "limits": [{"title": "model quota", "percent": 0.5, "remaining": 42}],
            },
        )
        result = self.collector.build_history(
            self.usage_dir,
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
            history_path=self.history_path,
        )
        periods = result["providers"]["antigravity"]["modelUsageByPeriod"]
        self.assertEqual(periods, {"today": {}, "7d": {}, "month": {}, "all": {}})
        serialized = json.dumps(result).lower()
        self.assertNotIn("percent", serialized)
        self.assertNotIn("remaining", serialized)
        self.assertNotIn("limits", serialized)

    def test_private_sources_are_not_read(self):
        self.write_record(
            "codex.json",
            self.record(model_usage={"model-a": self.bucket(9)}, today={"model-a": 9}),
        )
        # These deliberately contain invalid JSON/text. They must be ignored by
        # filename/type filtering rather than opened as usage records.
        (self.usage_dir / "credentials.json").write_text("not-json", encoding="utf-8")
        (self.usage_dir / "prompts.json").write_text("not-json", encoding="utf-8")
        (self.usage_dir / "transcript.json").write_text("not-json", encoding="utf-8")
        (self.usage_dir / "provider.db").write_bytes(b"opaque private database")
        (self.usage_dir / "messages.txt").write_text("private transcript", encoding="utf-8")

        result = self.collector.build_history(
            self.usage_dir,
            now=datetime(2026, 9, 15, tzinfo=timezone.utc),
            history_path=self.history_path,
        )
        self.assertEqual(set(result["providers"]), {"codex"})
        serialized = json.dumps(result)
        self.assertNotIn("private transcript", serialized)
        self.assertNotIn("opaque private database", serialized)

    def test_force_cli_is_silent_and_writes_only_sidecar(self):
        self.write_record(
            "codex.json",
            self.record(model_usage={"model-a": self.bucket(4)}, today={"model-a": 4}),
        )
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg-state")
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = self.collector.main(["--force"])
            self.assertEqual(return_code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue(
                (self.root / "xdg-state" / "omarchy" / "agents" / "usage" / ".model-history.json").is_file()
            )
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
