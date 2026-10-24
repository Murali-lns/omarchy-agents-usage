"""Unit tests for the OpenCode Go collector.

The collector is loaded from its hyphenated file name, and every test keeps
network and real home-directory state out of the picture.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parent / "opencode-go-collector.py"
spec = importlib.util.spec_from_file_location("opencode_go_collector", MODULE_PATH)
assert spec is not None and spec.loader is not None
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


LIVE_PAYLOAD = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-10-18T11:45:20.000Z"},
        "weekly": {"status": "ok", "percent": 28, "resetsAt": "2026-10-19T00:00:00.000Z"},
        "monthly": {"status": "ok", "percent": 14, "resetsAt": "2026-11-15T07:09:37.000Z"},
    }
}


class TestQuotaParsing(unittest.TestCase):
    def test_live_payload_yields_fraction_windows(self):
        windows = collector.parse_quota_windows(LIVE_PAYLOAD)
        self.assertEqual([w["label"] for w in windows], ["5-hour", "Weekly", "Monthly"])
        self.assertEqual([w["percent"] for w in windows], [0.0, 0.28, 0.14])
        self.assertEqual(windows[1]["resetsAt"], "2026-10-19T00:00:00+00:00")

    def test_flat_payload_and_fractional_percent_scale(self):
        windows = collector.parse_quota_windows({
            "rollingUsage": {"usedPercent": 0.5},
            "weeklyUsage": {"used": 3, "limit": 12},
        })
        self.assertEqual(windows[0]["percent"], 0.5)
        self.assertEqual(windows[1]["percent"], 0.25)

    def test_malformed_payload_never_fabricates_windows(self):
        self.assertEqual(collector.parse_quota_windows(None), [])
        self.assertEqual(collector.parse_quota_windows({"usage": {"rolling": {}}}), [])


class TestCredentialResolution(unittest.TestCase):
    def test_env_wins_then_hermes_env_then_auth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".hermes").mkdir()
            (home / ".hermes" / ".env").write_text(
                "OPENCODE_GO_API_KEY=key-from-hermes-env\n", encoding="utf-8"
            )
            auth_dir = home / ".local" / "share" / "opencode"
            auth_dir.mkdir(parents=True)
            (auth_dir / "auth.json").write_text(
                json.dumps({"opencode-go": {"type": "api", "key": "key-from-auth"}}), encoding="utf-8"
            )
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "key-from-env"}, clear=False):
                    self.assertEqual(collector.resolve_api_key(), "key-from-env")
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(collector.resolve_api_key(), "key-from-hermes-env")
                    (home / ".hermes" / ".env").write_text("", encoding="utf-8")
                    self.assertEqual(collector.resolve_api_key(), "key-from-auth")

    def test_no_credentials_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, "home", staticmethod(lambda: Path(tmp))):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertIsNone(collector.resolve_api_key())


class TestLocalSources(unittest.TestCase):
    def _write_opencode_db(self, home: Path) -> None:
        db_dir = home / ".local" / "share" / "opencode"
        db_dir.mkdir(parents=True)
        conn = sqlite3.connect(db_dir / "opencode.db")
        conn.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        rows = [
            {
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4.1-flash",
                "tokens": {"input": 10, "output": 4, "reasoning": 6, "cache": {"read": 100, "write": 0}},
            },
            {
                "role": "user",
                "providerID": "opencode-go",
                "modelID": "deepseek-v4.1-flash",
                "tokens": {"input": 999, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
            {
                "role": "assistant",
                "providerID": "opencode",
                "modelID": "some-free-model",
                "tokens": {"input": 500, "output": 500, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            },
        ]
        now_ms = int(datetime.now().timestamp() * 1000)
        for index, data in enumerate(rows):
            payload = dict(data)
            payload["time"] = {"created": now_ms, "completed": now_ms}
            conn.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                (f"m{index}", "ses_1", now_ms, json.dumps(payload)),
            )
        conn.commit()
        conn.close()

    def test_database_aggregation_counts_only_go_assistant_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._write_opencode_db(home)
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                stats = collector.collect_opencode_db(datetime.now())
            bucket = stats["models"]["deepseek-v4.1-flash"]
            self.assertEqual(bucket["inputTokens"], 10)
            # Reasoning tokens belong to the billed output stream.
            self.assertEqual(bucket["outputTokens"], 10)
            self.assertEqual(bucket["cacheReadInputTokens"], 100)
            self.assertNotIn("some-free-model", stats["models"])
            self.assertEqual(stats["total_calls"], 1)
            self.assertEqual(stats["today_calls"], 1)

    def test_hermes_route_and_cli_merge_without_losing_either_source(self):
        now = datetime(2026, 10, 18, 12, 0)
        today = now.date().isoformat()
        hermes_route = {
            "todayPrompts": 38,
            "todaySessions": 6,
            "todayTokensByModel": {"deepseek-v4.1-flash": 200},
            "recentDays": [{"date": today, "messageCount": 200}],
            "totalPrompts": 34208,
            "totalSessions": 1105,
            "activeDays": 80,
            "activeDates": ["2026-10-18"],
            "modelUsage": {
                "deepseek-v4.1-flash": {
                    "inputTokens": 1, "outputTokens": 2,
                    "cacheReadInputTokens": 3, "cacheCreationInputTokens": 0,
                }
            },
            "modelUsageByPeriod": {"today": {}, "7d": {}, "month": {}, "all": {}},
        }
        opencode = {
            "models": {
                "deepseek-v4.1-flash": {
                    "inputTokens": 10, "outputTokens": 10,
                    "cacheReadInputTokens": 100, "cacheCreationInputTokens": 0,
                }
            },
            "daily": {
                today: {
                    "deepseek-v4.1-flash": {
                        "inputTokens": 10, "outputTokens": 10,
                        "cacheReadInputTokens": 100, "cacheCreationInputTokens": 0,
                    }
                }
            },
            "today_calls": 1,
            "today_sessions": {"ses_1"},
            "total_calls": 1,
            "sessions": {"ses_1"},
            "active_dates": {today},
        }
        stats = collector.build_local_stats(hermes_route, opencode, now)
        self.assertEqual(stats["total_prompts"], 34209)
        self.assertEqual(stats["total_sessions"], 1106)
        self.assertEqual(stats["today_prompts"], 39)
        self.assertEqual(stats["today_sessions"], 7)
        self.assertEqual(stats["today_total_tokens"], 320)
        self.assertEqual(stats["active_days"], 1)
        self.assertEqual(stats["model_usage"]["deepseek-v4.1-flash"]["inputTokens"], 11)
        self.assertEqual(stats["model_usage"]["deepseek-v4.1-flash"]["outputTokens"], 12)
        today_entry = next(d for d in stats["recent_days"] if d["date"] == today)
        self.assertEqual(today_entry["messageCount"], 320)

    def test_missing_route_keeps_cli_only_stats(self):
        now = datetime(2026, 10, 18, 12, 0)
        stats = collector.build_local_stats(
            None,
            {
                "models": {}, "daily": {}, "today_calls": 0, "today_sessions": set(),
                "total_calls": 0, "sessions": set(), "active_dates": set(),
            },
            now,
        )
        self.assertFalse(stats["has_local"])
        self.assertEqual(stats["total_sessions"], 0)
        for period in ("today", "7d", "month", "all"):
            self.assertEqual(stats["model_usage_by_period"][period], {})


class TestRecordAssembly(unittest.TestCase):
    def _collect_with_quota(self, payload, reason=""):
        with mock.patch.object(collector, "load_hermes_route", return_value=None), \
             mock.patch.object(collector, "collect_opencode_db", return_value={
                 "models": {}, "daily": {}, "today_calls": 0, "today_sessions": set(),
                 "total_calls": 0, "sessions": set(), "active_dates": set(),
             }), \
             mock.patch.object(collector, "resolve_api_key", return_value="sk-test-key"), \
             mock.patch.object(collector, "_get_json", return_value=(payload, reason)):
            return collector.collect_usage()

    def test_quota_only_record_is_ready_and_never_contains_the_key(self):
        record = self._collect_with_quota(LIVE_PAYLOAD)
        self.assertTrue(record["ready"])
        self.assertFalse(record["hasLocalStats"])
        self.assertEqual([w["percent"] for w in record["limits"]], [0.0, 0.28, 0.14])
        self.assertEqual(record["usageStatusText"], "")
        self.assertNotIn("sk-test-key", json.dumps(record))

    def test_transient_failure_advises_a_retry_and_calls_limits_unavailable(self):
        now = datetime.now()
        day = now.date().isoformat()
        cli = {
            "models": {"m": {"inputTokens": 5, "outputTokens": 5,
                             "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
            "daily": {day: {"m": {"inputTokens": 5, "outputTokens": 5,
                                  "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            "today_calls": 1, "today_sessions": {"s"}, "total_calls": 1,
            "sessions": {"s"}, "active_dates": {day},
        }
        with mock.patch.object(collector, "load_hermes_route", return_value=None), \
             mock.patch.object(collector, "collect_opencode_db", return_value=cli), \
             mock.patch.object(collector, "resolve_api_key", return_value="sk-test-key"), \
             mock.patch.object(collector, "_get_json", return_value=(None, "usage-unavailable")):
            record = collector.collect_usage()
        # Local numbers stay usable; only the quota is reported missing, and
        # the panel retries the endpoint sooner.
        self.assertTrue(record["ready"])
        self.assertTrue(record["retryAdvised"])
        self.assertEqual(record["usageStatusText"], collector.USAGE_STATUS)

    def test_transient_failure_without_local_data_keeps_the_tab_hidden(self):
        record = self._collect_with_quota(None, "usage-unavailable")
        self.assertFalse(record["ready"])
        self.assertTrue(record["retryAdvised"])
        self.assertEqual(record["usageStatusText"], collector.MISSING_STATUS)

    def test_rejected_key_asks_for_a_refresh_not_a_retry(self):
        record = self._collect_with_quota(None, "auth-failed")
        self.assertNotIn("retryAdvised", record)
        self.assertEqual(record["authHelpText"], collector.AUTH_REJECTED_HELP)

    def test_missing_key_explains_how_to_provide_one(self):
        with mock.patch.object(collector, "load_hermes_route", return_value=None), \
             mock.patch.object(collector, "collect_opencode_db", return_value={
                 "models": {}, "daily": {}, "today_calls": 0, "today_sessions": set(),
                 "total_calls": 0, "sessions": set(), "active_dates": set(),
             }), \
             mock.patch.object(collector, "resolve_api_key", return_value=None):
            record = collector.collect_usage()
        self.assertFalse(record["ready"])
        self.assertEqual(record["usageStatusText"], collector.MISSING_STATUS)
        self.assertEqual(record["authHelpText"], collector.AUTH_HELP)

    def test_local_only_record_stays_ready_when_quota_is_down(self):
        now = datetime.now()
        day = now.date().isoformat()
        cli = {
            "models": {"m": {"inputTokens": 5, "outputTokens": 5,
                             "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}},
            "daily": {day: {"m": {"inputTokens": 5, "outputTokens": 5,
                                  "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            "today_calls": 3, "today_sessions": {"s"}, "total_calls": 3,
            "sessions": {"s"}, "active_dates": {day},
        }
        with mock.patch.object(collector, "load_hermes_route", return_value=None), \
             mock.patch.object(collector, "collect_opencode_db", return_value=cli), \
             mock.patch.object(collector, "resolve_api_key", return_value=None):
            record = collector.collect_usage()
        self.assertTrue(record["ready"])
        self.assertTrue(record["hasLocalStats"])
        self.assertEqual(record["limits"], [])
        self.assertEqual(record["todayTotalTokens"], 10)
        # The All Time card reads this published cumulative total.
        self.assertEqual(record["totalTokens"], 10)
        self.assertEqual(record["tokens"], 10)


class TestWindowHelpers(unittest.TestCase):
    def test_reset_seconds_are_converted_relative_to_now(self):
        now = 1_700_000_000.0
        windows = collector.parse_quota_windows({"rollingUsage": {"percent": 5, "resetInSec": 3600}}, now=now)
        expected = datetime.fromtimestamp(now + 3600, tz=timezone.utc).isoformat()
        self.assertEqual(windows[0]["resetsAt"], expected)

    def test_iso_timestamps_pass_through(self):
        stamp = datetime(2026, 10, 19, tzinfo=timezone.utc).isoformat()
        windows = collector.parse_quota_windows({"rollingUsage": {"percent": 5, "resetsAt": stamp}})
        self.assertEqual(windows[0]["resetsAt"], stamp)


class TestBoundedUsageDownload(unittest.TestCase):
    def test_source_never_reads_a_body_without_a_limit(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("urllib.request.urlopen", source)
        self.assertNotIn("response.read()", source)
        self.assertNotIn("fp.read(", source)
        self.assertIn("response.read(limit + 1)", source)
        self.assertIn("class _RedirectRefused", source)
        self.assertEqual(collector._MAX_USAGE_BODY_BYTES, 64 * 1024)

    def test_redirect_handler_does_not_read_the_body(self):
        fp = _FakeResponse(b"x" * 100)
        handler = collector._RedirectRefused()
        request = collector.urllib.request.Request("https://opencode.ai/zen/go/v1/usage")
        with self.assertRaises(collector.urllib.error.HTTPError) as caught:
            handler.http_error_302(request, fp, 302, "Found", {})
        self.assertEqual(fp.reads, [])
        caught.exception.close()
        self.assertEqual(fp.reads, [])
        self.assertTrue(fp.closed)

    def test_small_json_body_still_parses(self):
        payload = b'{"usage":{"rolling":{"percent":12}}}'
        response = _FakeResponse(payload)
        with mock.patch.object(collector, "_open_usage", return_value=response):
            parsed, reason = collector._get_json("sk-test")
        self.assertEqual(reason, "")
        self.assertEqual(parsed["usage"]["rolling"]["percent"], 12)
        self.assertEqual(response.reads, [collector._MAX_USAGE_BODY_BYTES + 1])

    def test_oversized_body_is_discarded_and_not_retried(self):
        response = _FakeResponse(b"x" * (collector._MAX_USAGE_BODY_BYTES + 50))
        with mock.patch.object(collector, "_open_usage", return_value=response) as opened:
            parsed, reason = collector._get_json("sk-test")
        self.assertIsNone(parsed)
        self.assertEqual(reason, "response-too-large")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(response.reads, [collector._MAX_USAGE_BODY_BYTES + 1])
        self.assertNotIn(None, response.reads)

    def test_declared_length_over_the_cap_never_reads_the_socket(self):
        response = _FakeResponse(
            b"{}",
            headers={"Content-Length": str(collector._MAX_USAGE_BODY_BYTES + 1)},
        )
        with mock.patch.object(collector, "_open_usage", return_value=response):
            parsed, reason = collector._get_json("sk-test")
        self.assertIsNone(parsed)
        self.assertEqual(reason, "response-too-large")
        self.assertEqual(response.reads, [])

    def test_real_socket_stops_after_the_cap(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        extra = collector._MAX_USAGE_BODY_BYTES + 8192

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                try:
                    self.wfile.write(b"x" * extra)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, format: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        address = server.server_address
        host, port = str(address[0]), int(address[1])
        try:
            with mock.patch.object(collector, "API_URL", f"http://{host}:{port}/usage"):
                parsed, reason = collector._get_json("sk-test")
        finally:
            server.server_close()
            thread.join(timeout=5)
        self.assertIsNone(parsed)
        self.assertEqual(reason, "response-too-large")

    def test_redirect_with_a_large_body_is_refused_and_not_followed(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        extra = collector._MAX_USAGE_BODY_BYTES + 8192
        followed = {"hit": False, "requests": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                followed["requests"] += 1
                if self.path != "/usage":
                    followed["hit"] = True
                    body = b'{"usage":{"rolling":{"percent":1}}}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = b"x" * extra
                self.send_response(302)
                self.send_header("Location", "/stolen")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, format: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = server.server_address
        host, port = str(address[0]), int(address[1])
        try:
            with mock.patch.object(collector, "API_URL", f"http://{host}:{port}/usage"):
                parsed, reason = collector._get_json("sk-test")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIsNone(parsed)
        self.assertEqual(reason, "redirect-refused")
        self.assertFalse(followed["hit"])
        self.assertEqual(followed["requests"], 1)


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = headers or {}
        self.reads: list = []
        self.closed = False

    def read(self, amt=None):
        self.reads.append(amt)
        if amt is None:
            return self.body
        return self.body[:amt]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


if __name__ == "__main__":
    unittest.main()
