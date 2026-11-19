"""
v8 (inbox 2026-10-30_eplan_ni_orodja_prikaz_lastnosti_na_skatli.md and
2026-10-30_eplan_live_read_page_brez_prikazanih_lastnosti.md):
live_place_function_property (show a function property on the sheet -
PropertyPlacement) and live_read_page(include_property_placements=True).
Offline: assertions on the generated C# and on the Python side.
"""

import inspect
import json
import re

import pytest

from api.actions import schematic as S, live_props as L
from api.actions import live_placements as P, parts_db_reflect as pdb
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


TOKENS = ("V8HANDLES", "V8PAGE", "V8PNAME", "V8PIDX", "V8PNUM", "V8POS", "V8DX", "V8DY", "V8X", "V8Y",
          "V8HEIGHT", "V8JUST", "V8REPOS", "V8REL", "V8RELIDX", "V8LOCAL", "V8DRY", "V8ROLLBACK", "V8BOXSRC",
          "V8PPMAX", "PAGENAME")


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

    monkeypatch.setattr(P, "_execute_script", fake)
    monkeypatch.setattr(L, "_execute_script", fake)
    return seen


PAGE = "=AB-C+ET-1/900"


def _var(cs, name):
    m = re.search(r"\b%s = ([^;]+);" % re.escape(name), cs)
    assert m, name
    return m.group(1).strip()


# ---------------------------------------------------------------------------
# 1  registration and descriptions
# ---------------------------------------------------------------------------

def test_tool_is_exported():
    assert "live_place_function_property" in A.__all__
    assert A.live_place_function_property is L.live_place_function_property
    assert "live_place_function_property" in L.__all__


def test_docstrings_name_the_feature():
    doc = L.live_place_function_property.__doc__
    for word in ("property placement", "PropertyPlacement", "FUNC_PLCTYPE_ID", "dry_run", "allow_real_project",
                 "madeVisible", "alreadyVisible", "created", "SwitchLocalPropertyPlacements", "include_property_placements"):
        assert word in doc, word
    rdoc = S.live_read_page.__doc__
    assert "include_property_placements" in rdoc and "live_place_function_property" in rdoc


def test_read_page_signature_is_backward_compatible():
    params = list(inspect.signature(S.live_read_page).parameters)
    assert params[:5] == ["page", "include_pins", "limit", "types", "timeout_seconds"]
    sig = inspect.signature(S.live_read_page)
    assert sig.parameters["include_property_placements"].default is False


def test_place_defaults_are_safe():
    sig = inspect.signature(L.live_place_function_property)
    assert sig.parameters["dry_run"].default is True
    assert sig.parameters["allow_real_project"].default is False
    assert sig.parameters["rollback_on_error"].default is True


# ---------------------------------------------------------------------------
# 2  live_read_page
# ---------------------------------------------------------------------------

def test_read_page_default_body_has_no_property_placements(capture):
    S.live_read_page(PAGE)
    body = capture["cs"].split("[Start]")[1]
    assert "DumpPropPls" not in body and "propertyPlacementCount" not in body


def test_read_page_with_property_placements(capture):
    S.live_read_page(PAGE, types=["PLC"], include_property_placements=True, property_placements_limit=7)
    cs = capture["cs"]
    body = cs.split("[Start]")[1]
    assert "DumpPropPls(pl, 7)" in body and 'results["propertyPlacementCount"]' in body
    assert "static List<object> DumpPropPls(object sr, int max)" in cs
    _filled(cs)
    _csharp5(cs)
    _balanced(cs)


def test_read_page_limit_validated(capture):
    out = S.live_read_page(PAGE, include_property_placements=True, property_placements_limit=0)
    assert out["success"] is False and not capture["scripts"]


