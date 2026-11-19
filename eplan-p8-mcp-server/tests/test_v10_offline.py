"""
v10 (inbox 2026-11-03_eplan_besedilo_poti_pathtext_branje_urejanje.md,
2026-11-03_eplan_set_function_properties_brez_paketa.md, 2026-11-02_eplan_set_function_properties_paketno.md):
live_read_page(include_text_content), live_set_texts (Text / PathText contents) and
live_set_function_properties(items=[...], dry_run).
Offline: assertions on the generated C# and on the Python side.
"""

import inspect
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


TOKENS = ("V10H", "V10T", "V10L", "V10P", "V10K", "V10HT", "V10DRY", "V10RB", "V10FN", "V10FMAX", "V10CHMAX",
          "V10VERB", "V10IH", "V10IN", "V10IP", "V10IC", "V10ILIM", "V10RI", "V10RN", "V10RX", "V10RV")


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


def _body(capture):
    return capture["cs"].split("[Start]")[1]


# ---------------------------------------------------------------------------
# registration, signatures, docstrings
# ---------------------------------------------------------------------------

def test_live_set_texts_registered():
    assert "live_set_texts" in A.__all__ and "live_set_texts" in L.__all__
    assert A.live_set_texts is L.live_set_texts
    doc = L.live_set_texts.__doc__
    for k in ("PathText", "path function text", "FUNC_TEXT_AUTOMATIC", "MultiLangString", "??_??", "dry_run",
              "allow_real_project", "ROLLED_BACK", "keep_other_languages", "functionsChanged", "include_text_content"):
        assert k in doc, k


def test_signatures_compatible():
    sig = inspect.signature(L.live_set_function_properties)
    names = list(sig.parameters)
    old = ["values", "name", "handle", "page", "connection_designation", "replace_article", "limit",
           "allow_real_project", "timeout_seconds", "replace_article_mode", "allow_unverified_db",
           "allow_missing_article"]
    assert names[:len(old)] == old
    for p in ("items", "dry_run", "rollback_on_error", "verbose", "max_items"):
        assert p in names and sig.parameters[p].default is not inspect.Parameter.empty
    assert sig.parameters["dry_run"].default is False
    rp = inspect.signature(S.live_read_page)
    assert list(rp.parameters)[-2:] == ["include_text_content", "text_functions_limit"]
    assert rp.parameters["include_text_content"].default is False
    st = inspect.signature(L.live_set_texts)
    assert st.parameters["dry_run"].default is True and st.parameters["allow_real_project"].default is False
    assert "items" in L.live_set_function_properties.__doc__ and "wouldWrite" in L.live_set_function_properties.__doc__
    assert "include_text_content" in S.live_read_page.__doc__ and "functionsShowingIt" in S.live_read_page.__doc__


# ---------------------------------------------------------------------------
# live_read_page(include_text_content)
# ---------------------------------------------------------------------------

def test_read_page_default_unchanged(capture):
    S.live_read_page("=AB-C+DI/214")
    cs = capture["cs"]
    assert "TxKind(" not in cs and "textContent" not in cs and "FnTextRows" not in cs


def test_read_page_text_content_script(capture):
    S.live_read_page("=AB-C+DI/214", types=["PathText"], include_text_content=True, text_functions_limit=7)
    cs = capture["cs"]
    b = _body(capture)
    assert 'v10byH[h]["textContent"] = rec;' in b and "int v10fmax = 7;" in b
    assert "static int TxKind(object o)" in cs and "static List<object[]> FnTextRows(" in cs
    assert '"PropertyPlacement"' in cs        # property texts are never taken as texts
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


def test_read_page_text_limits(capture):
    out = S.live_read_page("P", include_text_content=True, text_functions_limit=101)
    assert not out["success"] and "text_functions_limit" in out["error"]
    S.live_read_page("P", include_text_content=True, text_functions_limit=0)
    assert "int v10fmax = 0;" in _body(capture)


def test_read_page_text_display(capture):
    capture["result"] = {"success": True, "results": {"success": True, "placements": [
        {"clrType": "PathText", "handle": "a", "textContent": {"raw": "??_??@SKID OLJNA EMULZIJA;"}},
        {"clrType": "Text", "handle": "b", "textContent": {"raw": "sl_SI@Opomba;en_US@Note;"}},
        {"clrType": "Text", "handle": "c", "textContent": {"raw": "plain"}}]}}
    out = S.live_read_page("P", include_pins=False, include_text_content=True)
    tc = [p["textContent"] for p in out["placements"]]
    assert tc[0]["text"] == "SKID OLJNA EMULZIJA" and tc[0]["byLanguage"] == {"??_??": "SKID OLJNA EMULZIJA"}
    assert tc[1]["text"] == "Opomba" and tc[1]["byLanguage"] == {"sl_SI": "Opomba", "en_US": "Note"}
    assert tc[2]["text"] == "plain" and "byLanguage" not in tc[2]


