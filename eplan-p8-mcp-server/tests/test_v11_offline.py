"""
v11 (user 2026-11-06: copy / paste between EPLAN projects, batch editing after
the copy; inbox 2026-11-05_eplan_plc_tocka_*, 2026-11-04_eplan_odstrani_artikel_funkcije,
2026-11-04_eplan_okence_plc_zasuk_variante_simbola): api/actions/live_copy.py.
Offline: the Python side (page names, rules, rename plan, validation) and the
generated C# (tokens filled, C# 5, guard on the target, dry run without guard).
"""

import inspect
import re

import pytest

from api.actions import live_copy as C, live_props as L, schematic as S
import api.actions as A

NEW = ("live_copy_pages", "live_copy_objects", "live_save_macro", "live_insert_page_macro",
       "live_rename_devices", "live_set_function_definition", "live_remove_function_articles",
       "live_set_symbol_variant", "live_list_functions", "live_check_connections")

PAGES = [["=AB-C+DI/200", "Circuit"], ["=AB-C+DI/201", "Circuit"], ["=AB-C+DI/202", "Circuit"],
         ["=AB-C+DI/210", "Circuit"], ["=AB-C+DO/300", "Circuit"], ["=AB-C+ET-1/902", "Overview"]]
ROWS = [["1/2/3/0", "=AB-C+DI-510K3", "=AB-C+DI-510K3", "box", "", "=AB-C+DI/201"],
        ["1/2/4/0", "=AB-C+DI-510K3:1", "=AB-C+DI-510K3", "", "", "=AB-C+DI/201"],
        ["1/2/5/0", "=AB-C+DI-K1", "=AB-C+DI-K1", "", "main", "=AB-C+DI/201"]]


def _code(cs):
    return "\n".join(line.split("//")[0] for line in cs.splitlines())


def _csharp5(cs):
    code = _code(cs)
    assert "?." not in code and '$"' not in code and "nameof(" not in code and "out var " not in code


def _no_tokens(cs):
    left = sorted(set(re.findall(r"\bV11[A-Z0-9]+\b", _code(cs))))
    assert not left, "unfilled tokens %s" % left


@pytest.fixture
def capture(monkeypatch):
    seen = {"scripts": []}

    def fake(script, timeout=30.0):
        seen["scripts"].append(script)
        if "class V11Inv_" in script:
            return {"success": True, "results": {"success": True, "sourcePages": PAGES, "sameProject": True,
                                                 "macroPages": [["=A+B/1", "Circuit"], ["=A+B/2", "Circuit"]],
                                                 "source": {"path": "C:\\x\\a.elk"}, "target": {"path": "C:\\x\\a.elk"}}}
        if "class V11ListFn_" in script:
            return {"success": True, "results": {"success": True, "rows": ROWS, "usedWithPrefix": ["K1", "K2"]}}
        return {"success": True, "results": {"success": True, "eplanMs": 5}}

    monkeypatch.setattr(C, "_execute_script", fake)
    monkeypatch.setattr(L, "_execute_script", fake)
    monkeypatch.setattr(S, "_execute_script", fake)
    return seen


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_registered_and_dry_run_default():
    for n in NEW:
        assert n in A.__all__ and n in C.__all__
        assert getattr(A, n) is getattr(C, n)
        sig = inspect.signature(getattr(C, n))
        if "dry_run" in sig.parameters:
            assert sig.parameters["dry_run"].default is True, n
        if "allow_real_project" in sig.parameters:
            assert sig.parameters["allow_real_project"].default is False, n
        assert "v11" in (getattr(C, n).__doc__ or "")


def test_existing_tools_untouched():
    for n in ("live_set_device_tags", "live_insert_window_macro", "live_move_placements", "live_set_texts"):
        assert n in A.__all__


# ---------------------------------------------------------------------------
# page names and rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,parts", [
    ("=AB-C+DI/201", {"fa": "", "pl": "AB-C", "inst": "", "lo": "DI", "doc": "", "us": "", "ct": "201"}),
    ("==F1=A1++I1+L1&EFS1#U/12a", {"fa": "F1", "pl": "A1", "inst": "I1", "lo": "L1", "doc": "EFS1", "us": "U", "ct": "12a"}),
    ("+ET-1/902", {"fa": "", "pl": "", "inst": "", "lo": "ET-1", "doc": "", "us": "", "ct": "902"}),
    ("/5", {"fa": "", "pl": "", "inst": "", "lo": "", "doc": "", "us": "", "ct": "5"}),
])
def test_split_join_page_name(name, parts):
    assert C.split_page_name(name) == parts
    assert C.join_page_name(parts) == name


