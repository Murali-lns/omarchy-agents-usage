#!/usr/bin/env python3
# omarchy:summary=Print the OpenCode Go usage record as JSON
# omarchy:args=[--force] [--limits-only]
# omarchy:hidden=true
"""Collect OpenCode Go quota and local usage into one display-ready JSON record.

Quota comes from the Go plan's own usage endpoint —

    GET https://opencode.ai/zen/go/v1/usage

with the user's Go API key (resolved read-only from ``OPENCODE_GO_API_KEY`` /
``OPENCODE_API_KEY``, ``~/.hermes/.env``, or OpenCode's own
``~/.local/share/opencode/auth.json``). The key is sent only to that fixed
HTTPS endpoint, never logged, and never written into the record.

Local token statistics come from two files this machine already owns, both
read-only and neither containing prompts or message text:

  * the Hermes record's ``opencode-go`` billing route (authoritative, written
    by ``hermes-collector.py``), and
  * the OpenCode CLI's SQLite database, where assistant message rows carry
    token counts, the model id, the provider id, and a timestamp — this
    collector extracts only those numeric metadata fields, never message
    content.

Quota percentages are per account and cannot be derived locally; when the
endpoint is unavailable the record says so instead of fabricating numbers.
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
import time
import urllib.error
import urllib.request
from typing import Any

AGENT_ID = "opencode-go"
AGENT_NAME = "OpenCode Go"
API_URL = "https://opencode.ai/zen/go/v1/usage"
PROVIDER_ID = "opencode-go"
USAGE_STATUS = "Limit unavailable \u00b7 usage-only"
MISSING_STATUS = "OpenCode Go usage not found"
AUTH_HELP = (
    "Set OPENCODE_GO_API_KEY in ~/.hermes/.env (or run `opencode auth login`) "
    "to read the Go quota."
)
AUTH_REJECTED_HELP = (
    "OpenCode Go rejected the stored API key \u2014 refresh OPENCODE_GO_API_KEY "
    "or run `opencode auth login`."
)

# The endpoint flaps: it answers 503 {"message":"Go usage is unavailable"} for
# a share of calls regardless of credential, so transient failures are retried
# with a short backoff before the quota is reported unavailable.
_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.0)
_REQUEST_TIMEOUT_S = 10.0
_TRANSIENT_STATUSES = (429, 500, 502, 504)

_TOKEN_BUCKET_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)
_MODEL_USAGE_PERIODS = ("today", "7d", "month", "all")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}\Z")
_MAX_MESSAGES = 200000

_PERCENT_KEYS = ("percent", "usagePercent", "usedPercent", "percentUsed", "utilization")
_USED_KEYS = ("used", "usage", "consumed")
_LIMIT_KEYS = ("limit", "total", "quota")
_RESET_AT_KEYS = ("resetsAt", "resetAt", "reset_at", "resets_at")
_RESET_IN_SECONDS_KEYS = ("resetInSec", "resetSeconds", "resetsInSec")
_ROLLING_KEYS = ("rolling", "rollingUsage", "rolling_usage")
_WEEKLY_KEYS = ("weekly", "weeklyUsage", "weekly_usage")
_MONTHLY_KEYS = ("monthly", "monthlyUsage", "monthly_usage")
_WINDOW_LABELS = (("5-hour", _ROLLING_KEYS), ("Weekly", _WEEKLY_KEYS), ("Monthly", _MONTHLY_KEYS))


def usage_dir_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "omarchy" / "agents" / "usage"


def hermes_usage_record_path() -> Path:
    hermes_home = os.environ.get("HERMES_HOME")
    home = Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
    return home / "state.db"


def recent_date_strings(today: datetime) -> list[str]:
    return [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(6, -1, -1)]


# ------------------------------------------------------------------ credentials


def _env_api_key() -> str | None:
    for name in ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _hermes_env_api_key() -> str | None:
    env_path = Path.home() / ".hermes" / ".env"
    try:
        text = env_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        if name.strip() in ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"):
            trimmed = value.strip().strip("'\"")
            if trimmed:
                return trimmed
    return None


def _auth_file_api_key() -> str | None:
    xdg_data = os.environ.get("XDG_DATA_HOME")
    candidates = [Path.home() / ".local" / "share" / "opencode" / "auth.json"]
    if xdg_data:
        candidates.append(Path(xdg_data).expanduser() / "opencode" / "auth.json")
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # OpenCode stores one record per provider; the Go key can live under
        # any of these names depending on how it was connected.
        for provider in ("opencode-go", "opencode", "opencode-zen", "zen"):
            record = data.get(provider)
            if isinstance(record, dict):
                for field in ("key", "apiKey", "api_key"):
                    value = record.get(field)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    return None


def resolve_api_key() -> str | None:
    return _env_api_key() or _hermes_env_api_key() or _auth_file_api_key()


# ------------------------------------------------------------------ quota fetch


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _parse_timestamp(value: Any) -> str | None:
    number = _as_float(value)
    if number is not None:
        if number > 1_000_000_000_000:
            number /= 1000.0
        if number > 1_000_000_000:
            try:
                return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                return None
        return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    return None


def _window_percent(window: dict[str, Any]) -> float | None:
    direct = None
    for key in _PERCENT_KEYS:
        direct = _as_float(window.get(key))
        if direct is not None:
            break
    if direct is None:
        used = next((_as_float(window[key]) for key in _USED_KEYS if _as_float(window.get(key)) is not None), None)
        limit = next((_as_float(window[key]) for key in _LIMIT_KEYS if _as_float(window.get(key)) is not None), None)
        if used is None or limit is None or limit <= 0:
            return None
        direct = (used / limit) * 100.0
    # The live payload reports 0-100 integers; a fractional 0..1 value is a
    # fraction and must be scaled, while an integral ``1`` means one percent.
    if 0.0 < direct <= 1.0 and direct != int(direct):
        direct *= 100.0
    return max(0.0, min(100.0, direct))


def _window_reset_iso(window: dict[str, Any], now: float) -> str:
    for key in _RESET_AT_KEYS:
        parsed = _parse_timestamp(window.get(key))
        if parsed is not None:
            return parsed
    for key in _RESET_IN_SECONDS_KEYS:
        seconds = _as_float(window.get(key))
        if seconds is not None and seconds >= 0:
            return datetime.fromtimestamp(now + seconds, tz=timezone.utc).isoformat()
    return ""


def parse_quota_windows(payload: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Extract rolling / weekly / monthly windows from the usage payload."""
    if not isinstance(payload, dict):
        return []
    moment = time.time() if now is None else now
    # The live payload nests the windows under "usage"; a flat payload works too.
    nested = payload.get("usage")
    scope: dict[str, Any] = nested if isinstance(nested, dict) else payload
    windows: list[dict[str, Any]] = []
    for label, aliases in _WINDOW_LABELS:
        window = next((scope[key] for key in aliases if isinstance(scope.get(key), dict)), None)
        if window is None:
            continue
        percent = _window_percent(window)
        if percent is None:
            continue
        entry: dict[str, Any] = {"label": label, "percent": round(percent / 100.0, 4)}
        reset_iso = _window_reset_iso(window, moment)
        if reset_iso:
            entry["resetsAt"] = reset_iso
        windows.append(entry)
    return windows


