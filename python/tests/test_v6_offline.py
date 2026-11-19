"""
v6 changes (inbox 2026-10-28/29 eplan_*, live test v5):
displayed device tag, merge detection, renamed functions, compact answers,
page name with a letter, project path, article not in the parts database.
Offline: assertions on the generated C# and on the Python side.
"""

import json
import os
import re

import pytest

from api.actions import live_props, schematic as S, project, scripted
from api.actions import live_placements as P, parts_db_reflect as pdb


def _code(cs):
    return "\n".join(line.split("//")[0] for line in cs.splitlines())


def _csharp5(cs):
    code = _code(cs)
    assert "?." not in code
    assert '$"' not in code
    assert "nameof(" not in code


def _balanced(cs):
    code = _code(cs)
    stripped = re.sub(r'@"[^"]*"', '""', code)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)
    stripped = re.sub(r"'(?:\\.|[^'\\])'", "''", stripped)
    assert stripped.count("{") == stripped.count("}")
    assert stripped.count("(") == stripped.count(")")


TOKENS = ("DRYRUN", "ALLOWMERGE", "ROLLBACK", "NAMEROUTE", "EVALPAGES", "IHANDLE", "INAME",
          "IPAGE", "ICONN", "ITAG", "INEWCONN", "IDEVICE", "PNWANT", "PNEXPECTED")


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

    monkeypatch.setattr(live_props, "_execute_script", fake)
    monkeypatch.setattr(S, "_execute_script", fake)
    monkeypatch.setattr(P, "_execute_script", fake)
    return seen


BOX = {"handle": "120/17/31524/0", "tag": "510S1"}


def _run(cs):
    return _code(cs).split("public void Run()")[1]


# ---------------------------------------------------------------------------
# 1  displayed device tag (inbox 2026-10-29_eplan_vidni_bmk_brez_stevilke.md)
# ---------------------------------------------------------------------------

def test_tags_default_route_is_auto_with_page_evaluation(capture):
    out = live_props.live_set_device_tags([BOX])
    assert out["success"] is True
    cs = capture["cs"]
    assert 'string nameRoute = "auto";' in cs
    assert "bool evalPages = true;" in cs
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


def test_tags_use_nameservice_for_the_visible_name(capture):
    live_props.live_set_device_tags([BOX])
    cs = capture["cs"]
    assert 'FindType("Eplan.EplApi.HEServices.NameService")' in cs
    assert 'NsCall(ns, "AdjustVisibleName", FnPage(fn), fn)' in cs
    assert 'NsCall(ns, "AdjustFullName", FnPage(fn), fn)' in cs
    assert '"RemovalOfUnnecessaryDeviceTagNamePartsIsEnabled", true' in cs
    assert '"FindNameGivingFunction"' in cs
    run = _run(cs)
    assert "WriteDeviceTag(ns, nameRoute, fn, wantName[k], newDt[k], iTag[i])" in run
    assert '"EvaluateAndSetAllNames"' in run


def test_tags_verify_the_displayed_tag(capture):
    live_props.live_set_device_tags([BOX])
    run = _run(capture["cs"])
    assert "bool visOk = VisibleOk(fn, newDt[k]);" in run
    assert "displayed device tag is '" in run
    assert 'd["visibleDeviceTag"] = vis;' in run
    # the computed DT properties are still never written
    for computed in ("FUNC_IDENTDEVICETAG", "FUNC_VISIBLEDEVICETAG", "FUNC_FULLDEVICETAG"):
        assert computed not in run


def test_tags_nameservice_checked_before_first_write(capture):
    live_props.live_set_device_tags([BOX])
    run = _run(capture["cs"])
    first = run.index("SetConnDesignation(fn, connIdx[k]")
    assert run.index("NameService not available - nothing written") < first
    assert run.index("NameService has no AdjustVisibleName - nothing written") < first


def test_tags_boxes_before_points(capture):
    live_props.live_set_device_tags([BOX])
    run = _run(capture["cs"])
    assert run.index('GetType().Name != "Terminal") order.Add(k0)') < \
        run.index('GetType().Name == "Terminal") order.Add(k0)')
    assert "k = order[pos];" in run


def test_tags_no_sign_added(capture):
    live_props.live_set_device_tags([BOX])
    run = _run(capture["cs"])
    assert "newDt[k] = ComposeDtV6(dt, iTag[i]);" in run
    assert "ComposeDt(dt, iTag[i])" not in run


@pytest.mark.parametrize("route", ["auto", "visible", "name", " Visible "])
def test_tags_name_route_values(capture, route):
    live_props.live_set_device_tags([BOX], name_route=route, evaluate_pages=False)
    assert 'string nameRoute = "%s";' % route.strip().lower() in capture["cs"]
    assert "bool evalPages = false;" in capture["cs"]


@pytest.mark.parametrize("route", ["", "full", 3, "auto;evil"])
def test_tags_bad_name_route(capture, route):
    out = live_props.live_set_device_tags([BOX], name_route=route)
    if route == "":
        assert out["success"] is True   # "" -> default "auto"
        return
    assert out["success"] is False and not capture["scripts"]


