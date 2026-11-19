"""
Offline tests for api/actions/live_props.py (EPLAN MCP v3):
live_read_function_properties, live_set_function_properties,
live_insert_window_macro.

_execute_script is stubbed, so these assert on the GENERATED C# - a compile
error in EPLAN is only ever seen as a timeout, so the C# is the thing to test.
"""

import os
import re

import pytest

from api.actions import live_props
import api.actions as actions
from cs_json import prepare_script


@pytest.fixture
def capture(monkeypatch):
    seen = {}

    def fake(script, timeout=30.0):
        seen["cs"] = script
        seen["timeout"] = timeout
        return {"success": True, "results": {"success": True, "functions": [], "matched": 0}}

    monkeypatch.setattr(live_props, "_execute_script", fake)
    return seen


@pytest.fixture
def ema(tmp_path):
    p = tmp_path / "6ES7131-6BF01-0BA0.ema"
    p.write_text("<EplanPxfRoot/>", encoding="utf-8")
    return str(p)


def _code(cs):
    """Drop // comments so prose is not mistaken for code."""
    return "\n".join(line.split("//")[0] for line in cs.splitlines())


def _csharp5(cs):
    code = _code(cs)
    assert "?." not in code
    assert "?[" not in code
    assert '$"' not in code
    assert "nameof(" not in code
    assert not re.search(r"new Dictionary<[^>]*>\s*\{\s*\[", code)


def _no_compile_time_object_model(cs):
    for ns in ("Eplan.EplApi.DataModel", "Eplan.EplApi.HEServices", "Eplan.EplApi.MasterData"):
        assert "using %s;" % ns not in cs, ns


def _balanced(cs):
    code = _code(cs)
    stripped = re.sub(r'@"[^"]*"', '""', code)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
    stripped = re.sub(r"'(?:\\.|[^'\\])'", "''", stripped)
    assert stripped.count("{") == stripped.count("}")
    assert stripped.count("(") == stripped.count(")")


TOKENS = ("SELNAME", "SELCONTAINS", "SELPAGE", "SELCONN", "SELHANDLE", "SELCATEGORY",
          "PNAMES", "PIDX", "SKIP", "TAKE", "WITHART", "WNAMES", "WIDX", "WVALS",
          "ARTFROM", "ARTTO", "MAXN", "PAGENAME", "MACROPATH", "POSX", "POSY",
          "VARIANT", "MOVEKIND")


def _filled(cs):
    for t in TOKENS:
        assert not re.search(r"\b%s\b" % t, cs), "unfilled token " + t


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_new_tools_are_registered():
    for n in ("live_read_function_properties", "live_set_function_properties",
              "live_insert_window_macro"):
        assert n in actions.__all__
        assert callable(getattr(actions, n))


# ---------------------------------------------------------------------------
# M-1 read
# ---------------------------------------------------------------------------

def test_read_defaults(capture):
    out = live_props.live_read_function_properties(name="=AB-C+DI-510K3")
    assert out["success"] is True
    cs = capture["cs"]
    for p in live_props.DEFAULT_READ_PROPERTIES:
        assert '"%s"' % p in cs
    assert '"=AB-C+DI-510K3"' in cs
    assert capture["timeout"] == 120.0
    _csharp5(cs)
    _no_compile_time_object_model(cs)
    _balanced(cs)
    _filled(cs)


def test_read_selector_and_index(capture):
    live_props.live_read_function_properties(
        contains="510K3", page="=AB-C+DI/200", connection_designation="9",
        category_contains="PLC", properties=["FUNC_PLCADDRESS", "FUNC_CONNECTIONDESIGNATION[2]"],
        offset=10, limit=5)
    cs = capture["cs"]
    assert 'FnMatches(fn, "", "510K3", "=AB-C+DI/200", "9", "", "PLC")' in cs
    assert 'string[] pNames = new string[] { "FUNC_PLCADDRESS", "FUNC_CONNECTIONDESIGNATION" };' in cs
    assert "int[] pIdx = new int[] { 0, 2 };" in cs
    assert "if (matched <= 10) continue;" in cs
    assert "if (items.Count < 5)" in cs


def test_read_is_read_only(capture):
    live_props.live_read_function_properties(name="X")
    code = _code(capture["cs"])
    run = code.split("public void Run()")[1]
    for w in ("SetLiveProp(", "SetValue(", "GuardScratch(", ".Set("):
        assert w not in run, w


@pytest.mark.parametrize("props", [["FUNC X"], ["FUNC_X[0]"], ["FUNC_X]"], "FUNC_TEXT", [1]])
def test_read_rejects_bad_property_names(capture, props):
    out = live_props.live_read_function_properties(name="X", properties=props)
    assert out["success"] is False
    assert "cs" not in capture


@pytest.mark.parametrize("kw", [{"offset": -1}, {"limit": 0}, {"limit": "5; x"}, {"limit": 5000}])
def test_read_rejects_bad_paging(capture, kw):
    out = live_props.live_read_function_properties(name="X", **kw)
    assert out["success"] is False
    assert "cs" not in capture


