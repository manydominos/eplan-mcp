"""
v7 (inbox 2026-10-30_eplan_remove_placement_brez_kompaktnega_izhoda_in_mnozicnega_brisanja.md):
compact answer for one removed placement, and live_remove_placements
(handles / region / macro_of, exclusions, dry_run, max_items).
Offline: assertions on the generated C# and on the Python side.
"""

import json
import os
import re

import pytest

from api.actions import schematic as S, scripted
import api.actions as A


def _code(cs):
    return "\n".join(line.split("//")[0] for line in cs.splitlines())


def _csharp5(cs):
    code = _code(cs)
    assert "?." not in code
    assert '$"' not in code
    assert "nameof(" not in code
    assert "out var " not in code


def _balanced(cs):
    code = _code(cs)
    stripped = re.sub(r'@"[^"]*"', '""', code)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
    stripped = re.sub(r"'(?:\\.|[^'\\])'", "''", stripped)
    assert stripped.count("{") == stripped.count("}")
    assert stripped.count("(") == stripped.count(")")


TOKENS = ("V7MODE", "V7HANDLES", "V7EXTAGS", "V7EXHANDLES", "V7TYPES", "V7RX1", "V7RY1", "V7RX2", "V7RY2",
          "V7MACRO", "V7DRY", "V7FULL", "V7MAX", "V7SAVEPAGE", "V7REGIONMATCH", "V7INCGROUP", "V7INCBOX",
          "V7EXEXPAND", "PAGENAME")


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
    return seen


PAGE = "=AB-C+ET-1/900"
PAGE_AFTER = {"page": PAGE, "placementCount": 63, "returned": 63, "truncated": False,
              "placements": [{"clrType": "PLC", "name": "500K3"}, {"clrType": "PolyLine"}, {"clrType": "PolyLine"}]}
REMOVED = {"clrType": "PolyLine", "handle": "218/34/197415/0", "location": {"x": 1, "y": 2},
           "boundingBox": [{"x": 1, "y": 2}], "absentMembers": ["SymbolVariant"]}


# ---------------------------------------------------------------------------
# 1  one placement: compact answer
# ---------------------------------------------------------------------------

def test_single_remove_is_compact_by_default(capture):
    capture["result"] = {"success": True, "results": {"success": True, "page": PAGE, "removed": REMOVED,
                                                      "removedType": "PolyLine", "page_after": PAGE_AFTER}}
    out = S.live_remove_placement(PAGE, handle="218/34/197415/0", expect_type="PolyLine", allow_real_project=True)
    assert out["success"] is True
    assert "page_after" not in out
    assert out["pageAfter"] == {"placementCount": 63, "returned": 63, "truncated": False,
                                "countsByType": {"PLC": 1, "PolyLine": 2}}
    assert out["removed"] == {"clrType": "PolyLine", "handle": "218/34/197415/0", "location": {"x": 1, "y": 2}}
    assert len(json.dumps(out)) < 600


def test_single_remove_verbose_is_v6(capture):
    capture["result"] = {"success": True, "results": {"success": True, "page": PAGE, "removed": dict(REMOVED),
                                                      "page_after": PAGE_AFTER}}
    out = S.live_remove_placement(PAGE, handle="h", verbose=True)
    assert out["page_after"] == PAGE_AFTER and out["removed"]["absentMembers"] == ["SymbolVariant"]
    assert "pageAfter" not in out and "pageAfterFile" not in out


def test_single_remove_page_after_file(capture, tmp_path, monkeypatch):
    monkeypatch.setattr(scripted, "_WORK_ROOT", str(tmp_path))
    capture["result"] = {"success": True, "results": {"success": True, "page": PAGE, "removed": REMOVED,
                                                      "page_after": PAGE_AFTER}}
    out = S.live_remove_placement(PAGE, handle="h", save_page_files=True)
    assert os.path.dirname(out["pageAfterFile"]) == os.path.join(str(tmp_path), "page_after")
    with open(out["pageAfterFile"], encoding="utf-8") as f:
        assert json.load(f) == PAGE_AFTER
    assert "page_after" not in out


def test_single_remove_script_unchanged(capture):
    S.live_remove_placement(PAGE, handle="h", expect_type="PolyLine")
    cs = capture["cs"]
    assert 'results["page_after"] = ReadPage(page, 200, false, null);' in cs
    assert "GuardScratch(project, false," in cs


def test_single_remove_failure_untouched(capture):
    capture["result"] = {"success": True, "results": {"success": False, "error": "No placement with handle"}}
    out = S.live_remove_placement(PAGE, handle="h")
    assert out["success"] is False and "pageAfter" not in out


# ---------------------------------------------------------------------------
# 2  live_remove_placements
# ---------------------------------------------------------------------------