def test_read_page_display_text(capture):
    capture["result"] = {"success": True, "results": {"success": True, "page": PAGE, "placements": [
        {"clrType": "PLC", "handle": "h", "propertyPlacements": [
            {"propertyId": 20494, "text": "??_??@6ES7 131-6BF01-0BA0;", "visible": False}]}]}}
    out = S.live_read_page(PAGE, include_pins=False, include_property_placements=True)
    assert out["success"]
    assert out["placements"][0]["propertyPlacements"][0]["displayText"] == "6ES7 131-6BF01-0BA0"


# ---------------------------------------------------------------------------
# 3  live_place_function_property: generated C#
# ---------------------------------------------------------------------------

def test_dry_run_reads_only(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handles=["1/2/3/0", "1/2/4/0"])
    cs = capture["cs"]
    body = cs.split("[Start]")[1]
    assert "GuardScratch(project" not in body
    assert _var(body, "bool v8dry") == "true"
    assert _var(body, "string v8pname") == '"FUNC_PLCTYPE_ID"'
    assert _var(body, "int v8pnum") == "-1"
    assert _var(body, "string[] v8handles") == 'new string[] { "1/2/3/0", "1/2/4/0" }'
    assert _var(body, "string v8pos") == '"above_top_left"'
    assert _var(body, "double v8dy") == "1.0"
    assert _var(body, "string v8just") == '"BottomLeft"'
    assert _var(body, "bool v8repos") == "false"
    assert _var(body, "double v8height") == "-1.0"
    _filled(cs)
    _csharp5(cs)
    _balanced(cs)
    assert capture["timeout"] == 180.0


def test_real_run_is_guarded(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="1/2/3/0", dry_run=False)
    body = capture["cs"].split("[Start]")[1]
    assert "GuardScratch(project, false," in body
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="1/2/3/0", dry_run=False, allow_real_project=True)
    assert "GuardScratch(project, true," in capture["cs"].split("[Start]")[1]


def test_numeric_and_indexed_property(capture):
    L.live_place_function_property("20011[2]", handle="h")
    body = capture["cs"].split("[Start]")[1]
    assert _var(body, "int v8pnum") == "20011" and _var(body, "int v8pidx") == "2"
    assert _var(body, "string v8pname") == '""'
    L.live_place_function_property("FUNC_CONNECTIONDESIGNATION[1]", handle="h")
    body = capture["cs"].split("[Start]")[1]
    assert _var(body, "string v8pname") == '"FUNC_CONNECTIONDESIGNATION"' and _var(body, "int v8pidx") == "1"


