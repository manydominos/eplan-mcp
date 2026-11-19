"""
v9 (inbox 2026-10-30_eplan_skrij_property_placement.md,
2026-10-30_eplan_place_function_property_relativna_lega.md,
2026-11-02_eplan_place_function_property_above_top_left_absolutna_v_relativno.md,
2026-10-30_eplan_move_placements_dvojni_premik_napisov_skatle.md):
live_hide_function_property, the Location frame of live_place_function_property
and live_move_placements without a second move of relative texts.
Offline: assertions on the generated C# and on the Python side.
"""

import inspect
import json
import re

import pytest

from api.actions import schematic as S, live_props as L
import api.actions as A


def _code(cs):
    return "\n".join(line.split("//")[0] for line in cs.splitlines())


def _csharp5(cs):
    code = _code(cs)
    assert "?." not in code and '$"' not in code and "nameof(" not in code and "out var " not in code


def _balanced(cs):
    code = _code(cs)
    stripped = re.sub(r'@"[^"]*"', '""', code)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
    stripped = re.sub(r"'(?:\\.|[^'\\])'", "''", stripped)
    assert stripped.count("{") == stripped.count("}")
    assert stripped.count("(") == stripped.count(")")


TOKENS = ("V9HANDLES", "V9PAGE", "V9PNAMES", "V9PIDX", "V9PNUM", "V9ACTION", "V9REL", "V9RELIDX", "V9LOCAL",
          "V9DRY", "V9ROLLBACK", "V9VERBOSE", "V9FRAME", "V9ALLOWOUT")


def _filled(cs):
    for t in TOKENS:
        assert not re.search(r"\b%s\b" % t, cs), "unfilled token " + t


@pytest.fixture
def capture(monkeypatch):
    seen = {"scripts": [], "result": {"success": True, "results": {"success": True}}}

    def fake(script, timeout=30.0):
        seen["scripts"].append(script)
        seen["cs"] = script
        seen["timeout"] = timeout
        return seen["result"]

    monkeypatch.setattr(S, "_execute_script", fake)
    monkeypatch.setattr(L, "_execute_script", fake)
    return seen


def _var(cs, name):
    m = re.search(r"\b%s = ([^;]+);" % re.escape(name), cs)
    assert m, name
    return m.group(1).strip()


def _body(capture):
    return capture["cs"].split("[Start]")[1]


# ---------------------------------------------------------------------------
# 1  live_hide_function_property
# ---------------------------------------------------------------------------

def test_hide_exported_and_described():
    assert "live_hide_function_property" in A.__all__
    assert A.live_hide_function_property is L.live_hide_function_property
    assert "live_hide_function_property" in L.__all__
    doc = L.live_hide_function_property.__doc__
    for word in ("property placement", "ContextPropertyPlacement", "22002", "docked", "dry_run", "allow_real_project",
                 "IsSetAsVisible", "SwitchLocalPropertyPlacements", "live_remove_placements", "othersUnchanged",
                 "symbolKept", "ROLLED_BACK", "include_property_placements", "remove"):
        assert word in doc, word
    sig = inspect.signature(L.live_hide_function_property)
    assert sig.parameters["dry_run"].default is True
    assert sig.parameters["allow_real_project"].default is False
    assert sig.parameters["rollback_on_error"].default is True
    assert sig.parameters["action"].default == "hide"


