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
from api.actions import live_placements as P, parts_db_reflect as pdb
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
    monkeypatch.setattr(P, "_execute_script", fake)
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


# ---------------------------------------------------------------------------
# B  replace_article without the modal parts-database dialog
# ---------------------------------------------------------------------------

REFUSAL = {"success": False, "errorType": "PARTS_DB_UNAVAILABLE", "error": "Parts database NOT opened"}


def test_replace_article_auto_is_refused_without_eplan_call(capture, monkeypatch):
    from api.actions import scripted
    calls = []
    monkeypatch.setattr(pdb, "parts_db_guard",
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
    monkeypatch.setattr(pdb, "parts_db_guard",
                        lambda allow_unverified_db=False: pytest.fail("guard called"))
    out = live_props.live_set_function_properties(handle="h", values={"FUNC_PLCADDRESS": "I1.0"})
    assert out["success"] is True
    assert "bool projectOnly = false;" in capture["cs"]


def test_replace_article_auto_ok_runs(capture, monkeypatch):
    from api.actions import scripted
    monkeypatch.setattr(pdb, "parts_db_guard", lambda allow_unverified_db=False: None)
    out = live_props.live_set_function_properties(
        handle="h", replace_article={"from": "A", "to": "B"})
    assert out["success"] is True and out["replaceArticleMode"] == "auto"
    assert "bool projectOnly = false;" in capture["cs"]


def test_replace_article_project_only_skips_guard_and_checks_project(capture, monkeypatch):
    from api.actions import scripted
    monkeypatch.setattr(pdb, "parts_db_guard",
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


def test_backup_media_not_sent_by_default(manager_spy):
    """Upstream behaviour is kept: without backup_media no BACKUPMEDIA is sent."""
    backup.backup_project("Z:/arhiv", "V2_v01.zw1", project_name="Z:/V2.elk", include_images=True)
    assert "BACKUPMEDIA" not in manager_spy[0]


def test_backup_sends_backupmedia_disk(manager_spy):
    """EPLAN 2.9 needs /BACKUPMEDIA:DISK; live_copy_pages(backup_before=True) passes it."""
    backup.backup_project("Z:/arhiv", "V2_v01.zw1", project_name="Z:/V2.elk", include_images=True,
                          backup_media="DISK")
    a = manager_spy[0]
    assert "/BACKUPMEDIA:DISK" in a
    assert "/BACKUPMETHOD:BACKUP" in a and "/INCLIMAGES:1" in a


@pytest.mark.parametrize("kw", [{"backup_media": "CD"}, {"backup_media": ""},
                                {"backup_media": "email", "backup_method": "ARCHIVE"}])
def test_backup_media_validation(manager_spy, kw):
    out = backup.backup_project("d", "a.zw1", project_name="p.elk", **kw)
    assert out["success"] is False and manager_spy == []


def test_backup_media_email(manager_spy):
    backup.backup_project("d", "a.zw1", project_name="p.elk", backup_media="email")
    assert "/BACKUPMEDIA:EMAIL" in manager_spy[0]


# ---------------------------------------------------------------------------
# D  new page with description (#11011)
# ---------------------------------------------------------------------------

def test_create_page_without_description_is_unchanged(capture):
    S.live_create_page(location="L", counter=1)
    assert "PAGE_NOMINATIOMN" not in capture["cs"]


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
