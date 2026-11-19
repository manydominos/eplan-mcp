"""
Function properties and window macros in the live object model (EPLAN MCP v3).

Added for a project in EPLAN P8 2.9, notes
inbox/2026-10-27_eplan_*:

    live_read_function_properties   M-1  read FunctionPropertyList values of
                                         functions selected by name / page /
                                         connection point / handle (READ-ONLY)
    live_set_function_properties    M-6  write FunctionPropertyList values (PLC
                                         address, channel, function text, ...)
                                         and optionally swap an article
                                         reference (WRITE, scratch-guarded)
    live_insert_window_macro        M-5  Insert.WindowMacro(.ema) on a page at a
                                         coordinate (WRITE, scratch-guarded)

Same technique as live.py and schematic.py: runtime reflection inside the
standard _script scaffold (no `using Eplan.EplApi.DataModel;` - CS0234 on the
script engine), C# 5 only, every caller value through cs_escape / the
schematic_model validators, results through schematic._shape.

Why a selector richer than "exact name": the connection points of one PLC card
share ONE device tag (16 functions all named "...-510K3"), so name + limit=1 -
what live_set_function_text does - cannot address "510K3 point 9". These tools
select by handle (from live_read_function_properties / live_read_page) or by
name + connection point designation (+ page), and a write that matches more
functions than `limit` is refused as a whole rather than applied partially.

The device tag (BMK) is deliberately NOT written by live_set_function_properties
- live_set_device_tag already does that with its merge guard. v5 adds
live_set_device_tags: device tags (and the connection point designation) of
PLC connection points, PLC boxes and every other Function subclass, one by
handle or many in one call, validated before the first write, verified and
rolled back on error (inbox 2026-10-28_eplan_bmk_plc_prikljucka_in_skatle.md).

API names used on EPLAN 2.9 (eplan.help API 2.9):
    FunctionPropertyList.FUNC_PLCADDRESS                     #20400
    FunctionPropertyList.FUNC_PLCCHANNEL_DESIGNATION_MANUAL  #20407
    FunctionPropertyList.FUNC_PLCCHANNEL_DESIGNATION_CALCULATED #20436
    Insert.WindowMacro(string, int, Page, PointD, Insert.MoveKind) -> StorableObject[]
      ("Representation type is taken from given page.")
None of this was run against a live 2.9 here; every lookup that can miss
throws with what the type really declares, so the first live run documents
itself (MemberList / PropNamesContaining).
"""

import os
import re

from ._base import cs_escape
from .live import _script
from .scripted import _execute_script
from . import scripted as _scripted
from .schematic import _HELPERS_SCHEMATIC, _guard_prelude, _fill, _shape, _cls, _err
from .schematic_model import SchematicValueError, cs_bool, cs_double, cs_int, cs_text

__all__ = [
    "live_read_function_properties",
    "live_set_function_properties",
    "live_insert_window_macro",
    "live_set_device_tags",
]


# Read when the caller names none. Names that a given EPLAN version does not
# declare come back in "unknownProperties" (never as silent empty strings).
DEFAULT_READ_PROPERTIES = (
    "FUNC_TEXT",
    "FUNC_PLCADDRESS",
    "FUNC_PLCCHANNEL_DESIGNATION_MANUAL",
    "FUNC_PLCCHANNEL_DESIGNATION_CALCULATED",
    "FUNC_PLCDATATYPE",
    "FUNC_PLCSYMBOLICADDRESS_MANUAL",
    # v5: the function text of a PLC connection point lives here (from the
    # PLC structure data), FUNC_TEXT is empty on them (live test v4, Z2.1).
    "FUNC_TEXT_AUTOMATIC",
)

# v5: replace_article modes of live_set_function_properties.
_ARTICLE_MODES = ("auto", "project_only")

# "FUNC_X" or "FUNC_X[3]" - the name becomes a reflection lookup (a string
# literal), the index is interpolated as code, so both are validated here.
_PROP_SPEC = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d{1,3})\])?$")

_MOVE_KINDS = {"absolute": "Absolute", "relative": "Relative"}

# EPLAN's internal MultiLangString encoding, e.g. "de_DE@Text;en_US@Text;" or
# "??_??@Text;" for "no language set".
_MLS_SEGMENT = re.compile(r"([a-z?]{2}_[A-Z?]{2})@([^;]*);?")


def _parse_props(props, what):
    """[(name, index)] from ["FUNC_X", "FUNC_Y[2]"]; index 0 = plain property."""
    if isinstance(props, str) or not isinstance(props, (list, tuple)):
        raise SchematicValueError(
            "%s must be a list of property names, e.g. [\"FUNC_PLCADDRESS\", "
            "\"FUNC_CONNECTIONDESIGNATION[1]\"]." % what)
    out = []
    for p in props:
        if not isinstance(p, str):
            raise SchematicValueError("%s: %r is not a string." % (what, p))
        m = _PROP_SPEC.match(p.strip())
        if not m:
            raise SchematicValueError(
                "%s: %r is not a property name. Use the FunctionPropertyList member "
                "name (FUNC_...), optionally with an index: FUNC_CONNECTIONDESIGNATION[1]."
                % (what, p))
        idx = int(m.group(2)) if m.group(2) else 0
        if m.group(2) is not None and idx < 1:
            raise SchematicValueError("%s: index in %r is 1-based." % (what, p))
        out.append((m.group(1), idx))
    return out


def _cs_str_array(values):
    return "new string[] { " + ", ".join('"%s"' % cs_escape(v) for v in values) + " }"


def _cs_int_array(values):
    return "new int[] { " + ", ".join(str(int(v)) for v in values) + " }"


def multilang_display(raw):
    """
    Split EPLAN's MultiLangString encoding into {language: text}.

    Returns (display, by_language) or (None, None) when `raw` is not in that
    encoding (most properties are plain text).
    """
    if not isinstance(raw, str) or "@" not in raw:
        return None, None
    segs = _MLS_SEGMENT.findall(raw)
    if not segs or "".join("%s@%s;" % s for s in segs).rstrip(";") != raw.rstrip(";"):
        return None, None
    by_lang = {}
    for lang, text in segs:
        by_lang[lang] = text
    display = next((t for _, t in segs if t), "")
    return display, by_lang


def _add_display_texts(functions):
    for fn in functions or []:
        props = fn.get("properties")
        if not isinstance(props, dict):
            continue
        disp = {}
        for k, v in props.items():
            d, by_lang = multilang_display(v)
            if d is not None:
                disp[k] = d
        if disp:
            fn["displayText"] = disp


def _shape_keep(raw, keep=("details", "project")):
    """schematic._shape, but a failed write keeps what it already changed."""
    out = _shape(raw)
    if not out.get("success") and isinstance(raw, dict):
        inner = raw.get("results")
        if isinstance(inner, dict):
            for k in keep:
                if k in inner and k not in out:
                    out[k] = inner[k]
    return out


# ---------------------------------------------------------------------------
# C# helpers for this module, spliced in ahead of [Start] (after schematic's).
# ---------------------------------------------------------------------------

