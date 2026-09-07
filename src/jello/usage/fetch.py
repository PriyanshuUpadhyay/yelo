"""`jello usage fetch`: refresh every provider's API cache on demand.

`usage-hud-fetch` ported into Python. Claude uses Anthropic's OAuth usage endpoint, Codex
the installed CLI's app-server rate-limit method, so neither provider needs a running agent
session to report current limits.

For each Claude profile: read the profile's OAuth access token from the Keychain, GET the
usage endpoint, and on a fully valid response atomically rewrite that profile's
`.usage-api-cache.json` (and the Fable weekly window into `.usage-api-cache-fable.json`
when the payload exposes it). 401 or 403 reports `auth-stale`; the person signs in
through the named Claude launcher. Reading usage never starts an agent, runs hooks,
or loads MCP integrations. Exit 0 unless every account failed.

The token NEVER leaves memory (law L1): it lives in one local in `fetch_claude`, goes into
the request header, and is never formatted into a message, a log line, an exception, or a
file. The request refuses every redirect, so the `Authorization` header can never be
replayed at an origin the response chose, the way the reference's `curl` never followed
one; and everything a trace prints goes through `redact`, so a body that echoes the bearer
value back cannot ride out on the trace. USAGE_HUD_FETCH_DEBUG traces stages without token
bytes or bodies; USAGE_HUD_FETCH_DUMP prints the parsed usage payload only.

CLAUDE_KEYCHAIN_SERVICE and CODEX_BIN retain their override names. JELLO_USAGE_API_URL redirects the endpoint for tests only.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from ..profile import core
from . import codex

API_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
REQUEST_TIMEOUT_SECONDS = 20
API_CACHE = ".usage-api-cache.json"
API_FABLE_CACHE = ".usage-api-cache-fable.json"
CODEX_CACHE = ".usage-hud-api-cache.json"
PERCENTAGE_KEYS = ("used_percentage", "utilization", "used_percent", "percent")
RESET_KEYS = ("resets_at", "reset_at", "resets")


def debug(message):
    """Stage trace under USAGE_HUD_FETCH_DEBUG. Stage names, the Keychain service and
    account (paths, not secrets), HTTP codes, and the derived window list only -- NEVER
    token bytes or a response body."""
    if os.environ.get("USAGE_HUD_FETCH_DEBUG"):
        print(f"[dbg] {message}", file=sys.stderr)


def api_url():
    return os.environ.get("JELLO_USAGE_API_URL") or API_URL


def codex_bin():
    return shutil.which(os.environ.get("CODEX_BIN") or "codex")


# --- read-only answers the rest of the group asks for ----------------------------------

def profiles():
    """(name, cache dir, identity dir) per claude profile, in census order."""
    return [(row["name"], row["dir"], os.path.join(core.HOME, ".claude-" + row["name"]))
            for row in core.claude_rows()]


def capabilities():
    """The providers a fetch can actually refresh, so a row only promises what would move.
    Claude always; Codex only with a codex binary to talk to."""
    return ("claude", "codex") if codex_bin() else ("claude",)


def check_credentials():
    """(name, dir, ok|missing) per profile. The token is read to prove it resolves, then
    dropped -- never printed."""
    return [(name, directory, "ok" if read_keychain_token(name) else "missing")
            for name, directory, _ in profiles()]


# --- Keychain --------------------------------------------------------------------------

def read_keychain_token(name):
    """The profile's OAuth access token, or None. Claude Code stores the blob as a
    generic-password whose value is {"claudeAiOauth":{"accessToken": ...}}; the service
    keys the identity path, so a rename can never hand one account another's credentials."""
    try:
        service, user = core.keychain_service(name)
    except OSError:
        return None
    service = os.environ.get("CLAUDE_KEYCHAIN_SERVICE") or service
    command = [core.security_bin(), "find-generic-password", "-s", service, "-a", user, "-w"]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=core.KEYCHAIN_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        debug(f"{name}: keychain probe failed svc='{service}' acct='{user}'")
        return None
    if result.returncode != 0 or not result.stdout.strip():
        debug(f"{name}: keychain miss svc='{service}' acct='{user}'")
        return None
    try:
        blob = json.loads(result.stdout)
    except ValueError:
        blob = None
    token = None
    if isinstance(blob, dict):
        oauth = blob.get("claudeAiOauth")
        if isinstance(oauth, dict):
            token = oauth.get("accessToken")
        if not token:
            token = blob.get("accessToken")
    if isinstance(token, str) and token:
        debug(f"{name}: token found svc='{service}' acct='{user}'")
        return token
    debug(f"{name}: item found but no accessToken svc='{service}' acct='{user}'")
    return None