def test_position_given_moves_existing_unless_said_otherwise(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h", position="above_top_right")
    body = capture["cs"].split("[Start]")[1]
    assert _var(body, "bool v8repos") == "true" and _var(body, "string v8just") == '"BottomRight"'
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h", position="above_top_right", reposition=False)
    assert _var(capture["cs"].split("[Start]")[1], "bool v8repos") == "false"


def test_absolute_offsets_justification_height_relation(capture):
    L.live_place_function_property("20100", handle="h", position="absolute", x=10, y=20.5, dx=1, dy=-2,
                                   justification="middleleft", height=2.5, relation="MainFunctionArticle",
                                   relation_index=2, make_local=False, box_source="bounding_box", page=PAGE)
    body = capture["cs"].split("[Start]")[1]
    assert _var(body, "double v8x") == "10.0" and _var(body, "double v8y") == "20.5"
    assert _var(body, "double v8dx") == "1.0" and _var(body, "double v8dy") == "-2.0"
    assert _var(body, "string v8just") == '"MiddleLeft"' and _var(body, "double v8height") == "2.5"
    assert _var(body, "string v8rel") == '"MainFunctionArticle"' and _var(body, "int v8relIdx") == "2"
    assert _var(body, "bool v8local") == "false" and _var(body, "string v8boxsrc") == '"bounding_box"'
    assert _var(body, "string v8page") == '"%s"' % PAGE


def test_helpers_use_api_29_members(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="h", dry_run=False, allow_real_project=True)
    cs = capture["cs"]
    for member in ('"PropertyPlacements"', '"UseLocalPropertyPlacements"', '"SwitchLocalPropertyPlacements"',
                   '"Eplan.EplApi.DataModel.Graphics.PropertyPlacement"', '"SymbolReference", "AnyPropertyId"',
                   '"RelationType", "Int32"', '"DisplayedProperty"', '"IsSetAsVisible"', '"IsVisible"',
                   '"GetDisplayString"', '"GetLogicalArea"', '"LowerLeft"', '"UpperRight"', '"Justification"',
                   '"Height"', '"Location"', '"Eplan.EplApi.DataModel.AnyPropertyId"'):
        assert member in cs, member
    assert "new PointD(" not in _code(cs)


@pytest.mark.parametrize("kw", [
    {},
    {"property_id": "FUNC_PLCTYPE_ID"},
    {"property_id": "FUNC_PLCTYPE_ID", "handles": "1/2/3/0"},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "position": "top"},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "position": "absolute", "x": 1},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "justification": "Up"},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "height": 0},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "height": "x"},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "dx": "1; x()"},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "box_source": "logical"},
    {"property_id": "FUNC_PLCTYPE_ID", "handle": "h", "relation": "a\"b"},
    {"property_id": "FUNC_X\"); evil(", "handle": "h"},
    {"property_id": "FUNC_X[0]", "handle": "h"},
    {"property_id": "20011[0]", "handle": "h"},
    {"property_id": True, "handle": "h"},
    {"property_id": "FUNC_PLCTYPE_ID", "handles": ["a", "b", "c"], "max_items": 2},
    {"property_id": "FUNC_PLCTYPE_ID", "handles": ["a", ""]},
])
def test_bad_arguments_refused_before_any_script(capture, kw):
    out = L.live_place_function_property(**kw)
    assert out["success"] is False and out["error"]
    assert not capture["scripts"]


def test_handle_and_handles_merged_without_duplicates(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle="a", handles=["a", "b"])
    assert _var(capture["cs"].split("[Start]")[1], "string[] v8handles") == 'new string[] { "a", "b" }'


def test_handles_escaped(capture):
    L.live_place_function_property("FUNC_PLCTYPE_ID", handle='1/2/"3/0')
    assert 'new string[] { "1/2/\\"3/0" }' in capture["cs"]


# ---------------------------------------------------------------------------
# 4  live_place_function_property: answer shaping
# ---------------------------------------------------------------------------

def test_counts_warnings_and_display_text(capture):
    capture["result"] = {"success": True, "results": {"success": True, "propertyId": 20494, "items": [
        {"handle": "a", "name": "=AB-C+ET-1-501S1", "propertyValue": "6ES7", "action": "create", "result": "created",
         "after": {"text": "??_??@6ES7;", "visible": True}, "textNearTarget": True},
        {"handle": "b", "name": "=AB-C+ET-1-501S3", "propertyValue": "", "action": "create", "result": "created",
         "textNearTarget": False, "locationDelta": 12.5},
        {"handle": "c", "name": "=AB-C+ET-1-501S2", "propertyValue": "6ES7", "action": "alreadyVisible",
         "result": "alreadyVisible"}]}}
    out = L.live_place_function_property("FUNC_PLCTYPE_ID", handles=["a", "b", "c"], dry_run=False,
                                         allow_real_project=True)
    assert out["success"] and out["dryRun"] is False
    assert out["counts"] == {"created": 2, "alreadyVisible": 1}
    assert out["items"][0]["after"]["displayText"] == "6ES7"
    assert "EMPTY" in out["warnings"][0] and "501S3" in out["warnings"][0]
    assert "not drawn near" in out["warnings"][1]


def test_dry_run_counts_by_action(capture):
    capture["result"] = {"success": True, "results": {"success": True, "items": [
        {"handle": "a", "action": "makeVisible"}, {"handle": "b", "action": "create"}]}}
    out = L.live_place_function_property("FUNC_PLCTYPE_ID", handles=["a", "b"])
    assert out["dryRun"] is True and out["counts"] == {"makeVisible": 1, "create": 1}
    assert "warnings" not in out