# ---------------------------------------------------------------------------
# live_set_texts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw, msg", [
    ({}, "Nothing to change"),
    ({"handle": "h"}, "together"),
    ({"items": "x"}, "items must be a list"),
    ({"items": [{"handle": "h", "text": "a"}, {"handle": "h", "text": "b"}]}, "twice"),
    ({"items": [{"handle": "h", "text": "a;b"}]}, "';'"),
    ({"items": [{"handle": "h", "text": 5}]}, "text must be a string"),
    ({"items": [{"handle": "", "text": "a"}]}, "handle must be"),
    ({"items": [{"handle": "h", "text": "a", "colour": 1}]}, "unknown key"),
    ({"items": [{"handle": "h", "text": "a", "height": 0}]}, "height"),
    ({"handle": "h", "text": "a", "language": "slovene"}, "language"),
    ({"handle": "h", "text": "abc", "language": "raw"}, "MultiLangString form"),
    ({"handle": "h", "text": "a", "check_functions": "all"}, "check_functions"),
    ({"items": [{"handle": "h%d" % i, "text": "a"} for i in range(3)], "max_items": 2}, "max_items"),
])
def test_set_texts_validation(capture, kw, msg):
    out = L.live_set_texts(**kw)
    assert not out["success"] and msg in out["error"]
    assert not capture["scripts"]


def test_set_texts_dry_run_default_no_guard(capture):
    L.live_set_texts(handle="377/83/194356/0", text="S5V9 Tripotni ventil – Skid Mineralno olje")
    cs = capture["cs"]
    b = _body(capture)
    assert "GuardScratch(project" not in b and "bool tDry = true;" in b
    assert '"S5V9 Tripotni ventil \\u2013 Skid Mineralno olje"' in b and "–" not in cs
    assert 'string[] tL = new string[] { "auto" };' in b and 'string tFn = "page";' in b
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


def test_set_texts_real_guard_and_items(capture):
    L.live_set_texts([{"handle": "a", "text": "x\ny", "language": "sl_SI", "page": "=AB-C+DI/214", "height": 2.5},
                      {"handle": "b", "text": "sl_SI@a;en_US@b;", "language": "raw", "keep_other_languages": False}],
                     dry_run=False, allow_real_project=True, check_functions="project", function_list_limit=4,
                     rollback_on_error=False)
    b = _body(capture)
    assert "GuardScratch(project, true," in b and "bool tDry = false;" in b and "bool tRb = false;" in b
    assert 'string[] tT = new string[] { "x\\ny", "sl_SI@a;en_US@b;" };' in b
    assert 'string[] tL = new string[] { "sl_SI", "raw" };' in b
    assert 'string[] tP = new string[] { "=AB-C+DI/214", "" };' in b
    assert "bool[] tK = new bool[] { true, false };" in b and "double[] tHt = new double[] { 2.5, -1.0 };" in b
    assert 'string tFn = "project";' in b and "int tFmax = 4;" in b and "int tChMax = 80;" in b
    _filled(capture["cs"])


def test_set_texts_shaping(capture):
    capture["result"] = {"success": True, "results": {"success": True, "dryRun": False, "items": [
        {"i": 0, "handle": "a", "kind": "PathText", "before": "??_??@OLD;", "after": "??_??@NEW;", "result": "changed",
         "displayedMatches": False},
        {"i": 1, "handle": "b", "kind": "Text", "before": "x", "after": "x", "result": "unchanged"}],
        "functionsChecked": 5, "functionsChangedCount": 0}}
    out = L.live_set_texts([{"handle": "a", "text": "NEW"}, {"handle": "b", "text": "x"}], dry_run=False,
                           allow_real_project=True)
    assert out["success"] and out["counts"] == {"changed": 1, "unchanged": 1}
    assert out["items"][0]["textBefore"] == "OLD" and out["items"][0]["textAfter"] == "NEW"
    assert any("Displayed text differs" in w for w in out["warnings"])
    assert any("No FUNC_TEXT_AUTOMATIC changed" in w for w in out["warnings"])


def test_set_texts_failed(capture):
    capture["result"] = {"success": True, "results": {"success": True, "items": [
        {"i": 0, "handle": "a", "kind": "PathText", "result": "rolledBack"}],
        "failedAt": {"i": 1, "handle": "b", "step": "Contents", "error": "Locked"}, "rolledBack": 1}}
    out = L.live_set_texts([{"handle": "a", "text": "N"}, {"handle": "b", "text": "M"}], dry_run=False,
                           allow_real_project=True)
    assert not out["success"] and out["errorType"] == "ROLLED_BACK" and "restored" in out["error"]
    assert not any("FUNC_TEXT_AUTOMATIC" in w for w in out.get("warnings") or [])
    out = L.live_set_texts([{"handle": "a", "text": "N"}], dry_run=False, allow_real_project=True,
                           rollback_on_error=False)
    assert out["errorType"] == "PARTIAL"