# --- the usage request -----------------------------------------------------------------

def redact(text, secret):
    """Everything a trace prints goes through here. A 200 body can quote the bearer value
    back at us -- an echo endpoint, an error that repeats the header it rejected -- and a
    trace must not be the thing that writes it to a terminal or a launchd log."""
    if secret and isinstance(text, str):
        return text.replace(secret, "<token>")
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 30x, same origin included. urllib's default handler replays the whole
    header set -- `Authorization` with it -- at whatever origin the response names, and the
    reference's `curl` never followed a redirect at all. Returning None here leaves the
    30x to the default error handler, so the caller reads it as an ordinary status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_usage(token):
    """(status, body). A 4xx, 5xx, or 30x is an answer, not an exception, so the caller can
    act on the code; a network failure or timeout is (None, None)."""
    request = urllib.request.Request(api_url(), headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
    })
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def number(value):
    """jq reads a number or nothing: `1e999` and `NaN` parse as floats in Python but are
    not values jq would carry into a window, so they are read as absent here too."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def probe(window, keys):
    for key in keys:
        value = window.get(key)
        if value is not None and value is not False:
            return value
    return None


def percentage(window):
    return number(probe(window, PERCENTAGE_KEYS)) if isinstance(window, dict) else None


def epoch(window):
    """resets_at arrives as epoch seconds or as ISO-8601 with fractional seconds and a
    `+00:00` offset, which the reference's fromdateiso8601 rejects -- strip the fraction
    and normalize the zero offset first."""
    if not isinstance(window, dict):
        return None
    value = probe(window, RESET_KEYS)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value
    fraction = text.find(".")
    if fraction >= 0:
        end = fraction + 1
        while end < len(text) and text[end].isdigit():
            end += 1
        text = text[:fraction] + text[end:]
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    try:
        parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        return int(parsed.replace(tzinfo=datetime.timezone.utc).timestamp())
    except ValueError:
        pass
    numeric = number(text)
    return None if numeric is None else int(numeric)


def fable_window(payload):
    """Fable's weekly limit is not a top-level window: it is the limits[] entry with
    kind == weekly_scoped scoped to the Fable model (confirmed live 2026-07-23). The
    top-level probes stay as a fallback."""
    direct = probe(payload, ("fable", "seven_day_fable", "fable_seven_day", "fable_week"))
    if direct is not None:
        return direct
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return None
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if (name or "") == "Fable":
            return entry
    return None


def cache_document(five, seven, ts):
    document = {
        "five_hour": {"used_percentage": percentage(five), "resets_at": epoch(five)},
        "ts": ts, "fetched_at": ts, "source": "api",
    }
    if seven is not None:
        document["seven_day"] = {"used_percentage": percentage(seven), "resets_at": epoch(seven)}
    return document


def map_response(payload, ts):
    """(base cache, fable cache, window names). A window is valid only when its percentage
    is numeric. seven_day rides along only when the payload carries it -- some accounts
    have no overall weekly limit yet still expose 5h plus a Fable weekly, and the snapshot
    is per-window, so the absent 7d row says so instead of reading zero."""
    if not isinstance(payload, dict):
        return None, None, []
    five = probe(payload, ("five_hour", "fiveHour", "five_hour_window"))
    seven = probe(payload, ("seven_day", "sevenDay", "seven_day_window"))
    fable = fable_window(payload)
    windows = [name for name, window in (("five_hour", five), ("seven_day", seven),
                                         ("fable", fable))
               if isinstance(window, dict) and percentage(window) is not None]
    if "five_hour" not in windows:
        return None, None, windows
    base = cache_document(five, seven if "seven_day" in windows else None, ts)
    # The fable cache carries the SHARED 5h as well, so a fresher fable cache can never
    # override the 5h window with a hole; its seven_day is the Fable weekly.
    fable_cache = None
    if "fable" in windows:
        fable_cache = {
            "five_hour": {"used_percentage": percentage(five), "resets_at": epoch(five)},
            "seven_day": {"used_percentage": percentage(fable), "resets_at": epoch(fable)},
            "ts": ts, "fetched_at": ts, "source": "api",
        }
    return base, fable_cache, windows


# --- writes ----------------------------------------------------------------------------

def write_cache(path, document):
    """Temp file in the target directory plus os.replace, so a reader never sees half a
    cache and a concurrent statusline write cannot be interleaved with (law L5)."""
    directory = os.path.dirname(path)
    handle = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=os.path.basename(path) + ".tmp.",
            delete=False,
        )
        with handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
        return True
    except (OSError, ValueError, TypeError):
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        return False


# --- one account -----------------------------------------------------------------------

def fetch_claude(name, directory, identity):
    """(status word, succeeded). `fable-write-failed` counts as a success: the base cache
    already landed, so the run is not a failure, but the status line says the fable cache
    stayed stale rather than reporting a clean `ok`."""
    token = read_keychain_token(name)
    if token is None:
        return "fetch-failed", False
    # The token that authorized the response now in hand, kept only so a trace can scrub it
    # out of the body it prints.
    secret = token
    code, body = request_usage(token)
    if code is None:
        debug(f"{name}: request failed (network/timeout)")
        return "fetch-failed", False
    debug(f"{name}: http {code}")
    del token
    if code in (401, 403):
        return "auth-stale", False
    if code != 200:
        return "fetch-failed", False
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if os.environ.get("USAGE_HUD_FETCH_DUMP"):
        # Usage stats only, never credentials: a body that quotes the bearer value back is
        # still a body, so it is redacted on the way out.
        dumped = json.dumps(payload, separators=(",", ":")) if payload is not None else "unparseable"
        print(f"[dump] {name}: {redact(dumped, secret)}", file=sys.stderr)
    del secret
    ts = int(time.time())
    base, fable, windows = map_response(payload, ts)
    debug(f"{name}: windows found = {','.join(windows) or 'none'}")
    if base is None:
        debug(f"{name}: no valid base (needs numeric five_hour)")
        return "fetch-failed", False
    if not write_cache(os.path.join(directory, API_CACHE), base):
        return "fetch-failed", False
    if fable is not None and not write_cache(os.path.join(directory, API_FABLE_CACHE), fable):
        return "fable-write-failed", True
    return "ok", True


def fetch_codex(label, directory):
    binary = codex_bin()
    if binary is None:
        return "fetch-failed", False
    try:
        payload = codex.fetch_rate_limits(binary, directory, codex.timeout_seconds())
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        debug(f"{label}: codex fetch failed ({error})")
        return "fetch-failed", False
    if not write_cache(os.path.join(directory, CODEX_CACHE), payload):
        return "fetch-failed", False
    return "ok", True


def contained(name, function, arguments):
    """One outcome line per discovered profile is the contract, so an unexpected exception
    in one account's job may not take every other account's line with it. The class name
    only: an exception's message can carry a response body."""
    try:
        return function(*arguments)
    except Exception as error:  # noqa: BLE001 - containment is the point
        debug(f"{name}: unexpected {type(error).__name__}")
        return "fetch-failed", False


def run(args):
    """Every account concurrently, one status line each in census order, exit 0 unless
    every account failed."""
    del args
    jobs = [(core.hud_label("claude", {"name": name}), fetch_claude, (name, directory, identity))
            for name, directory, identity in profiles()]
    jobs += [(core.hud_label("codex", row), fetch_codex, (core.hud_label("codex", row), row["dir"]))
             for row in core.codex_rows()]
    if not jobs:
        return 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = [pool.submit(contained, name, function, arguments)
                   for name, function, arguments in jobs]
        outcomes = [result.result() for result in results]
    any_ok = False
    for (name, _, _), (word, succeeded) in zip(jobs, outcomes):
        print(f"{name}: {word}")
        any_ok = any_ok or succeeded
    return 0 if any_ok else 1
