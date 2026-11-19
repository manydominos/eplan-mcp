"""v4: EPLAN_BUSY / EPLAN_CALL_TIMEOUT / eplan_status name the tool behind eplan_tools_call."""

import asyncio
import json
import threading
import time

import call_guard


def test_label_names_the_target_of_tools_call():
    assert call_guard.call_label("eplan_tools_call", {"name": "eplan_parts_db_count"}) == \
        "eplan_tools_call -> eplan_parts_db_count"
    assert call_guard.call_label("eplan_tools_call", {}) == "eplan_tools_call"
    assert call_guard.call_label("eplan_live_read_page", {"name": "x"}) == "eplan_live_read_page"


def test_timeout_and_busy_carry_the_label(monkeypatch):
    monkeypatch.setenv("EPLAN_MCP_CALL_TIMEOUT_S", "0.3")
    release = threading.Event()

    def meta(name, arguments=None) -> str:
        release.wait(5)
        return "{}"

    g = call_guard.guarded(meta, "eplan_tools_call")

    async def scenario():
        out = json.loads(await g(name="eplan_parts_db_count"))
        assert out["errorType"] == "EPLAN_CALL_TIMEOUT"
        assert out["tool"] == "eplan_tools_call -> eplan_parts_db_count"
        busy = json.loads(await g(name="eplan_parts_db_query"))
        assert busy["errorType"] == "EPLAN_BUSY"
        assert busy["busy"]["tool"] == "eplan_tools_call -> eplan_parts_db_count"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    deadline = time.time() + 5
    while call_guard.busy_info() and time.time() < deadline:
        time.sleep(0.05)
    assert call_guard.busy_info() == {}
