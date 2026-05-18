"""Shared 429 retry handling for ``claude --print`` subprocess calls.

Both ``ClaudeCliClient`` (single-call agents) and ``ClaudeCliCoder``
(tool-loop coder) spawn the same CLI subprocess shape and face the
same two flavors of 429 rate-limit response:

1. **Transient server-side 429** — backend throttling, no reset
   string in the body. Short backoff (10s, 30s, 60s), then give up.
2. **Max-plan usage-cap 429** — body contains
   ``"resets HH:MM(am|pm) (TZ)"``. Parse the reset time, sleep
   until then (capped by ``BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S``,
   default 6h), and retry indefinitely.

Before this module existed, only ``ClaudeCliClient`` had retry
handling. ``ClaudeCliCoder`` re-implemented subprocess.run inline
and treated 429 exit codes as fatal `CoderError`s — which caused
recipe_v2 M3 to lose 13 issues to Anthropic usage-cap windows.

This module is the deliberate de-duplication point.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9
    ZoneInfo = None  # type: ignore


# Max wall-clock wait when sleeping for a Max-plan usage-cap reset.
# 5h windows are typical; cap at 6h so a bug in time parsing can't
# sleep forever. Overridable via ``BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S``.
_DEFAULT_USAGE_CAP_MAX_WAIT_S = 6 * 60 * 60

# Short backoff schedule for transient server-side 429s.
_TRANSIENT_BACKOFF_S = [10.0, 30.0, 60.0]

# Matches the reset-time string the CLI emits on a Max-plan usage cap:
#   "You've hit your limit · resets 11:20am (America/Los_Angeles)"
#   "resets 3:05pm (UTC)"
_RESET_RE = re.compile(
    r"resets?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]m)"
    r"(?:\s*\((?P<tz>[A-Za-z][A-Za-z0-9_/+\-]*)\))?",
    re.IGNORECASE,
)


def parse_usage_cap_reset(body: str) -> Optional[float]:
    """Parse the reset-time string from a Max-plan 429 body and return
    seconds-until-reset (with a 30s buffer past the actual reset).

    Returns None for 429s without a parseable reset time (transient
    server-side throttles — caller uses short backoff instead).
    """
    if not body:
        return None
    m = _RESET_RE.search(body)
    if not m:
        return None
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    ampm = m.group("ampm").lower()
    tz_name = m.group("tz") or ""
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    tz = None
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (target - now).total_seconds()
    if delta < -60:
        # Reset already happened > 1 min ago — must mean tomorrow.
        target = target + timedelta(days=1)
        delta = (target - now).total_seconds()
    if delta < 0:
        delta = 0  # very recent reset; treat as "now"
    return delta + 30.0  # 30s buffer past the published reset


def _max_usage_wait_s() -> float:
    return float(
        os.environ.get(
            "BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S",
            str(_DEFAULT_USAGE_CAP_MAX_WAIT_S),
        )
    )


def _detect_429(stdout: str) -> Tuple[bool, str]:
    """Inspect a ``claude --print --output-format=json`` stdout
    payload. Returns ``(is_429, body)`` — body is the first 200 chars
    of the result string when the call was rate-limited.
    """
    if not stdout:
        return (False, "")
    try:
        early = json.loads(stdout)
    except Exception:
        return (False, "")
    if (
        early.get("api_error_status") == 429
        or "Rate limited" in (early.get("result") or "")
    ):
        return (True, (early.get("result") or "")[:200])
    return (False, "")


def run_with_429_retry(
    cmd: list,
    *,
    input: Optional[str] = None,  # noqa: A002 — mirrors subprocess.run
    timeout: float,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    log_prefix: str = "[claude_cli]",
) -> subprocess.CompletedProcess:
    """Run a ``claude --print`` subprocess with 429 retry handling.

    On transient 429: backoff 10s/30s/60s, then raise on exhaustion.
    On usage-cap 429: parse reset time, sleep until then (capped by
    ``BIZNIZ_CLAUDE_USAGE_CAP_MAX_WAIT_S``), retry indefinitely.

    Non-429 outcomes (success, other errors, timeout) are returned
    or raised through to the caller as-is. ``log_prefix`` distinguishes
    the two call-sites in stderr logs.

    Raises:
        subprocess.TimeoutExpired — propagated unchanged.
        FileNotFoundError — propagated unchanged.
        RuntimeError — when transient retries are exhausted.
    """
    max_usage_wait = _max_usage_wait_s()
    last_429_body = ""
    transient_attempt = 0

    while True:
        proc = subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )

        is_429, body = _detect_429(proc.stdout)
        if not is_429:
            return proc
        last_429_body = body

        # Usage-cap branch: sleep until reset, retry indefinitely.
        usage_cap_wait = parse_usage_cap_reset(last_429_body)
        if usage_cap_wait is not None:
            actual_wait = min(usage_cap_wait, max_usage_wait)
            print(
                f"  {log_prefix} Max-plan usage cap hit, sleeping "
                f"{actual_wait:.0f}s ({actual_wait/60:.1f} min) until "
                f"reset — {last_429_body[:120]}",
                file=sys.stderr, flush=True,
            )
            slept = 0.0
            while slept < actual_wait:
                chunk = min(300.0, actual_wait - slept)
                time.sleep(chunk)
                slept += chunk
                if slept < actual_wait:
                    remaining = actual_wait - slept
                    print(
                        f"  {log_prefix} still waiting for usage-cap "
                        f"reset — {remaining/60:.1f} min to go...",
                        file=sys.stderr, flush=True,
                    )
            continue

        # Transient branch: short backoff, give up after N attempts.
        if transient_attempt >= len(_TRANSIENT_BACKOFF_S):
            raise RuntimeError(
                f"claude --print rate-limited (429) after "
                f"{len(_TRANSIENT_BACKOFF_S) + 1} attempts: "
                f"{last_429_body}"
            )
        wait_s = _TRANSIENT_BACKOFF_S[transient_attempt]
        print(
            f"  {log_prefix} transient 429 (no reset time), backing "
            f"off {wait_s:.0f}s before retry "
            f"({transient_attempt + 1}/{len(_TRANSIENT_BACKOFF_S)})...",
            file=sys.stderr, flush=True,
        )
        time.sleep(wait_s)
        transient_attempt += 1
