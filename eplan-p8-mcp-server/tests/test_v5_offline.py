"""
v5 changes (inbox 2026-10-28_eplan_bmk_plc_prikljucka_in_skatle.md,
2026-10-28_eplan_replace_article_modalno_okno.md,
2026-10-28_eplan_backup_project_backupmedia.md, live test v4 notes).
Offline: assertions on the generated C# and on the Python side.
"""

import json
import re

import pytest

from api.actions import live_props, schematic as S, backup, catalog
import api.actions as actions


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


TAG_TOKENS = ("DRYRUN", "ALLOWMERGE", "ROLLBACK", "IHANDLE", "INAME", "IPAGE", "ICONN",
              "ITAG", "INEWCONN", "IDEVICE", "PROJECTONLY", "DESCR")


def _filled(cs):
    for t in TAG_TOKENS:
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
    return seen


ITEM = {"name": "=AB-C+DI-510K3", "connection": "9", "page": "=AB-C+DI/201",
        "tag": "510K4", "connection_designation": "1"}


# ---------------------------------------------------------------------------
# A  live_set_device_tags
# ---------------------------------------------------------------------------

def test_device_tags_tool_is_registered():
    assert "live_set_device_tags" in actions.__all__
    assert callable(actions.live_set_device_tags)


def test_device_tags_script(capture):
    out = live_props.live_set_device_tags([ITEM, {"handle": "200/17/31107/0", "tag": "550K9",
                                                  "scope": "device"}])
    assert out["success"] is True
    cs = capture["cs"]
    assert 'string[] iName = new string[] { "=AB-C+DI-510K3", "" };' in cs
    assert 'string[] iHandle = new string[] { "", "200/17/31107/0" };' in cs
    assert 'string[] iConn = new string[] { "9", "" };' in cs
    assert 'string[] iTag = new string[] { "510K4", "550K9" };' in cs
    assert 'string[] iNewConn = new string[] { "1", "" };' in cs
    assert "bool[] iDevice = new bool[] { false, true };" in cs
    assert "GuardScratch(project, false," in cs
    assert "bool allowMerge = false;" in cs and "bool rollback = true;" in cs
    assert capture["timeout"] == 300.0
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


def test_device_tags_validate_before_first_write(capture):
    live_props.live_set_device_tags([ITEM])
    code = _code(capture["cs"]).split("public void Run()")[1]
    first_write = code.index("SetConnDesignation(fn, connIdx[k]")
    for check in ("functions match - nothing written", "no function matches",
                  "both address", "cannot tell which connection point designation",
                  "would MERGE"):
        assert code.index(check) < first_write, check


def test_device_tags_write_through_function_name_not_computed_props(capture):
    live_props.live_set_device_tags([ITEM])
    cs = capture["cs"]
    assert 'GetWritable(fn.GetType(), "Name")' in cs
    code = _code(cs).split("public void Run()")[1]
    for computed in ("FUNC_IDENTDEVICETAG", "FUNC_VISIBLEDEVICETAG", "FUNC_FULLDEVICETAG",
                     "FUNC_DEVICETAG_MAIN"):
        assert computed not in code


def test_device_tags_accept_function_subclasses(capture):
    live_props.live_set_device_tags([ITEM])
    cs = capture["cs"]
    assert "IsFunctionObject(fn0)" in cs
    assert 'GetType().Name != "Function"' not in _code(cs)


def test_device_tags_rollback_and_undo(capture):
    live_props.live_set_device_tags([ITEM], rollback_on_error=False, allow_merge=True,
                                    allow_real_project=True)
    cs = capture["cs"]
    assert "bool rollback = false;" in cs and "bool allowMerge = true;" in cs
    assert "GuardScratch(project, true," in cs
    assert 'results["rollback"] = rb;' in cs
    assert 'u["tag"] = DtPart(prevName[k]);' in cs


def test_device_tags_dry_run_has_no_guard(capture):
    live_props.live_set_device_tags([ITEM], dry_run=True)
    cs = capture["cs"]
    assert "GuardScratch(project," not in cs
    assert "bool dryRun = true;" in cs


