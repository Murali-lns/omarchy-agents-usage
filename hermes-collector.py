#!/usr/bin/env python3
# omarchy:summary=Print the Hermes usage record as JSON
# omarchy:args=[--force] [--limits-only]
# omarchy:hidden=true
"""Collect Hermes usage into one display-ready JSON record.

Reads the active default profile database under Hermes home directory read-only
(SQLite URI mode=ro plus PRAGMA query_only=ON), restricts sessions to source=tui,
aggregates all available TUI history by model and billing route from session_model_usage
(including main and aux task rows without double counting), and prints
and atomically writes the record in the Omarchy record contract.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
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


_MODEL_USAGE_PERIODS = ("today", "7d", "month", "all")
_TOKEN_BUCKET_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)


def _empty_token_bucket() -> dict[str, int]:
    return {key: 0 for key in _TOKEN_BUCKET_KEYS}


def _empty_model_usage_by_period() -> dict[str, dict[str, dict[str, int]]]:
    return {period: {} for period in _MODEL_USAGE_PERIODS}


def _accumulate_model_bucket(
    model_usage: dict[str, dict[str, int]],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> None:
    bucket = model_usage.setdefault(model, _empty_token_bucket())
    bucket["inputTokens"] += input_tokens
    bucket["outputTokens"] += output_tokens
    bucket["cacheReadInputTokens"] += cache_read_tokens
    bucket["cacheCreationInputTokens"] += cache_creation_tokens


def _accumulate_daily_model_bucket(
    daily_model_usage: dict[str, dict[str, dict[str, int]]],
    day: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> None:
    if not day:
        return
    day_usage = daily_model_usage.setdefault(day, {})
    _accumulate_model_bucket(
        day_usage,
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
    )


def _filter_model_usage(raw_usage: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    out = {}
    for model in sorted(raw_usage.keys()):
        bucket = raw_usage[model]
        if sum(bucket[key] for key in _TOKEN_BUCKET_KEYS) > 0:
            out[model] = bucket
    return out


def _model_usage_by_period(
    daily_model_usage: dict[str, dict[str, dict[str, int]]],
    period_dates: dict[str, set[str]],
    all_model_usage: dict[str, dict[str, int]],
) -> dict[str, dict[str, dict[str, int]]]:
    by_period: dict[str, dict[str, dict[str, int]]] = {}
    for period in ("today", "7d", "month"):
        period_usage: dict[str, dict[str, int]] = {}
        for day in period_dates[period]:
            for model, bucket in daily_model_usage.get(day, {}).items():
                _accumulate_model_bucket(
                    period_usage,
                    model,
                    bucket["inputTokens"],
                    bucket["outputTokens"],
                    bucket["cacheReadInputTokens"],
                    bucket["cacheCreationInputTokens"],
                )
        by_period[period] = _filter_model_usage(period_usage)

    # Keep the all-time period sourced from the existing all-time modelUsage
    # aggregation, including its established model filtering and ordering.
    by_period["all"] = all_model_usage
    return by_period


# Route attribution is display metadata, not a user-controlled label. Keep an
# explicit allow-list so an arbitrary provider/mode value cannot turn into a
# route label, route id, secret fragment, or endpoint-like string.
_ROUTE_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*\Z")
_MAX_ROUTE_TOKEN_LENGTH = 64

_KNOWN_PROVIDER_ALIASES: dict[str, tuple[str, str]] = {
    # alias -> (canonical_id, canonical_label)
    "openai-codex": ("openai-codex", "OpenAI Codex"),
    "codex": ("openai-codex", "OpenAI Codex"),
    "opencode-go": ("opencode-go", "OpenCode Go"),
    "opencode": ("opencode-go", "OpenCode Go"),
    "anthropic": ("anthropic", "Anthropic"),
    "claude": ("anthropic", "Anthropic"),
    "xai": ("xai", "Grok"),
    # Hermes uses this provider ID for its subscription OAuth route.
    "xai-oauth": ("xai", "Grok"),
    "grok": ("xai", "Grok"),
    "google": ("google", "Google Gemini"),
    "gemini": ("google", "Google Gemini"),
    "openrouter": ("openrouter", "OpenRouter"),
    "nous": ("nous", "Nous"),
    "openai": ("openai", "OpenAI"),
    "deepseek": ("deepseek", "DeepSeek"),
    "together": ("together", "Together AI"),
    "together_ai": ("together", "Together AI"),
    "togetherai": ("together", "Together AI"),
    "together-ai": ("together", "Together AI"),
    "fireworks": ("fireworks", "Fireworks"),
    "groq": ("groq", "Groq"),
    "hermes": ("hermes", "Hermes"),
    "ollama": ("ollama", "Ollama"),
    "aws": ("aws", "AWS"),
    "azure": ("azure", "Azure"),
    "github": ("github", "GitHub"),
}


def _safe_route_token(raw_value: Any) -> str:
    """Return a bounded, syntax-safe route token or an empty string."""
    if not isinstance(raw_value, str):
        return ""
    value = raw_value.strip().lower()
    if len(value) > _MAX_ROUTE_TOKEN_LENGTH or not _ROUTE_TOKEN_RE.fullmatch(value):
        return ""
    return value


def friendly_provider_name(provider: str) -> str:
    """Return a fixed label for a known provider; never title-case unknown text."""
    value = _safe_route_token(provider)
    if value in _KNOWN_PROVIDER_ALIASES:
        return _KNOWN_PROVIDER_ALIASES[value][1]
    return "Unattributed"


def friendly_mode_name(mode: str) -> str:
    """Return sanitized mode token or empty string."""
    if mode is None or (isinstance(mode, str) and mode.strip() == ""):
        return ""
    return _safe_route_token(mode)


_KNOWN_SAFE_MODES = {
    "chat_completions",
    "codex_responses",
    "anthropic_messages",
    "messages",
    "completions",
    "responses",
    "subscription",
    "api_key",
    "api-key",
    "apikey",
    "oauth",
    "managed",
    "free",
    "local",
    "credits",
}


def normalize_route_info(raw_provider: Any, raw_mode: Any) -> tuple[str, str, str, str]:
    """Normalize a safe provider route and optionally retain a known-safe mode.

    Returns (canonical_route_id, canonical_route_label, canonical_provider, safe_mode).
    An unknown, missing, malformed, URL-like, or secret-like provider collapses to
    ('unattributed', 'Unattributed', '', ''). A missing or unfamiliar transport
    mode does *not* erase a valid provider: it is omitted from the display record.
    Routes are grouped strictly by provider (never split by API transport mode).
    """
    provider = _safe_route_token(raw_provider)
    mode_missing = raw_mode is None or (isinstance(raw_mode, str) and raw_mode.strip() == "")

    if provider not in _KNOWN_PROVIDER_ALIASES:
        return "unattributed", "Unattributed", "", ""

    canonical_id, canonical_label = _KNOWN_PROVIDER_ALIASES[provider]

    if mode_missing:
        return canonical_id, canonical_label, canonical_id, ""

    # billing_mode is transport metadata, not subscription identity. Keep only
    # modes from the safe, known vocabulary; future/unknown modes must not make
    # an otherwise valid provider route disappear or leak arbitrary text.
    mode = _safe_route_token(raw_mode)
    safe_mode = mode if mode in _KNOWN_SAFE_MODES else ""
    return canonical_id, canonical_label, canonical_id, safe_mode


def empty_route(
    route_id: str,
    label: str,
    is_all: bool = False,
    billing_provider: str = "",
    billing_mode: str = "",
    recent_dates: list[str] | None = None,
) -> dict[str, Any]:
    if recent_dates is None:
        recent_dates = recent_date_strings(datetime.now())
    return {
        "id": route_id,
        "label": label,
        "isAll": is_all,
        "billingProvider": billing_provider,
        "billingMode": billing_mode,
        "tokens": 0,
        "totalTokens": 0,
        "todayTotalTokens": 0,
        "todayTokens": 0,
        "apiCallCount": 0,
        "api_call_count": 0,
        "totalPrompts": 0,
        "todayPrompts": 0,
        "sessions": 0,
        "totalSessions": 0,
        "todaySessions": 0,
        "activeDays": 0,
        "activeDates": [],
        "recentDays": [{"date": d, "messageCount": 0} for d in recent_dates],
        "modelUsage": {},
        "todayTokensByModel": {},
        "modelUsageByPeriod": _empty_model_usage_by_period(),
    }


def empty_record(status_text: str = "", help_text: str = "") -> dict[str, Any]:
    now = datetime.now()
    recent_dates = recent_date_strings(now)
    all_route = empty_route("all", "All subscriptions", is_all=True, recent_dates=recent_dates)
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
        "modelUsageByPeriod": _empty_model_usage_by_period(),
        "limits": [],
        "tierLabel": "",
        "usageStatusText": status_text,
        "authHelpText": help_text,
        "routes": [all_route],
    }


def collect_usage() -> dict[str, Any]:
    db_file = get_hermes_db_path()
    if not db_file.is_file():
        return empty_record("Hermes database not found")

    now = datetime.now()
    today = now.date().isoformat()
    recent_dates = recent_date_strings(now)
    recent_map = {d: 0 for d in recent_dates}
    local_today = now.date()
    period_dates = {
        "today": {local_today.isoformat()},
        "7d": {(local_today - timedelta(days=offset)).isoformat() for offset in range(7)},
        "month": {(local_today - timedelta(days=offset)).isoformat() for offset in range(30)},
    }

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

        has_billing_provider = "billing_provider" in smu_cols
        has_billing_mode = "billing_mode" in smu_cols
        provider_col = "smu.billing_provider" if has_billing_provider else "NULL"
        mode_col = "smu.billing_mode" if has_billing_mode else "NULL"

        source_filter = "tui"

        cur.execute(
            f"""
            SELECT
                s.id,
                smu.model,
                COALESCE(smu.api_call_count, 0),
                COALESCE(smu.input_tokens, 0),
                COALESCE(smu.output_tokens, 0),
                COALESCE(smu.cache_read_tokens, 0),
                COALESCE(smu.cache_write_tokens, 0),
                strftime('%Y-%m-%d', COALESCE(smu.first_seen, s.started_at), 'unixepoch', 'localtime') AS day,
                {provider_col} AS raw_provider,
                {mode_col} AS raw_mode
            FROM session_model_usage smu
            JOIN sessions s ON smu.session_id = s.id
            WHERE s.source = ?
            """,
            (source_filter,),
        )
        rows = cur.fetchall()

        all_sessions = set()
        all_today_sessions = set()
        all_total_prompts = 0
        all_today_prompts = 0
        all_active_dates = set()
        all_recent_map = {d: 0 for d in recent_dates}
        all_model_usage: dict[str, dict[str, int]] = {}
        all_model_usage_by_day: dict[str, dict[str, dict[str, int]]] = {}
        all_today_tokens_by_model: dict[str, int] = {}

        route_accs: dict[str, dict[str, Any]] = {}

        for row in rows:
            sess_id = str(row[0])
            model = str(row[1])
            api_calls = int(row[2] or 0)
            in_tok = int(row[3] or 0)
            out_tok = int(row[4] or 0)
            cr_tok = int(row[5] or 0)
            cw_tok = int(row[6] or 0)
            tot_tok = in_tok + out_tok + cr_tok + cw_tok
            day = str(row[7]) if row[7] else ""
            raw_prov = row[8]
            raw_mode = row[9]

            all_sessions.add(sess_id)
            all_total_prompts += api_calls
            if tot_tok > 0 and day:
                all_active_dates.add(day)
            if day in all_recent_map:
                all_recent_map[day] += tot_tok

            if model not in all_model_usage:
                all_model_usage[model] = {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                }
            all_model_usage[model]["inputTokens"] += in_tok
            all_model_usage[model]["outputTokens"] += out_tok
            all_model_usage[model]["cacheReadInputTokens"] += cr_tok
            all_model_usage[model]["cacheCreationInputTokens"] += cw_tok
            _accumulate_daily_model_bucket(
                all_model_usage_by_day,
                day,
                model,
                in_tok,
                out_tok,
                cr_tok,
                cw_tok,
            )

            if day == today:
                all_today_sessions.add(sess_id)
                all_today_prompts += api_calls
                if tot_tok > 0:
                    all_today_tokens_by_model[model] = all_today_tokens_by_model.get(model, 0) + tot_tok

            r_id, r_label, clean_prov, clean_mode = normalize_route_info(raw_prov, raw_mode)
            if r_id not in route_accs:
                route_accs[r_id] = {
                    "id": r_id,
                    "label": r_label,
                    "isAll": False,
                    "billingProvider": clean_prov,
                    "billingMode": clean_mode,
                    "modes": set([clean_mode]) if clean_mode else set(),
                    "sessions": set(),
                    "today_sessions": set(),
                    "total_prompts": 0,
                    "today_prompts": 0,
                    "total_tokens": 0,
                    "today_tokens": 0,
                    "active_dates": set(),
                    "recent_map": {d: 0 for d in recent_dates},
                    "model_usage": {},
                    "model_usage_by_day": {},
                    "today_tokens_by_model": {},
                }
            r_acc = route_accs[r_id]
            if clean_mode:
                r_acc["modes"].add(clean_mode)
            r_acc["sessions"].add(sess_id)
            r_acc["total_prompts"] += api_calls
            r_acc["total_tokens"] += tot_tok
            if tot_tok > 0 and day:
                r_acc["active_dates"].add(day)
            if day in r_acc["recent_map"]:
                r_acc["recent_map"][day] += tot_tok

            if model not in r_acc["model_usage"]:
                r_acc["model_usage"][model] = {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                }
            r_acc["model_usage"][model]["inputTokens"] += in_tok
            r_acc["model_usage"][model]["outputTokens"] += out_tok
            r_acc["model_usage"][model]["cacheReadInputTokens"] += cr_tok
            r_acc["model_usage"][model]["cacheCreationInputTokens"] += cw_tok
            _accumulate_daily_model_bucket(
                r_acc["model_usage_by_day"],
                day,
                model,
                in_tok,
                out_tok,
                cr_tok,
                cw_tok,
            )

            if day == today:
                r_acc["today_sessions"].add(sess_id)
                r_acc["today_prompts"] += api_calls
                r_acc["today_tokens"] += tot_tok
                if tot_tok > 0:
                    r_acc["today_tokens_by_model"][model] = r_acc["today_tokens_by_model"].get(model, 0) + tot_tok

        model_usage = _filter_model_usage(all_model_usage)
        model_usage_by_period = _model_usage_by_period(
            all_model_usage_by_day,
            period_dates,
            model_usage,
        )
        total_prompts = all_total_prompts
        total_sessions = len(all_sessions)
        active_dates = sorted(all_active_dates)
        active_days = len(active_dates)
        today_prompts = all_today_prompts
        today_sessions = len(all_today_sessions)
        today_tokens_by_model = {
            m: all_today_tokens_by_model[m]
            for m in sorted(all_today_tokens_by_model)
            if all_today_tokens_by_model[m] > 0
        }
        today_total_tokens = sum(today_tokens_by_model.values())
        recent_days = [{"date": d, "messageCount": all_recent_map[d]} for d in recent_dates]
        all_total_tokens = sum(
            b["inputTokens"] + b["outputTokens"] + b["cacheReadInputTokens"] + b["cacheCreationInputTokens"]
            for b in model_usage.values()
        )

        all_route = {
            "id": "all",
            "label": "All subscriptions",
            "isAll": True,
            "billingProvider": "",
            "billingMode": "",
            "tokens": all_total_tokens,
            "totalTokens": all_total_tokens,
            "todayTotalTokens": today_total_tokens,
            "todayTokens": today_total_tokens,
            "apiCallCount": total_prompts,
            "api_call_count": total_prompts,
            "totalPrompts": total_prompts,
            "todayPrompts": today_prompts,
            "sessions": total_sessions,
            "totalSessions": total_sessions,
            "todaySessions": today_sessions,
            "activeDays": active_days,
            "activeDates": active_dates,
            "recentDays": recent_days,
            "modelUsage": model_usage,
            "todayTokensByModel": today_tokens_by_model,
            "modelUsageByPeriod": model_usage_by_period,
        }

        individual_routes = []
        for r_id, r_acc in route_accs.items():
            r_models = _filter_model_usage(r_acc["model_usage"])
            r_model_usage_by_period = _model_usage_by_period(
                r_acc["model_usage_by_day"],
                period_dates,
                r_models,
            )
            if r_acc["total_prompts"] == 0 and r_acc["total_tokens"] == 0 and len(r_models) == 0:
                continue
            r_recent_days = [{"date": d, "messageCount": r_acc["recent_map"][d]} for d in recent_dates]
            r_today_tokens_by_model = {
                m: r_acc["today_tokens_by_model"][m]
                for m in sorted(r_acc["today_tokens_by_model"])
                if r_acc["today_tokens_by_model"][m] > 0
            }
            r_active_dates = sorted(r_acc["active_dates"])
            billing_mode = next(iter(r_acc.get("modes", set()))) if len(r_acc.get("modes", set())) == 1 else ""
            individual_routes.append({
                "id": r_acc["id"],
                "label": r_acc["label"],
                "isAll": False,
                "billingProvider": r_acc["billingProvider"],
                "billingMode": billing_mode,
                "tokens": r_acc["total_tokens"],
                "totalTokens": r_acc["total_tokens"],
                "todayTotalTokens": r_acc["today_tokens"],
                "todayTokens": r_acc["today_tokens"],
                "apiCallCount": r_acc["total_prompts"],
                "api_call_count": r_acc["total_prompts"],
                "totalPrompts": r_acc["total_prompts"],
                "todayPrompts": r_acc["today_prompts"],
                "sessions": len(r_acc["sessions"]),
                "totalSessions": len(r_acc["sessions"]),
                "todaySessions": len(r_acc["today_sessions"]),
                "activeDays": len(r_active_dates),
                "activeDates": r_active_dates,
                "recentDays": r_recent_days,
                "modelUsage": r_models,
                "todayTokensByModel": r_today_tokens_by_model,
                "modelUsageByPeriod": r_model_usage_by_period,
            })

        individual_routes.sort(key=lambda r: (r["id"] == "unattributed", r["label"].lower(), r["id"]))
        routes = [all_route] + individual_routes

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
            "modelUsageByPeriod": model_usage_by_period,
            "limits": [],
            "tierLabel": "",
            "usageStatusText": "",
            "authHelpText": "",
            "routes": routes,
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