@pytest.mark.parametrize("bad", ["=AB-C+DI", "", "=A/1/2", None])
def test_split_page_name_refuses(bad):
    with pytest.raises(C.SchematicValueError):
        C.split_page_name(bad)


def test_page_targets_rules():
    names = [p[0] for p in PAGES]
    src = ["=AB-C+DI/201", "=AB-C+DI/202"]
    assert C.page_targets(src, C._check_rule(None), names) == src
    assert C.page_targets(src, C._check_rule({"replace": [{"from": "+DI/", "to": "+DO/"}]}), names) == \
        ["=AB-C+DO/201", "=AB-C+DO/202"]
    assert C.page_targets(src, C._check_rule({"structure": "=AB-C+DX"}), names) == ["=AB-C+DX/201", "=AB-C+DX/202"]
    # next free after the highest counter of the (new) structure
    assert C.page_targets(src, C._check_rule({"replace": [{"from": "+DI/", "to": "+DO/"}], "next_free": True}), names) == \
        ["=AB-C+DO/301", "=AB-C+DO/302"]
    # same structure: 200, 201, 202, 210 used -> start 200 skips them
    assert C.page_targets(src, C._check_rule({"next_free": True, "start": 200}), names) == ["=AB-C+DI/203", "=AB-C+DI/204"]
    assert C.page_targets(src, C._check_rule({"next_free": True, "start": 400, "step": 10}), names) == \
        ["=AB-C+DI/400", "=AB-C+DI/410"]
    assert C.page_targets(src, C._check_rule({"map": {"=AB-C+DI/201": "=X/1"}, "structure": "=Y"}), names) == \
        ["=X/1", "=Y/202"]
    assert C.page_targets(src, C._check_rule("next_free"), names) == ["=AB-C+DI/211", "=AB-C+DI/212"]


@pytest.mark.parametrize("rule", [{"foo": 1}, {"replace": [{"from": "", "to": "x"}]}, {"structure": 5},
                                  {"map": {"a": "no-slash"}}, {"step": 0}, "bogus"])
def test_check_rule_refuses(rule):
    with pytest.raises(C.SchematicValueError):
        C._check_rule(rule)


def test_select_pages():
    names = [p[0] for p in PAGES]
    assert C.select_pages(names, pages=["=AB-C+DI/202"]) == ["=AB-C+DI/202"]
    assert C.select_pages(names, page_range=["=AB-C+DI/201", "=AB-C+DI/210"]) == \
        ["=AB-C+DI/201", "=AB-C+DI/202", "=AB-C+DI/210"]
    assert C.select_pages(names, page_range={"from": "=AB-C+DI/202", "to": "=AB-C+DI/200"}) == \
        ["=AB-C+DI/200", "=AB-C+DI/201", "=AB-C+DI/202"]
    assert C.select_pages(names, page_contains="+do/") == ["=AB-C+DO/300"]
    # different structures -> project order
    assert C.select_pages(names, page_range=["=AB-C+DI/210", "=AB-C+DO/300"]) == ["=AB-C+DI/210", "=AB-C+DO/300"]
    for kw in ({}, {"pages": ["x"], "page_contains": "y"}, {"pages": ["=NO/1"]}, {"page_contains": "zzz"},
               {"pages": ["=AB-C+DI/200", "=AB-C+DI/200"]}):
        with pytest.raises(C.SchematicValueError):
            C.select_pages(names, **kw)


# ---------------------------------------------------------------------------
# rename plan
# ---------------------------------------------------------------------------

def test_rename_plan_replace_box_first():
    plan = C.rename_plan(ROWS, replace=[{"from": "510K", "to": "610K"}])
    assert plan == {"=AB-C+DI-510K3": {"handle": "1/2/3/0", "functions": 2, "to": "=AB-C+DI-610K3"}}


