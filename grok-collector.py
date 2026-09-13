#!/usr/bin/env python3
# omarchy:summary=Print the Grok usage record as JSON
# omarchy:args=[--force] [--limits-only]
# omarchy:hidden=true
"""Collect local Grok CLI usage into one display-ready JSON record.

Omarchy's packaged updater remains authoritative. If
``$OMARCHY_PATH/bin/omarchy-agent-usage-grok`` exists, this collector
exits without writing so the official collector owns ``grok.json``.

Otherwise it reads only Grok's documented per-session ``usage.json``
files under ``$GROK_HOME/sessions`` (default ``~/.grok/sessions``) and
the sibling ``summary.json`` session-kind field used to skip subagents.
It never opens transcripts, chat history, prompts, credentials, or
billing endpoints, and it never infers a subscription from a model name.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

AGENT_ID = "grok"
AGENT_NAME = "Grok"
USAGE_STATUS = "Limit unavailable / usage-only"
AUTH_HELP = "Run `grok login` to authenticate the Grok CLI."
USAGE_FILE_NAME = "usage.json"
SUMMARY_FILE_NAME = "summary.json"
MAX_USAGE_BYTES = 2 * 1024 * 1024
MAX_SESSIONS = 4096

_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}\Z")
_TOKEN_BUCKET_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)
_MODEL_USAGE_PERIODS = ("today", "7d", "month", "all")
_SUBAGENT_KEYS = (
    "session_kind",
    "sessionKind",
    "session_relationship",
    "sessionRelationship",
    "kind",
)


def grok_home() -> Path:
    raw = os.environ.get("GROK_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".grok"


def usage_dir_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        base = Path(xdg_state).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / "omarchy" / "agents" / "usage"


def packaged_grok_collector() -> Path | None:
    omarchy_path = os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")
    candidate = Path(omarchy_path).expanduser() / "bin" / "omarchy-agent-usage-grok"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def recent_date_strings(today_dt: datetime) -> list[str]:
    return [(today_dt - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(6, -1, -1)]


def _empty_token_bucket() -> dict[str, int]:
    return {key: 0 for key in _TOKEN_BUCKET_KEYS}


def _empty_model_usage_by_period() -> dict[str, dict[str, dict[str, int]]]:
    return {period: {} for period in _MODEL_USAGE_PERIODS}


def number(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def safe_model_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or not _MODEL_ID_RE.fullmatch(value):
        return ""
    lowered = value.lower()
    if any(token in lowered for token in ("secret", "password", "api-key", "api_key", "access-token", "access_token")):
        return ""
    return value


def parse_local_day(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, rest = text.split(".", 1)
        digits = ""
        tz = ""
        for index, char in enumerate(rest):
            if char.isdigit():
                digits += char
            else:
                tz = rest[index:]
                break
        text = f"{head}.{(digits + '000000')[:6]}{tz}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d")


def split_tokens(blob: dict[str, Any]) -> tuple[int, int, int, int]:
    input_tokens = number(blob.get("inputTokens", blob.get("input_tokens")))
    output_tokens = number(blob.get("outputTokens", blob.get("output_tokens")))
    cache_read = number(
        blob.get("cachedReadTokens", blob.get("cacheReadInputTokens", blob.get("cache_read_tokens")))
    )
    cache_write = number(
        blob.get("cacheCreationTokens", blob.get("cacheCreationInputTokens", blob.get("cache_write_tokens")))
    )
    exclusive_input = input_tokens - cache_read if input_tokens >= cache_read else input_tokens
    return exclusive_input, output_tokens, cache_read, cache_write


def _accumulate_model_bucket(
    model_usage: dict[str, dict[str, int]],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> None:
    if not model:
        return
    bucket = model_usage.setdefault(model, _empty_token_bucket())
    bucket["inputTokens"] += input_tokens
    bucket["outputTokens"] += output_tokens
    bucket["cacheReadInputTokens"] += cache_read
    bucket["cacheCreationInputTokens"] += cache_write


def _filter_model_usage(raw_usage: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for model in sorted(raw_usage):
        bucket = raw_usage[model]
        if sum(bucket[key] for key in _TOKEN_BUCKET_KEYS) > 0:
            out[model] = bucket
    return out


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_USAGE_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _is_subagent(summary: dict[str, Any] | None) -> bool:
    if not isinstance(summary, dict):
        return False
    candidates: list[Any] = [summary.get(key) for key in _SUBAGENT_KEYS]
    info = summary.get("info")
    if isinstance(info, dict):
        candidates.extend(info.get(key) for key in _SUBAGENT_KEYS)
    for raw in candidates:
        value = str(raw or "").strip().lower()
        if value.startswith("subagent"):
            return True
    return False


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
        "recentDays": [{"date": day, "messageCount": 0} for day in recent_dates],
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
    }


def _model_usage_from_blob(blob: dict[str, Any]) -> dict[str, dict[str, int]]:
    usage: dict[str, dict[str, int]] = {}
    raw_models = blob.get("modelUsage")
    if isinstance(raw_models, dict) and raw_models:
        for raw_model, raw_bucket in raw_models.items():
            model = safe_model_id(raw_model)
            if not model or not isinstance(raw_bucket, dict):
                continue
            _accumulate_model_bucket(usage, model, *split_tokens(raw_bucket))
        return usage
    model = safe_model_id(blob.get("primaryModelId") or blob.get("model"))
    if model:
        _accumulate_model_bucket(usage, model, *split_tokens(blob))
    return usage


def collect_usage() -> dict[str, Any]:
    sessions_root = grok_home() / "sessions"
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    recent_dates = recent_date_strings(now)
    local_today = now.date()
    period_dates = {
        "today": {local_today.isoformat()},
        "7d": {(local_today - timedelta(days=offset)).isoformat() for offset in range(7)},
        "month": {(local_today - timedelta(days=offset)).isoformat() for offset in range(30)},
    }

    if not sessions_root.is_dir():
        return empty_record("Grok CLI usage not found", AUTH_HELP)

    all_model_usage: dict[str, dict[str, int]] = {}
    daily_model_usage: dict[str, dict[str, dict[str, int]]] = {}
    today_tokens_by_model: dict[str, int] = {}
    recent_map = {day: 0 for day in recent_dates}
    active_dates: set[str] = set()
    session_ids: set[str] = set()
    today_sessions: set[str] = set()
    total_prompts = 0
    today_prompts = 0
    scanned = 0

    for usage_path in sessions_root.rglob(USAGE_FILE_NAME):
        if scanned >= MAX_SESSIONS:
            break
        if usage_path.name != USAGE_FILE_NAME or not usage_path.is_file():
            continue
        scanned += 1
        summary = _read_json_object(usage_path.with_name(SUMMARY_FILE_NAME))
        if _is_subagent(summary):
            continue
        payload = _read_json_object(usage_path)
        if not payload:
            continue

        session_blob = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        if not isinstance(session_blob, dict):
            continue
        session_id = str(payload.get("sessionId") or usage_path.parent.name)
        session_ids.add(session_id)

        session_models = _model_usage_from_blob(session_blob)
        for model, bucket in session_models.items():
            _accumulate_model_bucket(
                all_model_usage,
                model,
                bucket["inputTokens"],
                bucket["outputTokens"],
                bucket["cacheReadInputTokens"],
                bucket["cacheCreationInputTokens"],
            )

        turns = payload.get("turns")
        turn_rows = turns if isinstance(turns, list) else []
        counted_turns = 0
        for turn in turn_rows:
            if not isinstance(turn, dict):
                continue
            day = parse_local_day(turn.get("endedAt") or turn.get("timestamp"))
            turn_models = _model_usage_from_blob(turn)
            turn_tokens = 0
            for model, bucket in turn_models.items():
                tokens = sum(bucket[key] for key in _TOKEN_BUCKET_KEYS)
                turn_tokens += tokens
                if day:
                    day_usage = daily_model_usage.setdefault(day, {})
                    _accumulate_model_bucket(
                        day_usage,
                        model,
                        bucket["inputTokens"],
                        bucket["outputTokens"],
                        bucket["cacheReadInputTokens"],
                        bucket["cacheCreationInputTokens"],
                    )
                    if day == today:
                        today_tokens_by_model[model] = today_tokens_by_model.get(model, 0) + tokens
            if turn_tokens > 0 and day:
                active_dates.add(day)
                if day in recent_map:
                    recent_map[day] += turn_tokens
            counted_turns += 1
            if day == today:
                today_prompts += 1
                today_sessions.add(session_id)

        session_turn_count = number(session_blob.get("turnCount"))
        total_prompts += session_turn_count if session_turn_count > 0 else counted_turns

    all_model_usage = _filter_model_usage(all_model_usage)
    today_total_tokens = sum(today_tokens_by_model.values())
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
    by_period["all"] = all_model_usage

    ready = bool(session_ids) and (
        total_prompts > 0 or sum(sum(bucket.values()) for bucket in all_model_usage.values()) > 0
    )
    return {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "ready": ready,
        "hasLocalStats": True,
        "todayPrompts": today_prompts,
        "todaySessions": len(today_sessions),
        "todayTotalTokens": today_total_tokens,
        "todayTokensByModel": dict(sorted(today_tokens_by_model.items())),
        "recentDays": [{"date": day, "messageCount": recent_map[day]} for day in recent_dates],
        "totalPrompts": total_prompts,
        "totalSessions": len(session_ids),
        "activeDays": len(sorted(active_dates)),
        "activeDates": sorted(active_dates),
        "modelUsage": all_model_usage,
        "modelUsageByPeriod": by_period,
        "limits": [],
        "tierLabel": "",
        "usageStatusText": USAGE_STATUS if ready else "Grok CLI usage not found",
        "authHelpText": "" if ready else AUTH_HELP,
    }


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
    parser = argparse.ArgumentParser(description="Collect Grok CLI usage into Omarchy agents record")
    parser.add_argument("--force", action="store_true", help="Force refresh usage stats")
    parser.add_argument("--limits-only", action="store_true", help="Refresh limits only")
    parser.parse_args(argv)

    if packaged_grok_collector() is not None:
        return 0

    record = collect_usage()
    usage_dir = usage_dir_path()
    try:
        atomic_write_record(usage_dir, record)
    except Exception as exc:
        print(f"grok-collector: failed to write record to {usage_dir}: {exc}", file=sys.stderr)

    print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