@pytest.mark.parametrize("items", [
    [], None, "x", [1], [{"tag": "K1"}], [{"handle": "h"}], [{"handle": "h", "tag": "510K4:1"}],
    [{"handle": "h", "tag": "-"}], [{"handle": "h", "tag": "K1", "scope": "all"}],
    [{"handle": "h", "tag": "K1", "scope": "device", "connection_designation": "1"}],
    [{"handle": "h", "tag": "K1", "colour": "red"}],
    [{"handle": "h", "tag": "x{{RESULT_PATH}}"}],
    [{"handle": "h", "tag": "K1"}] * 501,
])
def test_device_tags_reject_bad_items(capture, items):
    out = live_props.live_set_device_tags(items)
    assert out["success"] is False
    assert not capture["scripts"]


def test_device_tags_escape(capture):
    live_props.live_set_device_tags([{"name": 'A"); evil(); ("', "tag": "K1"}])
    assert 'A\\"); evil(); (\\"' in capture["cs"]
    _balanced(capture["cs"])


def test_device_tags_failure_keeps_rollback(capture):
    capture["result"] = {"success": True, "results": {
        "success": False, "error": "Renaming stopped: x", "failedAtItem": 3, "modified": 3,
        "details": [{"handle": "h1"}], "rollback": [{"handle": "h1", "restored": True}],
        "undo": [], "planned": [{"handle": "h1"}]}}
    out = live_props.live_set_device_tags([ITEM])
    assert out["success"] is False
    assert out["rollback"][0]["restored"] is True
    assert out["failedAtItem"] == 3 and out["planned"]


def test_device_tags_dry_run_note(capture):
    capture["result"] = {"success": True, "results": {
        "success": True, "dryRun": True, "wouldRefuse": True,
        "merges": {"=AB-C+DI-510K3": {"existingFunctions": 9}}, "planned": []}}
    out = live_props.live_set_device_tags([ITEM], dry_run=True)
    assert "allow_merge" in out["note"]


def test_device_tags_compiles_helpers(capture):
    live_props.live_set_device_tags([ITEM])
    cs = capture["cs"]
    for h in ("static string DtPart(", "static string ComposeDt(", "static bool NameMatches(",
              "static bool ItemMatches(", "static string SetFunctionName(",
              "static string SetConnDesignation("):
        assert cs.count(h) == 1, h


# ---------------------------------------------------------------------------
# A  live_set_device_tag (single) accepts subclasses
# ---------------------------------------------------------------------------

def test_single_tag_reports_previous_name_and_type(capture):
    S.live_set_device_tag("P", "h", "=AB-C+DI-510K4")
    cs = capture["cs"]
    assert 'results["previousName"] = PropText(target, "Name");' in cs
    assert 'results["clrType"] = target.GetType().Name;' in cs
    assert "!IsFunctionType(pl)" in cs


# ---------------------------------------------------------------------------
# B  replace_article without the modal parts-database dialog
# ---------------------------------------------------------------------------

REFUSAL = {"success": False, "errorType": "PARTS_DB_UNAVAILABLE", "error": "Parts database NOT opened"}


def test_replace_article_auto_is_refused_without_eplan_call(capture, monkeypatch):
    from api.actions import scripted
    calls = []
    monkeypatch.setattr(scripted, "_parts_db_guard",
                        lambda allow_unverified_db=False: (calls.append(allow_unverified_db), REFUSAL)[1])
    out = live_props.live_set_function_properties(
        handle="h", values={"FUNC_TEXT": "x"},
        replace_article={"from": "WAGO.750-333", "to": "SIE.6ES7155-6BA01-0CN0"},
        allow_unverified_db=True)
    assert out["success"] is False
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert out["nothingWritten"] is True and "project_only" in out["alternative"]
    assert calls == [True]
    assert not capture["scripts"], "nothing may reach EPLAN on a refusal"