def test_rename_plan_mapping_and_next_free():
    plan = C.rename_plan(ROWS, mapping={"510K3": "710K3"})
    assert plan["=AB-C+DI-510K3"]["to"] == "=AB-C+DI-710K3"
    plan = C.rename_plan(ROWS, mapping={"=AB-C+DI-K1": "=AB-C+DO-K9"})
    assert plan == {"=AB-C+DI-K1": {"handle": "1/2/5/0", "functions": 1, "to": "=AB-C+DO-K9"}}
    plan = C.rename_plan(ROWS, next_free={"prefix": "K", "start": 1, "digits": 0}, used_with_prefix=["K1", "K2", "k5"])
    assert plan["=AB-C+DI-510K3"]["to"] == "=AB-C+DI-K3"
    assert plan["=AB-C+DI-K1"]["to"] == "=AB-C+DI-K4"
    assert C.rename_plan(ROWS, replace=[{"from": "zzz", "to": "y"}]) == {}


# ---------------------------------------------------------------------------
# tools: validation + generated C#
# ---------------------------------------------------------------------------

def test_copy_pages_dry_run_plans_without_writing(capture):
    out = C.live_copy_pages(pages=["=AB-C+DI/201"], page_rename={"structure": "=AB-C+DO"})
    assert out["success"] and out["dryRun"] and out["plan"] == ["=AB-C+DI/201 -> =AB-C+DO/201"]
    assert len(capture["scripts"]) == 1 and "class V11Inv_" in capture["scripts"][0]
    assert "GuardScratch(" not in capture["scripts"][0].split("[Start]")[1]
    assert "timing" in out and "inventoryMs" in out["timing"]


def test_copy_pages_refuses_existing_targets(capture):
    out = C.live_copy_pages(pages=["=AB-C+DI/201"], dry_run=False)
    assert not out["success"] and out["errorType"] == "TARGET_EXISTS"
    assert len(capture["scripts"]) == 1


def test_copy_pages_real_script(capture):
    out = C.live_copy_pages(page_range=["=AB-C+DI/200", "=AB-C+DI/202"],
                            page_rename={"replace": [{"from": "+DI/", "to": "+DO/"}], "next_free": True},
                            devices="number", dry_run=False, allow_real_project=True)
    assert out["success"], out
    cs = capture["scripts"][-1]
    body = cs.split("[Start]")[1]
    assert "class V11CopyPages_" in cs
    _no_tokens(body)
    _csharp5(cs)
    assert "GuardScratch(tproject, true" in body
    assert '"Number"' in body and '"=AB-C+DO/301"' in body and '"=AB-C+DO/303"' in body
    assert '"CopyTo"' in body and "PageMacro" in body


@pytest.mark.parametrize("kw", [
    {"pages": ["=AB-C+DI/201"], "devices": "renumber"},
    {"pages": ["=AB-C+DI/201"], "route": "clipboard"},
    {"pages": ["=AB-C+DI/201"], "source_project": "C:\\nope\\x.elk"},
    {"pages": ["=AB-C+DI/201"], "source_project": "relative.elk"},
    {"pages": ["=AB-C+DI/201"], "device_replace": [{"from": "", "to": "x"}]},
    {},
])
def test_copy_pages_validation(capture, kw):
    out = C.live_copy_pages(**kw)
    assert not out["success"]
    assert not capture["scripts"]


def test_copy_objects_dry_and_real(capture):
    out = C.live_copy_objects("=AB-C+ET-1/902", device_tags=["510S1"], dx=46, copies=3)
    assert out["success"] and out["dryRun"]
    body = capture["scripts"][-1].split("[Start]")[1]
    _no_tokens(body)
    assert "if (!true && refused == null)" in body          # dry run: the guarded branch never runs
    out = C.live_copy_objects("=AB-C+ET-1/902", region=[0, 0, 100, 100], to_x=10, to_y=20,
                              target_page="=AB-C+ET-1/903", numbering="number", dry_run=False)
    cs = capture["scripts"][-1]
    _csharp5(cs)
    body = cs.split("[Start]")[1]
    assert "GuardScratch(tproject, false" in body and '"Number"' in body and "V11InsertWindowMacro" in body