_HELPERS_PROPS = r'''
    // The open project's functions, via DMObjectsFinder (as live_query_functions).
    static IEnumerable AllFunctions(object project)
    {
        Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
        object finder = Activator.CreateInstance(finderType, new object[] { project });
        Type filterType = FindType("Eplan.EplApi.DataModel.FunctionsFilter");
        object filter = Activator.CreateInstance(filterType);
        MethodInfo getFunctions = RequireMethod(finderType, "GetFunctions",
            new string[] { filterType.Name }, false);
        object found = Call(getFunctions, finder, new object[] { filter });
        if (found == null)
            throw new Exception("GetFunctions returned null.");
        return (IEnumerable)found;
    }

    static Type _fnPropsType;

    static object FnProps(object fn)
    {
        PropertyInfo pi = GetPropInfo(fn.GetType(), "Properties");
        if (pi == null)
            throw new Exception("Function has no Properties member. " + MemberList(fn.GetType(), false));
        object p = pi.GetValue(fn, null);
        if (p == null) throw new Exception("Function.Properties returned null.");
        if (_fnPropsType == null) _fnPropsType = p.GetType();
        return p;
    }

    // An EMPTY PropertyValue throws on ToString(); IsEmpty first, SafeText after.
    static string PvText(object pv)
    {
        if (pv == null) return "";
        try
        {
            PropertyInfo ie = GetPropInfo(pv.GetType(), "IsEmpty");
            if (ie != null && (bool)ie.GetValue(pv, null)) return "";
        }
        catch { }
        string s = SafeText(pv);
        return s == null ? "" : s;
    }

    // idx 0 = the plain declaration, idx > 0 = the [int] declaration.
    static PropertyInfo FnPropInfo(object props, string name, int idx)
    {
        return idx > 0 ? GetPropInfoIdx(props.GetType(), name) : GetPropInfo(props.GetType(), name);
    }

    // false = the property list does not declare this property at all.
    static bool ReadFnProp(object props, string name, int idx, out string text)
    {
        text = null;
        PropertyInfo pi = FnPropInfo(props, name, idx);
        if (pi == null) return false;
        object v = null;
        try { v = idx > 0 ? pi.GetValue(props, new object[] { idx }) : pi.GetValue(props, null); }
        catch (TargetInvocationException tie) { text = "<error: " + Flatten(tie.InnerException) + ">"; return true; }
        catch (Exception ex) { text = "<error: " + Flatten(ex) + ">"; return true; }
        text = PvText(v);
        return true;
    }

    // Member names of a property list containing `part` - the one-turn fix for
    // a property name that does not exist on this EPLAN version.
    static List<string> PropNamesContaining(Type t, string part, int max)
    {
        List<string> names = new List<string>();
        if (t == null) return names;
        Type cur = t;
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        while (cur != null && names.Count < max)
        {
            if (cur.FullName == "System.Object") break;
            try
            {
                foreach (PropertyInfo pi in cur.GetProperties(bf))
                {
                    if (names.Count >= max) break;
                    if (pi.Name.IndexOf(part, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (!names.Contains(pi.Name)) names.Add(pi.Name);
                }
            }
            catch { }
            cur = cur.BaseType;
        }
        names.Sort();
        return names;
    }

    // Connection point designations in index order. The INDEXED form is the
    // one live_set_connection_designations proved on 2027; the plain form
    // (pilcrow-joined in EPLAN's dialogs) is the fallback.
    static List<string> ConnDesignations(object props)
    {
        List<string> list = new List<string>();
        PropertyInfo idxPi = GetPropInfoIdx(props.GetType(), "FUNC_CONNECTIONDESIGNATION");
        if (idxPi != null)
        {
            for (int i = 1; i <= 32; i++)
            {
                string s;
                try { s = PvText(idxPi.GetValue(props, new object[] { i })); }
                catch { break; }
                if (s.Length == 0) break;
                list.Add(s);
            }
        }
        if (list.Count == 0)
        {
            string all;
            if (ReadFnProp(props, "FUNC_CONNECTIONDESIGNATION", 0, out all) && all != null && all.Length > 0
                && !all.StartsWith("<error"))
                foreach (string part in all.Split(new char[] { '¶' }))
                    if (part.Length > 0) list.Add(part);
        }
        return list;
    }

    static string FnPageName(object fn)
    {
        object pg = TryRead(fn, "Page", null);
        if (pg == null) return "";
        string n = PropText(pg, "Name");
        return n == null ? "" : n;
    }

    static List<object> Articles(object fn, List<string> absent)
    {
        List<object> list = new List<object>();
        object refs = TryRead(fn, "ArticleReferences", absent);
        if (refs is IEnumerable)
        {
            foreach (object r in (IEnumerable)refs)
            {
                if (r == null) continue;
                Dictionary<string, object> d = new Dictionary<string, object>();
                object pn = TryRead(r, "PartNr", null);
                string pns = pn == null ? null : SafeText(pn);
                d["partNr"] = pns == null ? "" : pns;
                object cnt = TryRead(r, "Count", null);
                if (cnt != null) { string c = SafeText(cnt); if (c != null) d["count"] = c; }
                list.Add(d);
            }
        }
        return list;
    }

    static bool FnMatches(object fn, string exactName, string contains, string pageName,
                          string conn, string handle, string category)
    {
        if (handle.Length > 0 && Handle(fn) != handle) return false;
        string name = PropText(fn, "Name");
        if (exactName.Length > 0 && name != exactName) return false;
        if (contains.Length > 0 && !Matches(name, contains)) return false;
        if (pageName.Length > 0 && FnPageName(fn) != pageName) return false;
        if (category.Length > 0)
        {
            object cat = TryRead(fn, "Category", null);
            if (!Matches(cat == null ? null : SafeText(cat), category)) return false;
        }
        if (conn.Length > 0)
        {
            object props;
            try { props = FnProps(fn); } catch { return false; }
            if (!ConnDesignations(props).Contains(conn)) return false;
        }
        return true;
    }

    // v4: PLC boxes, terminals, black boxes ... are SUBCLASSES of Function
    // (clrType "PLC", "Terminal"); comparing GetType().Name with "Function"
    // missed all of them (window macro: functionNames []).
    static bool IsFunctionObject(object o)
    {
        Type t = o.GetType();
        while (t != null)
        {
            if (t.Name == "Function" && (t.Namespace == null || t.Namespace.StartsWith("Eplan.EplApi.DataModel")))
                return true;
            t = t.BaseType;
        }
        return false;
    }

    // v4: pure graphics (lines, arcs, ...) of an inserted macro.
    static bool IsGraphicObject(object o)
    {
        Type t = o.GetType();
        string ns = t.Namespace == null ? "" : t.Namespace;
        if (ns.EndsWith(".Graphics")) return true;
        switch (t.Name)
        {
            case "Line": case "Arc": case "Circle": case "Polyline": case "PolyLine":
            case "Rectangle": case "Ellipse": case "Hatch": case "Image":
                return true;
        }
        return false;
    }

    static Dictionary<string, object> DescribeFn(object fn, string[] pNames, int[] pIdx,
                                                 bool withArticles, List<string> unknown)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        List<string> absent = new List<string>();
        string name = PropText(fn, "Name");
        d["name"] = name == null ? "" : name;
        d["handle"] = Handle(fn);
        d["clrType"] = fn.GetType().Name;
        d["page"] = FnPageName(fn);
        object loc = TryRead(fn, "Location", absent);
        if (loc != null) { try { d["location"] = PtDict(loc); } catch { absent.Add("Location (not a point)"); } }
        object cat = TryRead(fn, "Category", absent);
        if (cat != null) { string c = SafeText(cat); if (c != null) d["category"] = c; }
        object main = TryRead(fn, "IsMainFunction", absent);
        if (main != null) { try { d["isMainFunction"] = Convert.ToBoolean(main); } catch { } }

        object props = FnProps(fn);
        d["connectionDesignations"] = ConnDesignations(props);
        Dictionary<string, object> pv = new Dictionary<string, object>();
        List<string> notApplicable = new List<string>();
        for (int i = 0; i < pNames.Length; i++)
        {
            string key = pIdx[i] > 0 ? pNames[i] + "[" + pIdx[i] + "]" : pNames[i];
            string t;
            if (ReadFnProp(props, pNames[i], pIdx[i], out t))
            {
                // v4: declared by the property list but not valid for THIS
                // function (PLC properties on a coil or contact). Not a value
                // and not an error: null + listed in notApplicable.
                if (t != null && t.StartsWith("<error:")
                    && t.IndexOf("PropertyNotFoundException", StringComparison.Ordinal) >= 0)
                {
                    pv[key] = null;
                    notApplicable.Add(key);
                }
                else pv[key] = t;
            }
            else if (!unknown.Contains(key)) unknown.Add(key);
        }
        d["properties"] = pv;
        if (notApplicable.Count > 0) d["notApplicable"] = notApplicable;
        if (withArticles) d["articles"] = Articles(fn, absent);
        if (absent.Count > 0) d["absentMembers"] = absent;
        return d;
    }

    // v5 (live_set_device_tags): one item's selector. Name: exact, device tag
    // without ":x", or the last tag segment (NameMatches, schematic helpers).
    static bool ItemMatches(object fn, string handle, string name, string pageName, string conn)
    {
        if (handle.Length > 0 && Handle(fn) != handle) return false;
        if (name.Length > 0 && !NameMatches(PropText(fn, "Name"), name)) return false;
        if (pageName.Length > 0 && FnPageName(fn) != pageName) return false;
        if (conn.Length > 0)
        {
            object props;
            try { props = FnProps(fn); } catch { return false; }
            if (!ConnDesignations(props).Contains(conn)) return false;
        }
        return true;
    }

    // v5: FunctionBase.Name { get; set; } (API 2.9) - inherited unchanged by
    // Terminal and PLC. Not FUNC_IDENTDEVICETAG & co.: those are computed and
    // PropertyValue.Set throws S063113 on them.
    static string SetFunctionName(object fn, string name)
    {
        PropertyInfo np = GetWritable(fn.GetType(), "Name");
        if (np == null)
            throw new Exception(fn.GetType().Name + " has no writable Name. " + MemberList(fn.GetType(), false));
        try { np.SetValue(fn, name, null); }
        catch (TargetInvocationException tie)
        { throw new Exception("Name = '" + name + "' on " + fn.GetType().Name + ": " + Flatten(tie.InnerException)); }
        return "Function.Name";
    }

    // v5: connection point designation at a 1-based index, read back.
    static string SetConnDesignation(object fn, int idx, string value)
    {
        object props = FnProps(fn);
        PropertyInfo ipi = GetPropInfoIdx(props.GetType(), "FUNC_CONNECTIONDESIGNATION");
        if (ipi == null)
            throw new Exception("FUNC_CONNECTIONDESIGNATION[int] is not declared by " + props.GetType().FullName);
        object pv = ipi.GetValue(props, new object[] { idx });
        Call(RequireMethod(pv.GetType(), "Set", new string[] { "String" }, false), pv, new object[] { value });
        List<string> after = ConnDesignations(FnProps(fn));
        return after.Count >= idx ? after[idx - 1] : "";
    }
'''


