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

    def _create_usage_schema(self):
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

    @staticmethod
    def _local_day_timestamp(days_ago):
        day = datetime.now().date() - timedelta(days=days_ago)
        return datetime(day.year, day.month, day.day, 12, 0, 0).timestamp()

    def _insert_usage_rows(self, rows):
        conn = sqlite3.connect(self.db_path)
        for index, row in enumerate(rows):
            session_id = f"synthetic-{index}"
            timestamp = self._local_day_timestamp(row["days_ago"])
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = row["bucket"]
            conn.execute(
                "INSERT INTO sessions VALUES (?, 'tui', ?)",
                (session_id, timestamp),
            )
            conn.execute("""
                INSERT INTO session_model_usage (
                    session_id, model, api_call_count, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, first_seen,
                    billing_provider, billing_mode, billing_base_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id,
                row["model"],
                row.get("api_calls", 1),
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
                timestamp,
                row.get("provider"),
                row.get("mode"),
                row.get("base_url"),
            ))
        conn.commit()
        conn.close()

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
        expected_periods = {"today": {}, "7d": {}, "month": {}, "all": {}}
        self.assertEqual(record["modelUsageByPeriod"], expected_periods)
        self.assertEqual(all_route["modelUsageByPeriod"], expected_periods)
        self.assertEqual(
            self.collector.empty_route("synthetic", "Synthetic")["modelUsageByPeriod"],
            expected_periods,
        )
        self.assertNotIn("subscriptions", record)

    def test_model_usage_by_period_aggregates_full_buckets(self):
        """Period maps sum complete model token buckets on local calendar days."""
        self._create_usage_schema()
        self._insert_usage_rows([
            {"days_ago": 0, "model": "alpha", "bucket": (10, 20, 30, 40)},
            {"days_ago": 0, "model": "beta", "bucket": (2, 3, 4, 5)},
            {"days_ago": 1, "model": "alpha", "bucket": (100, 200, 300, 400)},
            {"days_ago": 1, "model": "beta", "bucket": (20, 30, 40, 50)},
            {"days_ago": 6, "model": "alpha", "bucket": (600, 1200, 1800, 2400)},
            {"days_ago": 7, "model": "alpha", "bucket": (700, 800, 900, 1000)},
            {"days_ago": 29, "model": "beta", "bucket": (29, 29, 29, 29)},
            {"days_ago": 30, "model": "alpha", "bucket": (30, 30, 30, 30)},
        ])

        record = self.collector.collect_usage()
        periods = record["modelUsageByPeriod"]

        self.assertEqual(periods["today"], {
            "alpha": {
                "inputTokens": 10,
                "outputTokens": 20,
                "cacheReadInputTokens": 30,
                "cacheCreationInputTokens": 40,
            },
            "beta": {
                "inputTokens": 2,
                "outputTokens": 3,
                "cacheReadInputTokens": 4,
                "cacheCreationInputTokens": 5,
            },
        })
        self.assertEqual(periods["7d"]["alpha"], {
            "inputTokens": 710,
            "outputTokens": 1420,
            "cacheReadInputTokens": 2130,
            "cacheCreationInputTokens": 2840,
        })
        self.assertEqual(periods["7d"]["beta"], {
            "inputTokens": 22,
            "outputTokens": 33,
            "cacheReadInputTokens": 44,
            "cacheCreationInputTokens": 55,
        })
        self.assertEqual(periods["month"]["alpha"], {
            "inputTokens": 1410,
            "outputTokens": 2220,
            "cacheReadInputTokens": 3030,
            "cacheCreationInputTokens": 3840,
        })
        self.assertEqual(periods["month"]["beta"], {
            "inputTokens": 51,
            "outputTokens": 62,
            "cacheReadInputTokens": 73,
            "cacheCreationInputTokens": 84,
        })
        self.assertEqual(periods["all"]["alpha"], {
            "inputTokens": 1440,
            "outputTokens": 2250,
            "cacheReadInputTokens": 3060,
            "cacheCreationInputTokens": 3870,
        })
        self.assertEqual(periods["all"], record["modelUsage"])
        self.assertEqual(record["routes"][0]["modelUsageByPeriod"], periods)

        for period_usage in periods.values():
            for bucket in period_usage.values():
                self.assertEqual(
                    set(bucket),
                    {
                        "inputTokens",
                        "outputTokens",
                        "cacheReadInputTokens",
                        "cacheCreationInputTokens",
                    },
                )

    def test_model_usage_by_period_is_route_specific(self):
        """Each discovered route gets independent period buckets and boundaries."""
        self._create_usage_schema()
        self._insert_usage_rows([
            {
                "days_ago": 0,
                "model": "claude",
                "bucket": (10, 20, 30, 40),
                "provider": "anthropic",
                "mode": "subscription",
            },
            {
                "days_ago": 1,
                "model": "claude",
                "bucket": (100, 200, 300, 400),
                "provider": "anthropic",
                "mode": "subscription",
            },
            {
                "days_ago": 0,
                "model": "gpt",
                "bucket": (1, 2, 3, 4),
                "provider": "openai",
                "mode": "api_key",
            },
            {
                "days_ago": 7,
                "model": "gpt",
                "bucket": (7, 8, 9, 10),
                "provider": "openai",
                "mode": "api_key",
            },
            {
                "days_ago": 29,
                "model": "gpt",
                "bucket": (29, 30, 31, 32),
                "provider": "openai",
                "mode": "api_key",
            },
            {
                "days_ago": 30,
                "model": "gpt",
                "bucket": (300, 301, 302, 303),
                "provider": "openai",
                "mode": "api_key",
            },
        ])

        record = self.collector.collect_usage()
        routes = {route["id"]: route for route in record["routes"]}

        anthropic_periods = routes["anthropic"]["modelUsageByPeriod"]
        self.assertEqual(anthropic_periods["today"]["claude"], {
            "inputTokens": 10,
            "outputTokens": 20,
            "cacheReadInputTokens": 30,
            "cacheCreationInputTokens": 40,
        })
        self.assertEqual(anthropic_periods["7d"]["claude"], {
            "inputTokens": 110,
            "outputTokens": 220,
            "cacheReadInputTokens": 330,
            "cacheCreationInputTokens": 440,
        })
        self.assertEqual(anthropic_periods["month"], anthropic_periods["all"])

        openai_periods = routes["openai"]["modelUsageByPeriod"]
        self.assertEqual(openai_periods["today"]["gpt"], {
            "inputTokens": 1,
            "outputTokens": 2,
            "cacheReadInputTokens": 3,
            "cacheCreationInputTokens": 4,
        })
        self.assertEqual(openai_periods["7d"], openai_periods["today"])
        self.assertEqual(openai_periods["month"]["gpt"], {
            "inputTokens": 37,
            "outputTokens": 40,
            "cacheReadInputTokens": 43,
            "cacheCreationInputTokens": 46,
        })
        self.assertEqual(openai_periods["all"]["gpt"], {
            "inputTokens": 337,
            "outputTokens": 341,
            "cacheReadInputTokens": 345,
            "cacheCreationInputTokens": 349,
        })

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
        self.assertIn("anthropic", routes)
        self.assertIn("openai", routes)
        self.assertIn("unattributed", routes)

        # 1. All subscriptions aggregate
        all_r = routes["all"]
        self.assertEqual(all_r["label"], "All subscriptions")
        self.assertEqual(all_r["tokens"], 1050)
        self.assertEqual(all_r["apiCallCount"], 10)
        self.assertEqual(all_r["sessions"], 3)
        self.assertEqual(all_r["activeDays"], 1)

        # 2. Anthropic subscription route
        ant_r = routes["anthropic"]
        self.assertEqual(ant_r["label"], "Anthropic")
        self.assertEqual(ant_r["billingProvider"], "anthropic")
        self.assertEqual(ant_r["tokens"], 450)  # (100+50) + (200+100)
        self.assertEqual(ant_r["apiCallCount"], 5)  # 2 + 3
        self.assertEqual(ant_r["sessions"], 2)  # s1 and s2
        self.assertIn("claude-3-5-sonnet", ant_r["modelUsage"])
        self.assertEqual(ant_r["modelUsage"]["claude-3-5-sonnet"]["inputTokens"], 300)

        # 3. OpenAI API key route
        oai_r = routes["openai"]
        self.assertEqual(oai_r["label"], "OpenAI")
        self.assertEqual(oai_r["billingProvider"], "openai")
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
        self.assertIn("modelUsageByPeriod", record)
        for route in record["routes"]:
            self.assertIn("modelUsageByPeriod", route)
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
        self.assertEqual(routes["anthropic"]["activeDays"], 2)

        # OpenAI should have 1 active day
        self.assertEqual(routes["openai"]["activeDays"], 1)

        # Check recentDays mapping for route B on three days ago
        d_str = three_days_ago_dt.strftime("%Y-%m-%d")
        recent_b = {d["date"]: d["messageCount"] for d in routes["openai"]["recentDays"]}
        self.assertEqual(recent_b.get(d_str), 300)
        recent_a = {d["date"]: d["messageCount"] for d in routes["anthropic"]["recentDays"]}
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
        # 'together' and 'openai' should come before 'unattributed'
        self.assertEqual(route_ids[-1], "unattributed")
        self.assertIn("openai", route_ids)
        self.assertIn("together", route_ids)

        # Labels
        openai_r = next(r for r in routes if r["id"] == "openai")
        self.assertEqual(openai_r["label"], "OpenAI")
        together_r = next(r for r in routes if r["id"] == "together")
        self.assertEqual(together_r["label"], "Together AI")

        # The blank and untrusted providers should both collapse into 'unattributed'
        unatt_r = next(r for r in routes if r["id"] == "unattributed")
        self.assertEqual(unatt_r["label"], "Unattributed")
        # (30+30) + (40+40) = 140 tokens
        self.assertEqual(unatt_r["tokens"], 140)
        self.assertEqual(unatt_r["apiCallCount"], 2)

    def test_route_attribution_allowlist_and_mode_omission(self):
        """Only known provider labels are emitted; unsafe modes are omitted without hiding the provider."""
        known = self.collector.normalize_route_info("OpenAI", "API_KEY")
        self.assertEqual(known, ("openai", "OpenAI", "openai", "api_key"))
        provider_only = self.collector.normalize_route_info("OpenAI", None)
        self.assertEqual(provider_only, ("openai", "OpenAI", "openai", ""))

        unsafe_providers = [
            ("Acme Cloud", "subscription"),
            ("openai\nsecret", "subscription"),
            ("[NOT_EMITTED_ENDPOINT]", "subscription"),
            ("[REDACTED_VALUE]", "api_key"),
            ("x" * 65, "subscription"),
        ]
        for raw_provider, raw_mode in unsafe_providers:
            with self.subTest(raw_provider=raw_provider, raw_mode=raw_mode):
                self.assertEqual(
                    self.collector.normalize_route_info(raw_provider, raw_mode),
                    ("unattributed", "Unattributed", "", ""),
                )

        unsafe_modes = ["[NOT_EMITTED_ENDPOINT]", "[REDACTED_VALUE]", "api key"]
        for raw_mode in unsafe_modes:
            with self.subTest(raw_mode=raw_mode):
                self.assertEqual(
                    self.collector.normalize_route_info("openai", raw_mode),
                    ("openai", "OpenAI", "openai", ""),
                )

    def test_provider_alias_discovery_and_grouping(self):
        """Provider aliases must discover canonical routes and group across aliases."""
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
            ('s1', 'm1', 1, 100, 0, 0, 0, ?, 'openai-codex', 'codex_responses'),
            ('s1', 'm2', 2, 200, 0, 0, 0, ?, 'codex', 'chat_completions'),
            ('s1', 'm3', 3, 300, 0, 0, 0, ?, 'opencode-go', 'chat_completions'),
            ('s1', 'm4', 4, 400, 0, 0, 0, ?, 'opencode', 'chat_completions'),
            ('s1', 'm5', 5, 500, 0, 0, 0, ?, 'xai', 'chat_completions'),
            ('s1', 'm6', 6, 600, 0, 0, 0, ?, 'grok', 'chat_completions'),
            ('s1', 'm7', 7, 700, 0, 0, 0, ?, 'google', 'chat_completions'),
            ('s1', 'm8', 8, 800, 0, 0, 0, ?, 'gemini', 'chat_completions'),
            ('s1', 'm9', 9, 900, 0, 0, 0, ?, 'claude', 'anthropic_messages'),
            ('s1', 'm10', 10, 1000, 0, 0, 0, ?, 'anthropic', 'anthropic_messages'),
            ('s1', 'm11', 11, 1100, 0, 0, 0, ?, 'antigravity', 'chat_completions')
        """, (now_ts,) * 11)
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        routes = {r["id"]: r for r in record["routes"]}

        # openai-codex and codex must be merged into openai-codex
        self.assertIn("openai-codex", routes)
        self.assertEqual(routes["openai-codex"]["label"], "OpenAI Codex")
        self.assertEqual(routes["openai-codex"]["tokens"], 300)
        self.assertEqual(routes["openai-codex"]["apiCallCount"], 3)

        # opencode-go and opencode must be merged into opencode-go
        self.assertIn("opencode-go", routes)
        self.assertEqual(routes["opencode-go"]["label"], "OpenCode Go")
        self.assertEqual(routes["opencode-go"]["tokens"], 700)
        self.assertEqual(routes["opencode-go"]["apiCallCount"], 7)

        # xai and grok must be merged into xai with label Grok
        self.assertIn("xai", routes)
        self.assertEqual(routes["xai"]["label"], "Grok")
        self.assertEqual(routes["xai"]["tokens"], 1100)
        self.assertEqual(routes["xai"]["apiCallCount"], 11)

        # google and gemini must be merged into google with label Google Gemini
        self.assertIn("google", routes)
        self.assertEqual(routes["google"]["label"], "Google Gemini")
        self.assertEqual(routes["google"]["tokens"], 1500)
        self.assertEqual(routes["google"]["apiCallCount"], 15)

        # claude and anthropic must be merged into anthropic with label Anthropic
        self.assertIn("anthropic", routes)
        self.assertEqual(routes["anthropic"]["label"], "Anthropic")
        self.assertEqual(routes["anthropic"]["tokens"], 1900)
        self.assertEqual(routes["anthropic"]["apiCallCount"], 19)

        # antigravity
        self.assertIn("antigravity", routes)
        self.assertEqual(routes["antigravity"]["label"], "Antigravity")
        self.assertEqual(routes["antigravity"]["tokens"], 1100)
        self.assertEqual(routes["antigravity"]["apiCallCount"], 11)

    def test_same_provider_different_api_mode_aggregation(self):
        """Different billing_mode transport values for the same provider must aggregate into one route."""
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
        # openai-codex used with codex_responses, chat_completions, and missing mode
        conn.execute("""
            INSERT INTO session_model_usage VALUES
            ('s1', 'codex-model', 2, 200, 100, 0, 0, ?, 'openai-codex', 'codex_responses'),
            ('s1', 'chat-model', 3, 300, 150, 0, 0, ?, 'openai-codex', 'chat_completions'),
            ('s1', 'bare-model', 1, 100, 50, 0, 0, ?, 'openai-codex', NULL)
        """, (now_ts, now_ts, now_ts))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        routes = [r for r in record["routes"] if not r.get("isAll")]

        # There must be only ONE route for openai-codex, not 3 split by mode
        self.assertEqual(len(routes), 1)
        codex_route = routes[0]
        self.assertEqual(codex_route["id"], "openai-codex")
        self.assertEqual(codex_route["label"], "OpenAI Codex")
        self.assertEqual(codex_route["tokens"], 900)  # (200+100) + (300+150) + (100+50)
        self.assertEqual(codex_route["apiCallCount"], 6)  # 2 + 3 + 1
        self.assertEqual(codex_route["sessions"], 1)
        self.assertIn("codex-model", codex_route["modelUsage"])
        self.assertIn("chat-model", codex_route["modelUsage"])
        self.assertIn("bare-model", codex_route["modelUsage"])

    def test_new_transport_mode_does_not_hide_known_provider(self):
        """A new transport mode must not erase a safe provider subscription route."""
        route = self.collector.normalize_route_info("openai-codex", "future_responses_transport")
        self.assertEqual(route, ("openai-codex", "OpenAI Codex", "openai-codex", ""))

    def test_opencode_go_and_openai_codex_recognition(self):
        """Observed provider identifiers opencode-go and openai-codex, plus openrouter and nous, must be recognized."""
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
            ('s1', 'm1', 1, 10, 10, 0, 0, ?, 'opencode-go', 'chat_completions'),
            ('s1', 'm2', 2, 20, 20, 0, 0, ?, 'openai-codex', 'codex_responses'),
            ('s1', 'm3', 3, 30, 30, 0, 0, ?, 'openrouter', 'chat_completions'),
            ('s1', 'm4', 4, 40, 40, 0, 0, ?, 'nous', 'chat_completions')
        """, (now_ts, now_ts, now_ts, now_ts))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        routes = {r["id"]: r for r in record["routes"]}

        self.assertIn("opencode-go", routes)
        self.assertEqual(routes["opencode-go"]["label"], "OpenCode Go")

        self.assertIn("openai-codex", routes)
        self.assertEqual(routes["openai-codex"]["label"], "OpenAI Codex")

        self.assertIn("openrouter", routes)
        self.assertEqual(routes["openrouter"]["label"], "OpenRouter")

        self.assertIn("nous", routes)
        self.assertEqual(routes["nous"]["label"], "Nous")

    def test_model_name_versus_route_distinction(self):
        """Never infer a subscription from a model name alone (e.g. grok-4.6 through OpenCode Go remains OpenCode Go)."""
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
            ('s1', 'grok-4.6', 1, 100, 100, 0, 0, ?, 'opencode-go', 'chat_completions'),
            ('s1', 'claude-3-7-sonnet', 1, 200, 200, 0, 0, ?, 'openrouter', 'chat_completions'),
            ('s1', 'gpt-4o', 1, 300, 300, 0, 0, ?, '', NULL)
        """, (now_ts, now_ts, now_ts))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        routes = {r["id"]: r for r in record["routes"]}

        # grok-4.6 must NOT create a grok/xai route
        self.assertNotIn("grok", routes)
        self.assertNotIn("xai", routes)
        self.assertIn("opencode-go", routes)
        self.assertIn("grok-4.6", routes["opencode-go"]["modelUsage"])

        # claude-3-7-sonnet must NOT create an anthropic/claude route
        self.assertNotIn("claude", routes)
        self.assertNotIn("anthropic", routes)
        self.assertIn("openrouter", routes)
        self.assertIn("claude-3-7-sonnet", routes["openrouter"]["modelUsage"])

        # gpt-4o with blank provider must be unattributed, not openai
        self.assertNotIn("openai", routes)
        self.assertIn("unattributed", routes)
        self.assertIn("gpt-4o", routes["unattributed"]["modelUsage"])

    def test_unsafe_metadata_fallback(self):
        """Keep Unattributed for unsafe providers; omit unsafe modes while retaining safe providers."""
        unsafe_provider_cases = [
            ("", "subscription"),
            (None, "chat_completions"),
            ("   ", None),
            ("[NOT_EMITTED_ENDPOINT]", "chat_completions"),
            ("[REDACTED_SECRET]", "chat_completions"),
            ("Bearer [REDACTED]", "chat_completions"),
            ("untrusted/provider/value", "chat_completions"),
            ("provider; DROP TABLE sessions", "chat_completions"),
            ("a" * 100, "chat_completions"),
            ("unknown_cloud_provider", "chat_completions"),
        ]
        for raw_provider, raw_mode in unsafe_provider_cases:
            with self.subTest(raw_provider=raw_provider, raw_mode=raw_mode):
                res = self.collector.normalize_route_info(raw_provider, raw_mode)
                self.assertEqual(res, ("unattributed", "Unattributed", "", ""))

        unsafe_mode_cases = [
            "[NOT_EMITTED_ENDPOINT]",
            "[REDACTED_SECRET]",
            "mode with spaces",
            "m" * 100,
        ]
        for raw_mode in unsafe_mode_cases:
            with self.subTest(raw_mode=raw_mode):
                res = self.collector.normalize_route_info("openai-codex", raw_mode)
                self.assertEqual(res, ("openai-codex", "OpenAI Codex", "openai-codex", ""))

    def test_provider_limit_unavailable_behavior(self):
        """Hermes usage reports truthful limits unavailable (empty limits, no fabricated numeric quotas) while preserving usage."""
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
            ('s1', 'codex-model', 5, 500, 500, 0, 0, ?, 'openai-codex', 'codex_responses')
        """, (now_ts,))
        conn.commit()
        conn.close()

        record = self.collector.collect_usage()
        self.assertTrue(record["ready"])
        # Limits must be empty - no fabricated numeric quotas
        self.assertEqual(record["limits"], [])
        self.assertEqual(record["tierLabel"], "")
        # Local metrics must be preserved
        self.assertEqual(record["totalPrompts"], 5)
        self.assertEqual(record["totalSessions"], 1)
        self.assertIn("routes", record)

    def test_sync_snapshot_source_omits_local_routes(self):
        """The QML sync serializer must not write local route data."""
        main_qml = (Path(__file__).parent / "Main.qml").read_text(encoding="utf-8")
        start = main_qml.index("function providerSnapshot(record)")
        end = main_qml.index("\n  function localSnapshot()", start)
        provider_snapshot = main_qml[start:end]

        self.assertNotIn("routes", provider_snapshot)
        self.assertNotIn("subscriptions", provider_snapshot)
        self.assertIn("providerMap[String(record.id)] = providerSnapshot(record)", main_qml[end:])

    def test_qml_supports_canonical_provider_labels_and_safe_limits(self):
        """The QML adapter must render discovered route aliases and sanitize numeric windows."""
        main_qml = (Path(__file__).parent / "Main.qml").read_text(encoding="utf-8")
        panel_qml = (Path(__file__).parent / "Panel.qml").read_text(encoding="utf-8")
        self.assertIn('key === "openai-codex"', main_qml)
        self.assertIn('return "OpenAI Codex"', main_qml)
        self.assertIn('key === "opencode-go"', main_qml)
        self.assertIn('return "OpenCode Go"', main_qml)
        self.assertIn("if (used !== undefined && !isFinite(used)) used = undefined", panel_qml)
        self.assertIn("percent = clamp(percent, 0, 1)", panel_qml)

    def test_hermes_icon_assets_exist_for_runtime_panel(self):
        """The live panel must not request missing Hermes image assets."""
        assets = Path(__file__).parent / "assets"
        self.assertTrue((assets / "hermes.svg").is_file())
        self.assertTrue((assets / "hermes-light.svg").is_file())

    def test_manifest_advertises_requested_provider_slots(self):
        """The UI must be ready for Grok and Antigravity records when available."""
        manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
        providers = manifest["barWidget"]["defaults"]["providers"]
        for provider_id in ("claude", "codex", "fireworks", "hermes", "grok", "antigravity"):
            with self.subTest(provider_id=provider_id):
                self.assertTrue(providers[provider_id]["enabled"])


if __name__ == "__main__":
    unittest.main()