def test_read_escapes_selector(capture):
    live_props.live_read_function_properties(name='A"); evil(); ("\\')
    cs = capture["cs"]
    assert 'A\\"); evil(); (\\"\\\\' in cs
    _balanced(cs)


def test_read_rejects_result_token(capture):
    out = live_props.live_read_function_properties(name="x{{RESULT_PATH}}")
    assert out["success"] is False


def test_read_converts_to_portable_json_on_29(capture):
    live_props.live_read_function_properties(name="X")
    out = prepare_script(capture["cs"], "2.9")
    assert "Newtonsoft" not in out
    assert "_McpJson(results)" in out


def test_read_adds_display_text(monkeypatch):
    def fake(script, timeout=30.0):
        return {"success": True, "results": {"success": True, "matched": 1, "functions": [
            {"name": "-K1", "properties": {"FUNC_TEXT": "??_??@Ventil 1;", "FUNC_PLCADDRESS": "I1.0"}}]}}
    monkeypatch.setattr(live_props, "_execute_script", fake)
    out = live_props.live_read_function_properties(name="-K1")
    fn = out["functions"][0]
    assert fn["displayText"] == {"FUNC_TEXT": "Ventil 1"}
    assert fn["properties"]["FUNC_PLCADDRESS"] == "I1.0"


def test_read_failure_is_not_success(monkeypatch):
    monkeypatch.setattr(live_props, "_execute_script", lambda s, timeout=30.0: {
        "success": True, "results": {"success": False, "error": "No project is currently open"}})
    out = live_props.live_read_function_properties(name="X")
    assert out["success"] is False
    assert "No project" in out["error"]


@pytest.mark.parametrize("raw,display", [
    ("??_??@Ventil;", "Ventil"),
    ("de_DE@Ventil;en_US@Valve;", "Ventil"),
    ("I1.0", None),
    ("mail@x", None),
])
def test_multilang_display(raw, display):
    assert live_props.multilang_display(raw)[0] == display


# ---------------------------------------------------------------------------
# M-6 write
# ---------------------------------------------------------------------------

def test_set_requires_target_and_values(capture):
    assert live_props.live_set_function_properties(values={"FUNC_TEXT": "x"})["success"] is False
    assert live_props.live_set_function_properties(name="-K1")["success"] is False
    assert live_props.live_set_function_properties(name="-K1", values={"FUNC_TEXT": None})["success"] is False
    assert live_props.live_set_function_properties(name="-K1", values="FUNC_TEXT=x")["success"] is False
    assert "cs" not in capture


def test_set_is_scratch_guarded_by_default(capture):
    live_props.live_set_function_properties(handle="h1", values={"FUNC_PLCADDRESS": "I1.0"})
    assert "GuardScratch(project, false," in capture["cs"]


def test_set_allow_real_project(capture):
    live_props.live_set_function_properties(handle="h1", values={"FUNC_PLCADDRESS": "I1.0"},
                                            allow_real_project=True)
    assert "GuardScratch(project, true," in capture["cs"]


def test_set_validates_before_first_write(capture):
    live_props.live_set_function_properties(
        name="=AB-C+DI-510K3", connection_designation="9",
        values={"FUNC_PLCADDRESS": "I1.0", "FUNC_CONNECTIONDESIGNATION[1]": "DI0"})
    code = _code(capture["cs"]).split("public void Run()")[1]
    v = code.index("is not declared by")
    assert v < code.index("SetLiveProp(props")
    assert v < code.index("Call(set, pv")
    assert code.index("functions match, limit is 1 - nothing written") < v
    cs = capture["cs"]
    assert 'string[] wNames = new string[] { "FUNC_PLCADDRESS", "FUNC_CONNECTIONDESIGNATION" };' in cs
    assert "int[] wIdx = new int[] { 0, 1 };" in cs
    assert 'string[] wVals = new string[] { "I1.0", "DI0" };' in cs
    assert 'FnMatches(fn, "=AB-C+DI-510K3", "", "", "9", "", "")' in cs
    _csharp5(cs)
    _no_compile_time_object_model(cs)
    _balanced(cs)
    _filled(cs)


def test_set_reads_back_and_reports_previous(capture):
    live_props.live_set_function_properties(handle="h", values={"FUNC_TEXT": "x"})
    cs = capture["cs"]
    assert 'pd["previous"] = before;' in cs
    assert 'pd["new"] = after;' in cs
    assert 'pd["verified"] = after == wVals[i];' in cs


def test_set_escapes_values(capture):
    live_props.live_set_function_properties(handle="h", values={"FUNC_TEXT": 'a"b\\c'})
    assert '"a\\"b\\\\c"' in capture["cs"]
    _balanced(capture["cs"])


def test_set_numbers_are_written_as_text(capture):
    live_props.live_set_function_properties(handle="h", values={"FUNC_PLCADDRESS": 12})
    assert 'new string[] { "12" }' in capture["cs"]