@pytest.mark.parametrize("kw", [
    {},                                                  # no selector
    {"handles": ["1/2/3/0"], "region": [0, 0, 1, 1]},     # two selectors
    {"handles": ["1/2/3/0"]},                            # same page without offset
    {"handles": ["1/2/3/0"], "dx": 1, "to_x": 1, "to_y": 2},
    {"handles": ["1/2/3/0"], "dx": 1, "copies": 2, "step_dx": 0, "step_dy": 0},
    {"handles": ["1/2/3/0"], "dx": 1, "route": "placement", "numbering": "number"},
    {"handles": ["1/2/3/0"], "dx": 1, "numbering": "ignore"},
    {"handles": ["1/2/3/0"], "dx": 1, "copies": 2, "device_replace_per_copy": [[{"from": "a", "to": "b"}]]},
    {"region": [0, 0, 0, 5], "dx": 1},
])
def test_copy_objects_validation(capture, kw):
    out = C.live_copy_objects("=AB-C+ET-1/902", **kw)
    assert not out["success"], kw
    assert not capture["scripts"]


def test_save_macro(capture, tmp_path):
    ema = str(tmp_path / "okno.ema")
    out = C.live_save_macro(ema, page="=P/1", device_tags=["K1"], description="sl_SI@x;")
    assert out["success"]
    body = capture["scripts"][-1].split("[Start]")[1]
    _no_tokens(body)
    assert "V11WriteWindowMacro" in body and "GuardScratch(" not in body
    emp = str(tmp_path / "strani.emp")
    assert C.live_save_macro(emp, pages=["=P/1", "=P/2"])["success"]
    assert "PageMacro" in capture["scripts"][-1].split("[Start]")[1]
    assert not C.live_save_macro(emp, page="=P/1", whole_page=True)["success"]      # .emp needs pages
    assert not C.live_save_macro(str(tmp_path / "x.txt"), page="=P/1", whole_page=True)["success"]
    open(ema, "w").close()
    assert not C.live_save_macro(ema, page="=P/1", whole_page=True)["success"]     # exists


def test_insert_page_macro(capture, tmp_path):
    emp = tmp_path / "m.emp"
    emp.write_text("x")
    out = C.live_insert_page_macro(str(emp), page_rename={"structure": "=AB-C+DX"})
    assert out["success"] and out["plan"] == ["=A+B/1 -> =AB-C+DX/1", "=A+B/2 -> =AB-C+DX/2"]
    out = C.live_insert_page_macro(str(emp), page_rename={"structure": "=AB-C+DX"}, dry_run=False)
    body = capture["scripts"][-1].split("[Start]")[1]
    _no_tokens(body)
    assert "GuardScratch(tproject, false" in body and '"Ignore"' in body
    assert not C.live_insert_page_macro(str(tmp_path / "none.emp"))["success"]


def test_rename_devices(capture):
    out = C.live_rename_devices(pages=["=AB-C+DI/201"], replace=[{"from": "510K", "to": "610K"}])
    assert out["success"] and out["mapping"] == ["=AB-C+DI-510K3 -> =AB-C+DI-610K3 (2)"]
    tags = capture["scripts"][-1]
    assert "class SetTags_" in tags and '"=AB-C+DI-610K3"' in tags and '"1/2/3/0"' in tags
    assert not C.live_rename_devices(pages=["=P/1"])["success"]          # no rule


def test_rename_devices_merge_refused(capture):
    out = C.live_rename_devices(mapping={"510K3": "X1", "K1": "X1"})
    assert not out["success"] and out["errorType"] == "MERGE"


def test_set_function_definition(capture):
    out = C.live_set_function_definition(items=[{"handle": "1/2/3/0", "like_handle": "1/2/4/0"}])
    assert out["success"] and out["dryRun"]
    body = capture["scripts"][-1].split("[Start]")[1]
    _no_tokens(body)
    assert "GuardScratch(project, false" in body          # in the !dry branch
    out = C.live_set_function_definition(handles=["1/2/3/0"], category="PLCTerminal", group=1, id=2, dry_run=False)
    assert "\"PLCTerminal\"" in capture["scripts"][-1]
    for kw in ({"handles": ["h"]}, {"handles": ["h"], "like_handle": "a", "definition_name": "b"},
               {"handles": ["h"], "category": "X"}, {"handles": ["h"], "category": "a b", "group": 1, "id": 1},
               {"items": [{"handle": "h", "foo": 1}]}):
        assert not C.live_set_function_definition(**kw)["success"], kw