def _selector_subs(name, contains, page, connection_designation, handle, category_contains):
    """Validated selector values as quoted C# literals for _fill."""
    def lit(v, what):
        if v is None:
            return '""'
        return '"%s"' % cs_escape(cs_text(v, what, allow_empty=True))
    return {
        "SELNAME": lit(name, "name"),
        "SELCONTAINS": lit(contains, "contains"),
        "SELPAGE": lit(page, "page"),
        "SELCONN": lit(connection_designation, "connection_designation"),
        "SELHANDLE": lit(handle, "handle"),
        "SELCATEGORY": lit(category_contains, "category_contains"),
    }


# ---------------------------------------------------------------------------
# M-1  read
# ---------------------------------------------------------------------------

def live_read_function_properties(name: str = None, contains: str = None, page: str = None,
                                  connection_designation: str = None, handle: str = None,
                                  category_contains: str = None, properties: list = None,
                                  include_articles: bool = True, offset: int = 0,
                                  limit: int = 200, timeout_seconds: float = 120.0) -> dict:
    """
    Read properties of functions in the project currently open in EPLAN - READ-ONLY.

    Unlike live_query_functions (names only) and get_property (GUI selection
    only), this reads FunctionPropertyList values of functions chosen by name,
    page, connection point or handle - e.g. the PLC address, channel and
    function text of every PLC connection point of a card.

    Args:
        name: Exact identifying function name (device tag), e.g. "=AB-C+DI-510K3".
        contains: Case-insensitive substring of the name (alternative to name).
        page: Only functions on this page (exact page name).
        connection_designation: Only functions that have this connection point
            designation, e.g. "9" - selects one point of a PLC card whose points
            all share one device tag.
        handle: One function by handle (from this tool or live_read_page;
            valid only within the current EPLAN session).
        category_contains: Substring of Function.Category, e.g. "PLC".
        properties: FunctionPropertyList member names to read, optionally
            indexed: ["FUNC_PLCADDRESS", "FUNC_CONNECTIONDESIGNATION[1]"].
            Default: FUNC_TEXT, FUNC_PLCADDRESS,
            FUNC_PLCCHANNEL_DESIGNATION_MANUAL / _CALCULATED, FUNC_PLCDATATYPE,
            FUNC_PLCSYMBOLICADDRESS_MANUAL, FUNC_TEXT_AUTOMATIC (v5: the
            function text of PLC connection points). Names this EPLAN version does not
            declare are listed in "unknownProperties" (with "plcPropertyNames",
            the PLC-related names it does declare) - never returned as "".
        include_articles: Also list the article references (part numbers).
        offset: Skip this many matches (paging; "matched" is the full count).
        limit: Max functions returned (default 200, max 2000).
        timeout_seconds: Default 120 - the whole project is walked.

    Returns:
        {"success", "project", "matched", "offset", "returned", "truncated",
         "functions": [{"name", "handle", "page", "location", "category",
         "isMainFunction", "connectionDesignations", "properties": {name: text},
         "displayText": {name: text without MultiLangString markers},
         "articles": [{"partNr", "count"}]}], "unknownProperties"}
    """
    try:
        specs = _parse_props(properties if properties is not None
                             else list(DEFAULT_READ_PROPERTIES), "properties")
        offset = cs_int(offset, "offset", minimum=0)
        limit = cs_int(limit, "limit", minimum=1, maximum=2000)
        subs = _selector_subs(name, contains, page, connection_designation, handle,
                              category_contains)
    except SchematicValueError as exc:
        return _err(exc)

    body = '''
            string[] pNames = PNAMES;
            int[] pIdx = PIDX;
            List<string> unknown = new List<string>();
            List<object> items = new List<object>();
            int matched = 0;
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                if (!FnMatches(fn, SELNAME, SELCONTAINS, SELPAGE, SELCONN, SELHANDLE, SELCATEGORY)) continue;
                matched++;
                if (matched <= SKIP) continue;
                if (items.Count < TAKE) items.Add(DescribeFn(fn, pNames, pIdx, WITHART, unknown));
            }
            results["success"] = true;
            results["matched"] = matched;
            results["offset"] = SKIP;
            results["returned"] = items.Count;
            results["truncated"] = matched > SKIP + items.Count;
            results["functions"] = items;
            results["unknownProperties"] = unknown;
            if (unknown.Count > 0)
                results["plcPropertyNames"] = PropNamesContaining(_fnPropsType, "PLC", 120);
'''
    body = _fill(body,
                 PNAMES=_cs_str_array([n for n, _ in specs]),
                 PIDX=_cs_int_array([i for _, i in specs]),
                 SKIP=str(offset), TAKE=str(limit),
                 WITHART=cs_bool(include_articles), **subs)
    out = _shape(_execute_script(
        _script(_cls("ReadFnProps"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        _add_display_texts(out.get("functions"))
        na = {}
        for fn in out.get("functions") or []:
            for key in fn.get("notApplicable") or []:
                na[key] = na.get(key, 0) + 1
        if na:
            # v4: {property: number of returned functions it does not apply to}
            out["notApplicableProperties"] = na
        if not out.get("matched"):
            out["note"] = ("No function matched. Names are exact device tags incl. "
                           "structure (=..+..-..); use contains= or live_query_functions "
                           "to find them.")
    return out


# ---------------------------------------------------------------------------
# M-6  write
# ---------------------------------------------------------------------------

def _verify_by_display_text(details):
    """
    v4: EPLAN stores FUNC_TEXT as a MultiLangString ("??_??@TEST v3;"), so the
    exact read-back never equals the requested "TEST v3". When the display
    text equals the request, the write is verified (verifiedBy "displayText").
    """
    for d in details or []:
        for p in d.get("properties") or []:
            if p.get("verified"):
                continue
            requested = p.get("requested")
            disp, by_lang = multilang_display(p.get("new"))
            if disp is None or requested is None:
                continue
            if requested == disp or requested in (by_lang or {}).values():
                p["verified"] = True
                p["verifiedBy"] = "displayText"
                p["newDisplay"] = disp


def live_set_function_properties(values: dict = None, name: str = None, handle: str = None,
                                 page: str = None, connection_designation: str = None,
                                 replace_article: dict = None, limit: int = 1,
                                 allow_real_project: bool = False,
                                 timeout_seconds: float = 120.0,
                                 replace_article_mode: str = "auto",
                                 allow_unverified_db: bool = False) -> dict:
    """
    Set FunctionPropertyList values of functions in the open project. WRITES -
    scratch-only unless allow_real_project=True.

    Target: `handle` (from live_read_function_properties / live_read_page), or
    `name` (exact device tag) narrowed by `connection_designation` and/or
    `page`. If more functions match than `limit`, NOTHING is written. Every
    property name (and the article to replace) is validated on every target
    BEFORE the first write. Each write is read back from a fresh fetch.

    The device tag (BMK) is not set here - use live_set_device_tags (also PLC
    connection points and PLC boxes, many at once) or live_set_device_tag.

    Args:
        values: {property: value}, e.g. {"FUNC_PLCADDRESS": "I1.0",
            "FUNC_PLCCHANNEL_DESIGNATION_MANUAL": "DI0", "FUNC_TEXT": "Ventil 1",
            "FUNC_CONNECTIONDESIGNATION[1]": "DI0"}. Values are written as text.
        name, handle, page, connection_designation: the selector (see above).
        replace_article: {"from": "SIE.6ES7155-6BA00-0CN0",
            "to": "SIE.6ES7155-6BA01-0CN0"} - replace that article reference's
            part number on the target (normally the MAIN function of a device).
        limit: Max functions to modify (default 1).
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 120.
        replace_article_mode: v5, only with replace_article.
            "auto" (default): first the same parts-database preflight as
            parts_db_* - an Access database without 64-bit ACE is refused
            (errorType PARTS_DB_UNAVAILABLE, NOTHING sent to EPLAN, no modal
            dialog); undecidable -> PARTS_DB_UNVERIFIED (allow_unverified_db).
            "project_only": no database check; the new part number is written
            ONLY if the project already holds it as a project article
            (Project.Articles), else errorType ARTICLE_NOT_IN_PROJECT and
            nothing is written. Not verified on EPLAN 2.9 that EPLAN then never
            reads the parts database - keep timeout_seconds short.
        allow_unverified_db: v5, as in parts_db_*: with "auto", try even when
            the preflight cannot decide.

    Returns:
        {"success", "project", "matched", "modified", "details": [{"name",
         "handle", "page", "properties": [{"property", "previous", "requested",
         "new", "verified", "route"}], "article": {"from", "to", "previous",
         "after", "route"}}]}. "previous" makes every edit reversible. A failure
        mid-way keeps "details" of what was already written. Not saved -
        the change is on EPLAN's undo stack until the project is saved.
    """
    try:
        if not handle and not name:
            raise SchematicValueError("Give handle, or name (+ connection_designation / page).")
        items = []
        if values is not None:
            if not isinstance(values, dict):
                raise SchematicValueError("values must be a dict {property: value}.")
            for k, v in values.items():
                if v is None:
                    raise SchematicValueError("value for %s must not be None (use \"\" to clear)." % k)
                (pname, pidx), = _parse_props([k], "values")
                items.append((pname, pidx, cs_text(str(v), "value for %s" % k, allow_empty=True)))
        art_from = art_to = ""
        if replace_article is not None:
            if not isinstance(replace_article, dict) or set(replace_article) != {"from", "to"}:
                raise SchematicValueError('replace_article must be {"from": partNr, "to": partNr}.')
            art_from = cs_text(replace_article["from"], "replace_article.from")
            art_to = cs_text(replace_article["to"], "replace_article.to")
        if not items and not art_from:
            raise SchematicValueError("Nothing to write: give values and/or replace_article.")
        limit = cs_int(limit, "limit", minimum=1, maximum=500)
        subs = _selector_subs(name, None, page, connection_designation, handle, None)
        art_mode = str(replace_article_mode or "auto").strip().lower()
        if art_mode not in _ARTICLE_MODES:
            raise SchematicValueError("replace_article_mode must be 'auto' or 'project_only', got %r."
                                      % (replace_article_mode,))
    except SchematicValueError as exc:
        return _err(exc)

    if art_from and art_mode == "auto":
        # v5 (inbox 2026-10-28_eplan_replace_article_modalno_okno.md): replacing
        # an article can make EPLAN open the parts database - on a machine
        # without 64-bit ACE a MODAL dialog that blocked EPLAN for 240 s. Same
        # preflight as parts_db_*; on a refusal nothing is sent to EPLAN.
        blocked = _scripted._parts_db_guard(allow_unverified_db)
        if blocked:
            out = dict(blocked)
            if out.get("errorType") in ("PARTS_DB_UNAVAILABLE", "PARTS_DB_UNVERIFIED"):
                out["error"] = "replace_article NOT run, nothing written. " + str(out.get("error") or "")
                out["nothingWritten"] = True
                out["alternative"] = (
                    "If the new part number is already an article of this project, "
                    "replace_article_mode='project_only' writes only the part number "
                    "without a database check. Otherwise the parts database must be "
                    "made available (64-bit ACE / SQL) - a user decision.")
            return out

    body = _guard_prelude(allow_real_project) + '''
            bool projectOnly = PROJECTONLY;
            string[] wNames = WNAMES;
            int[] wIdx = WIDX;
            string[] wVals = WVALS;
            string artFrom = ARTFROM;
            string artTo = ARTTO;

            List<object> targets = new List<object>();
            List<string> seen = new List<string>();
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                if (!FnMatches(fn, SELNAME, SELCONTAINS, SELPAGE, SELCONN, SELHANDLE, SELCATEGORY)) continue;
                targets.Add(fn);
                if (seen.Count < 20) seen.Add(PropText(fn, "Name") + " @" + FnPageName(fn) + " [" + Handle(fn) + "]");
            }
            results["matched"] = targets.Count;
            if (targets.Count == 0)
                throw new Exception("No function matches the selector - nothing written. " +
                    "Use live_read_function_properties to find the handle.");
            if (targets.Count > MAXN)
                throw new Exception(targets.Count + " functions match, limit is MAXN - nothing written. " +
                    "Narrow with handle / connection_designation / page. Matches (up to 20): " +
                    string.Join(" | ", seen.ToArray()));

            // Validate EVERYTHING before the first write.
            foreach (object fn in targets)
            {
                object props = FnProps(fn);
                for (int i = 0; i < wNames.Length; i++)
                {
                    if (FnPropInfo(props, wNames[i], wIdx[i]) == null)
                        throw new Exception("Property '" + wNames[i] + (wIdx[i] > 0 ? "[" + wIdx[i] + "]" : "") +
                            "' is not declared by " + props.GetType().FullName + " - nothing written. " +
                            "PLC-related names here: " + string.Join(", ",
                            PropNamesContaining(props.GetType(), "PLC", 120).ToArray()));
                }
                if (artFrom.Length > 0)
                {
                    bool found = false;
                    foreach (object a in Articles(fn, null))
                        if (string.Equals((string)((Dictionary<string, object>)a)["partNr"], artFrom,
                                          StringComparison.OrdinalIgnoreCase)) found = true;
                    if (!found)
                        throw new Exception("Function " + PropText(fn, "Name") + " has no article reference '" +
                            artFrom + "' - nothing written. Articles of the MAIN function are listed by " +
                            "live_read_function_properties(include_articles=true).");
                }
            }

            // v5: project_only - write only a part number the project already
            // holds as a project article (Project.Articles), so EPLAN has no
            // reason to read the parts database. Checked BEFORE the first write.
            if (artFrom.Length > 0 && projectOnly)
            {
                object parts = TryRead(project, "Articles", null);
                if (!(parts is IEnumerable))
                    throw new Exception("ARTICLE_NOT_IN_PROJECT: Project.Articles is not readable - nothing written. " +
                        MemberList(project.GetType(), false));
                bool inProject = false;
                int nArt = 0;
                foreach (object pa in (IEnumerable)parts)
                {
                    if (pa == null) continue;
                    nArt++;
                    object pn = TryRead(pa, "PartNr", null);
                    if (string.Equals(pn == null ? null : SafeText(pn), artTo, StringComparison.OrdinalIgnoreCase))
                    {
                        inProject = true;
                        break;
                    }
                }
                results["projectArticlesChecked"] = nArt;
                if (!inProject)
                    throw new Exception("ARTICLE_NOT_IN_PROJECT: '" + artTo + "' is not an article of this project (" +
                        nArt + " checked) - nothing written. Writing it would make EPLAN read it from the parts database.");
                results["articleInProject"] = true;
            }

            List<object> details = new List<object>();
            results["details"] = details;
            foreach (object fn in targets)
            {
                Dictionary<string, object> d = new Dictionary<string, object>();
                d["name"] = PropText(fn, "Name");
                d["handle"] = Handle(fn);
                d["page"] = FnPageName(fn);
                List<object> plist = new List<object>();
                d["properties"] = plist;
                details.Add(d);
                object props = FnProps(fn);
                for (int i = 0; i < wNames.Length; i++)
                {
                    Dictionary<string, object> pd = new Dictionary<string, object>();
                    string key = wIdx[i] > 0 ? wNames[i] + "[" + wIdx[i] + "]" : wNames[i];
                    pd["property"] = key;
                    string before;
                    ReadFnProp(props, wNames[i], wIdx[i], out before);
                    pd["previous"] = before;
                    pd["requested"] = wVals[i];
                    string route;
                    if (wIdx[i] > 0)
                    {
                        PropertyInfo ipi = GetPropInfoIdx(props.GetType(), wNames[i]);
                        object pv = ipi.GetValue(props, new object[] { wIdx[i] });
                        MethodInfo set = RequireMethod(pv.GetType(), "Set", new string[] { "String" }, false);
                        Call(set, pv, new object[] { wVals[i] });
                        route = "indexed getter+Set(string)";
                    }
                    else
                    {
                        route = SetLiveProp(props, wNames[i], wVals[i]);
                    }
                    pd["route"] = route;
                    string after;
                    ReadFnProp(FnProps(fn), wNames[i], wIdx[i], out after);
                    pd["new"] = after;
                    pd["verified"] = after == wVals[i];
                    plist.Add(pd);
                }

                if (artFrom.Length > 0)
                {
                    Dictionary<string, object> ad = new Dictionary<string, object>();
                    ad["from"] = artFrom;
                    ad["to"] = artTo;
                    ad["previous"] = Articles(fn, null);
                    object refs = TryRead(fn, "ArticleReferences", null);
                    string aroute = null;
                    foreach (object r in (IEnumerable)refs)
                    {
                        if (r == null) continue;
                        object pn = TryRead(r, "PartNr", null);
                        string pns = pn == null ? null : SafeText(pn);
                        if (!string.Equals(pns, artFrom, StringComparison.OrdinalIgnoreCase)) continue;
                        PropertyInfo w = GetWritable(r.GetType(), "PartNr");
                        if (w != null)
                        {
                            try { w.SetValue(r, artTo, null); aroute = "ArticleReference.PartNr"; }
                            catch (Exception exW) { ad["partNrError"] = Flatten(exW); }
                        }
                        if (aroute == null)
                        {
                            object rprops = TryRead(r, "Properties", null);
                            if (rprops == null)
                                throw new Exception("ArticleReference has neither a writable PartNr nor Properties. " +
                                    MemberList(r.GetType(), false));
                            aroute = "ARTICLEREF_PARTNO " + SetLiveProp(rprops, "ARTICLEREF_PARTNO", artTo);
                        }
                        MethodInfo store = MethodByShape(r.GetType(), "StoreToObject", new string[] { }, false);
                        if (store != null) { Call(store, r, null); aroute += "+StoreToObject"; }
                        break;
                    }
                    ad["route"] = aroute;
                    List<object> afterArts = Articles(fn, null);
                    ad["after"] = afterArts;
                    bool ok = false;
                    foreach (object a in afterArts)
                        if (string.Equals((string)((Dictionary<string, object>)a)["partNr"], artTo,
                                          StringComparison.OrdinalIgnoreCase)) ok = true;
                    ad["verified"] = ok;
                    d["article"] = ad;
                }
            }
            results["modified"] = details.Count;
            results["success"] = true;
'''
    body = _fill(body,
                 WNAMES=_cs_str_array([n for n, _, _ in items]),
                 WIDX=_cs_int_array([i for _, i, _ in items]),
                 WVALS=_cs_str_array([v for _, _, v in items]),
                 ARTFROM='"%s"' % cs_escape(art_from),
                 ARTTO='"%s"' % cs_escape(art_to),
                 MAXN=str(limit), PROJECTONLY=cs_bool(art_mode == "project_only"), **subs)
    out = _shape_keep(_execute_script(
        _script(_cls("SetFnProps"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS),
        timeout=timeout_seconds,
    ))
    if not out.get("success") and "ARTICLE_NOT_IN_PROJECT" in str(out.get("error") or ""):
        out["errorType"] = "ARTICLE_NOT_IN_PROJECT"
    if art_from:
        out["replaceArticleMode"] = art_mode
    if out.get("success"):
        _verify_by_display_text(out.get("details"))
        bad = [p["property"] for d in out.get("details") or []
               for p in d.get("properties") or [] if not p.get("verified")]
        art_bad = [d.get("name") for d in out.get("details") or []
                   if isinstance(d.get("article"), dict) and not d["article"].get("verified")]
        if bad or art_bad:
            out["warning"] = (
                "Read-back differs from the requested value for %s%s. EPLAN may "
                "reformat a value (e.g. MultiLangString '??_??@' prefix on FUNC_TEXT) "
                "or refuse it silently - compare 'new' with 'requested'."
                % (", ".join(bad) or "-", "; article not verified on " + ", ".join(art_bad) if art_bad else ""))
    return out


# ---------------------------------------------------------------------------
# M-5  window macro
# ---------------------------------------------------------------------------

def live_insert_window_macro(page: str, macro_path: str, x: float, y: float,
                             variant: int = 0, move_kind: str = "absolute",
                             allow_real_project: bool = False,
                             timeout_seconds: float = 120.0,
                             include_graphics: bool = False) -> dict:
    """
    Insert a window macro (.ema) on a page at a coordinate. WRITES -
    scratch-only unless allow_real_project=True.

    Uses Insert.WindowMacro(fileName, variant, page, point, MoveKind). The
    REPRESENTATION is taken from the page type (EPLAN API): an Overview page
    gets the macro's overview representation, a Circuit page the multi-line
    one. A macro without a representation for that page type inserts nothing
    - reported as a warning, not as success with an empty list.

    Device tags come from the macro (and the project's numbering settings).
    A tag that already exists elsewhere in the project MERGES into that
    device - "duplicateNames" lists them; retag with live_set_device_tag.

    Args:
        page: Exact page name, e.g. "=AB-C+WAGO/900".
        macro_path: Absolute path of the .ema as seen by EPLAN's machine
            (e.g. on Z:). Checked to exist before anything runs.
        x, y: Insertion point in mm (page coordinates).
        variant: Macro variant, 0-based (0 = A ... 15 = P).
        move_kind: "absolute" (default) or "relative" (Insert.MoveKind).
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 120.
        include_graphics: v4. False (default) lists only non-graphic objects in
            "inserted" (functions, PLC boxes, terminals, ...) and counts the
            graphics (lines, arcs, ...) in "graphicsOmitted"; True lists all.

    Returns:
        {"success", "project", "page", "pageType", "boundSignature",
         "insertedCount" (all objects), "listedCount", "inserted": [placement
         as in live_read_page], "graphicsOmitted": {type: count},
         "functionNames" (every inserted Function incl. PLC boxes and
         terminals), "duplicateNames": {name: count of functions that existed
         BEFORE the insert and now carry that name}, "renamedExisting":
         [{"handle", "nameBefore", "nameAfter"}] (existing functions renamed by
         the merge), "placementsBefore", "placementsAfter", "warning"}
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        path = cs_text(macro_path, "macro_path")
        if not path.lower().endswith(".ema"):
            raise SchematicValueError("macro_path must be a window macro (.ema): %r" % path)
        if not os.path.isabs(path):
            raise SchematicValueError("macro_path must be absolute: %r" % path)
        if not os.path.isfile(path):
            raise SchematicValueError("macro_path does not exist on the EPLAN machine: %r" % path)
        xs = cs_double(x, "x")
        ys = cs_double(y, "y")
        var = cs_int(variant, "variant", minimum=0, maximum=15)
        mk = _MOVE_KINDS.get(str(move_kind).strip().lower())
        if mk is None:
            raise SchematicValueError("move_kind must be 'absolute' or 'relative', got %r." % move_kind)
    except SchematicValueError as exc:
        return _err(exc)

    body = _guard_prelude(allow_real_project) + '''
            object page = FindPage(project, PAGENAME);
            results["page"] = PropText(page, "Name");
            results["pageType"] = PropText(page, "PageType");

            // v4: names of all functions BEFORE the insert, by handle - a merge
            // can rename an existing function (=+-K1 -> =CA1+EAA-K1).
            Dictionary<string, string> namesBefore = new Dictionary<string, string>();
            foreach (object fn0 in AllFunctions(project))
            {
                if (fn0 == null) continue;
                string h0 = Handle(fn0);
                if (h0 == null || namesBefore.ContainsKey(h0)) continue;
                string n0 = PropText(fn0, "Name");
                namesBefore[h0] = n0 == null ? "" : n0;
            }
            int before = 0;
            foreach (object pl in PagePlacements(page)) if (pl != null) before++;
            results["placementsBefore"] = before;

            Type insType = FindType("Eplan.EplApi.HEServices.Insert");
            MethodInfo wm = RequireMethod(insType, "WindowMacro",
                new string[] { "String", "Int32", "Page", "PointD", "MoveKind" }, false);
            results["boundSignature"] = wm.ToString();
            ParameterInfo[] ps = wm.GetParameters();
            object pt = MakePoint(ps[3].ParameterType, POSX, POSY);
            object mk;
            try { mk = Enum.Parse(ps[4].ParameterType, MOVEKIND, true); }
            catch
            {
                throw new Exception("MoveKind " + MOVEKIND + " not found; values: " +
                    string.Join(", ", Enum.GetNames(ps[4].ParameterType)));
            }

            object ins = Activator.CreateInstance(insType);
            object ret = null;
            try { ret = Call(wm, ins, new object[] { MACROPATH, VARIANT, page, pt, mk }); }
            finally
            {
                IDisposable disp = ins as IDisposable;
                if (disp != null) { try { disp.Dispose(); } catch { } }
            }

            List<object> placed = new List<object>();
            List<string> fnNames = new List<string>();
            List<string> fnHandles = new List<string>();
            Dictionary<string, object> graphics = new Dictionary<string, object>();
            bool withGraphics = WITHGRAPHICS;
            int total = 0;
            if (ret is IEnumerable)
            {
                foreach (object o in (IEnumerable)ret)
                {
                    if (o == null) continue;
                    total++;
                    bool graphic = IsGraphicObject(o);
                    if (graphic)
                    {
                        string gt = o.GetType().Name;
                        graphics[gt] = graphics.ContainsKey(gt) ? (int)graphics[gt] + 1 : 1;
                    }
                    if (!graphic || withGraphics)
                    {
                        Dictionary<string, object> pd = DumpPlacement(o, false);
                        pd["page"] = FnPageName(o);
                        placed.Add(pd);
                    }
                    if (IsFunctionObject(o))
                    {
                        string n = PropText(o, "Name");
                        if (n != null && n.Length > 0 && !fnNames.Contains(n)) fnNames.Add(n);
                        fnHandles.Add(Handle(o));
                    }
                }
            }
            results["inserted"] = placed;
            results["insertedCount"] = total;
            results["listedCount"] = placed.Count;
            if (!withGraphics) results["graphicsOmitted"] = graphics;
            results["functionNames"] = fnNames;

            // v4: a duplicate is a function that existed BEFORE the insert and
            // now carries an inserted name (also when the merge renamed it).
            Dictionary<string, object> dup = new Dictionary<string, object>();
            List<object> renamed = new List<object>();
            if (fnNames.Count > 0)
            {
                foreach (object fn in AllFunctions(project))
                {
                    if (fn == null) continue;
                    string h = Handle(fn);
                    if (h == null || fnHandles.Contains(h)) continue;
                    string was;
                    if (!namesBefore.TryGetValue(h, out was)) continue;
                    string n = PropText(fn, "Name");
                    if (n == null || !fnNames.Contains(n)) continue;
                    dup[n] = dup.ContainsKey(n) ? (int)dup[n] + 1 : 1;
                    if (was != n && renamed.Count < 50)
                    {
                        Dictionary<string, object> rd = new Dictionary<string, object>();
                        rd["handle"] = h;
                        rd["nameBefore"] = was;
                        rd["nameAfter"] = n;
                        renamed.Add(rd);
                    }
                }
            }
            results["duplicateNames"] = dup;
            results["renamedExisting"] = renamed;   // v5: always a list ([] = none)

            int after = 0;
            foreach (object pl in PagePlacements(page)) if (pl != null) after++;
            results["placementsAfter"] = after;
            results["success"] = true;
'''
    body = _fill(body,
                 PAGENAME='"%s"' % page_cs,
                 MACROPATH='"%s"' % cs_escape(path),
                 POSX=xs, POSY=ys, VARIANT=str(var),
                 MOVEKIND='"%s"' % mk, WITHGRAPHICS=cs_bool(include_graphics))
    out = _shape(_execute_script(
        _script(_cls("InsWinMacro"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS),
        timeout=timeout_seconds,
    ))
    if out.get("success"):
        if not out.get("insertedCount"):
            out["warning"] = (
                "WindowMacro returned no objects. The representation is taken from "
                "the page type (%s): the macro may have no representation for it, "
                "or variant %d may not exist." % (out.get("pageType"), var))
        if out.get("duplicateNames"):
            out["duplicateWarning"] = (
                "Inserted device tag(s) already exist elsewhere in the project and "
                "are merged into those devices: %s. Retag with live_set_device_tag "
                "if that is not intended." % ", ".join(sorted(out["duplicateNames"])))
    return out


# ---------------------------------------------------------------------------
# v5  device tags of functions, PLC connection points and PLC boxes
# ---------------------------------------------------------------------------

_TAG_ITEM_KEYS = frozenset({"handle", "name", "page", "connection", "tag",
                            "connection_designation", "scope"})
_TAG_SCOPES = ("function", "device")
MAX_TAG_ITEMS = 500


def _tag_items(items):
    """Validated items -> list of dicts with every key present (strings)."""
    if not isinstance(items, (list, tuple)) or not items:
        raise SchematicValueError(
            'items must be a non-empty list, e.g. [{"handle": "200/17/28925/0", "tag": "510K4", '
            '"connection_designation": "1"}].')
    if len(items) > MAX_TAG_ITEMS:
        raise SchematicValueError("At most %d items per call, got %d." % (MAX_TAG_ITEMS, len(items)))
    out = []
    for n, it in enumerate(items):
        what = "items[%d]" % n
        if not isinstance(it, dict):
            raise SchematicValueError("%s must be an object." % what)
        extra = sorted(set(it) - _TAG_ITEM_KEYS)
        if extra:
            raise SchematicValueError("%s: unknown key(s) %s; allowed: %s."
                                      % (what, extra, sorted(_TAG_ITEM_KEYS)))
        d = {}
        for k in ("handle", "name", "page", "connection", "connection_designation"):
            v = it.get(k)
            d[k] = cs_text(v, "%s.%s" % (what, k), allow_empty=True).strip() if v is not None else ""
        if not d["handle"] and not d["name"]:
            raise SchematicValueError("%s: give handle, or name (+ connection / page)." % what)
        tag = cs_text(it.get("tag"), "%s.tag" % what).strip()
        if ":" in tag:
            raise SchematicValueError(
                "%s.tag %r contains ':' - give the device tag only (e.g. \"510K4\") and the "
                "connection point in connection_designation." % (what, tag))
        if not tag.strip("-=+"):
            raise SchematicValueError("%s.tag %r is not a device tag." % (what, tag))
        d["tag"] = tag
        scope = str(it.get("scope") or "function").strip().lower()
        if scope not in _TAG_SCOPES:
            raise SchematicValueError("%s.scope must be 'function' or 'device', got %r." % (what, it.get("scope")))
        if scope == "device" and d["connection_designation"]:
            raise SchematicValueError(
                "%s: scope 'device' renames every function of the device and keeps their "
                "connection points; connection_designation is only for scope 'function'." % what)
        d["device"] = scope == "device"
        out.append(d)
    return out


def _cs_bool_array(values):
    return "new bool[] { " + ", ".join(cs_bool(v) for v in values) + " }"


_SET_TAGS_BODY = '''
            bool dryRun = DRYRUN;
            bool allowMerge = ALLOWMERGE;
            bool rollback = ROLLBACK;
            string[] iHandle = IHANDLE;
            string[] iName = INAME;
            string[] iPage = IPAGE;
            string[] iConn = ICONN;
            string[] iTag = ITAG;
            string[] iNewConn = INEWCONN;
            bool[] iDevice = IDEVICE;
            results["dryRun"] = dryRun;

            // One walk over the project; Terminal / PLC / ... included.
            List<object> all = new List<object>();
            foreach (object fn0 in AllFunctions(project))
                if (fn0 != null && IsFunctionObject(fn0)) all.Add(fn0);
            results["functionsScanned"] = all.Count;

            // 1. Resolve every item to exactly one function (scope device: to
            //    every function with that device tag). Nothing is written yet.
            List<object> tFn = new List<object>();
            List<int> tItem = new List<int>();
            List<string> tHandle = new List<string>();
            for (int i = 0; i < iTag.Length; i++)
            {
                List<object> m = new List<object>();
                List<string> seen = new List<string>();
                foreach (object fn in all)
                {
                    if (!ItemMatches(fn, iHandle[i], iName[i], iPage[i], iConn[i])) continue;
                    m.Add(fn);
                    if (seen.Count < 10) seen.Add(PropText(fn, "Name") + " @" + FnPageName(fn) + " [" + Handle(fn) + "]");
                }
                if (m.Count == 0)
                    throw new Exception("Item " + i + ": no function matches (handle '" + iHandle[i] + "', name '" +
                        iName[i] + "', page '" + iPage[i] + "', connection '" + iConn[i] + "') - nothing written. " +
                        "live_read_function_properties lists names, handles and connection points.");
                if (m.Count > 1)
                    throw new Exception("Item " + i + ": " + m.Count + " functions match - nothing written. " +
                        "Narrow with connection / page / handle. Matches (up to 10): " + string.Join(" | ", seen.ToArray()));
                List<object> group = new List<object>();
                if (iDevice[i])
                {
                    string dt0 = DtPart(PropText(m[0], "Name"));
                    foreach (object fn in all)
                        if (string.Equals(DtPart(PropText(fn, "Name")), dt0, StringComparison.OrdinalIgnoreCase)) group.Add(fn);
                }
                else group.Add(m[0]);
                foreach (object fn in group)
                {
                    string h = Handle(fn);
                    int prev = tHandle.IndexOf(h);
                    if (prev >= 0)
                        throw new Exception("Items " + tItem[prev] + " and " + i + " both address " + PropText(fn, "Name") +
                            " [" + h + "] - nothing written.");
                    tFn.Add(fn);
                    tItem.Add(i);
                    tHandle.Add(h);
                }
            }

            // 2. Plan: new device tag in the old structure, ":designation"
            //    suffix kept (or moved to the new connection designation).
            int n = tFn.Count;
            string[] prevName = new string[n];
            string[] newDt = new string[n];
            string[] wantName = new string[n];
            int[] connIdx = new int[n];
            string[] prevConn = new string[n];
            List<object> plan = new List<object>();
            for (int k = 0; k < n; k++)
            {
                object fn = tFn[k];
                int i = tItem[k];
                string nm = PropText(fn, "Name");
                if (nm == null) nm = "";
                prevName[k] = nm;
                string dt = DtPart(nm);
                string suffix = nm.Substring(dt.Length);
                newDt[k] = ComposeDt(dt, iTag[i]);
                connIdx[k] = 0;
                prevConn[k] = "";
                if (iNewConn[i].Length > 0)
                {
                    List<string> cds = ConnDesignations(FnProps(fn));
                    int at = iConn[i].Length > 0 ? cds.IndexOf(iConn[i]) : (cds.Count == 1 ? 0 : -1);
                    if (at < 0)
                        throw new Exception("Item " + i + ": cannot tell which connection point designation of " + nm +
                            " to change (designations: " + string.Join(", ", cds.ToArray()) +
                            "). Give 'connection' - nothing written.");
                    connIdx[k] = at + 1;
                    prevConn[k] = cds[at];
                    if (suffix == ":" + cds[at]) suffix = ":" + iNewConn[i];
                }
                wantName[k] = newDt[k] + suffix;
                Dictionary<string, object> pd = new Dictionary<string, object>();
                pd["item"] = i;
                pd["handle"] = tHandle[k];
                pd["page"] = FnPageName(fn);
                pd["clrType"] = fn.GetType().Name;
                pd["previousName"] = nm;
                pd["newName"] = wantName[k];
                if (connIdx[k] > 0)
                {
                    pd["connectionIndex"] = connIdx[k];
                    pd["previousConnection"] = prevConn[k];
                    pd["newConnection"] = iNewConn[i];
                }
                plan.Add(pd);
            }
            results["planned"] = plan;

            // 3. Merges: a new device tag that other functions (not renamed
            //    here) already carry makes these functions part of that device.
            Dictionary<string, int> exCount = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            Dictionary<string, List<string>> exSample = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
            foreach (object fn in all)
            {
                string h = Handle(fn);
                if (tHandle.Contains(h)) continue;
                string nmE = PropText(fn, "Name");
                string dtE = DtPart(nmE);
                if (dtE.Length == 0) continue;
                if (!exCount.ContainsKey(dtE)) { exCount[dtE] = 0; exSample[dtE] = new List<string>(); }
                exCount[dtE] = exCount[dtE] + 1;
                if (exSample[dtE].Count < 5) exSample[dtE].Add(nmE + " @" + FnPageName(fn) + " [" + h + "]");
            }
            Dictionary<string, object> merges = new Dictionary<string, object>();
            for (int k = 0; k < n; k++)
            {
                string key = newDt[k];
                if (merges.ContainsKey(key)) continue;
                if (string.Equals(key, DtPart(prevName[k]), StringComparison.OrdinalIgnoreCase)) continue;
                if (!exCount.ContainsKey(key)) continue;
                Dictionary<string, object> md = new Dictionary<string, object>();
                md["existingFunctions"] = exCount[key];
                md["examples"] = exSample[key];
                merges[key] = md;
            }
            results["merges"] = merges;
            List<string> mergeKeys = new List<string>(merges.Keys);
            if (mergeKeys.Count > 0 && !allowMerge)
            {
                results["wouldRefuse"] = true;
                if (!dryRun)
                    throw new Exception("New device tag(s) already exist in the project - the functions would MERGE " +
                        "into those devices: " + string.Join(", ", mergeKeys.ToArray()) + " - nothing written. " +
                        "Pass allow_merge=true if that is intended (e.g. points joining their PLC card).");
            }

            // 4. Write, read back, verify; on an error roll back what was written.
            if (!dryRun)
            {
                List<object> details = new List<object>();
                List<object> undo = new List<object>();
                results["details"] = details;
                results["undo"] = undo;
                bool[] nameTouched = new bool[n];
                bool[] connTouched = new bool[n];
                int modified = 0;
                int k = 0;
                try
                {
                    for (k = 0; k < n; k++)
                    {
                        object fn = tFn[k];
                        int i = tItem[k];
                        Dictionary<string, object> d = new Dictionary<string, object>((Dictionary<string, object>)plan[k]);
                        details.Add(d);
                        if (connIdx[k] > 0)
                        {
                            connTouched[k] = true;
                            string got = SetConnDesignation(fn, connIdx[k], iNewConn[i]);
                            d["connectionAfter"] = got;
                            if (got != iNewConn[i])
                                throw new Exception("Item " + i + ": connection designation read back '" + got +
                                    "', requested '" + iNewConn[i] + "'.");
                        }
                        nameTouched[k] = true;
                        string route = SetFunctionName(fn, wantName[k]);
                        string after = PropText(fn, "Name");
                        if (after == null) after = "";
                        string wantSuffix = wantName[k].Substring(newDt[k].Length);
                        if (wantSuffix.Length > 1
                            && string.Equals(DtPart(after), newDt[k], StringComparison.OrdinalIgnoreCase)
                            && after.Substring(DtPart(after).Length) != wantSuffix)
                        {
                            object props2 = FnProps(fn);
                            if (GetPropInfo(props2.GetType(), "FUNC_ADDITIONALIDENTIFYINGNAMEPART") != null)
                            {
                                route += " + FUNC_ADDITIONALIDENTIFYINGNAMEPART " +
                                    SetLiveProp(props2, "FUNC_ADDITIONALIDENTIFYINGNAMEPART", wantSuffix.Substring(1));
                                after = PropText(fn, "Name");
                                if (after == null) after = "";
                            }
                        }
                        d["route"] = route;
                        d["name"] = after;
                        d["visibleName"] = PropText(fn, "VisibleName");
                        bool exact = string.Equals(after, wantName[k], StringComparison.OrdinalIgnoreCase);
                        bool dtOk = string.Equals(DtPart(after), newDt[k], StringComparison.OrdinalIgnoreCase);
                        bool shortOk = string.Equals(ShortTag(DtPart(after)), ShortTag(newDt[k]), StringComparison.OrdinalIgnoreCase);
                        d["verified"] = exact || dtOk || shortOk;
                        if (!exact && dtOk) d["verifiedBy"] = "deviceTag";
                        if (!exact && !dtOk && shortOk) d["verifiedBy"] = "shortTag";
                        if (!exact && !dtOk && !shortOk)
                            throw new Exception("Item " + i + ": EPLAN stored '" + after + "' for requested '" + wantName[k] + "'.");
                        Dictionary<string, object> u = new Dictionary<string, object>();
                        u["handle"] = tHandle[k];
                        u["tag"] = DtPart(prevName[k]);
                        if (connIdx[k] > 0)
                        {
                            u["connection"] = iNewConn[i];
                            u["connection_designation"] = prevConn[k];
                        }
                        undo.Add(u);
                        modified++;
                    }
                }
                catch (Exception exW)
                {
                    results["failedAtItem"] = k < n ? tItem[k] : -1;
                    results["modified"] = modified;
                    if (rollback)
                    {
                        List<object> rb = new List<object>();
                        for (int r = n - 1; r >= 0; r--)
                        {
                            if (!nameTouched[r] && !connTouched[r]) continue;
                            Dictionary<string, object> rd = new Dictionary<string, object>();
                            rd["handle"] = tHandle[r];
                            try
                            {
                                if (connTouched[r]) SetConnDesignation(tFn[r], connIdx[r], prevConn[r]);
                                if (nameTouched[r]) SetFunctionName(tFn[r], prevName[r]);
                                rd["name"] = PropText(tFn[r], "Name");
                                rd["restored"] = string.Equals(PropText(tFn[r], "Name"), prevName[r], StringComparison.Ordinal);
                            }
                            catch (Exception exR)
                            {
                                rd["restored"] = false;
                                rd["error"] = Flatten(exR);
                            }
                            rb.Add(rd);
                        }
                        results["rollback"] = rb;
                        results["undo"] = new List<object>();
                    }
                    throw new Exception("Renaming stopped: " + Flatten(exW) + (rollback
                        ? " - every change of this call was rolled back (see rollback)."
                        : " - changes before this item stay written (see details / undo)."));
                }
                results["modified"] = modified;
            }
            results["success"] = true;
'''


def live_set_device_tags(items: list, allow_merge: bool = False, dry_run: bool = False,
                         rollback_on_error: bool = True, allow_real_project: bool = False,
                         timeout_seconds: float = 300.0) -> dict:
    """
    Set the device tag (BMK) of functions - also PLC connection points
    (clrType Terminal), PLC boxes (PLC) and every other Function subclass -
    one or many in ONE call. WRITES - scratch-only unless allow_real_project=True.

    v5: e.g. 510K3:9..16 -> 510K4:1..8, or the
    PLC box of an inserted ET 200SP macro K1 -> 510K3. Written to
    FunctionBase.Name {get; set;} (API 2.9) - not to FUNC_IDENTDEVICETAG & co.,
    which are computed (S063113).

    Each item:
        selector: "handle" (from live_read_function_properties / live_read_page)
            or "name" (exact name, the device tag without ":x", or only the last
            segment "510K3"), narrowed by "page" and "connection" (the CURRENT
            connection point designation, e.g. "9"). Must match ONE function.
        "tag": new device tag. Without '=' / '+' only the last segment is
            replaced and the structure kept ("510K4" on "=AB-C+DI-510K3" ->
            "=AB-C+DI-510K4"); with '=' or '+' it is the whole tag.
        "connection_designation": optional new connection point designation
            of that point (FUNC_CONNECTIONDESIGNATION[index]), e.g. "1".
        "scope": "function" (default) or "device" = every function that
            carries the target's device tag (box + all its points), each keeping
            its own connection point.
    Example: [{"name": "=AB-C+DI-510K3", "connection": "9", "page":
    "=AB-C+DI/201", "tag": "510K4", "connection_designation": "1"}, ...]

    Order: every item is resolved and checked BEFORE the first write (no match,
    several matches, two items on one function, unknown connection point ->
    nothing written). A new device tag that OTHER functions already carry
    merges these functions into that device: refused unless allow_merge=True
    ("merges" lists them). Each write is read back ("name", "verified"). On an
    error everything this call wrote is rolled back (rollback_on_error) and
    "rollback" reports it. "undo" holds the items that restore the previous
    tags - pass it back as items. Not saved: on EPLAN's undo stack until the
    project is saved.

    Args:
        items: list of items (see above), at most 500.
        allow_merge: Permit new tags that already exist in the project.
        dry_run: Resolve and plan only ("planned", "merges", "wouldRefuse");
            nothing is written, so no allow_real_project needed.
        rollback_on_error: Default True.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 300 (the project is walked once per call).

    Returns:
        {"success", "project", "dryRun", "functionsScanned", "planned":
         [{"item", "handle", "page", "clrType", "previousName", "newName",
         "connectionIndex", "previousConnection", "newConnection"}], "merges":
         {tag: {"existingFunctions", "examples"}}, "details": [... + "name",
         "visibleName", "route", "verified", "verifiedBy"], "modified",
         "undo": [items], "rollback", "failedAtItem"}
    """
    try:
        its = _tag_items(items)
        timeout_seconds = float(timeout_seconds)
    except SchematicValueError as exc:
        return _err(exc)
    except (TypeError, ValueError) as exc:
        return _err(exc)

    body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
        _SET_TAGS_BODY,
        DRYRUN=cs_bool(dry_run), ALLOWMERGE=cs_bool(allow_merge),
        ROLLBACK=cs_bool(rollback_on_error),
        IHANDLE=_cs_str_array([d["handle"] for d in its]),
        INAME=_cs_str_array([d["name"] for d in its]),
        IPAGE=_cs_str_array([d["page"] for d in its]),
        ICONN=_cs_str_array([d["connection"] for d in its]),
        ITAG=_cs_str_array([d["tag"] for d in its]),
        INEWCONN=_cs_str_array([d["connection_designation"] for d in its]),
        IDEVICE=_cs_bool_array([d["device"] for d in its]))
    out = _shape_keep(_execute_script(
        _script(_cls("SetTags"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS),
        timeout=timeout_seconds,
    ), keep=("details", "project", "planned", "merges", "undo", "rollback",
             "failedAtItem", "modified", "dryRun"))
    if out.get("success"):
        if dry_run and out.get("wouldRefuse"):
            out["note"] = ("Dry run: the write would be refused - %s already exist(s). "
                           "Pass allow_merge=true if the merge is intended."
                           % ", ".join(sorted(out.get("merges") or {})))
        shorts = [d.get("handle") for d in out.get("details") or [] if d.get("verifiedBy") == "shortTag"]
        if shorts:
            out["warning"] = ("EPLAN stored a different structure than requested for %s - "
                              "compare 'name' with 'newName'." % ", ".join(shorts))
    return out