def test_hide_dry_run_script(capture):
    L.live_hide_function_property(["22002", "FUNC_PLCGROUP_STARTADDRESS_1_INPUTS", "22002[2]"],
                                  handles=["377/17/33439/0", "377/17/33440/0"], page="=AB-C+ET-1/902")
    cs = capture["cs"]
    body = _body(capture)
    assert "GuardScratch(project" not in body
    assert _var(body, "bool v9dry") == "true"
    assert _var(body, "string[] v9pnames") == 'new string[] { "", "FUNC_PLCGROUP_STARTADDRESS_1_INPUTS", "" }'
    assert _var(body, "int[] v9pnum") == "new int[] { 22002, -1, 22002 }"
    assert _var(body, "int[] v9pidx") == "new int[] { 0, 0, 2 }"
    assert _var(body, "string v9action") == '"hide"'
    assert _var(body, "int v9relIdx") == "0"
    assert _var(body, "string v9page") == '"=AB-C+ET-1/902"'
    for member in ('"PropertyPlacements"', '"UseLocalPropertyPlacements"', '"SwitchLocalPropertyPlacements"',
                   '"IsSetAsVisible"', '"IsVisible"', '"RelationIndex"', '"Invisible"', '"Remove"'):
        assert member in cs, member
    _filled(cs)
    _csharp5(cs)
    _balanced(cs)
    assert capture["timeout"] == 180.0


def test_hide_star_and_int_and_remove(capture):
    L.live_hide_function_property(22002, handle="h", action="remove", relation="MainFunctionArticle",
                                  relation_index=1, dry_run=False, allow_real_project=True)
    body = _body(capture)
    assert "GuardScratch(project, true," in body
    assert _var(body, "int[] v9pnum") == "new int[] { 22002 }" and _var(body, "string v9action") == '"remove"'
    assert _var(body, "string v9rel") == '"MainFunctionArticle"' and _var(body, "int v9relIdx") == "1"
    L.live_hide_function_property("22002[*]", handle="h")
    assert _var(_body(capture), "int[] v9pidx") == "new int[] { 0 }"


def test_hide_real_without_allow_is_guarded(capture):
    L.live_hide_function_property("22002", handle="h", dry_run=False)
    assert "GuardScratch(project, false," in _body(capture)


def test_hide_never_removes_by_handle(capture):
    L.live_hide_function_property("22002", handle="h", action="remove", dry_run=False, allow_real_project=True)
    code = _code(_body(capture))
    # Remove is only called on the property placements found in the symbol's list
    assert code.count('"Remove", new string[]') == 1 and "Call(RequireMethod(pp.GetType(), \"Remove\"" in code


@pytest.mark.parametrize("kw", [
    {},
    {"property_id": "22002"},
    {"property_id": "22002", "handles": "1/2/3/0"},
    {"property_id": "22002", "handle": "h", "action": "delete"},
    {"property_id": "22002[0]", "handle": "h"},
    {"property_id": "FUNC_X\"); evil(", "handle": "h"},
    {"property_id": [], "handle": "h"},
    {"property_id": ["22002"] * 21 + ["1"], "handle": "h"},
    {"property_id": True, "handle": "h"},
    {"property_id": "22002", "handle": "h", "relation": "a b"},
    {"property_id": "22002", "handle": "h", "relation_index": 0},
    {"property_id": "22002", "handles": ["a", "b", "c"], "max_items": 2},
    {"property_id": "22002", "handles": ["a", ""]},
])
def test_hide_bad_arguments_refused_before_any_script(capture, kw):
    out = L.live_hide_function_property(**kw)
    assert out["success"] is False and out["error"]
    assert not capture["scripts"]


def test_hide_shaping(capture):
    capture["result"] = {"success": True, "results": {"success": True, "items": [
        {"handle": "a", "name": "=AB-C+ET-1-510S1", "action": "hide", "result": "hidden", "matched": [
            {"propertyId": 22002, "relationIndex": 1, "text": "??_??@6ES7131-6BF01-0BA0;", "action": "hide"},
            {"propertyId": 22002, "relationIndex": 2, "text": "6ES7193", "action": "hide"}]},
        {"handle": "b", "name": "=AB-C+ET-1-511S1", "action": "notFound", "result": "notFound", "matched": [],
         "notFound": ["22002"]}]}}
    out = L.live_hide_function_property("22002", handles=["a", "b"], dry_run=False, allow_real_project=True)
    assert out["success"] and out["counts"] == {"hidden": 1, "notFound": 1}
    assert out["textsMatched"] == 2 and out["textsHidden"] == 2 and out["action"] == "hide"
    assert out["items"][0]["matched"][0]["displayText"] == "6ES7131-6BF01-0BA0"
    assert "511S1" in out["warnings"][0]
    json.dumps(out)