def test_values_only_write_does_not_check_the_database(capture, monkeypatch):
    from api.actions import scripted
    monkeypatch.setattr(scripted, "_parts_db_guard",
                        lambda allow_unverified_db=False: pytest.fail("guard called"))
    out = live_props.live_set_function_properties(handle="h", values={"FUNC_PLCADDRESS": "I1.0"})
    assert out["success"] is True
    assert "bool projectOnly = false;" in capture["cs"]


def test_replace_article_auto_ok_runs(capture, monkeypatch):
    from api.actions import scripted
    monkeypatch.setattr(scripted, "_parts_db_guard", lambda allow_unverified_db=False: None)
    out = live_props.live_set_function_properties(
        handle="h", replace_article={"from": "A", "to": "B"})
    assert out["success"] is True and out["replaceArticleMode"] == "auto"
    assert "bool projectOnly = false;" in capture["cs"]


def test_replace_article_project_only_skips_guard_and_checks_project(capture, monkeypatch):
    from api.actions import scripted
    monkeypatch.setattr(scripted, "_parts_db_guard",
                        lambda allow_unverified_db=False: pytest.fail("guard called"))
    live_props.live_set_function_properties(
        handle="h", replace_article={"from": "A", "to": "B"}, replace_article_mode="project_only")
    cs = capture["cs"]
    assert "bool projectOnly = true;" in cs
    code = _code(cs).split("public void Run()")[1]
    assert code.index('TryRead(project, "Articles", null)') < code.index("StoreToObject")
    assert code.index("ARTICLE_NOT_IN_PROJECT: '") < code.index("SetLiveProp(props, wNames[i]")
    _balanced(cs)
    _filled(cs)


def test_replace_article_not_in_project_error_type(capture):
    capture["result"] = {"success": True, "results": {
        "success": False, "error": "ARTICLE_NOT_IN_PROJECT: 'B' is not an article of this project"}}
    out = live_props.live_set_function_properties(
        handle="h", replace_article={"from": "A", "to": "B"}, replace_article_mode="project_only")
    assert out["success"] is False and out["errorType"] == "ARTICLE_NOT_IN_PROJECT"


def test_replace_article_mode_validation(capture):
    out = live_props.live_set_function_properties(
        handle="h", replace_article={"from": "A", "to": "B"}, replace_article_mode="db")
    assert out["success"] is False and not capture["scripts"]


# ---------------------------------------------------------------------------
# C  backup BACKUPMEDIA
# ---------------------------------------------------------------------------

@pytest.fixture
def manager_spy(monkeypatch):
    calls = []

    class _M:
        def execute_action(self, action):
            calls.append(action)
            return {"success": True}

    monkeypatch.setattr(backup, "_get_connected_manager", lambda: (_M(), None))
    return calls


def test_backup_sends_backupmedia_disk_by_default(manager_spy):
    backup.backup_project("Z:/arhiv", "V2_v01.zw1", project_name="Z:/V2.elk", include_images=True)
    a = manager_spy[0]
    assert "/BACKUPMEDIA:DISK" in a
    assert "/BACKUPMETHOD:BACKUP" in a and "/INCLIMAGES:1" in a
    assert "AUTOCOPYREFDATA" not in a and "COPYREFDATA" not in a


def test_backup_copyrefdata_both_names(manager_spy):
    backup.backup_project("d", "a.zw1", project_name="p.elk", auto_copy_ref_data=True)
    assert "/AUTOCOPYREFDATA:1" in manager_spy[0] and "/COPYREFDATA:1" in manager_spy[0]


@pytest.mark.parametrize("kw", [{"backup_media": "CD"}, {"backup_media": None},
                                {"backup_media": "email", "backup_method": "ARCHIVE"}])
def test_backup_media_validation(manager_spy, kw):
    out = backup.backup_project("d", "a.zw1", project_name="p.elk", **kw)
    assert out["success"] is False and manager_spy == []


def test_backup_media_email(manager_spy):
    backup.backup_project("d", "a.zw1", project_name="p.elk", backup_media="email")
    assert "/BACKUPMEDIA:EMAIL" in manager_spy[0]


