"""
One stuck EPLAN call must not take the whole MCP server with it (EPLAN MCP v4).

Why the server froze (inbox 2026-10-28_eplan_parts_db_blokira_streznik.md):
FastMCP (mcp 1.30) calls a SYNC tool function directly on the asyncio event
loop thread (func_metadata.call_fn_with_arg_validation: `return fn(**args)`).
Every tool here is sync and ends in EplanRemoteClient.ExecuteAction with
SynchronousMode = True, which does not return while EPLAN shows a modal dialog.
So the event loop itself blocked, and with it every other request - including
eplan_ping, eplan_status and eplan_versions, which never touch EPLAN. The four
parts_db_* calls sent in parallel then each re-opened the dialog in turn.

What this module does, for every registered MCP tool:
  1. The sync function runs on its own daemon thread; the event loop only
     awaits it, so it stays free to answer other requests.
  2. Tools that talk to EPLAN take ONE process-wide lock - the same
     serialisation the blocked event loop used to impose, now explicit.
     eplan_status / eplan_versions / eplan_list_extensions / eplan_ping /
     eplan_tools_search / eplan_tools_describe do not take it.
  3. A deadline (EPLAN_MCP_CALL_TIMEOUT_S, default 240 s - below the 300 s
     after which the MCP client gives up) answers the caller with
     EPLAN_CALL_TIMEOUT. The worker cannot be interrupted (it is inside
     native code), so it keeps the lock until EPLAN returns; meanwhile other
     EPLAN tools answer EPLAN_BUSY at once instead of queueing. When the user
     closes the dialog the worker finishes and the server is usable again -
     no MCP restart.

A tool that takes its own `timeout_seconds` / `timeout` gets that plus 30 s
if it is longer than the default, so a deliberately long call is not cut.

EPLAN_MCP_THREADED=0 restores the v3 behaviour (direct call, no lock, no
deadline) - an escape hatch only.
"""

import asyncio
import functools
import json
import os
import threading
import time
import uuid

# Never touch EPLAN (or bound themselves, like ping): no EPLAN lock.
NO_LOCK_TOOLS = frozenset({
    "eplan_status",
    "eplan_versions",
    "eplan_list_extensions",
    "eplan_ping",
    "eplan_tools_search",
    "eplan_tools_describe",
})

DEFAULT_CALL_TIMEOUT_S = 240.0
_OWN_TIMEOUT_MARGIN_S = 30.0
_POLL_S = 0.1

_eplan_lock = threading.Lock()
_state_lock = threading.Lock()
_holder = {}   # {"id", "tool", "since", "timedOut"} while a locked call runs


def threaded_enabled():
    return os.environ.get("EPLAN_MCP_THREADED", "1").strip() != "0"


def call_timeout_s():
    raw = os.environ.get("EPLAN_MCP_CALL_TIMEOUT_S", "")
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_CALL_TIMEOUT_S