def test_remove_articles(capture):
    out = C.live_remove_function_articles(handle="1/2/3/0", part_numbers=["SIE.X"])
    assert out["success"]
    _no_tokens(capture["scripts"][-1].split("[Start]")[1])
    assert not C.live_remove_function_articles(handle="h")["success"]
    assert not C.live_remove_function_articles(handle="h", part_numbers=["a"], all_articles=True)["success"]


def test_symbol_variant(capture):
    assert C.live_set_symbol_variant(handles=["1/2/3/0"], variant=6)["success"]
    body = capture["scripts"][-1].split("[Start]")[1]
    _no_tokens(body)
    assert "SetSymbolVariant" in body
    assert not C.live_set_symbol_variant(handles=["h"])["success"]
    assert not C.live_set_symbol_variant(handles=["h"], variant=16)["success"]


def test_list_functions_columns(capture):
    out = C.live_list_functions(page_contains="+ET-1/", fields=["handle", "conn"],
                                properties=["FUNC_PLCADDRESS", "FUNC_CONNECTIONDESIGNATION[1]"])
    assert out["success"] and out["columns"] == ["handle", "conn", "FUNC_PLCADDRESS", "FUNC_CONNECTIONDESIGNATION[1]"]
    _no_tokens(capture["scripts"][-1].split("[Start]")[1])
    assert not C.live_list_functions(fields=["nope"])["success"]
    assert not C.live_list_functions(page_kind="x")["success"]


def test_list_functions_display_text(capture, monkeypatch):
    def fake(script, timeout=30.0):
        return {"success": True, "results": {"success": True, "rows": [["h", "en_US@In 1;de_DE@E 1;"]], "matched": 1}}
    monkeypatch.setattr(C, "_execute_script", fake)
    out = C.live_list_functions(fields=["handle"], properties=["FUNC_TEXT"])
    assert out["rows"] == [["h", "In 1"]] and out["truncated"] is False


def test_check_connections(capture):
    out = C.live_check_connections(page_contains="+ET")
    assert out["success"]
    body = capture["scripts"][-1].split("[Start]")[1]
    _no_tokens(body)
    assert "GuardScratch(" not in body
    assert not C.live_check_connections(checks=["nope"])["success"]

@pytest.mark.parametrize("old,new,tag", [
    ("=AB-C+DI-510K3", "=AB-C+DI-610K3", "=AB-C+DI-610K3"),
    ("510K3", "610K3", "610K3"),
    ("ET-1.3", "ET-1.4", "1.4"),
    ("=AB-C+ET-2-ET-2.6", "=AB-C+ET-2-ET-2.7", "=AB-C+ET-2-ET-2.7"),
    ("-K1", "-K2", "K2"),
    ("ET-1.3", "EX-1.3", None),
])
def test_tag_for(old, new, tag):
    assert C.tag_for(old, new) == tag


def test_rename_devices_dash_tags(capture, monkeypatch):
    rows = [["1/2/3/0", "ET-1.3", "ET-1.3", "box", "", "=P/1"], ["1/2/4/0", "ET-1.3:1", "ET-1.3", "", "", "=P/1"]]
    real = capture

    def fake(script, timeout=30.0):
        real["scripts"].append(script)
        if "class V11ListFn_" in script:
            return {"success": True, "results": {"success": True, "rows": rows}}
        return {"success": True, "results": {"success": True}}
    monkeypatch.setattr(C, "_execute_script", fake)
    monkeypatch.setattr(L, "_execute_script", fake)
    out = C.live_rename_devices(pages=["=P/1"], replace=[{"from": "ET-1.3", "to": "ET-1.4"}])
    assert out["success"] and '"1.4"' in real["scripts"][-1]
    out = C.live_rename_devices(pages=["=P/1"], replace=[{"from": "ET-1.3", "to": "EX-1.3"}])
    assert not out["success"] and out["errorType"] == "BAD_TAG"