def test_registry_knows_backupmedia():
    names = [p["name"] for p in catalog.action_describe("backup")["registry"]["params"]]
    for n in ("BACKUPMEDIA", "SPLITSIZE", "BACKUPAMOUNT", "COMPRESSPRJ", "COPYREFDATA"):
        assert n in names
    assert names.index("BACKUPMEDIA") == names.index("BACKUPMETHOD") + 1


# ---------------------------------------------------------------------------
# D  new page with description (#11011)
# ---------------------------------------------------------------------------

def test_create_page_without_description_is_unchanged(capture):
    S.live_create_page(location="L", counter=1)
    assert "PAGE_NOMINATIOMN" not in capture["cs"]


def test_create_page_with_description(capture):
    S.live_create_page(plant="AB-C", location="DI", counter=200,
                       description="sl_SI@ET 200SP DI;en_US@PLC REFERENCES;", allow_real_project=True)
    cs = capture["cs"]
    assert '"PAGE_NOMINATIOMN"' in cs
    assert '"SetAsString"' in cs
    assert '"sl_SI@ET 200SP DI;en_US@PLC REFERENCES;"' in cs
    assert cs.index("Call(create, page") < cs.index("PAGE_NOMINATIOMN")
    assert cs.index("PAGE_NOMINATIOMN") < cs.index('results["page_after"]')
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


def test_create_page_description_token_is_not_rescanned(capture):
    S.live_create_page(location="L", counter=1, description="PAGETYPE DESCR")
    assert '"PAGETYPE DESCR"' in capture["cs"]


@pytest.mark.parametrize("bad", ["", "   ", "{{RESULT_PATH}}", 5])
def test_create_page_bad_description(capture, bad):
    assert S.live_create_page(location="L", counter=1, description=bad)["success"] is False
    assert not capture["scripts"]


@pytest.mark.parametrize("req,stored,ok", [
    ("WAGO 750 DI", "??_??@WAGO 750 DI;", True),
    ("sl_SI@WAGO 750 DI;en_US@PLC REFERENCES;", "sl_SI@WAGO 750 DI;en_US@PLC REFERENCES;", True),
    ("sl_SI@A;en_US@B;", "en_US@B;sl_SI@A;", True),
    ("sl_SI@A;en_US@B;", "sl_SI@A;", False),
    ("X", "", False),
])
def test_create_page_description_verification(capture, req, stored, ok):
    capture["result"] = {"success": True, "results": {"success": True, "page": "=AB-C+DI/250",
                                                      "description": stored}}
    out = S.live_create_page(location="L", counter=1, description=req)
    assert out["descriptionVerified"] is ok
    assert ("warning" in out) is (not ok)


def test_create_page_failure_reports_the_created_page(capture):
    capture["result"] = {"success": False, "error": "x", "results": {
        "success": False, "error": "no PAGE_NOMINATIOMN", "page": "=AB-C+DI/250"}}
    out = S.live_create_page(location="L", counter=1, description="X")
    assert out["success"] is False and out["pageCreated"] is True
    assert out["undo"]["remove_page"] is True


def test_remove_whole_page_still_exists(capture):
    S.live_remove_placement("=AB-C+WAGO/902", remove_page=True)
    assert 'RequireMethod(page.GetType(), "Remove"' in capture["cs"]


# ---------------------------------------------------------------------------
# E  live test v4 notes
# ---------------------------------------------------------------------------

def test_read_defaults_include_func_text_automatic(capture):
    assert "FUNC_TEXT_AUTOMATIC" in live_props.DEFAULT_READ_PROPERTIES
    live_props.live_read_function_properties(contains="510K3")
    assert '"FUNC_TEXT_AUTOMATIC"' in capture["cs"]


def test_renamed_existing_is_always_a_list(capture, tmp_path):
    ema = tmp_path / "m.ema"
    ema.write_text("x", encoding="utf-8")
    live_props.live_insert_window_macro(page="P", macro_path=str(ema), x=1, y=2)
    cs = capture["cs"]
    assert 'results["renamedExisting"] = renamed;' in cs
    assert "if (renamed.Count > 0)" not in cs
