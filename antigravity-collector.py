#!/usr/bin/env python3
# omarchy:summary=Print Antigravity quota data as a local usage record
# omarchy:args=[--force] [--limits-only]
# omarchy:hidden=true
"""Collect Antigravity's documented quota views into an Omarchy record.

The official Antigravity CLI exposes ``/usage`` and ``/credits`` as slash
commands. Its non-interactive print mode currently returns bounded,
tab-separated rows for those views. This adapter invokes only those official
views, keeps only validated model/window, percentage, reset, and credit-count
fields, and atomically writes a local record.

It deliberately does not inspect Antigravity conversation databases, keyring
entries, environment credentials, prompts, or message content. If Omarchy
ships a native Antigravity collector, this fallback exits without touching its
record so the native collector remains authoritative.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable

AGENT_ID = "antigravity"
AGENT_NAME = "Antigravity"
NATIVE_COLLECTOR_NAME = "omarchy-agent-usage-antigravity"
CLI_NAME = "agy"
CLI_PRINT_TIMEOUT = "8s"
CLI_TIMEOUT_SECONDS = 15.0
MAX_OUTPUT_LINE_LENGTH = 512
MAX_OUTPUT_BYTES = 64 * 1024
CACHE_TTL_SECONDS = 120.0

_USAGE_ROW_RE = re.compile(r"^\s*([0-9]{1,3}(?:\.[0-9]+)?)%\s*$")
_CREDIT_LABEL_RE = re.compile(
    r"^(?:(?:ai|g1)\s+)?(?:credits?\s+remaining|remaining\s+credits?)$",
    re.IGNORECASE,
)
_CREDIT_VALUE_RE = re.compile(r"^[0-9]{1,12}(?:\.[0-9]+)?$")
_RESET_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$")
_SUSPICIOUS_LABEL_RE = re.compile(
    r"(?:://|[?&=]|\\|\b(?:access\s+token|api[_ -]?key|secret|password|credential)s?\b)",
    re.IGNORECASE,
)
_ALLOWED_ENV = {
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
}


# --------------------------------------------------------------------------- paths and process boundaries


def usage_dir_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return base / "omarchy" / "agents" / "usage"


def record_path() -> Path:
    return usage_dir_path() / f"{AGENT_ID}.json"


def record_is_fresh(target: Path | None = None, *, now: datetime | None = None) -> bool:
    """Use a recent adapter record without making another backend request."""
    destination = target or record_path()
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
        updated_at = payload.get("updatedAt")
        if not isinstance(updated_at, str):
            return False
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            return False
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age = (reference - updated.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= CACHE_TTL_SECONDS
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def native_collector_path() -> str:
    """Return a known Omarchy native collector path, or an empty string.

    Do not trust an arbitrary PATH shadow for the handoff decision. Omarchy
    installs its packaged collector under the Omarchy root's bin directory or
    in a system bin directory.
    """
    candidates: list[Path] = []
    omarchy_root = os.environ.get("OMARCHY_PATH")
    if omarchy_root:
        candidates.append(Path(omarchy_root).expanduser() / "bin" / NATIVE_COLLECTOR_NAME)
    candidates.extend(
        [
            Path("/usr/share/omarchy") / "bin" / NATIVE_COLLECTOR_NAME,
            Path("/usr/bin") / NATIVE_COLLECTOR_NAME,
            Path("/usr/local/bin") / NATIVE_COLLECTOR_NAME,
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def resolve_cli_path(cli_path: str | None = None) -> str:
    """Resolve the CLI without accepting a shell command string."""
    requested = cli_path or os.environ.get("ANTIGRAVITY_CLI") or CLI_NAME
    path = Path(requested).expanduser()
    if path.is_absolute() or "/" in requested:
        return str(path) if path.is_file() else ""
    return shutil.which(requested) or ""


def cli_environment() -> dict[str, str]:
    """Pass only ordinary desktop context; never forward arbitrary secrets."""
    env = {key: value for key, value in os.environ.items() if key in _ALLOWED_ENV}
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    return env


def run_cli_command(
    cli_path: str,
    slash_command: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Run one official slash view and return stdout, never stderr or errors."""
    command = [
        cli_path,
        "--print",
        slash_command,
        "--output-format",
        "text",
        "--print-timeout",
        CLI_PRINT_TIMEOUT,
    ]
    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            env=cli_environment(),
            shell=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    output = getattr(result, "stdout", "")
    if not isinstance(output, str):
        return ""
    try:
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return ""
    except UnicodeError:
        return ""
    return output


# --------------------------------------------------------------------------- strict display parsing