def test_set_replace_article(capture):
    live_props.live_set_function_properties(
        handle="h", replace_article={"from": "SIE.6ES7155-6BA00-0CN0", "to": "SIE.6ES7155-6BA01-0CN0"})
    cs = capture["cs"]
    assert 'string artFrom = "SIE.6ES7155-6BA00-0CN0";' in cs
    assert 'string artTo = "SIE.6ES7155-6BA01-0CN0";' in cs
    assert "StoreToObject" in cs
    _balanced(cs)


@pytest.mark.parametrize("ra", [{"from": "A"}, {"from": "A", "to": ""}, ["A", "B"],
                                {"from": "A", "to": "B", "x": 1}])
def test_set_replace_article_validation(capture, ra):
    assert live_props.live_set_function_properties(handle="h", replace_article=ra)["success"] is False
    assert "cs" not in capture


def test_set_failure_keeps_details(monkeypatch):
    monkeypatch.setattr(live_props, "_execute_script", lambda s, timeout=30.0: {
        "success": True, "results": {"success": False, "error": "boom",
                                     "details": [{"name": "-K1", "properties": [{"previous": "I0.0"}]}]}})
    out = live_props.live_set_function_properties(handle="h", values={"FUNC_PLCADDRESS": "I1.0"})
    assert out["success"] is False
    assert out["details"][0]["properties"][0]["previous"] == "I0.0"


def test_set_warns_on_unverified(monkeypatch):
    monkeypatch.setattr(live_props, "_execute_script", lambda s, timeout=30.0: {
        "success": True, "results": {"success": True, "details": [
            {"name": "-K1", "properties": [{"property": "FUNC_TEXT", "verified": False}]}]}})
    out = live_props.live_set_function_properties(handle="h", values={"FUNC_TEXT": "x"})
    assert out["success"] is True
    assert "FUNC_TEXT" in out["warning"]


# ---------------------------------------------------------------------------
# M-5 window macro
# ---------------------------------------------------------------------------

def test_macro_script(capture, ema):
    out = live_props.live_insert_window_macro(page="=CA1+EAA/90", macro_path=ema, x=100, y=200.5)
    cs = capture["cs"]
    assert '"String", "Int32", "Page", "PointD", "MoveKind"' in cs
    assert 'Enum.Parse(ps[4].ParameterType, "Absolute", true)' in cs
    assert "MakePoint(ps[3].ParameterType, 100.0, 200.5)" in cs
    assert "new object[] { " in cs and ", 0, page, pt, mk }" in cs
    assert "GuardScratch(project, false," in cs
    assert ema.replace("\\", "\\\\") in cs
    _csharp5(cs)
    _no_compile_time_object_model(cs)
    _balanced(cs)
    _filled(cs)
    # stub returned no insertedCount -> warning, not silent success
    assert "warning" in out


def test_macro_relative_variant(capture, ema):
    live_props.live_insert_window_macro(page="P", macro_path=ema, x=0, y=0, variant=3,
                                        move_kind="relative", allow_real_project=True)
    cs = capture["cs"]
    assert '"Relative"' in cs
    assert ", 3, page, pt, mk }" in cs
    assert "GuardScratch(project, true," in cs


@pytest.mark.parametrize("kw", [
    {"macro_path": "C:/nope/missing.ema"},
    {"macro_path": "relative.ema"},
    {"variant": 16},
    {"variant": -1},
    {"x": "1; evil"},
    {"y": float("nan")},
    {"move_kind": "diagonal"},
    {"page": ""},
])
def test_macro_rejects(capture, ema, kw):
    args = {"page": "P", "macro_path": ema, "x": 0, "y": 0}
    args.update(kw)
    out = live_props.live_insert_window_macro(**args)
    assert out["success"] is False
    assert "cs" not in capture


def test_macro_rejects_non_ema(capture, tmp_path):
    p = tmp_path / "x.emp"
    p.write_text("x")
    assert live_props.live_insert_window_macro(page="P", macro_path=str(p), x=0, y=0)["success"] is False
    assert "cs" not in capture


def test_macro_duplicate_warning(monkeypatch, ema):
    monkeypatch.setattr(live_props, "_execute_script", lambda s, timeout=30.0: {
        "success": True, "results": {"success": True, "insertedCount": 3,
                                     "duplicateNames": {"=AB-C+WAGO-510K3": 2}}})
    out = live_props.live_insert_window_macro(page="P", macro_path=ema, x=0, y=0)
    assert out["success"] is True
    assert "510K3" in out["duplicateWarning"]
    assert "warning" not in out


def test_all_three_compile_shapes_portable(capture, ema):
    for call in (lambda: live_props.live_read_function_properties(name="X"),
                 lambda: live_props.live_set_function_properties(handle="h", values={"FUNC_TEXT": "x"}),
                 lambda: live_props.live_insert_window_macro(page="P", macro_path=ema, x=0, y=0)):
        call()
        out = prepare_script(capture["cs"], "2.9")
        assert "Newtonsoft" not in out