def _get_json(api_key: str) -> tuple[Any | None, str]:
    """One quota round-trip with retries. Returns (payload, failure reason)."""
    reason = "no-data"
    for attempt in range(_ATTEMPTS):
        request = urllib.request.Request(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "omarchy-agents-usage/1.0",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
                body = response.read()
            try:
                return json.loads(body), ""
            except ValueError:
                reason, retryable = "bad-json", True
        except urllib.error.HTTPError as exc:
            if exc.fp is not None:
                exc.close()
            if exc.code in (401, 403):
                return None, "auth-failed"
            reason = "usage-unavailable" if exc.code == 503 else f"http-{exc.code}"
            retryable = exc.code in _TRANSIENT_STATUSES or exc.code == 503
        except Exception as exc:  # noqa: BLE001 - the record reports the failure
            reason = f"fetch-error:{type(exc).__name__}"
            retryable = True
        if not retryable or attempt == _ATTEMPTS - 1:
            break
        time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
    return None, reason


# ------------------------------------------------------------------ local data


def _empty_bucket() -> dict[str, int]:
    return {key: 0 for key in _TOKEN_BUCKET_KEYS}


def _accumulate(usage: dict[str, dict[str, int]], model: str, bucket: dict[str, int]) -> None:
    target = usage.setdefault(model, _empty_bucket())
    for key in _TOKEN_BUCKET_KEYS:
        target[key] += bucket[key]


def _merge_models(*sources: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for source in sources:
        for model, bucket in source.items():
            _accumulate(merged, model, bucket)
    return {
        model: bucket
        for model, bucket in sorted(merged.items())
        if sum(bucket[key] for key in _TOKEN_BUCKET_KEYS) > 0
    }


def _clean_model(raw: Any) -> str:
    text = str(raw or "")
    return text if _MODEL_ID_RE.match(text) else ""


def load_hermes_route() -> dict[str, Any] | None:
    """The Hermes record's OpenCode Go billing route, if it exists."""
    try:
        record = json.loads((usage_dir_path() / "hermes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    routes = record.get("routes")
    if not isinstance(routes, list):
        return None
    for route in routes:
        if isinstance(route, dict) and route.get("id") == PROVIDER_ID:
            return route
    return None


def collect_opencode_db(now: datetime) -> dict[str, Any]:
    """Token metadata for Go calls made through the OpenCode CLI itself."""
    db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    empty = {
        "models": {},
        "daily": {},
        "today_calls": 0,
        "today_sessions": set(),
        "total_calls": 0,
        "sessions": set(),
        "active_dates": set(),
    }
    if not db_path.is_file():
        return empty
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    except sqlite3.OperationalError:
        return empty
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 3000")
        cursor = conn.execute(
            """
            SELECT
                json_extract(data, '$.modelID'),
                json_extract(data, '$.tokens.input'),
                json_extract(data, '$.tokens.output'),
                json_extract(data, '$.tokens.reasoning'),
                json_extract(data, '$.tokens.cache.read'),
                json_extract(data, '$.tokens.cache.write'),
                json_extract(data, '$.time.created'),
                session_id
            FROM message
            WHERE json_extract(data, '$.providerID') = ?
              AND json_extract(data, '$.role') = 'assistant'
            LIMIT ?
            """,
            (PROVIDER_ID, _MAX_MESSAGES),
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        return empty
    finally:
        try:
            conn.close()
        except Exception:
            pass

    today = now.date().isoformat()
    for model_raw, in_tok, out_tok, reason_tok, cache_read, cache_write, created_ms, session_id in rows:
        model = _clean_model(model_raw)
        if model == "":
            continue
        bucket = {
            "inputTokens": int(in_tok or 0),
            # Reasoning tokens are part of the billed output stream.
            "outputTokens": int(out_tok or 0) + int(reason_tok or 0),
            "cacheReadInputTokens": int(cache_read or 0),
            "cacheCreationInputTokens": int(cache_write or 0),
        }
        _accumulate(empty["models"], model, bucket)
        empty["total_calls"] += 1
        empty["sessions"].add(str(session_id or ""))
        day = ""
        stamp = _as_float(created_ms)
        if stamp is not None:
            seconds = stamp / 1000.0 if stamp > 1_000_000_000_000 else stamp
            try:
                day = datetime.fromtimestamp(seconds).date().isoformat()
            except (OverflowError, OSError, ValueError):
                day = ""
        if day:
            _accumulate(empty["daily"].setdefault(day, {}), model, bucket)
            empty["active_dates"].add(day)
            if day == today:
                empty["today_calls"] += 1
                empty["today_sessions"].add(str(session_id or ""))
    return empty


def build_local_stats(hermes_route: dict[str, Any] | None, opencode: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Merge the Hermes Go route with OpenCode-CLI calls into one local view."""
    today = now.date().isoformat()
    recent_dates = recent_date_strings(now)
    period_dates = {
        "today": {today},
        "7d": {(now.date() - timedelta(days=offset)).isoformat() for offset in range(7)},
        "month": {(now.date() - timedelta(days=offset)).isoformat() for offset in range(30)},
    }

    route = hermes_route or {}
    route_models = {model: dict(bucket) for model, bucket in (route.get("modelUsage") or {}).items()}

    opencode_periods: dict[str, dict[str, dict[str, int]]] = {}
    for period, days in period_dates.items():
        period_usage: dict[str, dict[str, int]] = {}
        for day in days:
            for model, bucket in (opencode.get("daily") or {}).get(day, {}).items():
                _accumulate(period_usage, model, bucket)
        opencode_periods[period] = period_usage

    models_all = _merge_models(route_models, opencode.get("models") or {})
    by_period: dict[str, dict[str, dict[str, int]]] = {}
    for period in ("today", "7d", "month"):
        route_period = (route.get("modelUsageByPeriod") or {}).get(period) or {}
        by_period[period] = _merge_models(route_period, opencode_periods[period])
    by_period["all"] = models_all

    today_by_model: dict[str, int] = {}
    # The Hermes record publishes today-by-model as plain token totals, while
    # this collector's own aggregation carries full buckets.
    for model, value in (route.get("todayTokensByModel") or {}).items():
        token_total = sum(value.values()) if isinstance(value, dict) else int(value or 0)
        today_by_model[model] = today_by_model.get(model, 0) + token_total
    for model, bucket in opencode_periods["today"].items():
        today_by_model[model] = today_by_model.get(model, 0) + sum(bucket.values())
    today_by_model = {model: total for model, total in sorted(today_by_model.items()) if total > 0}

    opencode_daily_totals = {
        day: sum(sum(bucket.values()) for bucket in models.values())
        for day, models in (opencode.get("daily") or {}).items()
    }
    route_recent = {entry.get("date"): entry.get("messageCount", 0) for entry in (route.get("recentDays") or []) if isinstance(entry, dict)}
    recent_days = [
        {"date": day, "messageCount": int(route_recent.get(day, 0) or 0) + int(opencode_daily_totals.get(day, 0) or 0)}
        for day in recent_dates
    ]

    active_dates = set(route.get("activeDates") or []) | set(opencode.get("active_dates") or [])
    local_models = models_all
    return {
        "has_local": bool(hermes_route) or bool(opencode.get("sessions")),
        "today_prompts": int(route.get("todayPrompts") or 0) + int(opencode.get("today_calls") or 0),
        "today_sessions": int(route.get("todaySessions") or 0) + len(opencode.get("today_sessions") or set()),
        "today_tokens_by_model": today_by_model,
        "today_total_tokens": sum(today_by_model.values()),
        "recent_days": recent_days,
        "total_prompts": int(route.get("totalPrompts") or 0) + int(opencode.get("total_calls") or 0),
        "total_sessions": int(route.get("totalSessions") or 0) + len(opencode.get("sessions") or set()),
        "active_days": len(active_dates),
        "active_dates": sorted(active_dates),
        "model_usage": local_models,
        "model_usage_by_period": by_period,
    }


# ------------------------------------------------------------------ record


def collect_usage() -> dict[str, Any]:
    now = datetime.now()
    hermes_route = load_hermes_route()
    opencode = collect_opencode_db(now)
    local = build_local_stats(hermes_route, opencode, now)

    windows: list[dict[str, Any]] = []
    plan = ""
    quota_reason = "no-key"
    api_key = resolve_api_key()
    if api_key:
        payload, quota_reason = _get_json(api_key)
        if payload is not None:
            windows = parse_quota_windows(payload)
            if not windows:
                quota_reason = "no-data"
            if isinstance(payload, dict):
                raw_plan = payload.get("plan") or payload.get("planType")
                if isinstance(raw_plan, str):
                    plan = raw_plan.strip()

    quota_ok = len(windows) > 0
    ready = quota_ok or local["has_local"]

    usage_status = ""
    auth_help = ""
    if not quota_ok:
        usage_status = USAGE_STATUS
        if quota_reason == "auth-failed":
            auth_help = AUTH_REJECTED_HELP
        elif quota_reason == "no-key":
            auth_help = AUTH_HELP
    if not ready:
        usage_status = MISSING_STATUS
        auth_help = auth_help or AUTH_HELP

    # The panel's All Time card reads the published cumulative total rather
    # than extrapolating one from the seven-day window.
    all_time_tokens = sum(
        sum(bucket.values()) for bucket in local["model_usage"].values()
    )

    record: dict[str, Any] = {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "ready": ready,
        "hasLocalStats": local["has_local"],
        "todayPrompts": local["today_prompts"],
        "todaySessions": local["today_sessions"],
        "todayTotalTokens": local["today_total_tokens"],
        "todayTokensByModel": local["today_tokens_by_model"],
        "recentDays": local["recent_days"],
        "totalPrompts": local["total_prompts"],
        "totalSessions": local["total_sessions"],
        "totalTokens": all_time_tokens,
        "tokens": all_time_tokens,
        "activeDays": local["active_days"],
        "activeDates": local["active_dates"],
        "modelUsage": local["model_usage"],
        "modelUsageByPeriod": local["model_usage_by_period"],
        "limits": windows,
        "tierLabel": plan,
        "usageStatusText": usage_status,
        "authHelpText": auth_help,
    }
    if not quota_ok and quota_reason not in ("no-key", "auth-failed"):
        # A transient endpoint failure retries on the next refresh sooner.
        record["retryAdvised"] = True
    return record


def atomic_write_record(usage_dir: Path, record: dict[str, Any]) -> Path:
    usage_dir.mkdir(parents=True, exist_ok=True)
    target_path = usage_dir / f"{AGENT_ID}.json"
    content = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect OpenCode Go usage into an Omarchy agents record")
    parser.add_argument("--force", action="store_true", help="Force refresh usage stats")
    parser.add_argument("--limits-only", action="store_true", help="Refresh limits only")
    parser.parse_args(argv)

    record = collect_usage()
    usage_dir = usage_dir_path()
    try:
        atomic_write_record(usage_dir, record)
    except Exception as exc:  # noqa: BLE001 - report and continue to stdout
        print(f"opencode-go-collector: failed to write record to {usage_dir}: {exc}", file=sys.stderr)

    print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