def test_tags_rollback_restores_visible_name_and_reevaluates(capture):
    live_props.live_set_device_tags([BOX])
    run = _run(capture["cs"])
    rb = run[run.index("if (rollback)"):]
    assert 'NsCall(ns, "AdjustVisibleName", FnPage(tFn[r]), tFn[r])' in rb
    assert "EvaluateAndSetAllNames" in rb


# ---------------------------------------------------------------------------
# 2  merges / renamed functions
# ---------------------------------------------------------------------------

def test_tags_merge_detection_ignores_the_sign(capture):
    live_props.live_set_device_tags([BOX], dry_run=True)
    run = _run(capture["cs"])
    assert "string dtE = NormDt(DtPart(nmE));" in run
    assert "if (!SameDevice(kv.Key, newDt[k])) continue;" in run


def test_tags_report_other_renamed_functions(capture):
    live_props.live_set_device_tags([BOX])
    run = _run(capture["cs"])
    assert 'results["renamedOthers"] = RenamedOthers(all, nameBefore, tHandle, follow, 200, out others);' in run


def test_macro_renamed_existing_compares_every_function(capture, tmp_path):
    ema = tmp_path / "m.ema"
    ema.write_text("x")
    live_props.live_insert_window_macro("P", str(ema), 1, 2)
    run = _run(capture["cs"])
    assert "if (was != n)" in run
    assert "!fnNames.Contains(n)) continue" not in run
    assert 'results["renamedExistingCount"] = renamedTotal;' in run
    assert "if (IsBoxLike(pl)) boxesBefore.Add(DumpPlacement(pl, false));" in run
    _csharp5(capture["cs"])
    _balanced(capture["cs"])


# ---------------------------------------------------------------------------
# 3  compact answers
# ---------------------------------------------------------------------------

DETAILS = [
    {"item": 0, "handle": "h1", "clrType": "PLC", "previousName": "K1", "newName": "510S1",
     "name": "510S1", "visibleDeviceTag": "-510S1", "verified": True, "visibleVerified": True},
    {"item": 0, "handle": "h2", "clrType": "Terminal", "previousName": "K1:1", "newName": "510S1:1",
     "name": "510S1:1", "visibleDeviceTag": "", "takesTagFromBox": True, "verified": True,
     "visibleVerified": True},
]
OTHERS = [{"handle": "h3", "nameBefore": "K1:2", "nameAfter": "510S1:2", "followsRenamedDevice": True},
          {"handle": "h9", "nameBefore": "500S1:1", "nameAfter": "500S1-500S1:1", "followsRenamedDevice": False}]


def _result(**extra):
    r = {"success": True, "dryRun": False, "nameRoute": "auto", "planned": [dict(d) for d in DETAILS],
         "details": DETAILS, "modified": 2, "merges": {}, "undo": [{"handle": "h1", "tag": "K1"}],
         "renamedOthers": OTHERS, "renamedOthersCount": 2, "pagesEvaluated": ["=AB-C+WAGO/920"]}
    r.update(extra)
    return {"success": True, "results": r}


def test_tags_compact_by_default(capture):
    capture["result"] = _result()
    out = live_props.live_set_device_tags([BOX])
    assert "details" not in out and "planned" not in out and "renamedOthers" not in out
    assert out["functions"][0] == "h1: K1 -> 510S1, displayed '-510S1'"
    assert "(tag of its box)" in out["functions"][1]
    assert out["followersCount"] == 1
    assert [r["handle"] for r in out["unexpectedRenames"]] == ["h9"]
    assert out["undo"] == [{"handle": "h1", "tag": "K1"}]
    assert any("other function" in w for w in out["warnings"])
    assert len(json.dumps(out)) < 2000


def test_tags_verbose_keeps_everything(capture):
    capture["result"] = _result()
    out = live_props.live_set_device_tags([BOX], verbose=True)
    assert out["details"] == DETAILS and out["renamedOthers"] == OTHERS and "functions" not in out


def test_tags_compact_lists_unverified_in_full(capture):
    bad = dict(DETAILS[0], verified=False, visibleVerified=False, visibleDeviceTag="-S1")
    capture["result"] = _result(details=[bad])
    out = live_props.live_set_device_tags([BOX])
    assert out["unverified"] == [bad]
    assert out["functions"][0].endswith("NOT VERIFIED")


def test_tags_compact_dry_run(capture):
    capture["result"] = {"success": True, "results": {
        "success": True, "dryRun": True, "planned": [{"handle": "h1", "previousName": "K1", "newName": "510S1"}],
        "merges": {"510K3": {"existingFunctions": 9}}, "wouldRefuse": True}}
    out = live_props.live_set_device_tags([BOX], dry_run=True)
    assert out["functions"] == ["h1: K1 -> 510S1"]
    assert "allow_merge" in out["note"] and out["merges"]


