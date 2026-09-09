#!/usr/bin/env python3
# omarchy:summary=Collect rolling model-token history from standard usage records
# omarchy:args=[--force]
# omarchy:hidden=true
"""Collect a privacy-safe rolling model-token history.

Only direct, standard Omarchy ``*.json`` usage records in the usage directory
are inspected.  This collector never opens provider databases, transcripts,
prompts, credentials, or any other private runtime source.  Its only output is
the hidden ``.model-history.json`` sidecar in that same usage directory.

The sidecar keeps bounded local daily buckets so records that expose only
cumulative ``modelUsage`` can contribute new-token deltas without attributing
historical totals to the first observation.  A standard
``todayTokensByModel`` map, when present, is authoritative for the current day.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable


SIDECAR_NAME = ".model-history.json"
SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_MODELS = 1024
MAX_ROUTES = 256
MAX_DAILY_BUCKETS = 31
MAX_TOKEN_VALUE = 10**18

_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}\Z")
_PROVIDER_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_PRIVATE_SOURCE_NAME_RE = re.compile(
    r"(?:credential|secret|password|prompt|transcript|conversation|message|keyring|"
    r"api[-_]?key|access[-_]?token|database|(?:^|[-_.])db(?:$|[-_.]))",
    re.IGNORECASE,
)
_SENSITIVE_MODEL_RE = re.compile(
    r"(?:^|[-_.])(?:secret|credential|password|api[-_]?key|access[-_]?token)(?:$|[-_.])",
    re.IGNORECASE,
)
_TOKEN_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)
_PERIOD_NAMES = ("today", "7d", "month", "all")


class CollectorError(Exception):
    """Raised when a usage source or sidecar fails closed validation."""


class _UsageView:
    __slots__ = ("key", "model_usage", "today", "has_today")

    def __init__(
        self,
        key: str,
        model_usage: dict[str, int | float],
        today: dict[str, int | float],
        has_today: bool,
    ) -> None:
        self.key = key
        self.model_usage = model_usage
        self.today = today
        self.has_today = has_today


def usage_dir_path() -> Path:
    """Return the only source/output directory used by this collector."""
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return base / "omarchy" / "agents" / "usage"


def sidecar_path(usage_dir: Path | None = None) -> Path:
    """Return the hidden local sidecar path."""
    directory = usage_dir if usage_dir is not None else usage_dir_path()
    return Path(directory) / SIDECAR_NAME


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_json_file(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise CollectorError("usage source is too large")
        text = path.read_text(encoding="utf-8")
        return json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except CollectorError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise CollectorError("usage source is unreadable") from exc


def _safe_model_id(value: Any) -> str:
    if not isinstance(value, str):
        raise CollectorError("unsafe model identifier")
    if (
        not value
        or len(value) > 128
        or value != value.strip()
        or not _MODEL_ID_RE.fullmatch(value)
        or "//" in value
        or "://" in value
        or ".." in value
        or _SENSITIVE_MODEL_RE.search(value) is not None
    ):
        raise CollectorError("unsafe model identifier")
    return value


def _safe_provider_id(value: Any) -> str:
    if not isinstance(value, str):
        raise CollectorError("unsafe provider identifier")
    normalized = value.strip().lower()
    if (
        normalized != value
        or not _PROVIDER_ID_RE.fullmatch(normalized)
        or _PRIVATE_SOURCE_NAME_RE.search(normalized) is not None
    ):
        raise CollectorError("unsafe provider identifier")
    return normalized


def _safe_route_id(value: Any) -> str:
    return _safe_provider_id(value)


def _safe_output_key(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 128:
        raise CollectorError("unsafe history key")
    parts = value.split(":")
    if len(parts) == 1:
        _safe_provider_id(parts[0])
    elif len(parts) == 2:
        _safe_provider_id(parts[0])
        _safe_route_id(parts[1])
    else:
        raise CollectorError("unsafe history key")
    return value


def _safe_number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectorError("unsafe token number")
    if isinstance(value, float) and not math.isfinite(value):
        raise CollectorError("unsafe token number")
    if value < 0 or value > MAX_TOKEN_VALUE:
        raise CollectorError("unsafe token number")
    return value


def _add_numbers(left: int | float, right: int | float) -> int | float:
    total = left + right
    if isinstance(total, float) and not math.isfinite(total):
        raise CollectorError("token total overflow")
    if total < 0 or total > MAX_TOKEN_VALUE:
        raise CollectorError("token total overflow")
    return total


def _sorted_map(values: dict[str, int | float]) -> dict[str, int | float]:
    return {key: values[key] for key in sorted(values)}


def _normalize_scalar_map(raw: Any) -> dict[str, int | float]:
    if not isinstance(raw, dict) or len(raw) > MAX_MODELS:
        raise CollectorError("malformed model-token map")
    normalized: dict[str, int | float] = {}
    for raw_model, raw_value in raw.items():
        model = _safe_model_id(raw_model)
        normalized[model] = _safe_number(raw_value)
    return _sorted_map(normalized)


def _normalize_model_usage(raw: Any) -> dict[str, int | float]:
    if not isinstance(raw, dict) or len(raw) > MAX_MODELS:
        raise CollectorError("malformed model usage")
    normalized: dict[str, int | float] = {}
    for raw_model, raw_bucket in raw.items():
        model = _safe_model_id(raw_model)
        if not isinstance(raw_bucket, dict):
            raise CollectorError("malformed model usage bucket")
        total: int | float = 0
        for field in _TOKEN_FIELDS:
            if field in raw_bucket:
                total = _add_numbers(total, _safe_number(raw_bucket[field]))
        normalized[model] = total
    return _sorted_map(normalized)


def _merge_maps(*maps: dict[str, int | float]) -> dict[str, int | float]:
    merged: dict[str, int | float] = {}
    for values in maps:
        for model, value in values.items():
            # Every map has already been validated, but validate the internal
            # boundary too so state cannot grow beyond the bounded contract.
            safe_model = _safe_model_id(model)
            safe_value = _safe_number(value)
            merged[safe_model] = _add_numbers(merged.get(safe_model, 0), safe_value)
    return _sorted_map(merged)


def _positive_delta(
    current: dict[str, int | float],
    previous: dict[str, int | float] | None,
) -> dict[str, int | float]:
    """Return only deltas for models that had a previous cumulative baseline."""
    if previous is None:
        return {}
    delta: dict[str, int | float] = {}
    for model, current_value in current.items():
        if model not in previous:
            # A newly observed model may have a historical all-time total.
            # Never treat that first value as today's usage.
            continue
        previous_value = previous[model]
        if current_value >= previous_value:
            delta[model] = current_value - previous_value
    return _sorted_map(delta)


def _parse_day(value: Any) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise CollectorError("unsafe daily bucket date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CollectorError("unsafe daily bucket date") from exc


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise CollectorError("unsafe timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectorError("unsafe timestamp") from exc
    if parsed.tzinfo is None:
        raise CollectorError("unsafe timestamp")
    return value


def _coerce_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalize_daily_buckets(raw: Any) -> dict[str, dict[str, int | float]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CollectorError("malformed daily history")
    normalized: dict[str, dict[str, int | float]] = {}
    for raw_day, raw_values in raw.items():
        day = _parse_day(raw_day)
        normalized[day.isoformat()] = _normalize_scalar_map(raw_values)
    return normalized


def _prune_daily_buckets(
    buckets: dict[str, dict[str, int | float]],
    current_day: date,
) -> dict[str, dict[str, int | float]]:
    valid: list[tuple[date, str, dict[str, int | float]]] = []
    for raw_day, values in buckets.items():
        parsed_day = _parse_day(raw_day)
        if parsed_day > current_day:
            continue
        if values:
            valid.append((parsed_day, parsed_day.isoformat(), _sorted_map(values)))
    valid.sort(key=lambda item: item[0])
    return {day_key: values for _, day_key, values in valid[-MAX_DAILY_BUCKETS:]}


def _aggregate_daily(
    buckets: dict[str, dict[str, int | float]],
    *,
    start: date,
    end: date,
) -> dict[str, int | float]:
    selected: list[dict[str, int | float]] = []
    for raw_day, values in buckets.items():
        parsed_day = _parse_day(raw_day)
        if start <= parsed_day <= end:
            selected.append(values)
    return _merge_maps(*selected) if selected else {}


def _parse_usage_view(raw: Any, key: str) -> _UsageView:
    if not isinstance(raw, dict):
        raise CollectorError("malformed usage record")
    model_usage = _normalize_model_usage(raw.get("modelUsage", {}))
    has_today = "todayTokensByModel" in raw
    today = _normalize_scalar_map(raw["todayTokensByModel"]) if has_today else {}
    return _UsageView(key, model_usage, today, has_today)


def _parse_record(path: Path) -> list[_UsageView]:
    raw = _load_json_file(path)
    if not isinstance(raw, dict):
        raise CollectorError("malformed usage record")

    schema_version = raw.get("schemaVersion")
    if schema_version is not None and (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise CollectorError("unsupported usage record")

    raw_provider = raw.get("id", path.stem)
    provider = _safe_provider_id(raw_provider)
    views = [_parse_usage_view(raw, provider)]

    routes = raw.get("routes", [])
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        raise CollectorError("malformed usage routes")
    for route in routes:
        if not isinstance(route, dict):
            raise CollectorError("malformed usage route")
        route_id = _safe_route_id(route.get("id"))
        is_all = route.get("isAll", False)
        if not isinstance(is_all, bool):
            raise CollectorError("malformed usage route")
        route_key = f"{provider}:{route_id}"
        views.append(_parse_usage_view(route, route_key))
    return views


def _iter_usage_files(usage_dir: Path, excluded_names: set[str]) -> Iterable[Path]:
    if os.path.lexists(usage_dir):
        if usage_dir.is_symlink() or not usage_dir.is_dir():
            raise CollectorError("usage directory is not safe")
    else:
        return

    try:
        entries = sorted(usage_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise CollectorError("usage directory is unreadable") from exc

    for entry in entries:
        name = entry.name
        if name in excluded_names or name.startswith("."):
            continue
        if entry.suffix.lower() != ".json":
            continue
        # Avoid opening names that conventionally identify private runtime
        # sources. Provider records have ordinary provider names instead.
        if _PRIVATE_SOURCE_NAME_RE.search(name) is not None:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        yield entry


def _validate_history_state(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise CollectorError("malformed history sidecar")
    if raw.get("schemaVersion") != SCHEMA_VERSION:
        raise CollectorError("unsupported history sidecar")
    if not isinstance(raw.get("updatedAt"), str):
        raise CollectorError("malformed history sidecar")
    _safe_timestamp(raw["updatedAt"])
    raw_providers = raw.get("providers")
    if not isinstance(raw_providers, dict) or len(raw_providers) > MAX_MODELS:
        raise CollectorError("malformed history sidecar")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_key, raw_state in raw_providers.items():
        key = _safe_output_key(raw_key)
        if not isinstance(raw_state, dict):
            raise CollectorError("malformed history provider")
        raw_periods = raw_state.get("modelUsageByPeriod")
        if not isinstance(raw_periods, dict):
            raise CollectorError("malformed history periods")
        periods: dict[str, dict[str, int | float]] = {}
        for period in _PERIOD_NAMES:
            if period not in raw_periods:
                raise CollectorError("malformed history periods")
            periods[period] = _normalize_scalar_map(raw_periods[period])

        daily = _normalize_daily_buckets(raw_state.get("dailyTokenBuckets", {}))
        baseline_present = "lastModelUsage" in raw_state
        baseline = (
            _normalize_scalar_map(raw_state["lastModelUsage"])
            if baseline_present
            else None
        )
        last_seen = raw_state.get("lastSeenAt")
        if last_seen is not None:
            last_seen = _safe_timestamp(last_seen)
        normalized[key] = {
            "modelUsageByPeriod": periods,
            "dailyTokenBuckets": daily,
            "lastModelUsage": baseline,
            "baselinePresent": baseline_present,
            "lastSeenAt": last_seen,
        }
    return normalized


def _load_previous_history(path: Path) -> dict[str, dict[str, Any]]:
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_file():
        raise CollectorError("history sidecar is not safe")
    raw = _load_json_file(path)
    return _validate_history_state(raw)


def _build_provider_state(
    view: _UsageView,
    previous: dict[str, Any] | None,
    *,
    current_day: date,
    observed_at: str,
) -> dict[str, Any]:
    previous_daily = previous.get("dailyTokenBuckets", {}) if previous else {}
    daily = dict(previous_daily)
    previous_baseline = (
        previous.get("lastModelUsage")
        if previous and previous.get("baselinePresent")
        else None
    )

    if view.has_today:
        today = dict(view.today)
    else:
        delta = _positive_delta(view.model_usage, previous_baseline)
        today = _merge_maps(daily.get(current_day.isoformat(), {}), delta)

    if today:
        daily[current_day.isoformat()] = _sorted_map(today)
    else:
        daily.pop(current_day.isoformat(), None)
    daily = _prune_daily_buckets(daily, current_day)
    today = dict(daily.get(current_day.isoformat(), {}))

    seven_day_start = current_day - timedelta(days=6)
    # Match Omarchy's public "1 month" period: the current day plus the
    # preceding 29 local days, rather than a calendar-month reset.
    month_start = current_day - timedelta(days=29)
    periods = {
        "today": _sorted_map(today),
        "7d": _aggregate_daily(daily, start=seven_day_start, end=current_day),
        "month": _aggregate_daily(daily, start=month_start, end=current_day),
        "all": _sorted_map(view.model_usage),
    }
    return {
        "modelUsageByPeriod": periods,
        "dailyTokenBuckets": daily,
        "lastModelUsage": _sorted_map(view.model_usage),
        "lastSeenAt": observed_at,
    }


def _retain_provider_state(
    previous: dict[str, Any],
    *,
    current_day: date,
) -> dict[str, Any]:
    daily = _prune_daily_buckets(previous.get("dailyTokenBuckets", {}), current_day)
    old_periods = previous["modelUsageByPeriod"]
    if daily:
        periods = {
            "today": _sorted_map(daily.get(current_day.isoformat(), {})),
            "7d": _aggregate_daily(
                daily,
                start=current_day - timedelta(days=6),
                end=current_day,
            ),
            "month": _aggregate_daily(
                daily,
                start=current_day - timedelta(days=29),
                end=current_day,
            ),
            "all": _sorted_map(old_periods["all"]),
        }
    else:
        periods = {period: _sorted_map(old_periods[period]) for period in _PERIOD_NAMES}

    retained: dict[str, Any] = {
        "modelUsageByPeriod": periods,
        "dailyTokenBuckets": daily,
    }
    if previous.get("baselinePresent"):
        retained["lastModelUsage"] = _sorted_map(previous["lastModelUsage"])
    if previous.get("lastSeenAt") is not None:
        retained["lastSeenAt"] = previous["lastSeenAt"]
    return retained


def build_history(
    usage_dir: Path | str,
    *,
    now: datetime | None = None,
    history_path: Path | str | None = None,
) -> dict[str, Any]:
    """Read standard usage JSON and return a validated sidecar payload.

    The function has no access to private provider runtime locations. Invalid
    standard-looking input raises ``CollectorError`` before any output write.
    """
    directory = Path(usage_dir)
    target = Path(history_path) if history_path is not None else sidecar_path(directory)
    previous = _load_previous_history(target)
    current_time = _coerce_now(now)
    current_day = current_time.date()
    observed_at = _utc_timestamp(current_time)

    views_by_key: dict[str, _UsageView] = {}
    excluded_names = {SIDECAR_NAME, target.name}
    for path in _iter_usage_files(directory, excluded_names):
        for view in _parse_record(path):
            if view.key in views_by_key:
                raise CollectorError("duplicate provider history key")
            views_by_key[view.key] = view

    providers: dict[str, dict[str, Any]] = {}
    for key in sorted(views_by_key):
        providers[key] = _build_provider_state(
            views_by_key[key],
            previous.get(key),
            current_day=current_day,
            observed_at=observed_at,
        )

    # Keep a provider's bounded local history when its standard record is
    # temporarily absent, but never let absent data replace a current record.
    for key in sorted(set(previous) - set(providers)):
        providers[key] = _retain_provider_state(previous[key], current_day=current_day)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": observed_at,
        "providers": {key: providers[key] for key in sorted(providers)},
    }


def atomic_write_history(target: Path | str, payload: dict[str, Any]) -> Path:
    """Atomically replace the hidden sidecar using a same-directory temp file."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ) + "\n"

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    open_descriptor: int | None = file_descriptor
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            open_descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            directory_descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if open_descriptor is not None:
            os.close(open_descriptor)
        if temporary.exists():
            temporary.unlink()
    return destination


def collect_history(
    usage_dir: Path | str | None = None,
    *,
    now: datetime | None = None,
    history_path: Path | str | None = None,
) -> dict[str, Any]:
    """Convenience alias for callers that only need the in-memory payload."""
    directory = Path(usage_dir) if usage_dir is not None else usage_dir_path()
    return build_history(directory, now=now, history_path=history_path)


def collect_and_write(
    usage_dir: Path | str | None = None,
    *,
    now: datetime | None = None,
    history_path: Path | str | None = None,
) -> Path:
    """Build and atomically publish the hidden history sidecar."""
    directory = Path(usage_dir) if usage_dir is not None else usage_dir_path()
    target = Path(history_path) if history_path is not None else sidecar_path(directory)
    payload = build_history(directory, now=now, history_path=target)
    return atomic_write_history(target, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect local rolling model-token history")
    parser.add_argument(
        "--force",
        action="store_true",
        help="accepted for Omarchy collector compatibility",
    )
    args = parser.parse_args(argv)
    del args
    try:
        collect_and_write()
    except (CollectorError, OSError, TypeError, ValueError):
        # Do not expose paths, model IDs, token totals, or source contents.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
