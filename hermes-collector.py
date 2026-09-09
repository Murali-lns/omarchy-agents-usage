#!/usr/bin/env python3
# omarchy:summary=Print the Hermes usage record as JSON
# omarchy:args=[--force] [--limits-only]
# omarchy:hidden=true
"""Collect Hermes usage into one display-ready JSON record.

Reads the active default profile database under Hermes home directory read-only
(SQLite URI mode=ro plus PRAGMA query_only=ON), restricts sessions to source=tui,
aggregates all available TUI history by model from session_model_usage
(including main and aux task rows without double counting), and prints
and atomically writes the record in the Omarchy record contract.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any

AGENT_ID = "hermes"
AGENT_NAME = "Hermes"
DB_NAME = "state" + ".db"


def get_hermes_db_path() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).expanduser() / DB_NAME
    return Path.home() / ".hermes" / DB_NAME


def usage_dir_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        base = Path(xdg_state).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "omarchy" / "agents" / "usage"


def recent_date_strings(today_dt: datetime) -> list[str]:
    return [(today_dt - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(6, -1, -1)]


def empty_record(status_text: str = "", help_text: str = "") -> dict[str, Any]:
    now = datetime.now()
    recent_dates = recent_date_strings(now)
    return {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "ready": False,
        "hasLocalStats": True,
        "todayPrompts": 0,
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [{"date": d, "messageCount": 0} for d in recent_dates],
        "totalPrompts": 0,
        "totalSessions": 0,
        "activeDays": 0,
        "activeDates": [],
        "modelUsage": {},
        "limits": [],
        "tierLabel": "",
        "usageStatusText": status_text,
        "authHelpText": help_text,
    }


def collect_usage() -> dict[str, Any]:
    db_file = get_hermes_db_path()
    if not db_file.is_file():
        return empty_record("Hermes database not found")

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    recent_dates = recent_date_strings(now)
    recent_map = {d: 0 for d in recent_dates}

    uri = f"file:{db_file.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    except sqlite3.OperationalError as exc:
        return empty_record(f"Cannot open Hermes database: {exc}")

    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 3000")
        cur = conn.cursor()

        # Verify schema: ensure required tables exist
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions', 'session_model_usage')"
        )
        tables = {row[0] for row in cur.fetchall()}
        if "sessions" not in tables or "session_model_usage" not in tables:
            return empty_record("Hermes schema missing sessions or session_model_usage")

        # Verify required columns exist in session_model_usage
        cur.execute("PRAGMA table_info(session_model_usage)")
        smu_cols = {row[1] for row in cur.fetchall()}
        required_smu_cols = {
            "session_id", "model", "api_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens"
        }
        if not required_smu_cols.issubset(smu_cols):
            return empty_record("Hermes session_model_usage missing required columns")

        # Verify required columns exist in sessions
        cur.execute("PRAGMA table_info(sessions)")
        sess_cols = {row[1] for row in cur.fetchall()}
        required_sess_cols = {"id", "source", "started_at"}
        if not required_sess_cols.issubset(sess_cols):
            return empty_record("Hermes sessions missing required columns")

        source_filter = "tui"

        # 1. Model usage breakdown (all history)
        cur.execute(
            """
            SELECT
                smu.model,
                COALESCE(SUM(smu.input_tokens), 0) AS in_tokens,
                COALESCE(SUM(smu.output_tokens), 0) AS out_tokens,
                COALESCE(SUM(smu.cache_read_tokens), 0) AS cr_tokens,
                COALESCE(SUM(smu.cache_write_tokens), 0) AS cw_tokens
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
            GROUP BY smu.model
            HAVING (SUM(smu.input_tokens) + SUM(smu.output_tokens) + SUM(smu.cache_read_tokens) + SUM(smu.cache_write_tokens)) > 0
            ORDER BY smu.model
            """,
            (source_filter,),
        )
        model_usage: dict[str, dict[str, int]] = {}
        for row in cur.fetchall():
            model_usage[str(row[0])] = {
                "inputTokens": int(row[1]),
                "outputTokens": int(row[2]),
                "cacheReadInputTokens": int(row[3]),
                "cacheCreationInputTokens": int(row[4]),
            }

        # 2. Total prompts & total sessions
        cur.execute(
            """
            SELECT
                COALESCE(SUM(smu.api_call_count), 0) AS total_prompts,
                COUNT(DISTINCT s.id) AS total_sessions
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
            """,
            (source_filter,),
        )
        t_prompts_row = cur.fetchone()
        total_prompts = int(t_prompts_row[0]) if t_prompts_row else 0
        total_sessions = int(t_prompts_row[1]) if t_prompts_row else 0

        # 3. Active dates & active days count
        cur.execute(
            """
            SELECT DISTINCT
                strftime('%Y-%m-%d', COALESCE(smu.first_seen, s.started_at), 'unixepoch', 'localtime') AS day
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
              AND (smu.input_tokens + smu.output_tokens + smu.cache_read_tokens + smu.cache_write_tokens) > 0
            ORDER BY day
            """,
            (source_filter,),
        )
        active_dates = [str(r[0]) for r in cur.fetchall() if r[0]]
        active_days = len(active_dates)

        # 4. Today's prompts & sessions
        cur.execute(
            """
            SELECT
                COALESCE(SUM(smu.api_call_count), 0) AS today_prompts,
                COUNT(DISTINCT s.id) AS today_sessions
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
              AND strftime('%Y-%m-%d', COALESCE(smu.first_seen, s.started_at), 'unixepoch', 'localtime') = ?
            """,
            (source_filter, today),
        )
        today_row = cur.fetchone()
        today_prompts = int(today_row[0]) if today_row else 0
        today_sessions = int(today_row[1]) if today_row else 0

        # 5. Today's tokens by model & total
        cur.execute(
            """
            SELECT
                smu.model,
                COALESCE(SUM(smu.input_tokens + smu.output_tokens + smu.cache_read_tokens + smu.cache_write_tokens), 0) AS tokens
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
              AND strftime('%Y-%m-%d', COALESCE(smu.first_seen, s.started_at), 'unixepoch', 'localtime') = ?
            GROUP BY smu.model
            HAVING (SUM(smu.input_tokens + smu.output_tokens + smu.cache_read_tokens + smu.cache_write_tokens)) > 0
            ORDER BY smu.model
            """,
            (source_filter, today),
        )
        today_tokens_by_model = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        today_total_tokens = sum(today_tokens_by_model.values())

        # 6. Recent days tokens
        cur.execute(
            """
            SELECT
                strftime('%Y-%m-%d', COALESCE(smu.first_seen, s.started_at), 'unixepoch', 'localtime') AS day,
                COALESCE(SUM(smu.input_tokens + smu.output_tokens + smu.cache_read_tokens + smu.cache_write_tokens), 0) AS tokens
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
              AND strftime('%Y-%m-%d', COALESCE(smu.first_seen, s.started_at), 'unixepoch', 'localtime') >= ?
            GROUP BY day
            """,
            (source_filter, recent_dates[0]),
        )
        for r in cur.fetchall():
            day_str = str(r[0])
            if day_str in recent_map:
                recent_map[day_str] = int(r[1])
        recent_days = [{"date": d, "messageCount": recent_map[d]} for d in recent_dates]

        return {
            "schemaVersion": 1,
            "id": AGENT_ID,
            "name": AGENT_NAME,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "ready": total_prompts > 0 or len(model_usage) > 0,
            "hasLocalStats": True,
            "todayPrompts": today_prompts,
            "todaySessions": today_sessions,
            "todayTotalTokens": today_total_tokens,
            "todayTokensByModel": today_tokens_by_model,
            "recentDays": recent_days,
            "totalPrompts": total_prompts,
            "totalSessions": total_sessions,
            "activeDays": active_days,
            "activeDates": active_dates,
            "modelUsage": model_usage,
            "limits": [],
            "tierLabel": "",
            "usageStatusText": "",
            "authHelpText": "",
        }
    except sqlite3.OperationalError as exc:
        target_path = usage_dir_path() / f"{AGENT_ID}.json"
        if target_path.is_file():
            try:
                cached = json.loads(target_path.read_text(encoding="utf-8"))
                cached["usageStatusText"] = f"Hermes database busy: {exc}"
                return cached
            except Exception:
                pass
        return empty_record(f"Hermes database query error: {exc}")
    except Exception as exc:
        return empty_record(f"Hermes collector error: {exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def atomic_write_record(usage_dir: Path, record: dict[str, Any]) -> Path:
    usage_dir.mkdir(parents=True, exist_ok=True)
    target_path = usage_dir / f"{AGENT_ID}.json"

    content = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"

    # Atomic write via temporary file in the same directory
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=usage_dir,
        prefix=f".{AGENT_ID}.",
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        tmp_name = tmp_file.name
        tmp_file.write(content)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())

    os.chmod(tmp_name, 0o644)
    os.replace(tmp_name, target_path)
    return target_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Hermes usage into Omarchy agents record")
    parser.add_argument("--force", action="store_true", help="Force refresh usage stats")
    parser.add_argument("--limits-only", action="store_true", help="Refresh limits only")
    args = parser.parse_args()

    record = collect_usage()

    # Atomically update usage JSON file
    usage_dir = usage_dir_path()
    try:
        atomic_write_record(usage_dir, record)
    except Exception as exc:
        print(f"hermes-collector: failed to write record to {usage_dir}: {exc}", file=sys.stderr)

    # Print record to stdout matching Omarchy collector protocol
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