@pytest.mark.parametrize("rollback,etype,phrase", [(True, "ROLLED_BACK", "undone"), (False, "PARTIAL", "stay")])
def test_failure_is_reported(capture, rollback, etype, phrase):
    capture["result"] = {"success": True, "results": {"success": True, "items": [{"handle": "a", "action": "create"}],
                                                      "failedAt": {"handle": "a", "step": "PropertyPlacement.Create",
                                                                   "error": "ObjectCreationException"},
                                                      "rolledBack": 2}}
    out = L.live_place_function_property("FUNC_PLCTYPE_ID", handle="a", dry_run=False, allow_real_project=True,
                                         rollback_on_error=rollback)
    assert out["success"] is False and out["errorType"] == etype
    assert "PropertyPlacement.Create" in out["error"] and phrase in out["error"]


def test_script_error_keeps_items(capture):
    capture["result"] = {"success": True, "results": {"success": False, "error": "Nothing changed: x",
                                                      "items": [{"handle": "a"}]}}
    out = L.live_place_function_property("FUNC_PLCTYPE_ID", handle="a")
    assert out["success"] is False and "Nothing changed" in out["error"] and out["items"] == [{"handle": "a"}]


def test_answer_is_json(capture):
    capture["result"] = {"success": True, "results": {"success": True, "items": []}}
    json.dumps(L.live_place_function_property("FUNC_PLCTYPE_ID", handle="a"))


# ---------------------------------------------------------------------------
# 5  live_add_function_articles (coordinator 2026-10-30: module + BaseUnit)
# ---------------------------------------------------------------------------

MOD, BU = "SIE.6ES7131-6BF01-0BA0", "SIE.6ES7193-6BP20-0BA0"
TOKENS_B = ("V8ANR", "V8AVAR", "V8ACNT", "V8DUP", "V8TOX", "V8TOY", "V8ABS", "V8ANCHOR", "V8TPAGE", "V8MOVEPP",
            "V8ALLOWOVER", "V8ALLOWOUT", "V7MODE", "V7SAVEPAGE", "V7DRY")


def _filled_b(cs):
    _filled(cs)
    for t in TOKENS_B:
        assert not re.search(r"\b%s\b" % t, cs), "unfilled token " + t


@pytest.fixture
def artdb(monkeypatch):
    st = {"guard": None, "indb": {}, "lookups": []}
    monkeypatch.setattr(pdb, "parts_db_guard", lambda allow=False: st["guard"])

    def indb(pn, allow=False):
        st["lookups"].append(pn)
        return st["indb"].get(pn, True)

    monkeypatch.setattr(L, "_article_in_db", indb)
    return st


def test_articles_exported_and_described():
    assert "live_add_function_articles" in A.__all__ and A.live_add_function_articles is L.live_add_function_articles
    doc = L.live_add_function_articles.__doc__
    for word in ("AddArticleReference", "BaseUnit", "ARTICLE_NOT_IN_DB", "PARTS_DB_UNAVAILABLE", "connectionsChanged",
                 "alreadyPresent", "dry_run", "allow_real_project", "allow_missing_article", "replace_article"):
        assert word in doc, word
    sig = inspect.signature(L.live_add_function_articles)
    assert sig.parameters["dry_run"].default is True and sig.parameters["allow_real_project"].default is False