def test_new_tool_is_registered():
    assert "live_remove_placements" in A.__all__
    assert A.live_remove_placements is S.live_remove_placements
    assert "live_remove_placements" in S.__all__


@pytest.mark.parametrize("kw", [
    {}, {"handles": ["a"], "region": [0, 0, 1, 1]}, {"handles": ["a"], "macro_of": "500S1"},
    {"region": [0, 0, 1, 1], "macro_of": "500S1"},
])
def test_exactly_one_selector(capture, kw):
    out = S.live_remove_placements(PAGE, **kw)
    assert out["success"] is False and "exactly one selector" in out["error"]
    assert not capture["scripts"]


@pytest.mark.parametrize("kw", [
    {"region": [0, 0, 1]}, {"region": [0, 0, 0, 5]}, {"region": [0, "x", 1, 1]}, {"region": [0, float("nan"), 1, 1]},
    {"macro_of": "500S1", "region_match": "touch"}, {"macro_of": "500S1", "max_items": 0},
    {"macro_of": "500S1", "max_items": 6000}, {"handles": ["a", ""]}, {"handles": "a", "types": [3]},
    {"macro_of": "{{RESULT_PATH}}"}, {"macro_of": "500S1", "list_limit": -1},
])
def test_bad_arguments_refused_before_script(capture, kw):
    out = S.live_remove_placements(PAGE, **kw)
    assert out["success"] is False
    assert not capture["scripts"]


def test_default_is_dry_run_without_guard(capture):
    out = S.live_remove_placements(PAGE, macro_of="500S1")
    cs = capture["cs"]
    assert "bool v7dry = true;" in cs
    assert "GuardScratch(project" not in _code(cs).split("public void Run()")[1]
    assert out["dryRun"] is True and out["mode"] == "macro_of"
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


@pytest.mark.parametrize("allow", [False, True])
def test_real_run_is_guarded(capture, allow):
    S.live_remove_placements(PAGE, macro_of="500S1", dry_run=False, allow_real_project=allow)
    assert "GuardScratch(project, %s," % ("true" if allow else "false") in capture["cs"]
    assert "bool v7dry = false;" in capture["cs"]


def test_region_is_normalised_and_rendered(capture):
    S.live_remove_placements(PAGE, region=[300, 20, 100.5, 180], exclude_device_tags=["500K3", "500K3"],
                             exclude_handles="9/9/9/0", types=["PolyLine"])
    cs = capture["cs"]
    assert 'string v7mode = "region";' in cs
    assert "new double[] { 100.5, 20.0, 300.0, 180.0 }" in cs
    assert 'string[] v7exTags = new string[] { "500K3" };' in cs
    assert 'string[] v7exHandles = new string[] { "9/9/9/0" };' in cs
    assert 'string[] v7types = new string[] { "PolyLine" };' in cs
    assert '"inside" == "location"' in cs
    _filled(cs)


def test_region_as_dict(capture):
    S.live_remove_placements(PAGE, region={"x1": 0, "y1": 10, "x2": 50, "y2": -10}, region_match="location")
    cs = capture["cs"]
    assert "new double[] { 0.0, -10.0, 50.0, 10.0 }" in cs
    assert '"location" == "location"' in cs


def test_handles_mode(capture):
    S.live_remove_placements(PAGE, handles=["1/2/3/0", "1/2/4/0", "1/2/3/0"], max_items=5)
    cs = capture["cs"]
    assert 'string[] v7handles = new string[] { "1/2/3/0", "1/2/4/0" };' in cs
    assert 'string v7mode = "handles";' in cs
    assert "int v7max = 5;" in cs
    assert "Nothing removed: " in cs


def test_macro_parts_and_order(capture):
    S.live_remove_placements(PAGE, macro_of="=AB-C+ET-1-500S1", include_group=False, include_box_area=True,
                             exclude_expand=False)
    cs = capture["cs"]
    run = _code(cs).split("public void Run()")[1]
    assert 'string v7macro = "=AB-C+ET-1-500S1";' in cs
    assert "if (false)" in run and "if (true)" in run
    # anchors -> box area -> group, exclusions before the guard, record before removal
    assert run.index('"device"') < run.index('"insideBox"') < run.index('AddPick(pick, why, order, g, "group")')
    assert run.index('refused = "EXCLUDED_HIT"') < run.index('refused = "TOO_MANY"') < run.index("RequireMethod(pl.GetType(), \"Remove\"")
    assert run.index('results["items"] = items;') < run.index("RequireMethod(pl.GetType(), \"Remove\"")
    assert "if (!v7dry && refused == null && order.Count > 0)" in run