# ---------------------------------------------------------------------------
# live_set_function_properties(items / dry_run)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw, msg", [
    ({"items": []}, "non-empty list"),
    ({"items": [{"values": {"FUNC_TEXT": "a"}}]}, "give handle or name"),
    ({"items": [{"handle": "h", "values": {}}]}, "non-empty dict"),
    ({"items": [{"handle": "h", "values": {"FUNC_TEXT": None}}]}, "must not be None"),
    ({"items": [{"handle": "h", "values": {"bad name": "a"}}]}, "not a property name"),
    ({"items": [{"handle": "h", "values": {"FUNC_TEXT": "a"}, "limit": 0}]}, "limit"),
    ({"items": [{"handle": "h", "values": {"FUNC_TEXT": "a"}, "tag": "x"}]}, "unknown key"),
    ({"items": [{"handle": "h", "values": {"FUNC_TEXT": "a"}}], "name": "x"}, "do not pass name"),
    ({"items": [{"handle": "h", "values": {"FUNC_TEXT": "a"}}], "values": {"FUNC_TEXT": "a"}}, "do not pass values"),
    ({"items": [{"handle": "h%d" % i, "values": {"FUNC_TEXT": "a"}} for i in range(3)], "max_items": 2}, "max_items"),
    ({"values": {"FUNC_TEXT": "a"}, "handle": "h", "replace_article": {"from": "a", "to": "b"}, "dry_run": True},
     "replace_article"),
])
def test_items_validation(capture, kw, msg):
    out = L.live_set_function_properties(**kw)
    assert not out["success"] and msg in out["error"]
    assert not capture["scripts"]


def test_items_script(capture):
    L.live_set_function_properties(items=[
        {"handle": "1/2/3/0", "values": {"FUNC_SUPPLEMENTARYFIELD[1]": "Skid č"}},
        {"name": "=AB-C+DI-510K3", "connection_designation": 9, "page": "=AB-C+DI/201",
         "values": {"FUNC_PLCADDRESS": "I1.0", "FUNC_TEXT": ""}, "limit": 2}], allow_real_project=True)
    cs = capture["cs"]
    b = _body(capture)
    assert "GuardScratch(project, true," in b and "bool pDry = false;" in b and "bool pRb = true;" in b
    assert 'string[] iH = new string[] { "1/2/3/0", "" };' in b
    assert 'string[] iN = new string[] { "", "=AB-C+DI-510K3" };' in b
    assert 'string[] iC = new string[] { "", "9" };' in b and "int[] iLim = new int[] { 1, 2 };" in b
    assert "int[] rI = new int[] { 0, 1, 1 };" in b and "int[] rX = new int[] { 1, 0, 0 };" in b
    assert 'string[] rN = new string[] { "FUNC_SUPPLEMENTARYFIELD", "FUNC_PLCADDRESS", "FUNC_TEXT" };' in b
    assert 'string[] rV = new string[] { "Skid \\u010d", "I1.0", "" };' in b
    assert "static string FnRestore(" in cs and "static string FnWrite(" in cs
    _csharp5(cs)
    _balanced(cs)
    _filled(cs)


def test_single_dry_run_is_one_item(capture):
    L.live_set_function_properties(values={"FUNC_TEXT": "a"}, name="=AB-C+DI-510K3", connection_designation="9",
                                   limit=3, dry_run=True)
    b = _body(capture)
    assert "GuardScratch(project" not in b and "bool pDry = true;" in b and "int[] iLim = new int[] { 3 };" in b


def test_single_mode_unchanged(capture):
    L.live_set_function_properties(values={"FUNC_PLCADDRESS": "I1.0"}, handle="1/2/3/0", allow_real_project=True)
    b = _body(capture)
    assert "string[] wNames" in b and "string[] iH" not in b and "MlsRaw" not in capture["cs"]


def test_items_shaping(capture):
    capture["result"] = {"success": True, "results": {"success": True, "dryRun": False,
                                                      "counts": {"items": 1, "functions": 1, "written": 1, "unchanged": 0,
                                                                 "verified": 0, "unverified": 1},
                                                      "unverified": [{"item": 0, "property": "FUNC_PLCADDRESS"}]}}
    out = L.live_set_function_properties(items=[{"handle": "h", "values": {"FUNC_PLCADDRESS": "I1.0"}}],
                                         allow_real_project=True)
    assert out["success"] and "1 write(s)" in out["warning"]
    capture["result"] = {"success": True, "results": {"success": True, "counts": {},
                                                      "failedAt": {"item": 2, "name": "x", "property": "FUNC_TEXT",
                                                                   "error": "e"}, "rolledBack": 2}}
    out = L.live_set_function_properties(items=[{"handle": "h", "values": {"FUNC_TEXT": "a"}}], allow_real_project=True)
    assert not out["success"] and out["errorType"] == "ROLLED_BACK" and "undone" in out["error"]
