"""
v4 changes in live_props (inbox 2026-10-28_eplan_window_macro_functionnames_prazno.md,
2026-10-28_eplan_live_props_manjse.md). Offline: assertions on the generated C#
and on the Python post-processing of a simulated result.
"""

import os
import tempfile

import pytest

from api.actions import live_props
from api.actions import live_placements as P, parts_db_reflect as pdb


@pytest.fixture
def capture(monkeypatch):
    seen = {"result": {"success": True, "results": {"success": True}}}

    def fake(script, timeout=30.0):
        seen["cs"] = script
        return seen["result"]

    monkeypatch.setattr(live_props, "_execute_script", fake)
    return seen


@pytest.fixture
def ema():
    path = os.path.join(tempfile.gettempdir(), "v4_test_macro.ema")
    open(path, "w").close()
    return path


# ---------------------------------------------------------------------------
# window macro
# ---------------------------------------------------------------------------

def test_function_names_include_function_subclasses(capture, ema):
    live_props.live_insert_window_macro(page="P", macro_path=ema, x=1, y=2)
    cs = capture["cs"]
    assert 'o.GetType().Name == "Function"' not in cs, "PLC/Terminal were missed by this test"
    assert "if (IsFunctionObject(o))" in cs
    assert "static bool IsFunctionObject(object o)" in cs
    assert "t = t.BaseType;" in cs


def test_duplicates_are_compared_with_functions_before_the_insert(capture, ema):
    live_props.live_insert_window_macro(page="P", macro_path=ema, x=1, y=2)
    cs = capture["cs"]
    assert cs.index("namesBefore[h0]") < cs.index("WindowMacro"), "snapshot before the insert"
    assert "namesBefore.TryGetValue(h, out was)" in cs
    assert 'results["renamedExisting"]' in cs


def test_graphics_are_omitted_by_default(capture, ema):
    live_props.live_insert_window_macro(page="P", macro_path=ema, x=1, y=2)
    cs = capture["cs"]
    assert "bool withGraphics = false;" in cs
    assert 'results["graphicsOmitted"]' in cs
    assert 'results["insertedCount"] = total;' in cs


def test_include_graphics_lists_everything(capture, ema):
    live_props.live_insert_window_macro(page="P", macro_path=ema, x=1, y=2, include_graphics=True)
    assert "bool withGraphics = true;" in capture["cs"]


def test_merge_warning_from_simulated_result(capture, ema):
    capture["result"] = {"success": True, "results": {
        "success": True, "insertedCount": 73, "listedCount": 20,
        "functionNames": ["=CA1+EAA-K1"], "duplicateNames": {"=CA1+EAA-K1": 1},
        "renamedExisting": [{"handle": "94/17/19/0", "nameBefore": "=+-K1",
                             "nameAfter": "=CA1+EAA-K1"}]}}
    out = live_props.live_insert_window_macro(page="P", macro_path=ema, x=1, y=2)
    assert out["success"] is True
    assert "=CA1+EAA-K1" in out["duplicateWarning"]
    assert out["renamedExisting"][0]["nameBefore"] == "=+-K1"


# ---------------------------------------------------------------------------
# read: PLC properties on non-PLC functions
# ---------------------------------------------------------------------------

def test_property_not_found_becomes_not_applicable(capture):
    live_props.live_read_function_properties(contains="K1")
    cs = capture["cs"]
    assert '"PropertyNotFoundException"' in cs
    assert "pv[key] = null;" in cs
    assert 'd["notApplicable"] = notApplicable;' in cs


def test_not_applicable_summary(capture):
    capture["result"] = {"success": True, "results": {
        "success": True, "matched": 2, "functions": [
            {"name": "=+-K1", "properties": {"FUNC_TEXT": "??_??@x;", "FUNC_PLCADDRESS": None},
             "notApplicable": ["FUNC_PLCADDRESS"]},
            {"name": "=CA1+EAA-K1", "properties": {"FUNC_TEXT": "", "FUNC_PLCADDRESS": "I0.0"}}],
        "unknownProperties": []}}
    out = live_props.live_read_function_properties(contains="K1")
    assert out["notApplicableProperties"] == {"FUNC_PLCADDRESS": 1}
    assert out["functions"][0]["displayText"] == {"FUNC_TEXT": "x"}
    assert out["unknownProperties"] == []


# ---------------------------------------------------------------------------
# write: MultiLangString verified by display text
# ---------------------------------------------------------------------------

def _set_result(new, requested="TEST v3"):
    return {"success": True, "results": {"success": True, "matched": 1, "modified": 1, "details": [
        {"name": "=+-K1", "handle": "94/17/19/0", "page": "=CA1+EAA/90", "properties": [
            {"property": "FUNC_TEXT", "previous": "", "requested": requested, "new": new,
             "verified": new == requested, "route": "getter+Set(string)"}]}]}}


def test_multilang_readback_is_verified_by_display_text(capture):
    capture["result"] = _set_result("??_??@TEST v3;")
    out = live_props.live_set_function_properties(handle="94/17/19/0", values={"FUNC_TEXT": "TEST v3"})
    p = out["details"][0]["properties"][0]
    assert p["verified"] is True
    assert p["verifiedBy"] == "displayText"
    assert p["newDisplay"] == "TEST v3"
    assert "warning" not in out


def test_multilang_with_other_text_stays_unverified(capture):
    capture["result"] = _set_result("??_??@TEST v2;")
    out = live_props.live_set_function_properties(handle="94/17/19/0", values={"FUNC_TEXT": "TEST v3"})
    p = out["details"][0]["properties"][0]
    assert p["verified"] is False and "verifiedBy" not in p
    assert "warning" in out


def test_plain_mismatch_stays_unverified(capture):
    capture["result"] = _set_result("I0.1", requested="I0.0")
    out = live_props.live_set_function_properties(handle="h", values={"FUNC_PLCADDRESS": "I0.0"})
    assert out["details"][0]["properties"][0]["verified"] is False