def test_articles_script(capture, artdb):
    L.live_add_function_articles([{"partnr": MOD}, {"partNr": BU, "count": 2, "variant": "2"}],
                                 handles=["1/2/3/0"], dry_run=False, allow_real_project=True)
    cs = capture["cs"]
    body = cs.split("[Start]")[1]
    assert _var(body, "string[] aNr") == 'new string[] { "%s", "%s" }' % (MOD, BU)
    assert _var(body, "string[] aVar") == 'new string[] { "1", "2" }'
    assert _var(body, "int[] aCnt") == "new int[] { 1, 2 }"
    assert "GuardScratch(project, true," in body
    assert '"AddArticleReference", new string[] { "String", "String", "UInt32" }' in body
    assert '"RemoveArticleReference"' in body
    assert artdb["lookups"] == [MOD, BU]
    _filled_b(cs)
    _csharp5(cs)
    _balanced(cs)


def test_articles_dry_run_has_no_guard(capture, artdb):
    L.live_add_function_articles([MOD], handle="h")
    assert "GuardScratch(project" not in capture["cs"].split("[Start]")[1]


def test_articles_db_guard_blocks_before_eplan(capture, artdb):
    artdb["guard"] = {"success": False, "errorType": "PARTS_DB_UNAVAILABLE", "error": "no ACE"}
    out = L.live_add_function_articles([MOD], handle="h", dry_run=False, allow_real_project=True)
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE" and out["nothingWritten"] and not capture["scripts"]


def test_articles_not_in_db(capture, artdb):
    artdb["indb"] = {BU: False}
    out = L.live_add_function_articles([MOD, BU], handle="h", dry_run=False, allow_real_project=True)
    assert out["success"] is False and out["errorType"] == "ARTICLE_NOT_IN_DB" and not capture["scripts"]
    assert out["articleInDb"] == {MOD: True, BU: False}
    capture["result"] = {"success": True, "results": {"success": True, "items": [], "addedCount": 2}}
    out = L.live_add_function_articles([MOD, BU], handle="h", dry_run=False, allow_real_project=True,
                                       allow_missing_article=True)
    assert out["success"] and "NOT in the parts database" in " ".join(out["warnings"])


@pytest.mark.parametrize("kw", [
    {"articles": [], "handle": "h"},
    {"articles": "SIE.X", "handle": "h"},
    {"articles": [{"partnr": ""}], "handle": "h"},
    {"articles": [{"partnr": "A", "count": 0}], "handle": "h"},
    {"articles": [{"partnr": "A", "qty": 1}], "handle": "h"},
    {"articles": [MOD]},
    {"articles": [MOD], "handles": "1/2/3/0"},
    {"articles": [MOD] * 21, "handle": "h"},
])
def test_articles_bad_arguments(capture, artdb, kw):
    out = L.live_add_function_articles(**kw)
    assert out["success"] is False and not capture["scripts"] and not artdb["lookups"]


def test_articles_shaping(capture, artdb):
    capture["result"] = {"success": True, "results": {"success": True, "addedCount": 1, "connectionsChanged": True, "items": [
        {"handle": "a", "name": "=AB-C+ET-1-510S1", "isMainFunction": True, "verified": True, "connectionsChanged": True,
         "plan": [{"partNr": MOD, "action": "alreadyPresent"}, {"partNr": BU, "action": "add", "result": "added"}]}]}}
    out = L.live_add_function_articles([MOD, BU], handle="a", dry_run=False, allow_real_project=True)
    assert out["success"] and out["counts"] == {"alreadyPresent": 1, "added": 1}
    assert "changed the connection points" in " ".join(out["warnings"])
    capture["result"] = {"success": True, "results": {"success": True, "items": [],
                                                      "failedAt": {"handle": "a", "partNr": BU, "error": "x"}}}
    out = L.live_add_function_articles([MOD, BU], handle="a", dry_run=False, allow_real_project=True)
    assert out["success"] is False and out["errorType"] == "ROLLED_BACK" and BU in out["error"]


# ---------------------------------------------------------------------------
# 6  live_move_placements (coordinator 2026-10-30: lay modules out on pages)
# ---------------------------------------------------------------------------

