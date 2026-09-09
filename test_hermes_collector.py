#!/usr/bin/env python3
"""Focused synthetic SQLite tests for Hermes subscription-route usage."""

from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest


class TestHermesCollector(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hermes_home = Path(self.temp_dir.name) / ".hermes"
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.hermes_home / "state.db"

        self.xdg_state_home = Path(self.temp_dir.name) / ".local" / "state"
        self.xdg_state_home.mkdir(parents=True, exist_ok=True)

        self.orig_hermes_home = os.environ.get("HERMES_HOME")
        self.orig_xdg_state = os.environ.get("XDG_STATE_HOME")

        os.environ["HERMES_HOME"] = str(self.hermes_home)
        os.environ["XDG_STATE_HOME"] = str(self.xdg_state_home)

        # Import hermes-collector module dynamically
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hermes_collector",
            Path(__file__).parent / "hermes-collector.py",
        )
        self.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.collector)

    def tearDown(self):
        if self.orig_hermes_home is not None:
            os.environ["HERMES_HOME"] = self.orig_hermes_home
        else:
            os.environ.pop("HERMES_HOME", None)

        if self.orig_xdg_state is not None:
            os.environ["XDG_STATE_HOME"] = self.orig_xdg_state
        else:
            os.environ.pop("XDG_STATE_HOME", None)

        self.temp_dir.cleanup()

    def test_empty_database_routes(self):
        """Empty database should yield All subscriptions with 0 counts."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                first_seen REAL,
                billing_provider TEXT,
                billing_mode TEXT,
                billing_base_url TEXT
            )
        """)
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        self.assertFalse(record["ready"])
        self.assertEqual(record["totalPrompts"], 0)
        self.assertIn("routes", record)
        self.assertIsInstance(record["routes"], list)
        self.assertGreaterEqual(len(record["routes"]), 1)
        all_route = record["routes"][0]
        self.assertEqual(all_route["id"], "all")
        self.assertEqual(all_route["label"], "All subscriptions")
        self.assertEqual(all_route["tokens"], 0)
        self.assertEqual(all_route["apiCallCount"], 0)
        self.assertNotIn("subscriptions", record)

    def test_older_schema_compatibility_without_billing_columns(self):
        """Legacy schema lacking billing_provider/mode columns should work gracefully."""
        now_ts = time.time()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                first_seen REAL
            )
        """)
        conn.execute("INSERT INTO sessions VALUES ('s1', 'tui', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s1', 'claude-3-5-sonnet', 5, 1000, 500, 200, 100, ?)
        """, (now_ts,))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        self.assertTrue(record["ready"])
        self.assertEqual(record["totalPrompts"], 5)
        self.assertIn("routes", record)
        routes = record["routes"]
        # Should have All subscriptions and one Unattributed route
        self.assertEqual(len(routes), 2)
        self.assertEqual(routes[0]["id"], "all")
        self.assertEqual(routes[0]["tokens"], 1800)
        self.assertEqual(routes[1]["id"], "unattributed")
        self.assertEqual(routes[1]["label"], "Unattributed")
        self.assertEqual(routes[1]["tokens"], 1800)
        self.assertEqual(routes[1]["apiCallCount"], 5)
        self.assertEqual(routes[1]["sessions"], 1)

    def test_subscription_routes_grouping_and_metrics(self):
        """Test grouping by billing_provider + billing_mode with metrics."""
        now_dt = datetime.now()
        now_ts = now_dt.timestamp()

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                first_seen REAL,
                billing_provider TEXT,
                billing_mode TEXT,
                billing_base_url TEXT
            )
        """)

        # s1: TUI session with anthropic subscription
        conn.execute("INSERT INTO sessions VALUES ('s1', 'tui', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s1', 'claude-3-5-sonnet', 2, 100, 50, 0, 0, ?, 'anthropic', 'subscription', '[NOT_EMITTED_ENDPOINT]')
        """, (now_ts,))

        # s2: TUI session with both anthropic subscription and openai api_key
        conn.execute("INSERT INTO sessions VALUES ('s2', 'tui', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s2', 'claude-3-5-sonnet', 3, 200, 100, 0, 0, ?, 'anthropic', 'subscription', '[NOT_EMITTED_ENDPOINT]'),
            ('s2', 'gpt-4o', 4, 300, 200, 0, 0, ?, 'openai', 'api_key', '[NOT_EMITTED_ENDPOINT]')
        """, (now_ts, now_ts))

        # s3: TUI session with unattributed (blank/missing) billing
        conn.execute("INSERT INTO sessions VALUES ('s3', 'tui', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s3', 'deepseek-chat', 1, 50, 50, 0, 0, ?, '', NULL, NULL)
        """, (now_ts,))

        # s4: Non-TUI session (e.g. web/api) - should be ignored!
        conn.execute("INSERT INTO sessions VALUES ('s4', 'web', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s4', 'gpt-4o', 99, 5000, 5000, 0, 0, ?, 'openai', 'api_key', '[NOT_EMITTED_ENDPOINT]')
        """, (now_ts,))

        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        self.assertTrue(record["ready"])
        self.assertEqual(record["totalPrompts"], 10)  # 2 + 3 + 4 + 1
        self.assertEqual(record["totalSessions"], 3)  # s1, s2, s3

        routes = {r["id"]: r for r in record["routes"]}
        self.assertIn("all", routes)
        self.assertIn("anthropic:subscription", routes)
        self.assertIn("openai:api_key", routes)
        self.assertIn("unattributed", routes)

        # 1. All subscriptions aggregate
        all_r = routes["all"]
        self.assertEqual(all_r["label"], "All subscriptions")
        self.assertEqual(all_r["tokens"], 1050)
        self.assertEqual(all_r["apiCallCount"], 10)
        self.assertEqual(all_r["sessions"], 3)
        self.assertEqual(all_r["activeDays"], 1)

        # 2. Anthropic subscription route
        ant_r = routes["anthropic:subscription"]
        self.assertEqual(ant_r["label"], "Anthropic (Subscription)")
        self.assertEqual(ant_r["billingProvider"], "anthropic")
        self.assertEqual(ant_r["billingMode"], "subscription")
        self.assertEqual(ant_r["tokens"], 450)  # (100+50) + (200+100)
        self.assertEqual(ant_r["apiCallCount"], 5)  # 2 + 3
        self.assertEqual(ant_r["sessions"], 2)  # s1 and s2
        self.assertIn("claude-3-5-sonnet", ant_r["modelUsage"])
        self.assertEqual(ant_r["modelUsage"]["claude-3-5-sonnet"]["inputTokens"], 300)

        # 3. OpenAI API key route
        oai_r = routes["openai:api_key"]
        self.assertEqual(oai_r["label"], "OpenAI (API Key)")
        self.assertEqual(oai_r["billingProvider"], "openai")
        self.assertEqual(oai_r["billingMode"], "api_key")
        self.assertEqual(oai_r["tokens"], 500)  # 300 + 200
        self.assertEqual(oai_r["apiCallCount"], 4)
        self.assertEqual(oai_r["sessions"], 1)  # s2
        self.assertIn("gpt-4o", oai_r["modelUsage"])

        # 4. Unattributed route
        unatt_r = routes["unattributed"]
        self.assertEqual(unatt_r["label"], "Unattributed")
        self.assertEqual(unatt_r["tokens"], 100)
        self.assertEqual(unatt_r["apiCallCount"], 1)
        self.assertEqual(unatt_r["sessions"], 1)

    def test_secrets_and_billing_base_url_never_output(self):
        """billing_base_url or secret keys must NEVER leak into the record."""
        now_ts = time.time()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                first_seen REAL,
                billing_provider TEXT,
                billing_mode TEXT,
                billing_base_url TEXT
            )
        """)
        redacted_value = "[REDACTED_VALUE]"
        conn.execute("INSERT INTO sessions VALUES ('s1', 'tui', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s1', 'claude-3-5-sonnet', 1, 10, 10, 0, 0, ?, 'anthropic', 'subscription', ?)
        """, (now_ts, redacted_value))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        serialized = json.dumps(record)
        self.assertNotIn("billing_base_url", serialized)
        self.assertNotIn("billingBaseUrl", serialized)
        self.assertNotIn("confidential.corp.net", serialized)
        self.assertNotIn("[REDACTED_VALUE]", serialized)

    def test_active_days_and_recent_daily_tokens_per_route(self):
        """Test multi-day active days and recentDays per route."""
        now_dt = datetime.now()
        yesterday_dt = now_dt - timedelta(days=1)
        three_days_ago_dt = now_dt - timedelta(days=3)

        now_ts = now_dt.timestamp()
        yesterday_ts = yesterday_dt.timestamp()
        three_days_ago_ts = three_days_ago_dt.timestamp()

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                first_seen REAL,
                billing_provider TEXT,
                billing_mode TEXT
            )
        """)

        # Route A active today and yesterday
        conn.execute("INSERT INTO sessions VALUES ('s1', 'tui', ?)", (now_ts,))
        conn.execute("INSERT INTO sessions VALUES ('s2', 'tui', ?)", (yesterday_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s1', 'claude-3-5-sonnet', 1, 100, 0, 0, 0, ?, 'anthropic', 'subscription'),
            ('s2', 'claude-3-5-sonnet', 1, 200, 0, 0, 0, ?, 'anthropic', 'subscription')
        """, (now_ts, yesterday_ts))

        # Route B active only three days ago
        conn.execute("INSERT INTO sessions VALUES ('s3', 'tui', ?)", (three_days_ago_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s3', 'gpt-4o', 1, 300, 0, 0, 0, ?, 'openai', 'api_key')
        """, (three_days_ago_ts,))

        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        routes = {r["id"]: r for r in record["routes"]}

        # All subscriptions should have 3 active days
        self.assertEqual(routes["all"]["activeDays"], 3)

        # Anthropic should have 2 active days
        self.assertEqual(routes["anthropic:subscription"]["activeDays"], 2)

        # OpenAI should have 1 active day
        self.assertEqual(routes["openai:api_key"]["activeDays"], 1)

        # Check recentDays mapping for route B on three days ago
        d_str = three_days_ago_dt.strftime("%Y-%m-%d")
        recent_b = {d["date"]: d["messageCount"] for d in routes["openai:api_key"]["recentDays"]}
        self.assertEqual(recent_b.get(d_str), 300)
        recent_a = {d["date"]: d["messageCount"] for d in routes["anthropic:subscription"]["recentDays"]}
        self.assertEqual(recent_a.get(d_str), 0)

    def test_deterministic_order_and_attribution_sanitization(self):
        """Attributions with unusual casing or special chars should normalize safely and sort deterministically."""
        now_ts = time.time()
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_model_usage (
                session_id TEXT NOT NULL,
                model TEXT NOT NULL,
                api_call_count INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                first_seen REAL,
                billing_provider TEXT,
                billing_mode TEXT
            )
        """)

        conn.execute("INSERT INTO sessions VALUES ('s1', 'tui', ?)", (now_ts,))
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s1', 'model-1', 1, 10, 10, 0, 0, ?, 'OpenAI', 'API_KEY'),
            ('s1', 'model-2', 1, 20, 20, 0, 0, ?, 'together_ai', 'credits'),
            ('s1', 'model-3', 1, 30, 30, 0, 0, ?, '   ', 'subscription'),
            ('s1', 'model-4', 1, 40, 40, 0, 0, ?, 'untrusted/provider-value', 'api_key')
        """, (now_ts, now_ts, now_ts, now_ts))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        routes = record["routes"]
        route_ids = [r["id"] for r in routes]

        # 'all' must be first
        self.assertEqual(route_ids[0], "all")
        # 'together_ai:credits' and 'openai:api_key' should come before 'unattributed'
        self.assertEqual(route_ids[-1], "unattributed")
        self.assertIn("openai:api_key", route_ids)
        self.assertIn("together_ai:credits", route_ids)

        # Labels
        openai_r = next(r for r in routes if r["id"] == "openai:api_key")
        self.assertEqual(openai_r["label"], "OpenAI (API Key)")
        together_r = next(r for r in routes if r["id"] == "together_ai:credits")
        self.assertEqual(together_r["label"], "Together AI (Credits)")

        # The blank and untrusted providers should both collapse into 'unattributed'
        unatt_r = next(r for r in routes if r["id"] == "unattributed")
        self.assertEqual(unatt_r["label"], "Unattributed")
        # (30+30) + (40+40) = 140 tokens
        self.assertEqual(unatt_r["tokens"], 140)
        self.assertEqual(unatt_r["apiCallCount"], 2)

    def test_route_attribution_allowlist_collapses_unknown_values(self):
        """Only known provider/mode labels may appear in a route attribution."""
        known = self.collector.normalize_route_info("OpenAI", "API_KEY")
        self.assertEqual(known, ("openai:api_key", "OpenAI (API Key)", "openai", "api_key"))
        provider_only = self.collector.normalize_route_info("OpenAI", None)
        self.assertEqual(provider_only, ("openai", "OpenAI", "openai", ""))

        unsafe_values = [
            ("Acme Cloud", "subscription"),
            ("openai", "mystery_mode"),
            ("[NOT_EMITTED_ENDPOINT]", "subscription"),
            ("[REDACTED_VALUE]", "api_key"),
            ("x" * 65, "subscription"),
            ("openai", "[NOT_EMITTED_ENDPOINT]"),
            ("openai", "[REDACTED_VALUE]"),
            ("openai", "api key"),
            ("openai\nsecret", "subscription"),
        ]
        for raw_provider, raw_mode in unsafe_values:
            with self.subTest(raw_provider=raw_provider, raw_mode=raw_mode):
                self.assertEqual(
                    self.collector.normalize_route_info(raw_provider, raw_mode),
                    ("unattributed", "Unattributed", "", ""),
                )

    def test_sync_snapshot_source_omits_local_routes(self):
        """The QML sync serializer must not write local route data."""
        main_qml = (Path(__file__).parent / "Main.qml").read_text(encoding="utf-8")
        start = main_qml.index("function providerSnapshot(record)")
        end = main_qml.index("\n  function localSnapshot()", start)
        provider_snapshot = main_qml[start:end]

        self.assertNotIn("routes", provider_snapshot)
        self.assertNotIn("subscriptions", provider_snapshot)
        self.assertIn("providerMap[String(record.id)] = providerSnapshot(record)", main_qml[end:])


if __name__ == "__main__":
    unittest.main()
