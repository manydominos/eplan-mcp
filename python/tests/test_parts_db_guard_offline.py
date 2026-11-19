"""
The live article writers must never open a parts database EPLAN cannot open.

Field case (EPLAN 2.9 64-bit, no 64-bit Office/ACE): OpenDatabase() made EPLAN
show the modal "...ESS_part001.mdb database cannot be opened. Microsoft Office
64 bits is required ..." and blocked. live_set_function_properties
(replace_article), live_add_function_articles and live_remove_function_articles
therefore run parts_db_reflect's preflight first; it never opens the database.
Offline: no EPLAN; the preflight script's answer is simulated.

The parts_db_* tools themselves are upstream's and unchanged.
"""

import pytest

from api.actions import live_copy, live_props, parts_db_reflect, scripted

pytestmark = pytest.mark.parts_guard

MDB = r"C:\Users\Public\EPLAN\Data\Parts\Company name\ESS_part001.mdb"


def _ace(available12=False, available16=False):
    return [
        {"progId": "Microsoft.ACE.OLEDB.12.0", "available": available12, "registered": available12},
        {"progId": "Microsoft.ACE.OLEDB.16.0", "available": available16, "registered": available16},
    ]


def _preflight(**info):
    base = {"success": True, "is64BitProcess": True, "database": MDB,
            "databaseFileExists": True, "aceProviders": _ace()}
    base.update(info)
    return {"success": True, "results": base}


@pytest.fixture
def eplan(monkeypatch):
    """Fake script runners: answer the preflight, record every other script."""
    state = {"preflight": _preflight(), "scripts": [], "preflights": 0}

    def fake(script, timeout=30.0, label=None):
        if "PartsPreflight_" in script:
            state["preflights"] += 1
            return state["preflight"]
        state["scripts"].append(script)
        return {"success": True, "results": {"success": True, "found": True, "items": [], "details": []}}

    monkeypatch.setattr(scripted, "_execute_script", fake)
    monkeypatch.setattr(live_props, "_execute_script", fake)
    monkeypatch.setattr(live_copy, "_execute_script", fake)
    monkeypatch.setitem(parts_db_reflect._preflight_cache, "verdict", None)
    monkeypatch.setitem(parts_db_reflect._preflight_cache, "at", 0.0)
    return state


ART = {"from": "SIE.6ES7155-6BA00-0CN0", "to": "SIE.6ES7155-6BA01-0CN0"}

ALL_PATHS = [
    ("get_part", lambda **k: parts_db_reflect.get_part("SIE.6ES7131-6BF01-0BA0", **k)),
    ("replace_article", lambda **k: live_props.live_set_function_properties(
        handle="h", replace_article=ART, allow_real_project=True, **k)),
    ("add_articles", lambda **k: live_props.live_add_function_articles(
        articles=["SIE.6ES7131-6BF01-0BA0"], handle="h", dry_run=False, allow_real_project=True, **k)),
    ("remove_articles", lambda **k: live_copy.live_remove_function_articles(
        handle="h", part_numbers=["SIE.6ES7131-6BF01-0BA0"], dry_run=False, allow_real_project=True, **k)),
]
IDS = [n for n, _ in ALL_PATHS]


@pytest.mark.parametrize("call", [c for _, c in ALL_PATHS], ids=IDS)
def test_access_without_ace_is_refused_and_nothing_opens(eplan, call):
    out = call()
    assert out["success"] is False
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert out["partsDatabase"] == MDB
    assert "Microsoft Office 64 bits" in out["reason"]
    assert eplan["scripts"] == [], "nothing may reach EPLAN"


@pytest.mark.parametrize("call", [c for _, c in ALL_PATHS], ids=IDS)
def test_unavailable_is_refused_even_with_allow_unverified(eplan, call):
    out = call(allow_unverified_db=True)
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert eplan["scripts"] == []


@pytest.mark.parametrize("call", [c for _, c in ALL_PATHS], ids=IDS)
def test_access_with_64bit_ace_runs(eplan, call):
    eplan["preflight"] = _preflight(aceProviders=_ace(available16=True))
    call()
    assert eplan["scripts"], "the guarded script must run"


def test_get_part_script_has_no_compile_time_masterdata(eplan):
    eplan["preflight"] = _preflight(aceProviders=_ace(available16=True))
    parts_db_reflect.get_part("X")
    cs = eplan["scripts"][0]
    assert "using Eplan.EplApi.MasterData" not in cs
    assert 'FindMdType("Eplan.EplApi.MasterData.' in cs
    assert '"Eplan.EplApi.MasterDatau"' in cs, "2.9 assembly name must be a load candidate"
    assert '"GenericProductGroup"' in cs and "ProductTopGroup" not in cs.replace("Enums+ProductTopGroup", "")


def test_article_in_db_answers(eplan):
    eplan["preflight"] = _preflight(aceProviders=_ace(available16=True))
    assert parts_db_reflect.article_in_db("X") is True
    eplan["preflight"] = _preflight()
    parts_db_reflect._preflight_cache["verdict"] = None
    assert parts_db_reflect.article_in_db("X") is None, "refused = undecided"