def test_move_exported_and_described():
    assert "live_move_placements" in A.__all__ and A.live_move_placements is P.live_move_placements
    doc = P.live_move_placements.__doc__
    for word in ("Placement.Location", "macro_of", "to_x", "target_page", "OVERLAP", "OUTSIDE_PAGE",
                 "overlapsExistingBoxes", "dry_run", "allow_real_project", "NOT verified"):
        assert word in doc, word
    sig = inspect.signature(P.live_move_placements)
    assert sig.parameters["dry_run"].default is True and sig.parameters["allow_overlap"].default is False


def test_move_reuses_the_v7_selection(capture):
    P.live_move_placements("=AB-C+ET-1/902", macro_of="510S1", to_x=78, to_y=268, dry_run=False,
                           allow_real_project=True)
    cs = capture["cs"]
    body = cs.split("[Start]")[1]
    assert 'AddPick(pick, why, order, pl, "insideBox")' in body and 'AddPick(pick, why, order, g, "group")' in body
    assert "bool V7" not in body and _var(body, "string v7mode") == '"macro"'
    assert "GuardScratch(project, true," in body
    assert "SetLocXY(pl, oldLoc[h][0] + mvdx" in body and '"Remove", new string[]' not in body
    _filled_b(cs)
    _csharp5(cs)
    _balanced(cs)


def test_move_absolute_and_page(capture):
    P.live_move_placements("=AB-C+ET-1/902", handles=["1/2/3/0"], to_x=78, to_y=268, anchor_handle="1/2/3/0",
                           target_page="=AB-C+ET-1/903")
    body = capture["cs"].split("[Start]")[1]
    assert "mvdx = 78.0 - al[0]" in body and 'string ah = "1/2/3/0"' in body
    assert 'string tpName = "=AB-C+ET-1/903"' in body and "GuardScratch(project" not in body


@pytest.mark.parametrize("kw", [
    {"macro_of": "510S1"},
    {"macro_of": "510S1", "dx": 1, "to_x": 1, "to_y": 2},
    {"macro_of": "510S1", "to_x": 1},
    {"macro_of": "510S1", "dx": 1, "anchor_handle": "a"},
    {"macro_of": "510S1", "handles": ["a"], "dx": 1},
    {"dx": 1},
    {"region": [0, 0, 0, 10], "dx": 1},
    {"macro_of": "510S1", "dx": "1; x()"},
    {"macro_of": "510S1", "dx": 1, "region_match": "touch"},
])
def test_move_bad_arguments(capture, kw):
    out = P.live_move_placements("=AB-C+ET-1/902", **kw)
    assert out["success"] is False and not capture["scripts"]


@pytest.mark.parametrize("dry,key", [(True, "wouldRefuse"), (False, "errorType")])
def test_move_refusal(capture, dry, key):
    capture["result"] = {"success": True, "results": {"success": True, "items": [], "selectedCount": 6,
                                                      "refused": "OVERLAP", "refusedReason": "1 box(es) would overlap"}}
    out = P.live_move_placements("=AB-C+ET-1/902", macro_of="510S1", dx=-46, dry_run=dry, allow_real_project=True)
    assert out[key] == "OVERLAP" and out["success"] is (dry is True)


def test_move_failure_and_target_check(capture):
    capture["result"] = {"success": True, "results": {"success": True, "items": [],
                                                      "failedAt": {"handle": "h", "step": "verify", "error": "x"}}}
    out = P.live_move_placements("=AB-C+ET-1/902", macro_of="510S1", dx=-46, dry_run=False, allow_real_project=True)
    assert out["success"] is False and out["errorType"] == "ROLLED_BACK"
    capture["result"] = {"success": True, "results": {"success": True, "items": [], "movedCount": 6,
                                                      "notOnTargetPage": ["h"]}}
    out = P.live_move_placements("=AB-C+ET-1/902", macro_of="510S1", dx=-46, dry_run=False, allow_real_project=True)
    assert out["success"] is False and out["errorType"] == "NOT_ON_TARGET"