def safe_display_label(raw_value: Any) -> str:
    """Keep a short human label while rejecting endpoint/credential-shaped text."""
    if not isinstance(raw_value, str):
        return ""
    value = raw_value.strip()
    if not value or len(value) > 128 or _SUSPICIOUS_LABEL_RE.search(value):
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    return value


def safe_reset_value(raw_value: Any) -> str | None:
    """Return a validated UTC timestamp, empty for an absent reset, None if bad."""
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if value == "":
        return ""
    if not _RESET_RE.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _format_remaining_percent(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def parse_usage_output(output: str) -> list[dict[str, Any]]:
    """Parse only the current four-column Antigravity quota row contract.

    Antigravity reports the percentage remaining. The panel's standard meter
    represents percentage used, so the adapter converts ``remaining`` to
    ``1 - remaining`` and labels the source text explicitly.
    """
    limits: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, float]] = set()
    for line in str(output or "").splitlines():
        if len(line) > MAX_OUTPUT_LINE_LENGTH or line.count("\t") != 3:
            continue
        model_raw, window_raw, percent_raw, reset_raw = line.split("\t")
        model = safe_display_label(model_raw)
        window = safe_display_label(window_raw)
        reset_at = safe_reset_value(reset_raw)
        percent_match = _USAGE_ROW_RE.fullmatch(percent_raw)
        if not model or not window or reset_at is None or not percent_match:
            continue
        remaining = float(percent_match.group(1))
        if not math.isfinite(remaining) or remaining < 0 or remaining > 100:
            continue
        title = f"{model} · {window}"
        key = (model, window, reset_at, remaining)
        if key in seen:
            continue
        seen.add(key)
        limits.append(
            {
                "title": title,
                "label": title,
                "percent": round(1.0 - (remaining / 100.0), 6),
                "resetAt": reset_at,
                "status": f"{_format_remaining_percent(remaining)}% remaining",
                "source": "Antigravity CLI /usage",
            }
        )
    return limits


def parse_credits_output(output: str) -> int | float | None:
    """Return only a numeric AI-credit remainder from the documented view."""
    for line in str(output or "").splitlines():
        if len(line) > MAX_OUTPUT_LINE_LENGTH or line.count("\t") != 1:
            continue
        label_raw, value_raw = line.split("\t")
        label = label_raw.strip()
        value = value_raw.strip()
        if not _CREDIT_LABEL_RE.fullmatch(label) or not _CREDIT_VALUE_RE.fullmatch(value):
            continue
        number = float(value)
        if not math.isfinite(number) or number < 0:
            continue
        return int(number) if number.is_integer() else number
    return None


# --------------------------------------------------------------------------- standard Omarchy record


def build_record(usage_output: str, credits_output: str, *, now: datetime | None = None) -> dict[str, Any]:
    limits = parse_usage_output(usage_output)
    credit_remaining = parse_credits_output(credits_output)
    if credit_remaining is not None:
        # This is a count, not a currency balance and not a guessed allowance.
        limits.append(
            {
                "title": "AI credits",
                "label": "AI credits",
                "remaining": credit_remaining,
                "source": "Antigravity CLI /credits",
            }
        )

    ready = bool(limits)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": timestamp,
        "ready": ready,
        "hasLocalStats": False,
        "hasPromptStats": False,
        "todayPrompts": 0,
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [],
        "totalPrompts": 0,
        "totalSessions": 0,
        "activeDays": 0,
        "activeDates": [],
        "modelUsage": {},
        "limits": limits,
        "tierLabel": "",
        "usageStatusText": "" if ready else "Antigravity quota unavailable",
        "authHelpText": "" if ready else "Open Antigravity and sign in normally, then refresh.",
    }


def collect_record(
    cli_path: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    usage_output = run_cli_command(cli_path, "/usage", runner=runner)
    credits_output = run_cli_command(cli_path, "/credits", runner=runner)
    return build_record(usage_output, credits_output)


def write_record(record: dict[str, Any], target: Path | None = None) -> None:
    """Write a private record through a same-directory atomic replacement."""
    destination = target or record_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{AGENT_ID}.",
        suffix=".tmp",
        dir=str(destination.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--limits-only", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)

    # Native Omarchy support wins. In particular, do not race or overwrite the
    # official antigravity.json once the native collector is installed.
    if native_collector_path():
        return 0
    if not options.force and record_is_fresh():
        return 0

    cli_path = resolve_cli_path()
    record = collect_record(cli_path) if cli_path else build_record("", "")
    try:
        write_record(record)
    except OSError:
        print("Antigravity usage record could not be written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
