"""Codex rate limits through `codex app-server`.

`usage-hud-codex-fetch` ported as text: the three functions below are the reference's, with
its `--print-profiles`-style CLI dropped because `yelo usage fetch` calls
`fetch_rate_limits` in process (board B6). Codex auth is the codex binary's own business
through CODEX_HOME; nothing here handles a credential.

USAGE_HUD_CODEX_FETCH_TIMEOUT (default 25 s) and RUST_LOG keep their reference names.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import time

TIMEOUT_DEFAULT = 25.0


def timeout_seconds():
    try:
        return float(os.environ.get("USAGE_HUD_CODEX_FETCH_TIMEOUT") or TIMEOUT_DEFAULT)
    except ValueError:
        return TIMEOUT_DEFAULT


def normalize_window(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("rate-limit window is not an object")
    used = value.get("usedPercent")
    minutes = value.get("windowDurationMins")
    resets_at = value.get("resetsAt")
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool)
               for item in (used, minutes, resets_at)):
        raise ValueError("rate-limit window is incomplete")
    if not all(math.isfinite(float(item)) for item in (used, minutes, resets_at)):
        raise ValueError("rate-limit window contains a non-finite number")
    return {
        "used_percent": max(0, min(100, round(float(used)))),
        "window_minutes": max(0, round(float(minutes))),
        "resets_at": round(float(resets_at)),
    }


def terminate_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def fetch_rate_limits(codex_bin, codex_home, timeout):
    """Speak the app-server's JSON lines: initialize, then account/rateLimits/read. The
    server runs in its own session so a wedged one can be killed as a group."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    environment = os.environ.copy()
    environment["CODEX_HOME"] = codex_home
    environment.setdefault("RUST_LOG", "error")
    process = subprocess.Popen(
        [codex_bin, "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=environment,
        start_new_session=True,
    )
    if process.stdin is None or process.stdout is None:
        terminate_process(process)
        raise RuntimeError("app-server pipes are unavailable")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    try:
        send({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "usage_hud",
                    "title": "Usage HUD",
                    "version": "1.0.0",
                }
            },
        })
        requested = False
        while time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            if not selector.select(timeout=min(0.5, remaining)):
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1 and not requested:
                if "error" in message:
                    raise RuntimeError("app-server initialization failed")
                send({"method": "account/rateLimits/read", "id": 2})
                requested = True
                continue
            if message.get("id") != 2:
                continue
            if "error" in message:
                raise RuntimeError("account/rateLimits/read failed")
            result = message.get("result")
            rate_limits = result.get("rateLimits") if isinstance(result, dict) else None
            if not isinstance(rate_limits, dict):
                raise ValueError("rateLimits is missing")
            primary = normalize_window(rate_limits.get("primary"))
            if primary is None:
                raise ValueError("primary rate-limit window is missing")
            return {
                "rate_limits": {
                    "primary": primary,
                    "secondary": normalize_window(rate_limits.get("secondary")),
                },
                "fetched_at": int(time.time()),
                "source": "api",
            }
        raise TimeoutError("codex app-server did not return rate limits")
    finally:
        selector.close()
        terminate_process(process)
        process.stdin.close()
        process.stdout.close()
