"""
v4: parts_db_* must never open a parts database EPLAN cannot open.

Field case 2026-10-28 (EPLAN 2.9 64-bit, no 64-bit Office/ACE): OpenDatabase()
made EPLAN show the modal "...ESS_part001.mdb database cannot be opened.
Microsoft Office 64 bits is required ..." and the MCP server hung. Offline: no
EPLAN; the preflight script's answer is simulated.
"""

import pytest

from api.actions import scripted, parts_db_reflect

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
    """Fake _execute_script: answers the preflight, records every other script."""
    state = {"preflight": _preflight(), "scripts": [], "preflights": 0}

    def fake(script, timeout=30.0):
        if "PartsPreflight_" in script:
            state["preflights"] += 1
            return state["preflight"]
        state["scripts"].append(script)
        return {"success": True, "results": {"success": True, "count": 0}}

    monkeypatch.setattr(scripted, "_execute_script", fake)
    monkeypatch.setitem(scripted._parts_db_preflight_cache, "verdict", None)
    monkeypatch.setitem(scripted._parts_db_preflight_cache, "at", 0.0)
    return state


ALL_TOOLS = [
    ("query", lambda **k: scripted.parts_db_query(limit=5, **k)),
    ("count", lambda **k: scripted.parts_db_count(**k)),
    ("get_part", lambda **k: scripted.parts_db_get_part("SIE.6ES7131-6BF01-0BA0", **k)),
    ("groups", lambda **k: scripted.parts_db_list_product_groups(**k)),
    ("create", lambda **k: scripted.parts_db_create("PN-1", {"ARTICLE_DESCR1": "x"}, **k)),
    ("update", lambda **k: scripted.parts_db_update("PN-1", "ARTICLE_DESCR1", "x", **k)),
]
IDS = [n for n, _ in ALL_TOOLS]


@pytest.mark.parametrize("call", [c for _, c in ALL_TOOLS], ids=IDS)
def test_access_without_ace_is_refused_and_nothing_opens(eplan, call):
    out = call()
    assert out["success"] is False
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert out["partsDatabase"] == MDB
    assert "Microsoft Office 64 bits" in out["reason"]
    assert eplan["scripts"] == [], "no MDPartsManagement script may run"


@pytest.mark.parametrize("call", [c for _, c in ALL_TOOLS], ids=IDS)
def test_unavailable_is_refused_even_with_allow_unverified(eplan, call):
    out = call(allow_unverified_db=True)
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert eplan["scripts"] == []


@pytest.mark.parametrize("call", [c for _, c in ALL_TOOLS], ids=IDS)
def test_access_with_64bit_ace_runs(eplan, call):
    eplan["preflight"] = _preflight(aceProviders=_ace(available16=True))
    out = call()
    assert out["success"] is True
    assert len(eplan["scripts"]) == 1


def test_sql_database_runs_without_ace(eplan):
    eplan["preflight"] = _preflight(database="Data Source=SRV\\EPLAN;Initial Catalog=Parts;")
    assert scripted.parts_db_count()["success"] is True
    assert len(eplan["scripts"]) == 1


def test_missing_access_file_is_unavailable(eplan):
    eplan["preflight"] = _preflight(databaseFileExists=False, aceProviders=_ace(True, True))
    out = scripted.parts_db_count()
    assert out["errorType"] == "PARTS_DB_UNAVAILABLE"
    assert "does not exist" in out["reason"]


def test_pathmap_resolved_name_is_used(eplan):
    eplan["preflight"] = _preflight(database="$(MD_PARTS)\\Company name\\ESS_part001.mdb",
                                    databaseResolved=MDB)
    out = scripted.parts_db_count()
    assert out["partsDatabase"] == MDB
    assert out["preflight"]["databaseSetting"].startswith("$(MD_PARTS)")


def test_undecidable_is_refused_by_default_and_allowed_on_request(eplan):
    eplan["preflight"] = {"success": True, "results": {
        "success": True, "databaseError": "Exception: PartsService not found", "aceProviders": _ace()}}
    out = scripted.parts_db_query(limit=1)
    assert out["errorType"] == "PARTS_DB_UNVERIFIED"
    assert "allow_unverified_db" in out["hint"]
    assert eplan["scripts"] == []
    out = scripted.parts_db_query(limit=1, allow_unverified_db=True)
    assert out["success"] is True and len(eplan["scripts"]) == 1


def test_preflight_without_result_is_unverified(eplan):
    eplan["preflight"] = {"success": False, "errorType": "McpScriptNoResult",
                          "message": "Timeout waiting for script results"}
    out = scripted.parts_db_get_part("X")
    assert out["errorType"] == "PARTS_DB_UNVERIFIED"
    assert out["preflight"]["preflight"]["errorType"] == "McpScriptNoResult"


def test_not_connected_returns_the_usual_message(eplan):
    eplan["preflight"] = {"success": False,
                          "message": "Not connected to EPLAN. Call eplan_connect() first."}
    out = scripted.parts_db_count()
    assert out == eplan["preflight"]
    assert eplan["scripts"] == []


def test_definitive_verdict_is_cached(eplan):
    scripted.parts_db_count()
    scripted.parts_db_query(limit=1)
    assert eplan["preflights"] == 1


def test_unknown_verdict_is_not_cached(eplan):
    eplan["preflight"] = {"success": False, "message": "Timeout waiting for script results"}
    scripted.parts_db_count()
    scripted.parts_db_count()
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


def test_tool_docstrings_name_the_parameter():
    for f in (scripted.parts_db_query, scripted.parts_db_count, scripted.parts_db_get_part,
              scripted.parts_db_create, scripted.parts_db_update,
              scripted.parts_db_list_product_groups):
        assert "allow_unverified_db" in f.__doc__, f.__name__