def test_api_members_used(capture):
    S.live_remove_placements(PAGE, macro_of="500S1")
    cs = capture["cs"]
    for m in ('TryRead(pl, "Group", null)', 'TryRead(grp, "SubPlacements", null)', '"GetBoundingBox"',
              '"RemoveSubPlacement"', 'TryRead(o, "IsValid", null)', "PagePlacements(page)"):
        assert m in cs, m


def test_page_token_not_rescanned(capture):
    S.live_remove_placements("=V7MODE+X/1", macro_of="V7MACRO")
    cs = capture["cs"]
    assert 'object page = FindPage(project, "=V7MODE+X/1");' in cs
    assert 'string v7macro = "V7MACRO";' in cs


DRY = {"success": True, "page": PAGE, "placementCountBefore": 64, "selectedCount": 3,
       "countsByType": {"PLC": 1, "PolyLine": 2}, "countsByReason": {"device": 1, "insideBox": 2},
       "items": [{"handle": "a", "clrType": "PLC", "name": "500S1", "reason": "device"},
                 {"handle": "b", "clrType": "PolyLine", "reason": "insideBox"},
                 {"handle": "c", "clrType": "PolyLine", "reason": "insideBox"}],
       "excludedFunctions": ["500K3"], "excludedCount": 1, "groups": [], "warnings": []}


def test_dry_answer_shaping(capture):
    capture["result"] = {"success": True, "results": dict(DRY)}
    out = S.live_remove_placements(PAGE, macro_of="500S1", list_limit=2)
    assert out["success"] is True and out["dryRun"] is True
    assert len(out["items"]) == 2 and out["itemsTruncated"] == 3
    assert "warnings" not in out and "pageBeforeFile" not in out


def test_dry_refusal_is_reported_not_failed(capture):
    capture["result"] = {"success": True, "results": dict(DRY, refused="EXCLUDED_HIT", refusedReason="x")}
    out = S.live_remove_placements(PAGE, macro_of="500S1")
    assert out["success"] is True and out["wouldRefuse"] == "EXCLUDED_HIT" and out["wouldRefuseReason"] == "x"


def test_real_refusal_fails(capture):
    capture["result"] = {"success": True, "results": dict(DRY, refused="TOO_MANY", refusedReason="too many")}
    out = S.live_remove_placements(PAGE, macro_of="500S1", dry_run=False, allow_real_project=True)
    assert out["success"] is False and out["errorType"] == "TOO_MANY" and out["removedCount"] == 0
    assert out["countsByType"] == DRY["countsByType"]


def test_real_run_files(capture, tmp_path, monkeypatch):
    monkeypatch.setattr(scripted, "_WORK_ROOT", str(tmp_path))
    res = dict(DRY, removedCount=3, placementCountAfter=61, stillPresent=[], excludedStillPresent=1, partial=False,
               page_before={"placements": [1]}, page_after={"placements": [2]})
    capture["result"] = {"success": True, "results": res}
    out = S.live_remove_placements(PAGE, macro_of="500S1", dry_run=False, allow_real_project=True)
    assert out["success"] is True and out["removedCount"] == 3
    assert "page_before" not in out and "page_after" not in out
    with open(out["itemsFile"], encoding="utf-8") as f:
        assert len(json.load(f)["items"]) == 3
    for k, sub in (("pageBeforeFile", "page_before"), ("pageAfterFile", "page_after"), ("itemsFile", "removed_items")):
        assert os.path.dirname(out[k]) == os.path.join(str(tmp_path), sub)


@pytest.mark.parametrize("extra,etype", [
    ({"partial": True, "failedAt": {"handle": "b"}}, "PARTIAL"),
    ({"partial": False, "stillPresent": ["b"]}, "NOT_REMOVED"),
    ({"partial": False, "stillPresent": [], "excludedLost": ["500K3"]}, "EXCLUDED_LOST"),
])
def test_real_run_failures(capture, extra, etype):
    capture["result"] = {"success": True, "results": dict(DRY, removedCount=1, **extra)}
    out = S.live_remove_placements(PAGE, macro_of="500S1", dry_run=False, save_page_files=False,
                                   allow_real_project=True)
    assert out["success"] is False and out["errorType"] == etype


def test_script_exception_passthrough(capture):
    capture["result"] = {"success": True, "results": {"success": False, "error": "Nothing removed: 1 handle(s)"}}
    out = S.live_remove_placements(PAGE, handles=["x"])
    assert out == {"success": False, "error": "Nothing removed: 1 handle(s)", "project": None}


def test_docstrings_name_the_new_tool():
    assert "live_remove_placements" in S.live_remove_placement.__doc__
    doc = S.live_remove_placements.__doc__
    for word in ("macro_of", "exclude_device_tags", "max_items", "dry_run=True (DEFAULT)", "EXCLUDED_HIT", "TOO_MANY"):
        assert word in doc
