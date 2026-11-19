"""
Generated-C# assertions for the parts-database tools.

Both tools were completely non-functional and BOTH failed silently:

  parts_db_query    returned a list of empty dicts with success:true, because it
                    looked every property up on `part.Properties` - where none
                    of its own default property names exist.
  parts_db_get_part could only ever return "Timeout waiting for script results",
                    first because it referenced a member MDPart does not have
                    (CS1061 - a compile error reaches the caller only as a
                    timeout), and then because it put live EPLAN objects into the
                    dictionary that gets JSON-serialised at the end of the script.

Neither failure mode is visible from the Python side, which is why these tests
assert on the generated C# text instead. They run with EPLAN closed.
"""

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(os.path.dirname(HERE), "mcp_server")
for p in (MCP, os.path.join(MCP, "api")):
    if p not in sys.path:
        sys.path.insert(0, p)

from api.actions import scripted  # noqa: E402


@pytest.fixture
def capture(monkeypatch):
    """Capture the C# a tool generates, without executing anything."""
    seen = {}

    def fake(script, timeout=30.0):
        seen["cs"] = script
        seen["timeout"] = timeout
        return {"success": True, "results": {}}

    monkeypatch.setattr(scripted, "_execute_script", fake)
    return seen


# ---------------------------------------------------------------------------
# parts_db_get_part
# ---------------------------------------------------------------------------

def test_get_part_does_not_reference_the_nonexistent_producttopgroup(capture):
    """
    MDPart has no `ProductTopGroup`. Reflecting over MDPart on 2027.0.1 lists
    `ProductTopGroup GenericProductGroup` - the TYPE is ProductTopGroup, the
    MEMBER is GenericProductGroup. Referencing the type name as a member gave
    CS1061 and the tool never ran.
    """
    scripted.parts_db_get_part("X")
    cs = capture["cs"]
    assert "ProductTopGroup" not in cs.replace("Enums+ProductTopGroup", ""), (
        "MDPart.ProductTopGroup does not exist; this is the CS1061 that made "
        "the tool return only timeouts"
    )
    # v3: members are read by reflection, by name.
    assert '"GenericProductGroup"' in cs


def test_get_part_stringifies_every_value_before_serialising(capture):
    """
    props.ARTICLE_* returns MDPropertyValue - a live EPLAN object. Putting one
    in the results dictionary sends JsonConvert.SerializeObject walking a native
    object graph, and the script never finishes writing its result file.
    """
    scripted.parts_db_get_part("X")
    cs = capture["cs"]
    raw = re.findall(r"props\.(ARTICLE_\w+)\s*\?\?", cs)
    assert not raw, (
        "these ARTICLE_* values reach the results dict as EPLAN objects rather "
        "than strings, which hangs the JSON serialiser: %s" % raw
    )
    # v3: every value goes through ReadPartProperty, which returns Str(...).
    assert "partDict[outKeys[i]] = ReadPartProperty(part, srcNames[i]);" in cs
    assert "return Str(listProp.GetValue(pl, null));" in cs
    for member in ("ARTICLE_PARTNR", "ARTICLE_DESCR1", "ARTICLE_MANUFACTURER"):
        assert '"%s"' % member in cs, "%s must be read" % member


def test_get_part_defines_the_str_helper_it_uses(capture):
    scripted.parts_db_get_part("X")
    cs = capture["cs"]
    assert "static string Str(object value)" in cs


def test_get_part_escapes_the_part_number(capture):
    scripted.parts_db_get_part('weird" \\ value')
    cs = capture["cs"]
    assert 'weird\\" \\\\ value' in cs, "part_number must go through cs_escape"


def test_get_part_reports_not_found_rather_than_erroring(capture):
    scripted.parts_db_get_part("X")
    cs = capture["cs"]
    assert '"found"' in cs.replace("[", "").replace("]", "") or "found" in cs


# ---------------------------------------------------------------------------
# parts_db_query
# ---------------------------------------------------------------------------

def test_query_does_not_look_defaults_up_on_the_property_list(capture):
    """
    The regression: `part.Properties.GetType().GetProperty("PartNr")` returns
    null, because PartNr is a member of MDPart, not of
    MDPartsDatabaseItemPropertyList. The old code turned that null into "",
    so every part came back as an empty dict with success:true.
    """
    scripted.parts_db_query(limit=5)
    cs = capture["cs"]
    assert "part.Properties.GetType().GetProperty(propName)" not in cs, (
        "resolving caller property names against part.Properties alone is the "
        "bug that produced empty dicts"
    )
    assert "ReadPartProperty(part, propName)" in cs