def test_macro_compact_by_default_and_checks(capture, tmp_path):
    ema = tmp_path / "m.ema"
    ema.write_text("x")
    capture["result"] = {"success": True, "results": {
        "success": True, "page": "P", "pageType": "Overview", "insertedCount": 40,
        "pageSize": {"x": 420, "y": 297},
        "inserted": [{"clrType": "PLC", "handle": "b1", "name": "K1",
                      "boundingBox": [{"x": 30, "y": 100}, {"x": 138, "y": 160}]},
                     {"clrType": "Terminal", "handle": "t1", "name": "K1:1",
                      "boundingBox": [{"x": 32, "y": -46}, {"x": 40, "y": 110}]}],
        "existingBoxes": [{"clrType": "PLC", "handle": "b0", "name": "K1",
                           "boundingBox": [{"x": 100, "y": 120}, {"x": 172, "y": 170}]}],
        "functionNames": ["K1", "K1:1"], "duplicateNames": {},
        "renamedExisting": [{"handle": "x", "nameBefore": "K1", "nameAfter": "K1-K1"}],
        "renamedExistingCount": 1}}
    out = live_props.live_insert_window_macro("P", str(ema), 30, 150)
    assert "inserted" not in out and "existingBoxes" not in out
    assert out["boxes"] == [{"handle": "b1", "name": "K1", "clrType": "PLC"}]
    assert out["boundingBox"] == {"min": {"x": 30.0, "y": -46.0}, "max": {"x": 138.0, "y": 160.0}}
    assert out["overlapsExistingBoxes"] == ["PLC K1 [b0]"]
    assert out["outsidePage"] is True
    assert len(out["warnings"]) == 3


def test_macro_verbose_keeps_inserted(capture, tmp_path):
    ema = tmp_path / "m.ema"
    ema.write_text("x")
    capture["result"] = {"success": True, "results": {
        "success": True, "insertedCount": 1, "inserted": [{"clrType": "PLC", "handle": "b1"}],
        "existingBoxes": [], "renamedExisting": [], "duplicateNames": {}}}
    out = live_props.live_insert_window_macro("P", str(ema), 30, 150, verbose=True)
    assert out["inserted"] and "warnings" not in out


PAGE_BEFORE = {"page": "=AB-C+WAGO/920", "placementCount": 73, "returned": 73, "truncated": False,
               "placements": [{"clrType": "PLC", "name": "500S1"}, {"clrType": "Terminal", "name": "500S1:1"},
                              {"clrType": "Line"}, {"clrType": "Line"}]}


# ---------------------------------------------------------------------------
# 4  live_create_page(page_name="902a")
# ---------------------------------------------------------------------------


def test_create_page_without_page_name_is_unchanged(capture):
    S.live_create_page(location="DI", counter=250)
    cs = capture["cs"]
    assert 'SetProp(ppl, "PAGE_COUNTER", (int)250)' in cs
    assert "wantPn" not in cs and "already exists" not in cs


# ---------------------------------------------------------------------------
# 5  get_current_project: path + last database write
# ---------------------------------------------------------------------------

class _Mgr:
    def execute_action(self, action):
        return {"executor": "action", "success": True, "parameters": {"TYPE": "PROJECT"}}


# ---------------------------------------------------------------------------
# 6  replace_article (auto): article must be in the parts database
# ---------------------------------------------------------------------------

ART = {"from": "SIE.6ES7155-6BA00-0CN0", "to": "SIE.6ES7155-6BA01-0CN0"}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(pdb, "parts_db_guard", lambda allow_unverified_db=False: None)
    state = {"answer": {"success": True, "results": {"success": True, "found": False}}, "calls": []}

    def get_part(part_number, allow_unverified_db=False):
        state["calls"].append(part_number)
        return state["answer"]

    monkeypatch.setattr(pdb, "get_part", get_part)
    return state


def test_article_not_in_db_is_refused(capture, db):
    out = live_props.live_set_function_properties(handle="h", replace_article=ART)
    assert out["success"] is False and out["errorType"] == "ARTICLE_NOT_IN_DB"
    assert out["nothingWritten"] is True and out["articleInDb"] is False
    assert db["calls"] == ["SIE.6ES7155-6BA01-0CN0"]
    assert not capture["scripts"]


def test_article_not_in_db_allowed(capture, db):
    out = live_props.live_set_function_properties(handle="h", replace_article=ART, allow_missing_article=True)
    assert out["success"] is True and out["articleInDb"] is False and "NOT in the parts" in out["articleWarning"]


def test_article_in_db(capture, db):
    db["answer"] = {"success": True, "results": {"success": True, "found": True}}
    out = live_props.live_set_function_properties(handle="h", replace_article=ART)
    assert out["success"] is True and out["articleInDb"] is True and "articleWarning" not in out


def test_article_lookup_undecided_writes_with_warning(capture, db):
    db["answer"] = {"success": False, "message": "Not connected"}
    out = live_props.live_set_function_properties(handle="h", replace_article=ART)
    assert out["success"] is True and out["articleInDb"] is None and "Could not tell" in out["articleWarning"]


def test_project_only_does_not_look_up(capture, db):
    live_props.live_set_function_properties(handle="h", replace_article=ART, replace_article_mode="project_only")
    assert db["calls"] == []