def _own_timeout(kwargs):
    """The tool's own timeout argument (also inside eplan_tools_call arguments)."""
    sources = [kwargs]
    inner = kwargs.get("arguments")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            inner = None
    if isinstance(inner, dict):
        sources.append(inner)
    best = None
    for src in sources:
        for key in ("timeout_seconds", "timeout"):
            try:
                value = float(src.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0 and (best is None or value > best):
                best = value
    return best


def effective_timeout(kwargs):
    base = call_timeout_s()
    own = _own_timeout(kwargs)
    if own is not None and own + _OWN_TIMEOUT_MARGIN_S > base:
        return own + _OWN_TIMEOUT_MARGIN_S
    return base


def needs_eplan_lock(tool_name, kwargs):
    """eplan_tools_call inherits the policy of the tool it calls."""
    if tool_name == "eplan_tools_call":
        target = kwargs.get("name")
        if not isinstance(target, str):
            return True
        target = target.strip()
        if not target.startswith("eplan_"):
            target = "eplan_" + target
        return target not in NO_LOCK_TOOLS
    return tool_name not in NO_LOCK_TOOLS


def busy_info():
    """{} when no EPLAN call runs, else what runs, for how long, timed out?"""
    with _state_lock:
        if not _holder:
            return {}
        return {
            "tool": _holder.get("tool"),
            "runningSeconds": round(time.time() - _holder.get("since", time.time()), 1),
            "timedOut": bool(_holder.get("timedOut")),
        }


def _busy_response(tool_name, waited):
    info = busy_info()
    stuck = info.get("timedOut")
    return {
        "success": False,
        "errorType": "EPLAN_BUSY",
        "error": (
            "Not run: EPLAN is still busy with %s (%s s)%s. Nothing was sent to "
            "EPLAN for %s."
            % (info.get("tool") or "another call", info.get("runningSeconds", "?"),
               " - that call already timed out and is blocked inside EPLAN, most "
               "likely by a modal dialog waiting for a click" if stuck else "",
               tool_name)),
        "tool": tool_name,
        "busy": info,
        "waitedSeconds": round(waited, 1),
        "hint": ("Ask the user to look at the EPLAN window and close the dialog. "
                 "The server recovers by itself when EPLAN returns; no MCP restart. "
                 "eplan_ping / eplan_status keep answering meanwhile."
                 if stuck else
                 "Retry when the running call has finished (eplan_status shows it)."),
    }


def _timeout_response(tool_name, timeout):
    return {
        "success": False,
        "errorType": "EPLAN_CALL_TIMEOUT",
        "error": (
            "%s did not finish within %g s. This request is released, but the "
            "call is still running inside EPLAN - most likely EPLAN shows a modal "
            "dialog waiting for a click, or the operation is very long. Its result "
            "is lost; do not assume it did or did not happen." % (tool_name, timeout)),
        "tool": tool_name,
        "timeoutSeconds": timeout,
        "hint": ("Other EPLAN tools answer EPLAN_BUSY until it returns; eplan_ping / "
                 "eplan_status / eplan_versions keep answering. Ask the user to close "
                 "the dialog in EPLAN - no MCP restart is needed. Raise the limit with "
                 "EPLAN_MCP_CALL_TIMEOUT_S or the tool's timeout_seconds."),
    }


def call_label(tool_name, kwargs):
    """'eplan_tools_call -> eplan_parts_db_count' rather than just the meta tool."""
    target = kwargs.get("name") if tool_name == "eplan_tools_call" else None
    if isinstance(target, str) and target.strip():
        return "%s -> %s" % (tool_name, target.strip())
    return tool_name


def _as_tool_output(payload, returns_json_text):
    return json.dumps(payload, indent=2, ensure_ascii=False) if returns_json_text else payload


def _settle(fut, ok, value):
    if fut.done():
        return
    if ok:
        fut.set_result(value)
    else:
        fut.set_exception(value)


def guarded(fn, tool_name, returns_json_text=True):
    """
    Async MCP tool around the sync `fn` (see module docstring).

    The signature, name and docstring are those of `fn` (functools.wraps), so
    the published tool schema is unchanged. `returns_json_text`: every tool in
    this server returns a JSON string, so refusals are returned the same way.
    """
    if not threaded_enabled():
        return fn

    @functools.wraps(fn)
    async def eplan_guarded_tool(*args, **kwargs):
        timeout = effective_timeout(kwargs)
        start = time.monotonic()
        deadline = start + timeout
        locked = needs_eplan_lock(tool_name, kwargs)
        label = call_label(tool_name, kwargs)
        token = uuid.uuid4().hex

        if locked:
            while not _eplan_lock.acquire(blocking=False):
                info = busy_info()
                if info.get("timedOut") or time.monotonic() >= deadline:
                    return _as_tool_output(
                        _busy_response(label, time.monotonic() - start), returns_json_text)
                await asyncio.sleep(_POLL_S)
            with _state_lock:
                _holder.clear()
                _holder.update(id=token, tool=label, since=time.time(), timedOut=False)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def worker():
            ok, value = True, None
            try:
                value = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - handed to the awaiting side
                ok, value = False, exc
            finally:
                if locked:
                    with _state_lock:
                        if _holder.get("id") == token:
                            _holder.clear()
                    _eplan_lock.release()
            try:
                loop.call_soon_threadsafe(_settle, fut, ok, value)
            except RuntimeError:
                pass  # loop already closed (server shutting down)

        try:
            threading.Thread(target=worker, daemon=True,
                             name="eplan-tool-" + tool_name).start()
        except BaseException:
            if locked:
                with _state_lock:
                    if _holder.get("id") == token:
                        _holder.clear()
                _eplan_lock.release()
            raise

        remaining = max(0.0, deadline - time.monotonic())
        try:
            return await asyncio.wait_for(asyncio.shield(fut), remaining)
        except asyncio.TimeoutError:
            if locked:
                with _state_lock:
                    if _holder.get("id") == token:
                        _holder["timedOut"] = True
            return _as_tool_output(_timeout_response(label, timeout), returns_json_text)

    return eplan_guarded_tool
