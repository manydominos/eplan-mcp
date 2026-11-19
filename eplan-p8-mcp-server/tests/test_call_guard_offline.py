"""
v4: one stuck EPLAN call must not freeze the MCP server.

Field case 2026-10-28: a parts_db_* call blocked inside EPLAN (modal dialog);
because FastMCP runs a sync tool directly on the event loop, eplan_ping,
eplan_status and eplan_versions stopped answering too. Offline: no EPLAN.
"""

import asyncio
import inspect
import json
import threading
import time

import pytest

import call_guard


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_CALL_TIMEOUT_S", "0.5")
    monkeypatch.delenv("EPLAN_MCP_THREADED", raising=False)
    yield
    # never leave a held lock behind for the next test
    deadline = time.time() + 5
    while call_guard.busy_info() and time.time() < deadline:
        time.sleep(0.05)


def test_wrapper_is_async_and_keeps_signature_and_doc():
    def tool(a: int, b: str = "x") -> str:
        """Doc of the tool."""
        return json.dumps({"a": a, "b": b})

    g = call_guard.guarded(tool, "eplan_x")
    assert inspect.iscoroutinefunction(g)
    assert str(inspect.signature(g)) == str(inspect.signature(tool))
    assert g.__doc__ == "Doc of the tool." and g.__name__ == "tool"
    assert json.loads(_run(g(1, b="y"))) == {"a": 1, "b": "y"}


def test_exceptions_reach_the_caller():
    def boom() -> str:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        _run(call_guard.guarded(boom, "eplan_boom")())


def test_stuck_call_times_out_and_other_tools_still_answer():
    release = threading.Event()

    def stuck() -> str:
        release.wait(10)
        return "{}"

    def status() -> str:
        return json.dumps({"ok": True, "busy": call_guard.busy_info()})

    def other() -> str:
        return json.dumps({"ran": True})

    g_stuck = call_guard.guarded(stuck, "eplan_parts_db_count")
    g_status = call_guard.guarded(status, "eplan_status")
    g_other = call_guard.guarded(other, "eplan_export_pdf_project")

    async def scenario():
        t0 = time.monotonic()
        first = asyncio.ensure_future(g_stuck())
        await asyncio.sleep(0.1)
        st = json.loads(await g_status())          # answers while the stuck call runs
        assert st["ok"] and st["busy"]["tool"] == "eplan_parts_db_count"
        res = json.loads(await first)
        assert res["errorType"] == "EPLAN_CALL_TIMEOUT"
        assert time.monotonic() - t0 < 3
        assert call_guard.busy_info()["timedOut"] is True
        t1 = time.monotonic()
        busy = json.loads(await g_other())         # refused at once, not queued
        assert busy["errorType"] == "EPLAN_BUSY"
        assert busy["busy"]["tool"] == "eplan_parts_db_count"
        assert time.monotonic() - t1 < 0.5
        st2 = json.loads(await g_status())
        assert st2["ok"]

    try:
        _run(scenario())
    finally:
        release.set()
    deadline = time.time() + 5
    while call_guard.busy_info() and time.time() < deadline:
        time.sleep(0.05)
    assert call_guard.busy_info() == {}, "lock released once EPLAN returns"
    # and the server is usable again without a restart
    assert json.loads(_run(g_other())) == {"ran": True}


def test_parallel_eplan_calls_are_serialised():
    active, peak = [0], [0]
    lock = threading.Lock()

    def work() -> str:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return "{}"

    g = call_guard.guarded(work, "eplan_live_read_page")

    async def scenario():
        await asyncio.gather(*[g() for _ in range(5)])

    _run(scenario())
    assert peak[0] == 1


def test_no_lock_tools_run_concurrently_with_an_eplan_call():
    release = threading.Event()
    g_hold = call_guard.guarded(lambda: (release.wait(5), "{}")[1], "eplan_parts_db_query")
    names = ["eplan_ping", "eplan_versions", "eplan_tools_search", "eplan_tools_describe",
             "eplan_list_extensions", "eplan_status"]

    async def scenario():
        first = asyncio.ensure_future(g_hold())
        await asyncio.sleep(0.05)
        for n in names:
            out = await call_guard.guarded(lambda: '{"ok": true}', n)()
            assert json.loads(out) == {"ok": True}, n
        release.set()
        await first

    _run(scenario())


def test_tools_call_inherits_the_policy_of_its_target():
    assert call_guard.needs_eplan_lock("eplan_tools_call", {"name": "eplan_status"}) is False
    assert call_guard.needs_eplan_lock("eplan_tools_call", {"name": "ping"}) is False
    assert call_guard.needs_eplan_lock("eplan_tools_call", {"name": "eplan_parts_db_count"}) is True
    assert call_guard.needs_eplan_lock("eplan_tools_call", {}) is True


def test_own_timeout_argument_extends_the_deadline(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_CALL_TIMEOUT_S", "240")
    assert call_guard.effective_timeout({}) == 240
    assert call_guard.effective_timeout({"timeout_seconds": 600}) == 630
    assert call_guard.effective_timeout({"timeout_seconds": 60}) == 240
    assert call_guard.effective_timeout(
        {"name": "x", "arguments": {"timeout_seconds": 900}}) == 930
    assert call_guard.effective_timeout(
        {"name": "x", "arguments": '{"timeout_seconds": 300}'}) == 330


def test_default_timeout_is_below_the_client_limit(monkeypatch):
    monkeypatch.delenv("EPLAN_MCP_CALL_TIMEOUT_S", raising=False)
    assert call_guard.call_timeout_s() == 240
    monkeypatch.setenv("EPLAN_MCP_CALL_TIMEOUT_S", "abc")
    assert call_guard.call_timeout_s() == 240


def test_escape_hatch_restores_the_plain_function(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_THREADED", "0")

    def f() -> str:
        return "{}"

    assert call_guard.guarded(f, "eplan_x") is f


def test_server_registers_every_tool_async_with_unchanged_schema():
    """The published tool list (names, schemas, descriptions) is what v3 had."""
    import server
    tools = server.mcp._tool_manager._tools
    assert tools, "no tools registered"
    for name, tool in tools.items():
        assert tool.is_async, name
    st = json.loads(server.eplan_status())
    assert "connected" in st