def test_sql_database_runs_without_ace(eplan):
    eplan["preflight"] = _preflight(database="Data Source=SRV\\EPLAN;Initial Catalog=Parts;")
    assert parts_db_reflect.parts_db_guard() is None


def test_missing_access_file_is_unavailable(eplan):
    eplan["preflight"] = _preflight(databaseFileExists=False, aceProviders=_ace(available16=True))
    out = parts_db_reflect.get_part("X")
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert "does not exist" in out["reason"]


def test_pathmap_resolved_name_is_used(eplan):
    eplan["preflight"] = _preflight(database="$(MD_PARTS)\\Company name\\ESS_part001.mdb",
                                    databaseResolved=MDB)
    out = parts_db_reflect.get_part("X")
    assert out["partsDatabase"] == MDB
    assert out["preflight"]["databaseSetting"].startswith("$(MD_PARTS)")


def test_undecidable_is_refused_by_default_and_allowed_on_request(eplan):
    eplan["preflight"] = {"success": True, "results": {
        "success": True, "databaseError": "Exception: PartsService not found", "aceProviders": _ace()}}
    out = parts_db_reflect.get_part("X")
    assert out["errorType"] == "PARTS_DB_UNVERIFIED"
    assert "allow_unverified_db" in out["hint"]
    assert "EplanBlockedByDialog" in out["hint"]
    assert eplan["scripts"] == []
    out = parts_db_reflect.get_part("X", allow_unverified_db=True)
    assert out["success"] is True and len(eplan["scripts"]) == 1


def test_preflight_without_result_is_unverified(eplan):
    eplan["preflight"] = {"success": False, "errorType": "McpScriptNoResult",
                          "message": "Timeout waiting for script results"}
    out = parts_db_reflect.get_part("X")
    assert out["errorType"] == "PARTS_DB_UNVERIFIED"
    assert out["preflight"]["preflight"]["errorType"] == "McpScriptNoResult"


def test_not_connected_returns_the_usual_message(eplan):
    eplan["preflight"] = {"success": False,
                          "message": "Not connected to EPLAN. Call eplan_connect() first."}
    out = parts_db_reflect.get_part("X")
    assert out == eplan["preflight"]
    assert eplan["scripts"] == []


def test_definitive_verdict_is_cached(eplan):
    parts_db_reflect.parts_db_guard()
    parts_db_reflect.parts_db_guard()
    assert eplan["preflights"] == 1


def test_unknown_verdict_is_not_cached(eplan):
    eplan["preflight"] = {"success": False, "message": "Timeout waiting for script results"}
    parts_db_reflect.parts_db_guard()
    parts_db_reflect.parts_db_guard()
    assert eplan["preflights"] == 2


def test_32bit_process_is_unverified():
    v = parts_db_reflect.classify_parts_db(_preflight(is64BitProcess=False))
    assert v["verdict"] == "unknown"


def test_alk_database_needs_no_ace():
    v = parts_db_reflect.classify_parts_db(_preflight(database=r"C:\P\Database.alk"))
    assert v["verdict"] == "ok" and v["kind"] == "eplan"


def test_registered_but_dll_missing_is_not_available():
    prov = [{"progId": "Microsoft.ACE.OLEDB.16.0", "registered": True, "dllExists": False,
             "available": False}]
    v = parts_db_reflect.classify_parts_db(_preflight(aceProviders=prov))
    assert v["verdict"] == "unavailable"


# ---------------------------------------------------------------------------
# The preflight script itself
# ---------------------------------------------------------------------------

def test_preflight_never_touches_masterdata():
    cs = parts_db_reflect.preflight_script()
    assert "MasterData" not in cs
    assert "MDPartsManagement" not in cs
    assert "OpenDatabase" not in cs
    assert '"Eplan.EplApi.HEServices.PartsService"' in cs
    assert '"PartsDatabase"' in cs


def test_preflight_checks_clsid_in_the_process_registry_view():
    cs = parts_db_reflect.preflight_script()
    assert "RegistryView.Registry64" in cs
    assert "InprocServer32" in cs
    assert '"Microsoft.ACE.OLEDB.12.0"' in cs and '"Microsoft.ACE.OLEDB.16.0"' in cs


def test_preflight_has_unique_class_and_portable_json():
    from cs_json import prepare_script
    a, b = parts_db_reflect.preflight_script(), parts_db_reflect.preflight_script()
    assert a != b
    out = prepare_script(a, "2.9")
    assert "Newtonsoft" not in out and "_McpJson(results)" in out


def test_upstream_parts_db_tools_are_unchanged():
    """The guard is only for the live writers; the parts_db_* tools keep their signatures."""
    import inspect
    for f in (scripted.parts_db_query, scripted.parts_db_count, scripted.parts_db_get_part,
              scripted.parts_db_create, scripted.parts_db_update,
              scripted.parts_db_list_product_groups):
        assert "allow_unverified_db" not in inspect.signature(f).parameters, f.__name__