@pytest.mark.parametrize("rollback,etype", [(True, "ROLLED_BACK"), (False, "PARTIAL")])
def test_hide_failure(capture, rollback, etype):
    capture["result"] = {"success": True, "results": {"success": True, "items": [{"handle": "a", "action": "hide"}],
                                                      "failedAt": {"handle": "a", "step": "verify", "error": "x"}}}
    out = L.live_hide_function_property("22002", handle="a", dry_run=False, allow_real_project=True,
                                        rollback_on_error=rollback)
    assert out["success"] is False and out["errorType"] == etype and "verify" in out["error"]


# ---------------------------------------------------------------------------
# 2  live_place_function_property: Location frame (fix a)
# ---------------------------------------------------------------------------

def test_place_signature_compatible():
    params = list(inspect.signature(L.live_place_function_property).parameters)
    assert params[-2:] == ["location_frame", "allow_outside_page"]
    sig = inspect.signature(L.live_place_function_property)
    assert sig.parameters["location_frame"].default == "auto"
    assert sig.parameters["allow_outside_page"].default is False


def test_place_frame_in_script(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h")
    body = _body(capture)
    assert _var(body, "string v9frame") == '"auto"' and _var(body, "bool v9allowOut") == "false"
    assert "PpFrameOf(fn, out nRel, out nPage)" in body and "wx = px - sxy[0]" in body
    assert "plan.Add(new object[] { h, fn, anyId, move, wx, wy, just, hgt, it });" in body
    assert "OUTSIDE_PAGE" in body
    cs = capture["cs"]
    assert "static bool PpLooksRelative(" in cs and "static string PpFrameOf(" in cs
    _filled(cs)
    _csharp5(cs)
    _balanced(cs)
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h", position="page", x=78, y=275.5,
                                   location_frame="relative", allow_outside_page=True)
    body = _body(capture)
    assert _var(body, "string v8pos") == '"page"' and _var(body, "double v8x") == "78.0"
    assert _var(body, "string v9frame") == '"relative"' and _var(body, "bool v9allowOut") == "true"


@pytest.mark.parametrize("kw", [
    {"position": "page", "x": 1},
    {"location_frame": "absolute"},
])
def test_place_new_arguments_validated(capture, kw):
    out = L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h", **kw)
    assert out["success"] is False and not capture["scripts"]


def test_place_outside_page_error_type(capture):
    capture["result"] = {"success": True, "results": {"success": False, "error":
                         "Nothing changed: OUTSIDE_PAGE - the text of h would land at (156; 537)"}}
    out = L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h", dry_run=False, allow_real_project=True)
    assert out["success"] is False and out["errorType"] == "OUTSIDE_PAGE"


def test_place_docstring_explains_frame():
    doc = L.live_place_function_property.__doc__
    for word in ("RELATIVE", "insertion point", "location_frame", "allow_outside_page", "pageLocation",
                 "writeLocation", "\"page\""):
        assert word in doc, word


# ---------------------------------------------------------------------------
# 3  live_move_placements: no second move of relative texts (fix b)
# ---------------------------------------------------------------------------

def test_move_skips_relative_texts(capture):
    S.live_move_placements("=AB-C+ET-1/902", macro_of="510S1", to_x=78, to_y=268, dry_run=False,
                           allow_real_project=True)
    body = _body(capture)
    assert "if (PpLooksRelative(was, oldLoc[h], ob))" in body
    assert 'ppr["relativeKept"] = ppRelative' in body
    i_rel = body.index("PpLooksRelative(was, oldLoc[h], ob)")
    i_mov = body.index("SetLocXY(q[0], was[0] + mvdx, was[1] + mvdy)")
    assert i_rel < i_mov
    assert "relativeKept" in S.live_move_placements.__doc__


def test_read_page_reports_relation_index(capture):
    S.live_read_page("=AB-C+ET-1/900", include_property_placements=True)
    assert 'd["relationIndex"] = r' in capture["cs"]