def test_query_resolves_against_mdpart_first_then_the_property_list(capture):
    scripted.parts_db_query(limit=5)
    cs = capture["cs"]
    assert "FindUnambiguous(part.GetType(), propName)" in cs  # v3: no typeof(MDPart)
    assert "FindUnambiguous(pl.GetType(), articleName)" in cs


def test_query_uses_a_declaredonly_walk_to_dodge_ambiguousmatch(capture):
    """
    MDPart declares `Properties` twice and ARTICLE_PARTNR has a plain and an
    indexed form, so GetProperty(name) throws AmbiguousMatchException. The walk
    must ask for the non-indexed declaration, one level at a time.
    """
    scripted.parts_db_query(limit=5)
    cs = capture["cs"]
    assert "BindingFlags.DeclaredOnly" in cs
    assert "Type.EmptyTypes" in cs


def test_query_maps_friendly_names_to_article_fields(capture):
    scripted.parts_db_query(limit=5)
    cs = capture["cs"]
    for friendly, article in (
        ("Description1", "ARTICLE_DESCR1"),
        ("Manufacturer", "ARTICLE_MANUFACTURER"),
        ("OrderNr", "ARTICLE_ORDERNR"),
    ):
        assert 'm["%s"] = "%s";' % (friendly, article) in cs


def test_query_reports_an_unknown_property_instead_of_blanking_it(capture):
    scripted.parts_db_query(limit=5)
    cs = capture["cs"]
    assert "MissingMemberException" in cs, (
        "an unresolvable property name must say so - a silent empty string is "
        "indistinguishable from a part that genuinely has no value"
    )
    assert '"<error: "' in cs


def test_query_limit_must_be_an_integer():
    """limit is interpolated outside a string literal, so it is code."""
    bad = scripted.parts_db_query(limit="5; DROP")
    assert bad["success"] is False
    assert "limit" in bad["error"].lower()


def test_query_rejects_a_non_identifier_filter_property():
    bad = scripted.parts_db_query(filter_property="a b", filter_value="x")
    assert bad["success"] is False


def test_query_filter_value_is_escaped(capture):
    scripted.parts_db_query(filter_property="PartNr", filter_value='x" || true')
    cs = capture["cs"]
    assert 'x\\" || true' in cs


def test_query_declares_reflection_namespace(capture):
    """The helper uses PropertyInfo/BindingFlags, so the using must be there."""
    scripted.parts_db_query(limit=1)
    assert "using System.Reflection;" in capture["cs"]


# ---------------------------------------------------------------------------
# v3: no compile-time reference to Eplan.EplApi.MasterData (CS0234 on 2.9)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda: scripted.parts_db_query(limit=5),
    lambda: scripted.parts_db_query(filter_property="PartNr", filter_value="SIE", limit=5),
    lambda: scripted.parts_db_count(),
    lambda: scripted.parts_db_count(filter_property="PartNr", filter_value="SIE"),
    lambda: scripted.parts_db_get_part("SIE.6ES7131-6BF01-0BA0"),
    lambda: scripted.parts_db_list_product_groups(),
], ids=["query", "query_filtered", "count", "count_filtered", "get_part", "groups"])
def test_read_scripts_do_not_reference_masterdata_at_compile_time(capture, call):
    call()
    cs = capture["cs"]
    assert "using Eplan.EplApi.MasterData" not in cs
    assert "MDPartsManagement()" not in cs, "no statically typed constructor"
    assert "MDPart part" not in cs and "(MDPart)" not in cs and "typeof(MD" not in cs
    assert 'FindMdType("Eplan.EplApi.MasterData.' in cs
    assert '"Eplan.EplApi.MasterDatau"' in cs, "2.9 assembly name must be a load candidate"
    assert "Newtonsoft.Json.JsonConvert.SerializeObject(results)" in cs


def test_read_scripts_close_the_database(capture):
    scripted.parts_db_query(limit=1)
    assert "finally { if (db != null) CloseDb(db); }" in capture["cs"]


def test_query_filter_property_checked_at_runtime(capture):
    scripted.parts_db_query(filter_property="PartNr", filter_value="SIE", limit=3)
    cs = capture["cs"]
    assert 'string filterProp = "PartNr";' in cs
    assert 'string filterValue = "SIE";' in cs
    assert "int limit = 3;" in cs


def test_read_scripts_compile_as_portable_on_29(capture):
    """EPLAN 2.9 has no Newtonsoft in the script compiler: cs_json must convert."""
    from cs_json import prepare_script
    scripted.parts_db_get_part("X")
    out = prepare_script(capture["cs"], "2.9")
    assert "Newtonsoft" not in out
    assert "_McpJson(results)" in out
