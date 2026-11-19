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

v6 (inbox 2026-10-28/29, live test v5): live_set_device_tags sets the
DISPLAYED device tag too (NameService.AdjustVisibleName / VisibleName - v5
showed 510S1 as "-S1"), renames boxes before their PLC points (points take
the box's tag, no nested 500S1-500S1:1), detects merges without the sign "-",
evaluates the touched pages and reports other renamed functions; compact
answers by default (verbose=True for everything); live_insert_window_macro
reports every renamed existing function and box overlap; replace_article
(auto) refuses a part number that is not in the parts database.

v8 (inbox 2026-10-30_eplan_ni_orodja_prikaz_lastnosti_na_skatli.md):
live_place_function_property shows a function property on the sheet
(PropertyPlacement) - makes a hidden one visible or creates one, for many
symbols in one call. v8 (inbox 2026-10-30_eplan_ni_orodja_dodaj_artikel_
funkciji_brez_artikla.md): live_add_function_articles appends article
references (module + BaseUnit) to functions, with the parts database guard.

v9 (inbox 2026-10-30_eplan_skrij_property_placement.md):
live_hide_function_property hides (or removes) the text that shows a property
on a symbol - also the docked texts of a macro (ContextPropertyPlacement, e.g.
Type number 22002 of the module and the BaseUnit) - without touching the symbol
or its other texts. live_place_function_property writes the Location relative
to the symbol's insertion point (EPLAN 2.9; v8 wrote page mm -> off the sheet).

v10 (inbox 2026-11-03_eplan_besedilo_poti_pathtext_branje_urejanje.md):
live_set_texts changes the contents of Text / PathText (path function text -
it feeds FUNC_TEXT_AUTOMATIC of the functions of its path) and reports which
functions changed; live_read_page(include_text_content=True) reads them.
live_set_function_properties(items=[...]) writes different values to many
functions in one call (inbox 2026-11-03_eplan_set_function_properties_brez_paketa.md).

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
    "live_place_function_property",
    "live_add_function_articles",
    "live_hide_function_property",
    "live_set_texts",
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

    // ---- v6: device tag forms (inbox 2026-10-29_eplan_vidni_bmk_brez_stevilke.md,
    //      2026-10-28_eplan_set_device_tags_spoj_ni_zaznan.md) -----------------
    // "-510K3" / "510K3" -> "510K3": the device tag without the preceding sign.
    // EPLAN stores "510K3" for a requested "-510K3" - the same device.
    static string NormDt(string dt)
    {
        if (dt == null) return "";
        return dt.Trim().TrimStart('-');
    }

    // The device tag WITHOUT project structures: "=AB-C+WAGO-510S1" -> "510S1".
    // A nested tag stays nested: "-500S1-500S1" -> "500S1-500S1".
    static string DevPart(string dt)
    {
        string s = dt == null ? "" : dt.Trim();
        int plus = s.LastIndexOf('+');
        if (plus >= 0)
        {
            string rest = s.Substring(plus + 1);
            int d1 = rest.IndexOf('-');
            return d1 < 0 ? "" : rest.Substring(d1 + 1);
        }
        if (s.StartsWith("="))
        {
            int d2 = s.LastIndexOf('-');
            return d2 < 0 ? "" : s.Substring(d2 + 1);
        }
        return s.TrimStart('-');
    }

    // Same device: equal without the sign, or equal without structures when
    // one side carries none (V2 names have no structure: "510K3:9").
    static bool SameDevice(string a, string b)
    {
        if (a == null) a = "";
        if (b == null) b = "";
        if (string.Equals(NormDt(a), NormDt(b), StringComparison.OrdinalIgnoreCase)) return NormDt(a).Length > 0;
        bool aStruct = a.IndexOf('=') >= 0 || a.IndexOf('+') >= 0;
        bool bStruct = b.IndexOf('=') >= 0 || b.IndexOf('+') >= 0;
        if (aStruct && bStruct) return false;
        string pa = DevPart(a), pb = DevPart(b);
        return pa.Length > 0 && string.Equals(pa, pb, StringComparison.OrdinalIgnoreCase);
    }

    // v6: new device tag in the structure of the old one. Unlike ComposeDt (v5)
    // no "-" is added when the old tag has none ("K1" of a window macro, "510K3"
    // in V2); a tag with '=' / '+' is taken whole.
    static string ComposeDtV6(string oldDt, string tag)
    {
        if (tag.IndexOf('=') >= 0 || tag.IndexOf('+') >= 0) return tag;
        int dash = oldDt == null ? -1 : oldDt.LastIndexOf('-');
        if (dash < 0) return tag;
        return oldDt.Substring(0, dash) + "-" + tag.TrimStart('-');
    }

    // ":9" of "510K3:9" must survive the rename (as requested).
    static bool SuffixOk(string after, string wantName, string newDt)
    {
        if (after == null) after = "";
        string want = wantName.Length >= newDt.Length ? wantName.Substring(newDt.Length) : "";
        string got = after.Substring(DtPart(after).Length);
        return string.Equals(want, got, StringComparison.OrdinalIgnoreCase);
    }

    // v6: the DISPLAYED device tag (what the page shows above a box):
    // FUNC_VISIBLEDEVICETAG (#20010, read-only), else FunctionBase.VisibleName.
    static string VisibleDt(object fn)
    {
        try
        {
            object props = FnProps(fn);
            string t;
            if (ReadFnProp(props, "FUNC_VISIBLEDEVICETAG", 0, out t) && t != null && !t.StartsWith("<error"))
                return t;
        }
        catch { }
        string v = PropText(fn, "VisibleName");
        return v == null ? "" : v;
    }

    // v6: displayed tag names the requested device (sign / structures aside),
    // or is empty because the function takes the tag of its box (a PLC point
    // inside its PLC box) and its name already is the requested one.
    static bool VisibleOk(object fn, string newDt)
    {
        string vis = VisibleDt(fn);
        if (SameDevice(DtPart(vis), newDt)) return true;
        if (NormDt(vis).Length == 0) return SameDevice(DtPart(PropText(fn, "Name")), newDt);
        return false;
    }

    // ---- v6: NameService (Eplan.EplApi.HEServices, API 2.9) ------------------
    // Keeps full name, visible name AND visible name format consistent - what
    // the property dialog does. v5 set only FunctionBase.Name: the visible
    // format of the macro tag "-K1" (identifier + counter) stayed, so "510S1"
    // was displayed as "-S1" (prefix 510 dropped).
    static Type _nsType;

    static object NewNameService(object page)
    {
        if (_nsType == null) _nsType = FindType("Eplan.EplApi.HEServices.NameService");
        if (_nsType == null) throw new Exception("Eplan.EplApi.HEServices.NameService not found.");
        object ns = null;
        foreach (ConstructorInfo ci in _nsType.GetConstructors())
        {
            ParameterInfo[] ps = ci.GetParameters();
            if (ps.Length == 1 && page != null && ps[0].ParameterType.IsAssignableFrom(page.GetType()))
            {
                try { ns = ci.Invoke(new object[] { page }); }
                catch (TargetInvocationException tie) { throw new Exception("new NameService(Page): " + Flatten(tie.InnerException)); }
                break;
            }
        }
        if (ns == null)
        {
            ns = Activator.CreateInstance(_nsType);
            PropertyInfo pp = GetWritable(_nsType, "Page");
            if (pp != null && page != null) pp.SetValue(ns, page, null);
        }
        // As interactive editing: the visible name carries no structures equal
        // to the page / box and no device tag equal to the surrounding box - a
        // PLC point then TAKES the box's tag instead of nesting its own under it
        // (500S1-500S1:1, inbox 2026-10-29_..._ugnezdi_prikljucke_...).
        SetBoolIfPresent(ns, "RemovalOfUnnecessaryProjectStructuresIsEnabled", true);
        SetBoolIfPresent(ns, "RemovalOfUnnecessaryDeviceTagNamePartsIsEnabled", true);
        return ns;
    }

    static void SetBoolIfPresent(object o, string name, bool value)
    {
        PropertyInfo pi = GetWritable(o.GetType(), name);
        if (pi == null) return;
        try { pi.SetValue(o, value, null); } catch { }
    }

    static object FnPage(object fn) { return TryRead(fn, "Page", null); }

    static bool HasNsMethod(object ns, string method)
    {
        return MethodByShape(ns.GetType(), method, new string[] { "Page", "FunctionBase" }, false) != null
            || MethodByShape(ns.GetType(), method, new string[] { "FunctionBase" }, false) != null;
    }

    // NameService.<method>(Page, FunctionBase), else (FunctionBase).
    static string NsCall(object ns, string method, object page, object fn)
    {
        MethodInfo mi = page == null ? null
            : MethodByShape(ns.GetType(), method, new string[] { "Page", "FunctionBase" }, false);
        if (mi != null)
        {
            object r1 = Call(mi, ns, new object[] { page, fn });
            return "NameService." + method + "(Page,FunctionBase)" + (r1 is bool && !(bool)r1 ? "=false" : "");
        }
        mi = RequireMethod(ns.GetType(), method, new string[] { "FunctionBase" }, false);
        object r2 = Call(mi, ns, new object[] { fn });
        return "NameService." + method + "(FunctionBase)" + (r2 is bool && !(bool)r2 ? "=false" : "");
    }

    // The function whose name a function without its own visible device tag
    // takes over (its PLC box / black box), or null.
    static object NameGiver(object ns, object fn)
    {
        if (ns == null) return null;
        try
        {
            object pg = FnPage(fn);
            MethodInfo mi = pg == null ? null
                : MethodByShape(ns.GetType(), "FindNameGivingFunction", new string[] { "Page", "Function" }, false);
            if (mi != null) return mi.Invoke(ns, new object[] { pg, fn });
            mi = MethodByShape(ns.GetType(), "FindNameGivingFunction", new string[] { "Function" }, false);
            if (mi != null) return mi.Invoke(ns, new object[] { fn });
        }
        catch { }
        return null;
    }

    // Visible name route: as typing the displayed tag in the dialog, then the
    // full name evaluated from it. Empty = take the tag of the box.
    static string SetVisibleAndAdjust(object ns, object fn, string visible)
    {
        PropertyInfo vp = GetWritable(fn.GetType(), "VisibleName");
        if (vp == null)
            throw new Exception(fn.GetType().Name + " has no writable VisibleName. " + MemberList(fn.GetType(), false));
        try { vp.SetValue(fn, visible, null); }
        catch (TargetInvocationException tie)
        { throw new Exception("VisibleName = '" + visible + "' on " + fn.GetType().Name + ": " + Flatten(tie.InnerException)); }
        return "FunctionBase.VisibleName('" + visible + "') + " + NsCall(ns, "AdjustFullName", FnPage(fn), fn);
    }

    // v6: one function's device tag; returns the route taken.
    //   "name"    FunctionBase.Name only (v5 behaviour)
    //   "auto"    Name, then NameService.AdjustVisibleName (visible name and
    //             its format from the full name); if the displayed tag still
    //             differs: VisibleName + NameService.AdjustFullName
    //   "visible" VisibleName + NameService.AdjustFullName
    static string WriteDeviceTag(object ns, string mode, object fn, string wantName, string newDt, string tag)
    {
        string route = "";
        if (mode != "visible")
        {
            route = SetFunctionName(fn, wantName);
            string after = PropText(fn, "Name");
            if (after == null) after = "";
            string wantSuffix = wantName.Length >= newDt.Length ? wantName.Substring(newDt.Length) : "";
            if (wantSuffix.Length > 1 && SameDevice(DtPart(after), newDt) && !SuffixOk(after, wantName, newDt))
            {
                object props2 = FnProps(fn);
                if (GetPropInfo(props2.GetType(), "FUNC_ADDITIONALIDENTIFYINGNAMEPART") != null)
                    route += " + FUNC_ADDITIONALIDENTIFYINGNAMEPART " +
                        SetLiveProp(props2, "FUNC_ADDITIONALIDENTIFYINGNAMEPART", wantSuffix.Substring(1));
            }
            if (mode == "name") return route;
            route += " + " + NsCall(ns, "AdjustVisibleName", FnPage(fn), fn);
            if (VisibleOk(fn, newDt)) return route;
            route += " | displayed '" + VisibleDt(fn) + "' -> ";
        }
        object giver = NameGiver(ns, fn);
        string vis = (giver != null && SameDevice(DtPart(PropText(giver, "Name")), newDt)) ? "" : tag;
        return route + SetVisibleAndAdjust(ns, fn, vis);
    }

    // v6: functions (not in `skip`) whose name changed since `before`.
    static List<object> RenamedOthers(List<object> all, Dictionary<string, string> before,
                                      List<string> skip, List<string> followDts, int max, out int total)
    {
        List<object> list = new List<object>();
        total = 0;
        foreach (object fn in all)
        {
            string h = Handle(fn);
            if (h == null || skip.Contains(h)) continue;
            string was;
            if (!before.TryGetValue(h, out was)) continue;
            string now = PropText(fn, "Name");
            if (now == null) now = "";
            if (now == was) continue;
            total++;
            if (list.Count >= max) continue;
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["handle"] = h;
            d["page"] = FnPageName(fn);
            d["nameBefore"] = was;
            d["nameAfter"] = now;
            bool follows = false;
            foreach (string f in followDts)
                if (SameDevice(DtPart(was), f)) { follows = true; break; }
            d["followsRenamedDevice"] = follows;
            list.Add(d);
        }
        return list;
    }

    // v6: a placement that bounds other functions (PLC box, black box ...).
    static bool IsBoxLike(object o)
    {
        if (o == null || IsGraphicObject(o)) return false;
        string t = o.GetType().Name;
        return t == "PLC" || t.EndsWith("Box");
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


def _article_in_db(part_nr, allow_unverified_db=False):
    """v6: True / False when the parts database answered, None when it could not tell."""
    try:
        raw = _scripted.parts_db_get_part(part_nr, allow_unverified_db=allow_unverified_db)
    except Exception:  # noqa: BLE001 - undecided, never a crash of the write
        return None
    if not isinstance(raw, dict) or raw.get("success") is False:
        return None
    res = raw.get("results") if isinstance(raw.get("results"), dict) else raw
    if res.get("success") is False:
        return None
    found = res.get("found")
    return found if isinstance(found, bool) else None


def live_set_function_properties(values: dict = None, name: str = None, handle: str = None,
                                 page: str = None, connection_designation: str = None,
                                 replace_article: dict = None, limit: int = 1,
                                 allow_real_project: bool = False,
                                 timeout_seconds: float = 120.0,
                                 replace_article_mode: str = "auto",
                                 allow_unverified_db: bool = False,
                                 allow_missing_article: bool = False,
                                 items: list = None, dry_run: bool = False,
                                 rollback_on_error: bool = True, verbose: bool = False,
                                 max_items: int = 500) -> dict:
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
        allow_missing_article: v6, only with "auto". The new part number is
            looked up in the parts database first (as parts_db_get_part); not
            found -> errorType ARTICLE_NOT_IN_DB, nothing written (EPLAN would
            write a reference without description / manufacturer). True writes
            it anyway. "articleInDb" reports the lookup (null = undecided).
        items: v10 - MANY functions, each its own values, in ONE call (one
            script, one LockingStep): [{"handle": "...", "values": {...}} or
            {"name": "=AB-C+DI-510K3", "connection_designation": "9",
            "page"?: "...", "values": {"FUNC_SUPPLEMENTARYFIELD[1]": "Skid"},
            "limit"?: 2}] (up to max_items, default 500; limit = functions one
            item may match, default 1 - a PLC point on the schematic AND on the
            card overview has the same name: limit 2). Not with values / name /
            handle / page / connection_designation / replace_article. Every
            item is resolved and every property checked BEFORE the first write
            (an item without / with too many functions, two items on one
            function, an undeclared property -> nothing written). A value
            already equal (also the MultiLangString form "??_??@x;" of x) is
            not written ("unchanged"). COMPACT answer: "counts" {items,
            functions, written / wouldWrite, unchanged, verified, unverified},
            "unverified" [{item, handle, name, property, requested, new}]
            (up to 50), "failedAt"; verbose=True adds "details" per function
            (previous, requested, new, route).
        dry_run: v10, default False (as before). True: nothing written -
            items (or the single selector + values as one item) are resolved
            and counted ("wouldWrite" / "unchanged"); no scratch guard needed.
        rollback_on_error: v10, items only (default True): an error during the
            writes restores the previous value of every write of this call
            (errorType ROLLED_BACK; False -> PARTIAL).
        verbose: v10, items only - "details" per function.
        max_items: v10, items only (default 500).

    Returns:
        {"success", "project", "matched", "modified", "details": [{"name",
         "handle", "page", "properties": [{"property", "previous", "requested",
         "new", "verified", "route"}], "article": {"from", "to", "previous",
         "after", "route"}}], "articleInDb"}. "previous" makes every edit
        reversible. A failure mid-way keeps "details" of what was already
        written. EPLAN stores the change in the project database itself (no
        save command; get_current_project reports "databaseLastWrite").
        v10 items / dry_run: {"success", "dryRun", "matchedFunctions",
        "counts", "unverified", "warning"} (see items).
    """
    if items is not None:
        given = [k for k, v in (("values", values), ("name", name), ("handle", handle), ("page", page),
                                ("connection_designation", connection_designation),
                                ("replace_article", replace_article)) if v is not None]
        if given:
            return _err(SchematicValueError(
                "items=[...] carries selector and values per item - do not pass %s with it." % ", ".join(given)))
        return _set_function_properties_items(items, allow_real_project, timeout_seconds, dry_run,
                                               rollback_on_error, verbose, max_items)
    if dry_run:
        # v10: dry run of the single selector = one item
        if replace_article is not None:
            return _err(SchematicValueError("dry_run is not available with replace_article."))
        if not handle and not name:
            return _err(SchematicValueError("Give handle, or name (+ connection_designation / page)."))
        return _set_function_properties_items(
            [{"handle": handle, "name": name, "page": page, "connection_designation": connection_designation,
              "values": values or {}, "limit": limit}],
            allow_real_project, timeout_seconds, True, rollback_on_error, verbose, max_items)
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

    in_db = None
    if art_from and art_mode == "auto":
        # v6 (inbox 2026-10-28_eplan_replace_article_ni_v_bazi.md): live test v5 A2
        # wrote SIE.6ES7155-6BA01-0CN0, which the parts database does not hold.
        in_db = _article_in_db(art_to, allow_unverified_db)
        if in_db is False and not allow_missing_article:
            return {"success": False, "errorType": "ARTICLE_NOT_IN_DB", "nothingWritten": True,
                    "articleInDb": False, "replaceArticleMode": art_mode,
                    "error": ("replace_article NOT run, nothing written: %r is not in the parts "
                              "database (parts_db_get_part: found false). EPLAN would write an "
                              "article reference without description / manufacturer. Pass "
                              "allow_missing_article=true to write it anyway." % art_to)}

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
        if art_mode == "auto":
            out["articleInDb"] = in_db
            if out.get("success") and in_db is not True:
                out["articleWarning"] = (
                    "%r was written although it is NOT in the parts database (allow_missing_article)."
                    % art_to if in_db is False else
                    "Could not tell whether %r is in the parts database (lookup failed) - check "
                    "with parts_db_get_part." % art_to)
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

def _is_box(pl):
    """v6: a placement that bounds other functions (PLC box, black box ...)."""
    t = str((pl or {}).get("clrType") or "")
    return t == "PLC" or t.endswith("Box")


def _bbox(pls):
    """v6: union {"min": {x, y}, "max": {x, y}} of the placements' bounding boxes, or None."""
    xs, ys = [], []
    for pl in pls or []:
        for p in (pl or {}).get("boundingBox") or []:
            try:
                xs.append(float(p["x"]))
                ys.append(float(p["y"]))
            except (KeyError, TypeError, ValueError):
                continue
    if not xs:
        return None
    return {"min": {"x": min(xs), "y": min(ys)}, "max": {"x": max(xs), "y": max(ys)}}


def _overlap(a, b):
    return bool(a and b and a["min"]["x"] < b["max"]["x"] and b["min"]["x"] < a["max"]["x"]
                and a["min"]["y"] < b["max"]["y"] and b["min"]["y"] < a["max"]["y"])


def _macro_checks(out):
    """v6: bounding box of the whole insert, overlap with existing boxes, outside the page."""
    inserted = out.get("inserted") or []
    box = _bbox(inserted)
    out["boundingBox"] = box
    new_boxes = [pl for pl in inserted if _is_box(pl)]
    out["boxes"] = [{k: pl.get(k) for k in ("handle", "name", "clrType")} for pl in new_boxes]
    warnings = []
    mine = [b for b in (_bbox([pl]) for pl in new_boxes) if b] or ([box] if box else [])
    hits = []
    for ex in out.get("existingBoxes") or []:
        eb = _bbox([ex])
        if eb and any(_overlap(m, eb) for m in mine):
            hits.append("%s %s [%s]" % (ex.get("clrType"), ex.get("name"), ex.get("handle")))
    if hits:
        out["overlapsExistingBoxes"] = hits
        warnings.append(
            "The macro overlaps existing box(es) %s - EPLAN nests the device tags of functions "
            "inside a box (K1 -> K1-K1, live test v5 A1). Remove the page or the placements "
            "and insert elsewhere." % ", ".join(hits))
    size = out.get("pageSize") or {}
    try:
        w, h = float(size.get("x")), float(size.get("y"))
    except (TypeError, ValueError):
        w = h = None
    if box and w and h and (box["min"]["x"] < 0 or box["min"]["y"] < 0
                            or box["max"]["x"] > w or box["max"]["y"] > h):
        out["outsidePage"] = True
        warnings.append("Bounding box %s reaches outside the page (0..%g x 0..%g mm)." % (box, w, h))
    if out.get("renamedExisting"):
        warnings.append("EPLAN renamed %d existing function(s) - see renamedExisting."
                        % (out.get("renamedExistingCount") or len(out["renamedExisting"])))
    return warnings


def live_insert_window_macro(page: str, macro_path: str, x: float, y: float,
                             variant: int = 0, move_kind: str = "absolute",
                             allow_real_project: bool = False,
                             timeout_seconds: float = 120.0,
                             include_graphics: bool = False,
                             verbose: bool = False) -> dict:
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
    device - "duplicateNames" lists them; retag with live_set_device_tags
    (rename only the PLC box: its points take the box's tag).

    v6: "renamedExisting" lists EVERY existing function EPLAN renamed by the
    insert (compared by handle, before/after), "duplicateNames" every inserted
    device tag that existing functions carry (sign "-" ignored). A macro whose
    box overlaps an existing PLC / black box ("overlapsExistingBoxes": EPLAN
    nests tags, K1 -> K1-K1) or reaches outside the page ("outsidePage") is
    reported in "warnings". Compact by default: verbose=True adds every inserted
    placement ("inserted") and the existing boxes ("existingBoxes").

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
            Only with verbose=True.
        verbose: v6. False (default): no "inserted" / "existingBoxes" lists.

    Returns:
        {"success", "project", "page", "pageType", "boundSignature",
         "insertedCount" (all objects), "listedCount", "graphicsOmitted":
         {type: count}, "functionNames" (every inserted Function incl. PLC
         boxes and terminals), "boxes": [{"handle", "name", "clrType"}],
         "boundingBox": {"min", "max"}, "pageSize", "duplicateNames":
         {device tag: count of functions that existed BEFORE the insert and
         carry it}, "renamedExisting": [{"handle", "page", "nameBefore",
         "nameAfter"}], "renamedExistingCount", "overlapsExistingBoxes",
         "outsidePage", "placementsBefore", "placementsAfter", "warnings";
         verbose: + "inserted": [placement as in live_read_page],
         "existingBoxes"}
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
            object psize = TryRead(page, "Size", null);
            if (psize != null) { try { results["pageSize"] = PtDict(psize); } catch { } }

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
            List<object> boxesBefore = new List<object>();
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null) continue;
                before++;
                // v6: existing boxes on the page - overlap check (Python side).
                if (IsBoxLike(pl)) boxesBefore.Add(DumpPlacement(pl, false));
            }
            results["placementsBefore"] = before;
            results["existingBoxes"] = boxesBefore;

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

            // v6 (inbox 2026-10-28_eplan_window_macro_renamedexisting_prazno.md):
            // EVERY existing function whose name changed (by handle), and every
            // existing function that carries / carried an inserted device tag.
            List<string> insDts = new List<string>();
            foreach (string fnN in fnNames)
            {
                string dtI = NormDt(DtPart(fnN));
                if (dtI.Length > 0 && !insDts.Contains(dtI)) insDts.Add(dtI);
            }
            Dictionary<string, object> dup = new Dictionary<string, object>();
            List<object> renamed = new List<object>();
            int renamedTotal = 0;
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                string h = Handle(fn);
                if (h == null || fnHandles.Contains(h)) continue;
                string was;
                if (!namesBefore.TryGetValue(h, out was)) continue;
                string n = PropText(fn, "Name");
                if (n == null) n = "";
                if (was != n)
                {
                    renamedTotal++;
                    if (renamed.Count < 200)
                    {
                        Dictionary<string, object> rd = new Dictionary<string, object>();
                        rd["handle"] = h;
                        rd["page"] = FnPageName(fn);
                        rd["nameBefore"] = was;
                        rd["nameAfter"] = n;
                        renamed.Add(rd);
                    }
                }
                foreach (string dtI in insDts)
                {
                    if (SameDevice(DtPart(n), dtI) || SameDevice(DtPart(was), dtI))
                    {
                        dup[dtI] = dup.ContainsKey(dtI) ? (int)dup[dtI] + 1 : 1;
                        break;
                    }
                }
            }
            results["duplicateNames"] = dup;
            results["renamedExisting"] = renamed;   // v5: always a list ([] = none)
            results["renamedExistingCount"] = renamedTotal;

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
                "are merged into those devices: %s. Retag with live_set_device_tags "
                "if that is not intended." % ", ".join(sorted(out["duplicateNames"])))
        warnings = _macro_checks(out)
        if warnings:
            out["warnings"] = warnings
        if not verbose:
            for k in ("inserted", "existingBoxes"):
                out.pop(k, None)
            out["detailsOmitted"] = "inserted placements and existing boxes: verbose=true"
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
            string nameRoute = NAMEROUTE;
            bool evalPages = EVALPAGES;
            string[] iHandle = IHANDLE;
            string[] iName = INAME;
            string[] iPage = IPAGE;
            string[] iConn = ICONN;
            string[] iTag = ITAG;
            string[] iNewConn = INEWCONN;
            bool[] iDevice = IDEVICE;
            results["dryRun"] = dryRun;
            results["nameRoute"] = nameRoute;

            // One walk over the project; Terminal / PLC / ... included.
            List<object> all = new List<object>();
            foreach (object fn0 in AllFunctions(project))
                if (fn0 != null && IsFunctionObject(fn0)) all.Add(fn0);
            results["functionsScanned"] = all.Count;

            // v6: every name BEFORE, by handle - what else EPLAN renames.
            Dictionary<string, string> nameBefore = new Dictionary<string, string>();
            foreach (object fn0 in all)
            {
                string h0 = Handle(fn0);
                if (h0 == null || nameBefore.ContainsKey(h0)) continue;
                string n0 = PropText(fn0, "Name");
                nameBefore[h0] = n0 == null ? "" : n0;
            }

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
                        if (string.Equals(NormDt(DtPart(PropText(fn, "Name"))), NormDt(dt0), StringComparison.OrdinalIgnoreCase)) group.Add(fn);
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

            // 2. Plan: new device tag in the old structure (v6: no "-" added to
            //    a tag that had none), ":designation" suffix kept (or moved to
            //    the new connection designation).
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
                newDt[k] = ComposeDtV6(dt, iTag[i]);
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
                pd["visibleBefore"] = VisibleDt(fn);
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
            //    v6: compared without the sign "-" and, when one side has no
            //    structure, without structures ("-510K3" = "510K3").
            Dictionary<string, int> exCount = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            Dictionary<string, List<string>> exSample = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
            foreach (object fn in all)
            {
                string h = Handle(fn);
                if (tHandle.Contains(h)) continue;
                string nmE = PropText(fn, "Name");
                string dtE = NormDt(DtPart(nmE));
                if (dtE.Length == 0) continue;
                if (!exCount.ContainsKey(dtE)) { exCount[dtE] = 0; exSample[dtE] = new List<string>(); }
                exCount[dtE] = exCount[dtE] + 1;
                if (exSample[dtE].Count < 5) exSample[dtE].Add(nmE + " @" + FnPageName(fn) + " [" + h + "]");
            }
            Dictionary<string, object> merges = new Dictionary<string, object>();
            for (int k = 0; k < n; k++)
            {
                string key = NormDt(newDt[k]);
                if (merges.ContainsKey(key)) continue;
                if (SameDevice(newDt[k], DtPart(prevName[k]))) continue;
                int cnt = 0;
                List<string> ex = new List<string>();
                foreach (KeyValuePair<string, int> kv in exCount)
                {
                    if (!SameDevice(kv.Key, newDt[k])) continue;
                    cnt += kv.Value;
                    foreach (string s in exSample[kv.Key]) if (ex.Count < 5) ex.Add(s);
                }
                if (cnt == 0) continue;
                Dictionary<string, object> md = new Dictionary<string, object>();
                md["existingFunctions"] = cnt;
                md["examples"] = ex;
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
                // v6: boxes (every non-terminal) first, PLC points after - a
                // point inside a renamed box then takes over the box's tag.
                List<int> order = new List<int>();
                for (int k0 = 0; k0 < n; k0++) if (tFn[k0].GetType().Name != "Terminal") order.Add(k0);
                for (int k0 = 0; k0 < n; k0++) if (tFn[k0].GetType().Name == "Terminal") order.Add(k0);

                object ns = null;
                if (n > 0 && (nameRoute != "name" || evalPages))
                {
                    try { ns = NewNameService(FnPage(tFn[order[0]])); }
                    catch (Exception exNs)
                    {
                        if (nameRoute != "name")
                            throw new Exception("NameService not available - nothing written: " + Flatten(exNs));
                    }
                    if (ns != null && nameRoute == "auto" && !HasNsMethod(ns, "AdjustVisibleName"))
                        throw new Exception("NameService has no AdjustVisibleName - nothing written. " + MemberList(ns.GetType(), true));
                    if (ns != null && nameRoute != "name" && !HasNsMethod(ns, "AdjustFullName"))
                        throw new Exception("NameService has no AdjustFullName - nothing written. " + MemberList(ns.GetType(), true));
                }

                List<object> details = new List<object>();
                List<object> undo = new List<object>();
                results["details"] = details;
                results["undo"] = undo;
                bool[] nameTouched = new bool[n];
                bool[] connTouched = new bool[n];
                List<object> touchedPages = new List<object>();
                List<string> touchedPageNames = new List<string>();
                int modified = 0;
                int k = -1;
                try
                {
                    for (int pos = 0; pos < n; pos++)
                    {
                        k = order[pos];
                        object fn = tFn[k];
                        int i = tItem[k];
                        Dictionary<string, object> d = new Dictionary<string, object>((Dictionary<string, object>)plan[k]);
                        details.Add(d);
                        object pg = FnPage(fn);
                        string pgName = FnPageName(fn);
                        if (pg != null && !touchedPageNames.Contains(pgName)) { touchedPageNames.Add(pgName); touchedPages.Add(pg); }
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
                        string route = WriteDeviceTag(ns, nameRoute, fn, wantName[k], newDt[k], iTag[i]);
                        string after = PropText(fn, "Name");
                        if (after == null) after = "";
                        string vis = VisibleDt(fn);
                        d["route"] = route;
                        d["name"] = after;
                        d["visibleDeviceTag"] = vis;
                        d["visibleName"] = PropText(fn, "VisibleName");
                        bool exact = string.Equals(after, wantName[k], StringComparison.OrdinalIgnoreCase);
                        bool nameOk = SameDevice(DtPart(after), newDt[k]) && SuffixOk(after, wantName[k], newDt[k]);
                        bool visOk = VisibleOk(fn, newDt[k]);
                        if (nameOk && NormDt(vis).Length == 0) d["takesTagFromBox"] = true;
                        d["visibleVerified"] = visOk;
                        d["verified"] = nameOk && (visOk || nameRoute == "name");
                        if (nameOk && !exact) d["verifiedBy"] = "sameDevice";
                        if (!nameOk)
                            throw new Exception("Item " + i + ": EPLAN stored '" + after + "' for requested '" + wantName[k] + "'.");
                        if (!visOk && nameRoute != "name")
                            throw new Exception("Item " + i + ": displayed device tag is '" + vis + "' for requested '" +
                                iTag[i] + "' (name '" + after + "', route " + route + ").");
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

                    // v6: as EPLAN does on the next edit of the page - evaluate
                    // every name on the touched pages NOW, so a point that would
                    // nest (500S1-500S1:1) shows up in THIS call (and is rolled back).
                    if (evalPages && ns != null)
                    {
                        MethodInfo ev = MethodByShape(ns.GetType(), "EvaluateAndSetAllNames", new string[] { "Page" }, false);
                        if (ev == null) results["pagesEvaluated"] = "EvaluateAndSetAllNames(Page) not found";
                        else
                        {
                            for (int p = 0; p < touchedPages.Count; p++) Call(ev, ns, new object[] { touchedPages[p] });
                            results["pagesEvaluated"] = touchedPageNames;
                            for (int pos2 = 0; pos2 < n; pos2++)
                            {
                                int k2 = order[pos2];
                                Dictionary<string, object> d2 = (Dictionary<string, object>)details[pos2];
                                string a2 = PropText(tFn[k2], "Name");
                                if (a2 == null) a2 = "";
                                if (a2 != (string)d2["name"]) d2["nameAfterEvaluation"] = a2;
                                d2["visibleDeviceTag"] = VisibleDt(tFn[k2]);
                                if (!(SameDevice(DtPart(a2), newDt[k2]) && SuffixOk(a2, wantName[k2], newDt[k2])))
                                {
                                    k = k2;
                                    throw new Exception("Item " + tItem[k2] + ": after NameService.EvaluateAndSetAllNames the name is '" +
                                        a2 + "' for requested '" + wantName[k2] + "' (nested or changed device tag).");
                                }
                            }
                        }
                    }
                }
                catch (Exception exW)
                {
                    results["failedAtItem"] = (k >= 0 && k < n) ? tItem[k] : -1;
                    results["modified"] = modified;
                    if (rollback)
                    {
                        List<object> rb = new List<object>();
                        List<int> rbK = new List<int>();
                        for (int r0 = 0; r0 < n; r0++)
                        {
                            int r = order[r0];
                            if (!nameTouched[r] && !connTouched[r]) continue;
                            Dictionary<string, object> rd = new Dictionary<string, object>();
                            rd["handle"] = tHandle[r];
                            try
                            {
                                if (connTouched[r]) SetConnDesignation(tFn[r], connIdx[r], prevConn[r]);
                                if (nameTouched[r])
                                {
                                    SetFunctionName(tFn[r], prevName[r]);
                                    if (ns != null && nameRoute != "name")
                                    {
                                        try { NsCall(ns, "AdjustVisibleName", FnPage(tFn[r]), tFn[r]); }
                                        catch (Exception exA) { rd["adjustVisibleError"] = Flatten(exA); }
                                    }
                                }
                                rd["name"] = PropText(tFn[r], "Name");
                                rd["restored"] = string.Equals(PropText(tFn[r], "Name"), prevName[r], StringComparison.Ordinal);
                            }
                            catch (Exception exR)
                            {
                                rd["restored"] = false;
                                rd["error"] = Flatten(exR);
                            }
                            rb.Add(rd);
                            rbK.Add(r);
                        }
                        if (evalPages && ns != null)
                        {
                            try
                            {
                                MethodInfo ev2 = MethodByShape(ns.GetType(), "EvaluateAndSetAllNames", new string[] { "Page" }, false);
                                if (ev2 != null)
                                {
                                    for (int p = 0; p < touchedPages.Count; p++) Call(ev2, ns, new object[] { touchedPages[p] });
                                    for (int q = 0; q < rb.Count; q++)
                                    {
                                        Dictionary<string, object> rq = (Dictionary<string, object>)rb[q];
                                        string nq = PropText(tFn[rbK[q]], "Name");
                                        rq["name"] = nq;
                                        rq["restored"] = string.Equals(nq, prevName[rbK[q]], StringComparison.Ordinal);
                                    }
                                }
                            }
                            catch (Exception exE) { results["rollbackEvaluationError"] = Flatten(exE); }
                        }
                        results["rollback"] = rb;
                        results["undo"] = new List<object>();
                    }
                    List<string> followF = new List<string>();
                    for (int f = 0; f < n; f++) followF.Add(DtPart(prevName[f]));
                    int otherF;
                    results["renamedOthers"] = RenamedOthers(all, nameBefore, tHandle, followF, 200, out otherF);
                    results["renamedOthersCount"] = otherF;
                    throw new Exception("Renaming stopped: " + Flatten(exW) + (rollback
                        ? " - every change of this call was rolled back (see rollback)."
                        : " - changes before this item stay written (see details / undo)."));
                }
                results["modified"] = modified;
                List<string> follow = new List<string>();
                for (int f = 0; f < n; f++) follow.Add(DtPart(prevName[f]));
                int others;
                results["renamedOthers"] = RenamedOthers(all, nameBefore, tHandle, follow, 200, out others);
                results["renamedOthersCount"] = others;
            }
            results["success"] = true;
'''

_TAG_ROUTES = ("auto", "visible", "name")

# Kept in the compact answer (v6, inbox 2026-10-29_eplan_preobsezen_izhod_insert_set_tags.md).
_TAG_COMPACT_KEYS = ("success", "error", "errorType", "project", "dryRun", "nameRoute",
                     "functionsScanned", "modified", "merges", "wouldRefuse", "note", "warning",
                     "warnings", "undo", "rollback", "failedAtItem", "pagesEvaluated",
                     "renamedOthersCount", "rollbackEvaluationError")


def _tag_line(d):
    if "name" not in d:
        return "%s: %s -> %s" % (d.get("handle"), d.get("previousName"), d.get("newName"))
    return "%s: %s -> %s, displayed %r%s%s" % (
        d.get("handle"), d.get("previousName"), d.get("nameAfterEvaluation") or d.get("name"),
        d.get("visibleDeviceTag"), " (tag of its box)" if d.get("takesTagFromBox") else "",
        "" if d.get("verified") else " NOT VERIFIED")


def _compact_tags(out):
    """v6: one line per function instead of planned + details (~12 kB for one macro)."""
    rows = out.get("details") or out.get("planned") or []
    compact = {k: out[k] for k in _TAG_COMPACT_KEYS if k in out}
    compact["functions"] = [_tag_line(d) for d in rows]
    bad = [d for d in out.get("details") or [] if not d.get("verified")]
    if bad:
        compact["unverified"] = bad
    others = out.get("renamedOthers") or []
    compact["followersCount"] = sum(1 for r in others if r.get("followsRenamedDevice"))
    unexpected = [r for r in others if not r.get("followsRenamedDevice")]
    if unexpected:
        compact["unexpectedRenames"] = unexpected
    if not out.get("success") and "planned" in out:
        compact["planned"] = out["planned"]   # a failure keeps the whole plan
    compact["detailsOmitted"] = "planned / details / renamedOthers: verbose=true"
    return compact


def live_set_device_tags(items: list, allow_merge: bool = False, dry_run: bool = False,
                         rollback_on_error: bool = True, allow_real_project: bool = False,
                         timeout_seconds: float = 300.0, name_route: str = "auto",
                         evaluate_pages: bool = True, verbose: bool = False) -> dict:
    """
    Set the device tag (BMK) of functions - also PLC connection points
    (clrType Terminal), PLC boxes (PLC) and every other Function subclass -
    one or many in ONE call. WRITES - scratch-only unless allow_real_project=True.

    v5: e.g. 510K3:9..16 -> 510K4:1..8, or the
    PLC box of an inserted ET 200SP macro K1 -> 510K3. Not written to
    FUNC_IDENTDEVICETAG & co., which are computed (S063113).

    v6 - the DISPLAYED tag: v5 set only FunctionBase.Name; EPLAN kept the
    visible-name format of the macro tag "-K1", so 510S1 was shown as "-S1".
    name_route "auto" (default) sets the name, then lets EPLAN's NameService
    (API 2.9, as the property dialog) derive the visible name from it
    (AdjustVisibleName); if the displayed tag still differs it types the tag as
    the visible name (VisibleName + AdjustFullName). Each function is verified
    on the name AND the displayed tag (FUNC_VISIBLEDEVICETAG, "visibleDeviceTag").
    Boxes are renamed before PLC points; a point inside its box keeps no own
    displayed tag and TAKES the box's tag ("takesTagFromBox") - so a window
    macro needs ONE item: the box (by handle); its points follow. With
    evaluate_pages EPLAN re-evaluates every name on the touched pages in the
    same call, so a nested tag (500S1-500S1:1) fails here and is rolled back.

    Each item:
        selector: "handle" (from live_read_function_properties / live_read_page)
            or "name" (exact name, the device tag without ":x", or only the last
            segment "510K3"), narrowed by "page" and "connection" (the CURRENT
            connection point designation, e.g. "9"). Must match ONE function.
        "tag": new device tag, e.g. "510S1" or "-510S1" (displayed as given;
            v6 adds no "-" to a tag that had none). Without '=' / '+' only the
            last segment is replaced and the structure kept ("510K4" on
            "=AB-C+DI-510K3" -> "=AB-C+DI-510K4"); with '=' or '+' it is whole.
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
    ("merges" lists them; v6 compares without the sign "-", so "-510K3" finds
    an existing "510K3"). On an error everything this call wrote is rolled back
    (rollback_on_error) and "rollback" reports it. "undo" holds the items that
    restore the previous tags - pass it back as items. EPLAN stores the change
    in the project database itself (there is no save command; see
    get_current_project "databaseLastWrite").

    Args:
        items: list of items (see above), at most 500.
        allow_merge: Permit new tags that already exist in the project.
        dry_run: Resolve and plan only ("planned", "merges", "wouldRefuse");
            nothing is written, so no allow_real_project needed.
        rollback_on_error: Default True.
        allow_real_project: Must be True to write outside the scratch root.
        timeout_seconds: Default 300 (the project is walked once per call).
        name_route: v6. "auto" (default), "visible" (visible name +
            AdjustFullName only) or "name" (v5: FunctionBase.Name only, the
            displayed tag is reported but not enforced).
        evaluate_pages: v6. Default True: NameService.EvaluateAndSetAllNames on
            every touched page after the writes, then everything re-verified.
        verbose: v6. False (default): one line per function ("functions"),
            failures in full ("unverified"), "unexpectedRenames"; True: also
            "planned", "details", "renamedOthers".

    Returns:
        {"success", "project", "dryRun", "nameRoute", "functionsScanned",
         "functions": ["handle: previous -> name, displayed '...'"], "merges":
         {tag: {"existingFunctions", "examples"}}, "modified", "undo": [items],
         "pagesEvaluated", "renamedOthersCount", "followersCount" (points that
         followed their renamed box), "unexpectedRenames" (other functions EPLAN
         renamed), "unverified", "rollback", "failedAtItem"; verbose adds
         "planned": [{"item", "handle", "page", "clrType", "previousName",
         "visibleBefore", "newName", "connectionIndex", "previousConnection",
         "newConnection"}], "details": [... + "name", "visibleDeviceTag",
         "visibleName", "route", "verified", "visibleVerified", "verifiedBy",
         "takesTagFromBox", "nameAfterEvaluation"], "renamedOthers"}
    """
    try:
        its = _tag_items(items)
        timeout_seconds = float(timeout_seconds)
        route = str(name_route or "auto").strip().lower()
        if route not in _TAG_ROUTES:
            raise SchematicValueError("name_route must be 'auto', 'visible' or 'name', got %r." % (name_route,))
    except SchematicValueError as exc:
        return _err(exc)
    except (TypeError, ValueError) as exc:
        return _err(exc)

    body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
        _SET_TAGS_BODY,
        DRYRUN=cs_bool(dry_run), ALLOWMERGE=cs_bool(allow_merge),
        ROLLBACK=cs_bool(rollback_on_error),
        NAMEROUTE='"%s"' % route, EVALPAGES=cs_bool(evaluate_pages),
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
             "failedAtItem", "modified", "dryRun", "nameRoute", "renamedOthers",
             "renamedOthersCount", "pagesEvaluated", "rollbackEvaluationError"))
    if out.get("success"):
        if dry_run and out.get("wouldRefuse"):
            out["note"] = ("Dry run: the write would be refused - %s already exist(s). "
                           "Pass allow_merge=true if the merge is intended."
                           % ", ".join(sorted(out.get("merges") or {})))
        warnings = []
        unver = [d.get("handle") for d in out.get("details") or [] if not d.get("visibleVerified")]
        if unver and route == "name":
            warnings.append("name_route 'name': the displayed device tag differs from the requested "
                            "one for %s - compare 'visibleDeviceTag'." % ", ".join(unver))
        unexpected = [r for r in out.get("renamedOthers") or [] if not r.get("followsRenamedDevice")]
        if unexpected:
            warnings.append("EPLAN also renamed %d other function(s) - see unexpectedRenames / renamedOthers."
                            % len(unexpected))
        if warnings:
            out["warnings"] = warnings
    if not verbose and ("planned" in out or "details" in out):
        out = _compact_tags(out)
    return out


# ---------------------------------------------------------------------------
# v8  show a property of a function on the sheet (property placement)
# ---------------------------------------------------------------------------
# inbox 2026-10-30_eplan_ni_orodja_prikaz_lastnosti_na_skatli.md (
# FUNC_PLCTYPE_ID of 26 Siemens PLC boxes is filled in but not shown).
#
# API 2.9 (eplan.help, Eplan.EplApi.DataModel):
#   SymbolReference.PropertyPlacements          PropertyPlacement[] {get;}
#   SymbolReference.UseLocalPropertyPlacements  bool {get; set;}
#   SymbolReference.SwitchLocalPropertyPlacements(bool)
#   Graphics.PropertyPlacement()  +  .Create(SymbolReference, AnyPropertyId)
#                                    .Create(SymbolReference, AnyPropertyId, RelationType, int)
#   PropertyPlacement.DisplayedProperty (AnyPropertyId), IsSetAsVisible
#   (Placement.Visibility), IsVisible, Location, Height, Justification
#   (TextBase.JustificationType), GetDisplayString()
#   PropertyValue.Id (AnyPropertyId), AnyPropertyId.AsInt / Index
# Class example (PropertyPlacement, API 2.9):
#   oFunction.SwitchLocalPropertyPlacements(true);
#   PropertyPlacement p = new PropertyPlacement(); p.Create(oFunction, 20011);
#   p.IsVisible = true; p.Height = 15; p.Location = new PointD(10.0, 25.0);

_HELPERS_PP = r'''
    // ---- v8: live_place_function_property --------------------------------
    // The symbol outline: GetLogicalArea() (RectangleD LowerLeft/UpperRight;
    // API 2.9 throws NotImplementedException where there is none), else
    // GetBoundingBox(). mode: "auto" / "logical_area" / "bounding_box".
    static double[] SymbolBox(object sr, string mode, out string source, Dictionary<string, object> info)
    {
        source = null;
        double[] la = null;
        MethodInfo gl = MethodByShape(sr.GetType(), "GetLogicalArea", new string[] { }, false);
        if (gl != null)
        {
            try
            {
                object r = gl.Invoke(sr, null);
                object ll = r == null ? null : TryRead(r, "LowerLeft", null);
                object ur = r == null ? null : TryRead(r, "UpperRight", null);
                if (ll != null && ur != null)
                {
                    double[] b = BBoxAdd(BBoxAdd(null, ll), ur);
                    if (b[2] - b[0] > 0.001 && b[3] - b[1] > 0.001) la = b;
                }
            }
            catch (TargetInvocationException tie) { info["logicalAreaError"] = Flatten(tie.InnerException); }
            catch (Exception ex) { info["logicalAreaError"] = Flatten(ex); }
        }
        double[] bb = BBoxOf(sr);
        if (la != null) info["logicalArea"] = MinMax(la);
        if (bb != null) info["boundingBox"] = MinMax(bb);
        if (mode != "bounding_box" && la != null) { source = "logicalArea"; return la; }
        if (mode == "logical_area") return null;
        if (bb != null) source = "boundingBox";
        return bb;
    }

    static object PpFind(object sr, int want, int wantIdx, string rel, bool shownOnly)
    {
        object arr = TryRead(sr, "PropertyPlacements", null);
        if (!(arr is IEnumerable)) return null;
        object hidden = null;
        foreach (object pp in (IEnumerable)arr)
        {
            if (pp == null) continue;
            if (PpPropId(pp) != want) continue;
            int ix = PpPropIdx(pp);
            if (wantIdx > 0 && ix > 0 && ix != wantIdx) continue;
            if (rel.Length > 0 && SafeText(TryRead(pp, "Relation", null)) != rel) continue;
            if (PpShown(pp)) return pp;
            if (hidden == null) hidden = pp;
        }
        return shownOnly ? null : hidden;
    }

    static int PpCount(object sr, int want)
    {
        object arr = TryRead(sr, "PropertyPlacements", null);
        int n = 0;
        if (arr is IEnumerable) foreach (object pp in (IEnumerable)arr) if (pp != null && PpPropId(pp) == want) n++;
        return n;
    }

    // Height of the symbol's other SHOWN property texts ("like the other texts of the macro").
    static double PpTemplateHeight(object sr, int notId, out string from)
    {
        from = null;
        object arr = TryRead(sr, "PropertyPlacements", null);
        if (!(arr is IEnumerable)) return 0;
        foreach (object pp in (IEnumerable)arr)
        {
            if (pp == null || PpPropId(pp) == notId || !PpShown(pp)) continue;
            object h = TryRead(pp, "Height", null);
            double v = 0;
            try { v = h == null ? 0 : Convert.ToDouble(h); } catch { v = 0; }
            if (v > 0) { from = Handle(pp) + " (property " + PpPropId(pp) + ")"; return v; }
        }
        return 0;
    }

    static string PpMakeVisible(object pp)
    {
        List<string> routes = new List<string>();
        PropertyInfo iv = GetWritable(pp.GetType(), "IsVisible");
        if (iv != null)
        {
            try { iv.SetValue(pp, true, null); routes.Add("IsVisible=true"); }
            catch (TargetInvocationException tie) { routes.Add("IsVisible threw " + Flatten(tie.InnerException)); }
            catch (Exception ex) { routes.Add("IsVisible threw " + Flatten(ex)); }
        }
        PropertyInfo sv = GetWritable(pp.GetType(), "IsSetAsVisible");
        if (sv != null && sv.PropertyType.IsEnum)
        {
            try { sv.SetValue(pp, Enum.Parse(sv.PropertyType, "Visible"), null); routes.Add("IsSetAsVisible=Visible"); }
            catch (TargetInvocationException tie) { routes.Add("IsSetAsVisible threw " + Flatten(tie.InnerException)); }
            catch (Exception ex) { routes.Add("IsSetAsVisible threw " + Flatten(ex)); }
        }
        if (iv == null && sv == null)
            throw new Exception("PropertyPlacement has neither a writable IsVisible nor IsSetAsVisible. " +
                MemberList(pp.GetType(), false));
        if (!PpShown(pp))
            throw new Exception("Property placement " + Handle(pp) + " is still not visible after the write (" +
                string.Join("; ", routes.ToArray()) + "; IsSetAsVisible=" + PpVisibility(pp) + ").");
        return string.Join("; ", routes.ToArray());
    }

    static void PpSetLocation(object pp, double x, double y)
    {
        PropertyInfo pi = RequireWritable(pp.GetType(), "Location");
        try { pi.SetValue(pp, MakePoint(pi.PropertyType, x, y), null); }
        catch (TargetInvocationException tie) { throw new Exception("Location: " + Flatten(tie.InnerException)); }
    }

    static void PpSetJustification(object pp, string name)
    {
        PropertyInfo pi = RequireWritable(pp.GetType(), "Justification");
        object v;
        try { v = Enum.Parse(pi.PropertyType, name, true); }
        catch
        {
            throw new Exception("justification '" + name + "' is not a " + pi.PropertyType.FullName +
                " value. Values: " + string.Join(", ", Enum.GetNames(pi.PropertyType)));
        }
        try { pi.SetValue(pp, v, null); }
        catch (TargetInvocationException tie) { throw new Exception("Justification: " + Flatten(tie.InnerException)); }
    }

    static void PpSetHeight(object pp, double h)
    {
        PropertyInfo pi = RequireWritable(pp.GetType(), "Height");
        try { pi.SetValue(pp, h, null); }
        catch (TargetInvocationException tie) { throw new Exception("Height: " + Flatten(tie.InnerException)); }
    }

    static void PpRestore(object o, string prop, object value)
    {
        if (o == null || value == null) return;
        PropertyInfo pi = GetWritable(o.GetType(), prop);
        if (pi != null) pi.SetValue(o, value, null);
    }

    static MethodInfo PpCreateMethod(bool withRelation)
    {
        Type ppType = FindType("Eplan.EplApi.DataModel.Graphics.PropertyPlacement");
        return withRelation
            ? RequireMethod(ppType, "Create", new string[] { "SymbolReference", "AnyPropertyId", "RelationType", "Int32" }, false)
            : RequireMethod(ppType, "Create", new string[] { "SymbolReference", "AnyPropertyId" }, false);
    }

    static object PpCreate(object sr, object anyId, string rel, int relIdx)
    {
        Type ppType = FindType("Eplan.EplApi.DataModel.Graphics.PropertyPlacement");
        object pp = Activator.CreateInstance(ppType);
        if (rel.Length == 0)
        {
            Call(PpCreateMethod(false), pp, new object[] { sr, anyId });
            return pp;
        }
        MethodInfo cr = PpCreateMethod(true);
        Type rt = cr.GetParameters()[2].ParameterType;
        object rv;
        try { rv = Enum.Parse(rt, rel, true); }
        catch
        {
            throw new Exception("relation '" + rel + "' is not a " + rt.FullName + " value. Values: " +
                string.Join(", ", Enum.GetNames(rt)));
        }
        Call(cr, pp, new object[] { sr, anyId, rv, relIdx });
        return pp;
    }

    static bool ReadBool(object o, string name, bool dflt)
    {
        object v = TryRead(o, name, null);
        if (v == null) return dflt;
        try { return Convert.ToBoolean(v); } catch { return dflt; }
    }

    static Dictionary<string, object> XY(double x, double y)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["x"] = Math.Round(x, 4);
        d["y"] = Math.Round(y, 4);
        return d;
    }
'''


_PLACE_PP_BODY = '''
            string[] v8handles = V8HANDLES;
            string v8page = V8PAGE;
            string v8pname = V8PNAME;
            int v8pidx = V8PIDX;
            int v8pnum = V8PNUM;
            string v8pos = V8POS;
            double v8dx = V8DX;
            double v8dy = V8DY;
            double v8x = V8X;
            double v8y = V8Y;
            double v8height = V8HEIGHT;
            string v8just = V8JUST;
            bool v8repos = V8REPOS;
            string v8rel = V8REL;
            int v8relIdx = V8RELIDX;
            bool v8local = V8LOCAL;
            bool v8dry = V8DRY;
            bool v8rollback = V8ROLLBACK;
            string v8boxsrc = V8BOXSRC;
            string v9frame = V9FRAME;
            bool v9allowOut = V9ALLOWOUT;

            // ---- 1. resolve every handle BEFORE anything is written ----------
            List<string> wanted = new List<string>(v8handles);
            Dictionary<string, object> byH = new Dictionary<string, object>();
            if (v8page.Length > 0)
            {
                object pg = FindPage(project, v8page);
                foreach (object pl in PagePlacements(pg))
                {
                    if (pl == null) continue;
                    string h0 = Handle(pl);
                    // v9: a docked ContextPropertyPlacement has the SAME handle as its box -
                    // the symbol (it carries PropertyPlacements) wins over any other object
                    if (h0 != null && wanted.Contains(h0) && (!byH.ContainsKey(h0) ||
                        (!(TryRead(byH[h0], "PropertyPlacements", null) is IEnumerable) &&
                         TryRead(pl, "PropertyPlacements", null) is IEnumerable))) byH[h0] = pl;
                }
            }
            else
            {
                foreach (object fn in AllFunctions(project))
                {
                    if (fn == null) continue;
                    string h0 = Handle(fn);
                    if (h0 != null && wanted.Contains(h0) && !byH.ContainsKey(h0)) byH[h0] = fn;
                    if (byH.Count == wanted.Count) break;
                }
            }
            List<string> missing = new List<string>();
            foreach (string h in wanted) if (!byH.ContainsKey(h)) missing.Add(h);
            if (missing.Count > 0)
                throw new Exception("Nothing changed: " + missing.Count + " handle(s) not found" +
                    (v8page.Length > 0 ? " on page '" + v8page + "'" : " among the project's functions") + ": " +
                    string.Join(", ", missing.ToArray()) + ". Handles are session-scoped - re-read them " +
                    "(live_read_function_properties / live_read_page).");
            foreach (string h in wanted)
                if (!(TryRead(byH[h], "PropertyPlacements", null) is IEnumerable))
                    throw new Exception("Nothing changed: " + h + " is a " + byH[h].GetType().Name +
                        " - only symbols (SymbolReference: functions, PLC boxes, terminals ...) carry property placements.");

            // ---- 2. the property id ----------------------------------------
            Type anyIdType = FindType("Eplan.EplApi.DataModel.AnyPropertyId");
            object numId = null;
            if (v8pnum >= 0)
            {
                // AnyPropertyId op_Implicit(int) - the API 2.9 class example passes 20011.
                numId = MakeValue(anyIdType, v8pnum);
                if (v8pidx > 0)
                {
                    PropertyInfo ip = GetWritable(anyIdType, "Index");
                    if (ip == null) throw new Exception("AnyPropertyId has no writable Index. " + MemberList(anyIdType, false));
                    ip.SetValue(numId, v8pidx, null);
                }
            }
            results["createApi"] = PpCreateMethod(v8rel.Length > 0).ToString();

            // ---- 3. plan: per symbol, what will happen ----------------------
            List<object> items = new List<object>();
            List<object[]> plan = new List<object[]>();
            int wantId = -1;
            foreach (string h in wanted)
            {
                object fn = byH[h];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["handle"] = h;
                it["clrType"] = fn.GetType().Name;
                string nm = SafeText(TryRead(fn, "Name", null));
                if (nm != null && nm.Length > 0) it["name"] = nm;
                object anyId = numId;
                if (v8pnum < 0)
                {
                    object props = FnProps(fn);
                    PropertyInfo pi = FnPropInfo(props, v8pname, v8pidx);
                    if (pi == null)
                        throw new Exception("Nothing changed: " + props.GetType().Name + " of " + h + " declares no '" + v8pname +
                            (v8pidx > 0 ? "[int]" : "") + "'. Similar names: " +
                            string.Join(", ", PropNamesContaining(props.GetType(), v8pname.Length > 8 ? v8pname.Substring(5, 3) : v8pname, 60).ToArray()) +
                            ". A number (property id) is accepted too.");
                    object pv;
                    try { pv = v8pidx > 0 ? pi.GetValue(props, new object[] { v8pidx }) : pi.GetValue(props, null); }
                    catch (TargetInvocationException tie) { throw new Exception("Nothing changed: reading " + v8pname + " of " + h + ": " + Flatten(tie.InnerException)); }
                    anyId = pv == null ? null : TryRead(pv, "Id", null);
                    if (anyId == null)
                        throw new Exception("Nothing changed: " + v8pname + " of " + h + " gives no PropertyValue.Id (AnyPropertyId).");
                    it["propertyValue"] = PvText(pv);
                }
                int idNow = PpInt(anyId, "AsInt", -1);
                if (idNow < 0) throw new Exception("Nothing changed: AnyPropertyId.AsInt not readable. " + MemberList(anyId.GetType(), false));
                if (wantId < 0) wantId = idNow;
                it["propertyId"] = idNow;
                it["useLocalPropertyPlacements"] = ReadBool(fn, "UseLocalPropertyPlacements", true);
                object found = PpFind(fn, idNow, v8pidx, v8rel, false);
                it["existing"] = PpCount(fn, idNow);
                string action = found == null ? "create" : (PpShown(found) ? "alreadyVisible" : "makeVisible");
                it["action"] = action;
                if (found != null) it["before"] = DumpPropPl(found);

                // target point
                bool place = v8pos != "keep";
                double px = 0, py = 0;
                string jdef = "";
                if (v8pos == "absolute" || v8pos == "page") { px = v8x + v8dx; py = v8y + v8dy; }
                else if (place)
                {
                    string bsrc;
                    double[] box = SymbolBox(fn, v8boxsrc, out bsrc, it);
                    if (box == null)
                        throw new Exception("Nothing changed: no outline for " + h + " (box_source=" + v8boxsrc +
                            ") - use position='absolute' with x, y.");
                    it["boxSource"] = bsrc;
                    if (v8pos == "above_top_left") { px = box[0] + v8dx; py = box[3] + v8dy; jdef = "BottomLeft"; }
                    else if (v8pos == "above_top_right") { px = box[2] + v8dx; py = box[3] + v8dy; jdef = "BottomRight"; }
                    else if (v8pos == "below_bottom_left") { px = box[0] + v8dx; py = box[1] + v8dy; jdef = "TopLeft"; }
                    else if (v8pos == "below_bottom_right") { px = box[2] + v8dx; py = box[1] + v8dy; jdef = "TopRight"; }
                    else { px = box[0] + v8dx; py = box[3] + v8dy; jdef = "TopLeft"; }   // inside_top_left
                }
                string just = v8just.Length > 0 ? v8just : jdef;
                bool move = place && (action == "create" || v8repos);
                // v9 (inbox 2026-10-30_eplan_place_function_property_relativna_lega,
                // 2026-11-02_..._above_top_left_absolutna_v_relativno): on EPLAN 2.9 the
                // Location of a symbol's text is RELATIVE to the symbol's insertion point.
                // v8 wrote the page point (78; 269) there -> drawn at (156; 537), off the sheet.
                int nRel, nPage;
                string fGuess = PpFrameOf(fn, out nRel, out nPage);
                string frame = v9frame != "auto" ? v9frame : (fGuess.Length > 0 ? fGuess : "relative");
                double[] sxy = PlXY(fn);
                double wx = px, wy = py, gx = px, gy = py;   // written Location / point on the page
                if (move)
                {
                    it["locationFrame"] = frame;
                    it["frameSource"] = v9frame != "auto" ? "explicit" : (fGuess.Length > 0
                        ? "existing texts of the symbol: " + nRel + " relative, " + nPage + " page"
                        : "default (EPLAN 2.9: relative to the symbol insertion point)");
                    if (frame == "relative")
                    {
                        if (sxy == null)
                            throw new Exception("Nothing changed: " + h + " has no readable Location (insertion point) - " +
                                "pass location_frame='page' or position='absolute'.");
                        it["symbolLocation"] = XY(sxy[0], sxy[1]);
                        if (v8pos == "absolute") { gx = px + sxy[0]; gy = py + sxy[1]; }
                        else { wx = px - sxy[0]; wy = py - sxy[1]; }
                    }
                    it["targetPoint"] = XY(gx, gy);
                    it["writeLocation"] = XY(wx, wy);
                    object fpg = TryRead(fn, "Page", null);
                    object psz = fpg == null ? null : TryRead(fpg, "Size", null);
                    if (psz != null)
                    {
                        Dictionary<string, object> psd = PtDict(psz);
                        double pw = Convert.ToDouble(psd["x"]), ph = Convert.ToDouble(psd["y"]);
                        if (pw > 1 && ph > 1)
                        {
                            bool inside = gx >= -0.01 && gy >= -0.01 && gx <= pw + 0.01 && gy <= ph + 0.01;
                            it["insidePage"] = inside;
                            if (!inside && !v9allowOut)
                                throw new Exception("Nothing changed: OUTSIDE_PAGE - the text of " + h + " would land at (" +
                                    Math.Round(gx, 2) + "; " + Math.Round(gy, 2) + ") on the page, outside the page " +
                                    pw + " x " + ph + " mm (allow_outside_page=true to force).");
                        }
                    }
                }
                if (move && just.Length > 0) it["justification"] = just;
                string hfrom;
                double th = PpTemplateHeight(fn, idNow, out hfrom);
                double hgt = v8height > 0 ? v8height : (action == "create" ? th : 0);
                if (hgt > 0)
                {
                    it["height"] = Math.Round(hgt, 4);
                    it["heightSource"] = v8height > 0 ? "explicit" : "like " + hfrom;
                }
                else if (action == "create") it["heightSource"] = "EPLAN default (no other visible property text on this symbol)";
                items.Add(it);
                plan.Add(new object[] { h, fn, anyId, move, wx, wy, just, hgt, it });
            }
            if (wantId >= 0) results["propertyId"] = wantId;
            results["items"] = items;
            results["dryRun"] = v8dry;

            // ---- 4. write (one LockingStep, all or nothing with rollback) ---
            if (!v8dry)
            {
                List<object[]> undo = new List<object[]>();   // {kind, object, property, old value}
                Dictionary<string, object> failedAt = null;
                foreach (object[] p in plan)
                {
                    string h = (string)p[0];
                    object fn = p[1];
                    object anyId = p[2];
                    bool move = (bool)p[3];
                    double px = (double)p[4], py = (double)p[5];
                    string just = (string)p[6];
                    double hgt = (double)p[7];
                    Dictionary<string, object> it = (Dictionary<string, object>)p[8];
                    string step = "start";
                    try
                    {
                        string action = (string)it["action"];
                        bool needWrite = action != "alreadyVisible" || move || hgt > 0;
                        if (!needWrite) { it["result"] = "alreadyVisible"; continue; }
                        // Placements of the symbol VARIANT cannot be changed (read-only symbol
                        // library) - local copies first, as in the API 2.9 class example.
                        if (!ReadBool(fn, "UseLocalPropertyPlacements", true))
                        {
                            if (!v8local)
                                throw new Exception("the symbol uses its variant's property placements and make_local=false");
                            step = "SwitchLocalPropertyPlacements(true)";
                            Call(RequireMethod(fn.GetType(), "SwitchLocalPropertyPlacements", new string[] { "Boolean" }, false), fn, new object[] { true });
                            undo.Add(new object[] { "local", fn, null, null });
                            it["switchedToLocal"] = true;
                        }
                        step = "find";
                        object pp = PpFind(fn, PpInt(anyId, "AsInt", -1), v8pidx, v8rel, false);
                        string result;
                        if (pp == null)
                        {
                            step = "PropertyPlacement.Create";
                            pp = PpCreate(fn, anyId, v8rel, v8relIdx);
                            undo.Add(new object[] { "created", pp, null, null });
                            step = "make visible";
                            it["visibleRoute"] = PpMakeVisible(pp);
                            result = "created";
                        }
                        else if (!PpShown(pp))
                        {
                            step = "make visible";
                            undo.Add(new object[] { "set", pp, "IsSetAsVisible", TryRead(pp, "IsSetAsVisible", null) });
                            undo.Add(new object[] { "set", pp, "IsVisible", TryRead(pp, "IsVisible", null) });
                            it["visibleRoute"] = PpMakeVisible(pp);
                            result = "madeVisible";
                        }
                        else result = "alreadyVisible";
                        if (hgt > 0)
                        {
                            step = "Height";
                            if (result != "created") undo.Add(new object[] { "set", pp, "Height", TryRead(pp, "Height", null) });
                            PpSetHeight(pp, hgt);
                        }
                        if (move)
                        {
                            if (just.Length > 0)
                            {
                                step = "Justification";
                                if (result != "created") undo.Add(new object[] { "set", pp, "Justification", TryRead(pp, "Justification", null) });
                                PpSetJustification(pp, just);
                            }
                            step = "Location";
                            if (result != "created") undo.Add(new object[] { "set", pp, "Location", TryRead(pp, "Location", null) });
                            PpSetLocation(pp, px, py);
                            if (result != "created") result += "+moved";
                        }
                        it["result"] = result;
                        Dictionary<string, object> after = DumpPropPl(pp);
                        it["after"] = after;
                        if (move)
                        {
                            // Read back: the stored Location, and whether the drawn text sits at the
                            // requested point (a Location in another frame would show up here).
                            object loc = TryRead(pp, "Location", null);
                            double[] bb = BBoxOf(pp);
                            if (loc != null)
                            {
                                Dictionary<string, object> l = PtDict(loc);
                                double ddx = Convert.ToDouble(l["x"]) - px, ddy = Convert.ToDouble(l["y"]) - py;
                                it["locationDelta"] = Math.Round(Math.Sqrt(ddx * ddx + ddy * ddy), 4);
                            }
                            if (bb != null)
                            {
                                double tol = Math.Max(5.0, 3 * (hgt > 0 ? hgt : 2.5));
                                it["textNearTarget"] = px >= bb[0] - tol && px <= bb[2] + tol && py >= bb[1] - tol && py <= bb[3] + tol;
                            }
                            // v9: where the text lands on the page (Location + insertion point when relative)
                            if (loc != null && it.ContainsKey("locationFrame"))
                            {
                                Dictionary<string, object> l2 = PtDict(loc);
                                double lx = Convert.ToDouble(l2["x"]), ly = Convert.ToDouble(l2["y"]);
                                double[] s2 = PlXY(fn);
                                if ((string)it["locationFrame"] == "relative" && s2 != null) { lx += s2[0]; ly += s2[1]; }
                                it["pageLocation"] = XY(lx, ly);
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["handle"] = h;
                        failedAt["step"] = step;
                        failedAt["error"] = Flatten(ex);
                        break;
                    }
                }
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (v8rollback)
                    {
                        List<string> rbErr = new List<string>();
                        int rb = 0;
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            object[] u = undo[k];
                            try
                            {
                                string kind = (string)u[0];
                                if (kind == "created")
                                {
                                    if (IsValidObj(u[1])) Call(RequireMethod(u[1].GetType(), "Remove", new string[] { }, false), u[1], null);
                                }
                                else if (kind == "local")
                                    Call(RequireMethod(u[1].GetType(), "SwitchLocalPropertyPlacements", new string[] { "Boolean" }, false), u[1], new object[] { false });
                                else if (IsValidObj(u[1])) PpRestore(u[1], (string)u[2], u[3]);
                                rb++;
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
            }
'''


_PP_POSITIONS = {
    # position: (default dx, default dy, default justification)
    "above_top_left": (0.0, 1.0, "BottomLeft"),
    "above_top_right": (0.0, 1.0, "BottomRight"),
    "below_bottom_left": (0.0, -1.0, "TopLeft"),
    "below_bottom_right": (0.0, -1.0, "TopRight"),
    "inside_top_left": (1.0, -1.0, "TopLeft"),
    "absolute": (0.0, 0.0, ""),
    "keep": (0.0, 0.0, ""),
    "page": (0.0, 0.0, ""),      # v9: x, y in page mm (converted to the symbol's frame)
}

# TextBase.JustificationType (API 2.9)
_PP_JUSTIFICATIONS = (
    "FromLayer", "TopLeft", "TopCenter", "TopRight", "MiddleLeft", "MiddleCenter", "MiddleRight",
    "BottomLeft", "BottomCenter", "BottomRight", "BaseLineLeft", "BaseLineCenter", "BaseLineRight",
    "SpecialLeft", "SpecialCenter", "SpecialRight", "JicSpecialLeft", "JicSpecialCenter", "JicSpecialRight")

_PP_NUMERIC = re.compile(r"^(\d{1,7})(?:\[(\d{1,3})\])?$")
_PP_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _pp_property(property_id):
    """'FUNC_PLCTYPE_ID' / 'FUNC_X[2]' / '20011' / 20011 -> (name, index, number)."""
    if isinstance(property_id, bool) or property_id is None:
        raise SchematicValueError(
            "property_id is required: a FunctionPropertyList name (\"FUNC_PLCTYPE_ID\", "
            "\"FUNC_CONNECTIONDESIGNATION[1]\") or a property number (\"20011\").")
    if isinstance(property_id, int):
        property_id = str(property_id)
    if not isinstance(property_id, str):
        raise SchematicValueError("property_id must be a string, got %s." % type(property_id).__name__)
    p = property_id.strip()
    m = _PP_NUMERIC.match(p)
    if m:
        idx = int(m.group(2)) if m.group(2) else 0
        if m.group(2) is not None and idx < 1:
            raise SchematicValueError("property_id: index in %r is 1-based." % p)
        return "", idx, int(m.group(1))
    (name, idx), = _parse_props([p], "property_id")
    return name, idx, -1


def live_place_function_property(property_id: str = None, handles: list = None,
                                 handle: str = None, page: str = None,
                                 position: str = None, dx: float = None, dy: float = None,
                                 x: float = None, y: float = None, height: float = None,
                                 justification: str = None, reposition: bool = None,
                                 box_source: str = "auto", relation: str = None,
                                 relation_index: int = 1, make_local: bool = True,
                                 dry_run: bool = True, allow_real_project: bool = False,
                                 rollback_on_error: bool = True, max_items: int = 100,
                                 timeout_seconds: float = 180.0,
                                 location_frame: str = "auto",
                                 allow_outside_page: bool = False) -> dict:
    """
    v8: SHOW a property of a function on the sheet - property placement
    (Eigenschaftsplatzierung / "Eigenschaftsanordnung", display a property
    text such as the PLC type designation FUNC_PLCTYPE_ID on a PLC box).
    WRITES (unless dry_run, the default) - scratch-only unless
    allow_real_project=True (dry_run only reads).

    For each symbol (Function, PLC box, terminal, ... - any SymbolReference)
    given by handle:
      - the property is ALREADY SHOWN       -> nothing ("alreadyVisible"),
      - a placement of it exists but hidden -> made visible ("madeVisible";
        IsVisible=true + IsSetAsVisible=Visible) - no second text is added,
      - none exists                         -> a new PropertyPlacement is
        created (PropertyPlacement.Create(symbol, AnyPropertyId), API 2.9),
        made visible, sized and placed ("created").
    Many symbols in ONE call (handles=[...], one LockingStep); every handle
    and the property are checked before the first write; on an error all
    changes of this call are undone (rollback_on_error).

    A symbol that uses its variant's placements (UseLocalPropertyPlacements
    false) is switched to local placements first
    (SwitchLocalPropertyPlacements(true), as in the API 2.9 example) -
    make_local=False refuses instead.

    Args:
        property_id: FunctionPropertyList name, e.g. "FUNC_PLCTYPE_ID" (PLC type
            designation), "FUNC_TECHNICAL_CHARACTERISTIC", "FUNC_PLCRACK",
            "FUNC_PLCSOCKET", optionally indexed "FUNC_X[2]"; or the property
            number as a string, "20011". The value is read per function
            ("propertyValue" - an EMPTY value is warned about: the text would
            show nothing).
        handles / handle: the symbols (from live_read_function_properties or
            live_read_page; session-scoped). At most max_items (default 100).
        page: optional - resolve the handles on this page only (faster than
            the whole project).
        position: where the NEW text goes (and an existing one when moved):
            "above_top_left" (default; justification BottomLeft, dy=+1 mm),
            "above_top_right", "below_bottom_left", "below_bottom_right",
            "inside_top_left", "page" (x, y in page mm), "absolute" (x, y
            written to Location AS IS) or "keep" (where EPLAN puts it /
            where it is).
            Relative positions use the symbol outline: GetLogicalArea, else
            GetBoundingBox (box_source "auto" / "logical_area" /
            "bounding_box"; both are reported per item).
            v9: on EPLAN 2.9 the Location of a symbol's text is RELATIVE to
            the symbol's insertion point (device tag of a PLC box at (-1, 0));
            the page point is converted (minus SymbolReference.Location).
            v8 wrote page mm there - the text landed off the sheet.
        dx, dy: offset in mm added to the point (defaults per position).
        x, y: page mm for position="page"; for "absolute" the raw
            Location (on 2.9 = mm from the insertion point, e.g. x=0, y=1:
            1 mm above the insertion point - the v8 workaround keeps working).
        location_frame: v9. "auto" (default: the frame the symbol's other
            texts use; no texts -> "relative", as on EPLAN 2.9),
            "relative" or "page". Reported per item: "locationFrame",
            "frameSource", "symbolLocation", "targetPoint" (page mm),
            "writeLocation" (written), after the write "pageLocation".
        allow_outside_page: v9. Default False - a text that would land
            outside the page (Page.Size) -> nothing changed, errorType
            OUTSIDE_PAGE ("insidePage" per item).
        height: text height in mm. Default: like the symbol's other visible
            property texts ("heightSource"); existing texts keep theirs.
        justification: TextBase.JustificationType name (BottomLeft, TopLeft,
            ...); default per position.
        reposition: move an EXISTING text to the position too. Default: only
            when position was given explicitly.
        relation, relation_index: the 4-argument Create(..., RelationType,
            int) for article properties (e.g. relation name of
            PropertyPlacement.RelationType); omit for function properties.
        make_local: see above (default True).
        dry_run: default True - reports per symbol "action", "before",
            "targetPoint", "height", "boxSource" and changes nothing.
        allow_real_project: must be True to write outside the scratch root.
        rollback_on_error: default True.
        timeout_seconds: default 180.

    Returns:
        {"success", "dryRun", "propertyId", "counts" {action/result: n},
         "items": [{handle, name, clrType, propertyValue, existing, action,
         before, targetPoint, justification, height, heightSource, boxSource,
         logicalArea, boundingBox, useLocalPropertyPlacements, and after a real
         run: result, after {handle, text, visible, setAsVisible, location,
         boundingBox, height}, locationDelta, textNearTarget, switchedToLocal}],
         "warnings"}. A failed write: success false, errorType "ROLLED_BACK"
        (or "PARTIAL" with rollback_on_error=False), "failedAt" {handle, step,
        error}. Check the result with live_read_page(...,
        include_property_placements=True).
    """
    try:
        pname, pidx, pnum = _pp_property(property_id)
        if handles is not None and not isinstance(handles, (list, tuple)):
            raise SchematicValueError("handles must be a list of strings (use handle= for one).")
        hl = []
        for v in ([handle] if handle else []) + list(handles or []):
            if not isinstance(v, str) or not v.strip():
                raise SchematicValueError("handles entries must be non-empty strings, got %r." % (v,))
            if v.strip() not in hl:
                hl.append(v.strip())
        if not hl:
            raise SchematicValueError("Pass handle='...' or handles=[...] (symbols from live_read_function_properties / live_read_page).")
        mx = cs_int(max_items, "max_items", minimum=1, maximum=500)
        if len(hl) > mx:
            raise SchematicValueError("%d handles, more than max_items=%d - nothing changed." % (len(hl), mx))
        pos = position if position is not None else "above_top_left"
        if pos not in _PP_POSITIONS:
            raise SchematicValueError("position must be one of %s, got %r." % (", ".join(_PP_POSITIONS), position))
        ddx, ddy, djust = _PP_POSITIONS[pos]
        if pos == "absolute" and (x is None or y is None):
            raise SchematicValueError("position='absolute' needs x and y (page mm).")
        if pos == "page" and (x is None or y is None):
            raise SchematicValueError("position='page' needs x and y (page mm).")
        if location_frame not in ("auto", "relative", "page"):
            raise SchematicValueError("location_frame must be 'auto', 'relative' or 'page', got %r." % (location_frame,))
        just = djust
        if justification is not None:
            match = [j for j in _PP_JUSTIFICATIONS if j.lower() == str(justification).lower()]
            if not match:
                raise SchematicValueError("justification must be one of %s, got %r." % (", ".join(_PP_JUSTIFICATIONS), justification))
            just = match[0]
        if box_source not in ("auto", "logical_area", "bounding_box"):
            raise SchematicValueError("box_source must be 'auto', 'logical_area' or 'bounding_box', got %r." % (box_source,))
        hv = -1.0
        if height is not None:
            hv = float(cs_double(height, "height"))
            if not 0.1 <= hv <= 100:
                raise SchematicValueError("height must be 0.1 .. 100 mm, got %r." % (height,))
        rel = ""
        if relation:
            if not isinstance(relation, str) or not _PP_IDENT.match(relation.strip()):
                raise SchematicValueError("relation must be a PropertyPlacement.RelationType name, got %r." % (relation,))
            rel = relation.strip()
        repos = bool(reposition) if reposition is not None else (position is not None)
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _PLACE_PP_BODY,
            V8HANDLES=_cs_str_array(hl),
            V8PAGE='"%s"' % cs_escape(cs_text(page, "page", allow_empty=True)),
            V8PNAME='"%s"' % cs_escape(pname),
            V8PIDX=str(pidx),
            V8PNUM=str(pnum),
            V8POS='"%s"' % pos,
            V8DX=cs_double(ddx if dx is None else dx, "dx"),
            V8DY=cs_double(ddy if dy is None else dy, "dy"),
            V8X=cs_double(0.0 if x is None else x, "x"),
            V8Y=cs_double(0.0 if y is None else y, "y"),
            V8HEIGHT=cs_double(hv, "height"),
            V8JUST='"%s"' % just,
            V8REPOS=cs_bool(repos),
            V8REL='"%s"' % rel,
            V8RELIDX=str(cs_int(relation_index, "relation_index", minimum=0, maximum=999)),
            V8LOCAL=cs_bool(make_local),
            V8DRY=cs_bool(dry_run),
            V8ROLLBACK=cs_bool(rollback_on_error),
            V8BOXSRC='"%s"' % box_source,
            V9FRAME='"%s"' % location_frame,
            V9ALLOWOUT=cs_bool(allow_outside_page),
        )
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    out = _shape_keep(_execute_script(
        _script(_cls("PlaceProp"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS + _HELPERS_PP),
        timeout=timeout_seconds,
    ), keep=("items", "failedAt", "rolledBack", "rollbackErrors", "project"))
    if not out.get("success"):
        if "OUTSIDE_PAGE" in str(out.get("error") or ""):
            out["errorType"] = "OUTSIDE_PAGE"
        return out
    out["dryRun"] = bool(dry_run)
    counts, empty, warnings = {}, [], []
    for it in out.get("items") or []:
        key = it.get("result") or it.get("action") or "?"
        counts[key] = counts.get(key, 0) + 1
        for k in ("before", "after"):
            if isinstance(it.get(k), dict):
                disp, _ = multilang_display(it[k].get("text"))
                if disp is not None:
                    it[k]["displayText"] = disp
        if "propertyValue" in it and not str(it.get("propertyValue") or "").strip():
            empty.append(it.get("name") or it.get("handle"))
        if it.get("insidePage") is False:
            warnings.append("%s: the text lies OUTSIDE the page (targetPoint %s; allow_outside_page)."
                            % (it.get("name") or it.get("handle"), it.get("targetPoint")))
        if it.get("textNearTarget") is False:
            warnings.append("%s: the text was not drawn near the requested point (locationDelta %s) - check with "
                            "live_read_page(include_property_placements=True)." % (it.get("name") or it.get("handle"),
                                                                                  it.get("locationDelta")))
    out["counts"] = counts
    if empty:
        warnings.insert(0, "The property is EMPTY on %d symbol(s) - the text shows nothing there: %s"
                        % (len(empty), ", ".join(empty[:20])))
    if warnings:
        out["warnings"] = warnings
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
        fa = out["failedAt"]
        out["error"] = ("Writing failed at %s (step %s): %s. %s" % (
            fa.get("handle"), fa.get("step"), fa.get("error"),
            "All changes of this call were undone." if rollback_on_error and not out.get("rollbackErrors")
            else "Changes before it stay - check the page."))
    return out


# ---------------------------------------------------------------------------
# v8  add article references to functions
# ---------------------------------------------------------------------------
# inbox 2026-10-30_eplan_ni_orodja_dodaj_artikel_funkciji_brez_artikla.md +
# coordinator 2026-10-30 (user: module article + BaseUnit as 2nd article on
# every Siemens PLC box, for the parts list).
# API 2.9: Function.AddArticleReference(string strArticleNR, string strVariantNR,
# uint nCount) -> ArticleReference ("stores the article from the system article
# database in the project"; CannotAddArticleException e.g. a 2nd article on a
# 2D function of category ArticlePlacement); RemoveArticleReference (rollback).
# Live test v3 S2: AddArticleReference (3 overloads) present on 2.9.

_HELPERS_ART = r'''
    // ---- v8: live_add_function_articles ------------------------------------
    static List<object> ArtList(object fn)
    {
        List<object> list = new List<object>();
        object refs = TryRead(fn, "ArticleReferences", null);
        if (!(refs is IEnumerable)) return list;
        foreach (object r in (IEnumerable)refs)
        {
            if (r == null) continue;
            Dictionary<string, object> d = new Dictionary<string, object>();
            string pn = SafeText(TryRead(r, "PartNr", null));
            d["partNr"] = pn == null ? "" : pn;
            string vn = SafeText(TryRead(r, "VariantNr", null));
            if (vn != null) d["variant"] = vn;
            object cnt = TryRead(r, "Count", null);
            if (cnt != null) { try { d["count"] = Convert.ToInt32(cnt); } catch { } }
            list.Add(d);
        }
        return list;
    }

    static bool ArtHas(List<object> arts, string partNr)
    {
        foreach (object a in arts)
            if (string.Equals((string)((Dictionary<string, object>)a)["partNr"], partNr, StringComparison.OrdinalIgnoreCase))
                return true;
        return false;
    }

    static int PinCount(object fn)
    {
        object cps = TryRead(fn, "GraphicalConnectionPoints", null);
        int n = 0;
        if (cps is IEnumerable) foreach (object p in (IEnumerable)cps) if (p != null) n++;
        return n;
    }

    // functions per device tag (box + its connection points / sub-functions)
    static Dictionary<string, int> DtCounts(object project, List<string> dts)
    {
        Dictionary<string, int> c = new Dictionary<string, int>();
        foreach (string dt in dts) c[dt] = 0;
        foreach (object fn in AllFunctions(project))
        {
            if (fn == null) continue;
            string dt = DtPart(SafeText(TryRead(fn, "Name", null)));
            if (dt != null && c.ContainsKey(dt)) c[dt] = c[dt] + 1;
        }
        return c;
    }
'''

_ADD_ART_BODY = '''
            string[] v8handles = V8HANDLES;
            string v8page = V8PAGE;
            string[] aNr = V8ANR;
            string[] aVar = V8AVAR;
            int[] aCnt = V8ACNT;
            bool v8dup = V8DUP;
            bool v8dry = V8DRY;
            bool v8rollback = V8ROLLBACK;

            List<string> wanted = new List<string>(v8handles);
            Dictionary<string, object> byH = new Dictionary<string, object>();
            if (v8page.Length > 0)
            {
                object pg = FindPage(project, v8page);
                foreach (object pl in PagePlacements(pg))
                {
                    if (pl == null) continue;
                    string h0 = Handle(pl);
                    if (h0 != null && wanted.Contains(h0) && !byH.ContainsKey(h0)) byH[h0] = pl;
                }
            }
            else
            {
                foreach (object fn in AllFunctions(project))
                {
                    if (fn == null) continue;
                    string h0 = Handle(fn);
                    if (h0 != null && wanted.Contains(h0) && !byH.ContainsKey(h0)) byH[h0] = fn;
                    if (byH.Count == wanted.Count) break;
                }
            }
            List<string> missing = new List<string>();
            foreach (string h in wanted) if (!byH.ContainsKey(h)) missing.Add(h);
            if (missing.Count > 0)
                throw new Exception("Nothing written: " + missing.Count + " handle(s) not found" +
                    (v8page.Length > 0 ? " on page '" + v8page + "'" : " among the project's functions") + ": " +
                    string.Join(", ", missing.ToArray()) + ". Handles are session-scoped - re-read them.");
            MethodInfo addM = null;
            foreach (string h in wanted)
            {
                object fn = byH[h];
                if (!IsFunctionObject(fn))
                    throw new Exception("Nothing written: " + h + " is a " + fn.GetType().Name + ", not a function.");
                MethodInfo m = MethodByShape(fn.GetType(), "AddArticleReference", new string[] { "String", "String", "UInt32" }, false);
                if (m == null)
                    throw new Exception("Nothing written: " + fn.GetType().Name + " has no AddArticleReference(String, String, UInt32). " +
                        MemberList(fn.GetType(), true));
                if (addM == null) addM = m;
            }
            results["addApi"] = addM.ToString();

            // ---- plan --------------------------------------------------------
            List<string> dts = new List<string>();
            List<object> items = new List<object>();
            foreach (string h in wanted)
            {
                object fn = byH[h];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["handle"] = h;
                it["clrType"] = fn.GetType().Name;
                string nm = SafeText(TryRead(fn, "Name", null));
                it["name"] = nm == null ? "" : nm;
                it["page"] = FnPageName(fn);
                object main = TryRead(fn, "IsMainFunction", null);
                if (main != null) { try { it["isMainFunction"] = Convert.ToBoolean(main); } catch { } }
                object cat = TryRead(fn, "Category", null);
                if (cat != null) it["category"] = SafeText(cat);
                List<object> before = ArtList(fn);
                it["articlesBefore"] = before;
                List<object> plan = new List<object>();
                List<string> planned = new List<string>();
                for (int i = 0; i < aNr.Length; i++)
                {
                    Dictionary<string, object> pa = new Dictionary<string, object>();
                    pa["partNr"] = aNr[i];
                    pa["variant"] = aVar[i];
                    pa["count"] = aCnt[i];
                    bool present = ArtHas(before, aNr[i]) || planned.Contains(aNr[i].ToUpperInvariant());
                    pa["action"] = present && !v8dup ? "alreadyPresent" : "add";
                    if ((string)pa["action"] == "add") planned.Add(aNr[i].ToUpperInvariant());
                    plan.Add(pa);
                }
                it["plan"] = plan;
                it["connectionDesignationsBefore"] = ConnDesignations(FnProps(fn));
                it["pinsBefore"] = PinCount(fn);
                string dt = DtPart(nm);
                if (dt != null && dt.Length > 0 && !dts.Contains(dt)) dts.Add(dt);
                items.Add(it);
            }
            results["items"] = items;
            results["dryRun"] = v8dry;

            // ---- write -------------------------------------------------------
            if (!v8dry)
            {
                Dictionary<string, int> dtBefore = DtCounts(project, dts);
                List<object[]> undo = new List<object[]>();
                Dictionary<string, object> failedAt = null;
                int added = 0;
                foreach (object o in items)
                {
                    Dictionary<string, object> it = (Dictionary<string, object>)o;
                    string h = (string)it["handle"];
                    object fn = byH[h];
                    foreach (object po in (List<object>)it["plan"])
                    {
                        Dictionary<string, object> pa = (Dictionary<string, object>)po;
                        if ((string)pa["action"] != "add") continue;
                        try
                        {
                            MethodInfo m = MethodByShape(fn.GetType(), "AddArticleReference", new string[] { "String", "String", "UInt32" }, false);
                            object ar = Call(m, fn, new object[] { (string)pa["partNr"], (string)pa["variant"], (uint)(int)pa["count"] });
                            undo.Add(new object[] { fn, ar });
                            pa["result"] = "added";
                            added++;
                        }
                        catch (Exception exa)
                        {
                            failedAt = new Dictionary<string, object>();
                            failedAt["handle"] = h;
                            failedAt["partNr"] = pa["partNr"];
                            failedAt["error"] = Flatten(exa);
                            break;
                        }
                    }
                    if (failedAt != null) break;
                }
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (v8rollback)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            try
                            {
                                MethodInfo rm = MethodByShape(undo[k][0].GetType(), "RemoveArticleReference", new string[] { "" }, false);
                                if (rm == null) throw new Exception("no RemoveArticleReference(1)");
                                Call(rm, undo[k][0], new object[] { undo[k][1] });
                                rb++;
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
                results["addedCount"] = added;
                // ---- read back: articles appended, connections unchanged? --------
                Dictionary<string, int> dtAfter = DtCounts(project, dts);
                bool anyConn = false;
                foreach (object o in items)
                {
                    Dictionary<string, object> it = (Dictionary<string, object>)o;
                    object fn = byH[(string)it["handle"]];
                    if (!IsValidObj(fn)) { it["functionGone"] = true; anyConn = true; continue; }
                    List<object> after = ArtList(fn);
                    it["articlesAfter"] = after;
                    int nb = ((List<object>)it["articlesBefore"]).Count;
                    bool ok = after.Count >= nb;
                    for (int i = 0; i < nb && ok; i++)
                        ok = string.Equals((string)((Dictionary<string, object>)((List<object>)it["articlesBefore"])[i])["partNr"],
                                           (string)((Dictionary<string, object>)after[i])["partNr"], StringComparison.OrdinalIgnoreCase);
                    it["existingKept"] = ok;
                    bool allIn = true;
                    foreach (object po in (List<object>)it["plan"])
                        if (!ArtHas(after, (string)((Dictionary<string, object>)po)["partNr"])) allIn = false;
                    it["verified"] = ok && (failedAt == null || !v8rollback ? allIn : true);
                    List<string> cAfter = ConnDesignations(FnProps(fn));
                    int pAfter = PinCount(fn);
                    List<string> cBefore = (List<string>)it["connectionDesignationsBefore"];
                    bool changed = pAfter != (int)it["pinsBefore"] || cAfter.Count != cBefore.Count;
                    for (int i = 0; i < cAfter.Count && !changed; i++) if (cAfter[i] != cBefore[i]) changed = true;
                    string dt = DtPart((string)it["name"]);
                    if (dt != null && dtBefore.ContainsKey(dt) && dtBefore[dt] != dtAfter[dt])
                    {
                        changed = true;
                        it["functionsWithTagBefore"] = dtBefore[dt];
                        it["functionsWithTagAfter"] = dtAfter[dt];
                    }
                    it["connectionsChanged"] = changed;
                    if (changed) { it["connectionDesignationsAfter"] = cAfter; it["pinsAfter"] = pAfter; anyConn = true; }
                }
                results["connectionsChanged"] = anyConn;
            }
'''

_ART_KEYS = {"partnr": "partnr", "partNr": "partnr", "part_nr": "partnr", "variant": "variant", "count": "count"}


def _parse_articles(articles):
    if not isinstance(articles, (list, tuple)) or not articles:
        raise SchematicValueError(
            'articles must be a non-empty list, e.g. [{"partnr": "SIE.6ES7131-6BF01-0BA0", "count": 1}, '
            '{"partnr": "SIE.6ES7193-6BP20-0BA0"}].')
    out = []
    for a in articles:
        if isinstance(a, str):
            a = {"partnr": a}
        if not isinstance(a, dict):
            raise SchematicValueError("articles entries must be objects {partnr, variant, count}, got %r." % (a,))
        unknown = [k for k in a if k not in _ART_KEYS]
        if unknown:
            raise SchematicValueError("articles: unknown key(s) %s (use partnr, variant, count)." % unknown)
        d = {_ART_KEYS[k]: v for k, v in a.items()}
        pn = cs_text(d.get("partnr"), "articles.partnr").strip()
        var = cs_text(str(d.get("variant", "1")), "articles.variant").strip()
        cnt = cs_int(d.get("count", 1), "articles.count", minimum=1, maximum=9999)
        out.append((pn, var, cnt))
    if len(out) > 20:
        raise SchematicValueError("at most 20 articles per call.")
    return out


def live_add_function_articles(articles: list = None, handles: list = None, handle: str = None,
                               page: str = None, allow_duplicate: bool = False,
                               dry_run: bool = True, allow_real_project: bool = False,
                               allow_missing_article: bool = False, allow_unverified_db: bool = False,
                               rollback_on_error: bool = True, max_items: int = 100,
                               timeout_seconds: float = 240.0) -> dict:
    """
    v8: ADD article references (parts) to functions - e.g. the module article
    and, as a SECOND article, its BaseUnit on every Siemens PLC box, so the
    parts list prints both. Appends to the function's article list
    (Function.AddArticleReference(partNr, variant, count), API 2.9); existing
    articles are never removed or changed. WRITES (unless dry_run, the
    default) - scratch-only unless allow_real_project=True.

    For a function that already has none, one or more articles. To CHANGE an
    existing part number use live_set_function_properties(replace_article).

    Safety (as replace_article): the parts database preflight first (Access
    without 64-bit ACE -> PARTS_DB_UNAVAILABLE, nothing sent to EPLAN, no
    modal dialog); every part number is looked up (parts_db_get_part) - not
    in the database -> errorType ARTICLE_NOT_IN_DB, nothing written, unless
    allow_missing_article=true ("articleInDb" per part number). An article the
    function already has is skipped ("alreadyPresent") unless
    allow_duplicate. Every handle is checked before the first write; on an
    error all articles added by this call are removed again
    (rollback_on_error, RemoveArticleReference).

    EPLAN may change the function when an article brings a connection point
    pattern: per function "connectionsChanged" compares the connection point
    designations, the pin count and the number of functions with the same
    device tag before / after (+ "connectionsChanged" for the whole call).

    Args:
        articles: [{"partnr": "SIE.6ES7131-6BF01-0BA0", "count": 1},
            {"partnr": "SIE.6ES7193-6BP20-0BA0"}] - "variant" default "1",
            "count" default 1; a plain string = partnr. Added in this order.
        handles / handle: the functions (normally the MAIN function / PLC box;
            from live_read_function_properties or live_read_page).
        page: optional - resolve the handles on this page only.
        allow_duplicate: add a part number the function already has.
        dry_run: default True - per function "articlesBefore" and "plan"
            (add / alreadyPresent), nothing written.
        allow_real_project: must be True to write outside the scratch root.
        allow_missing_article, allow_unverified_db: see above.
        rollback_on_error: default True. max_items: default 100.

    Returns:
        {"success", "dryRun", "articleInDb" {partNr: true/false/null},
         "items": [{handle, name, page, isMainFunction, category,
         articlesBefore, plan [{partNr, variant, count, action, result}],
         articlesAfter, existingKept, verified, connectionsChanged, ...}],
         "addedCount", "connectionsChanged", "counts", "warnings"}.
        Failed write: errorType ROLLED_BACK (or PARTIAL), "failedAt"
        {handle, partNr, error}. EPLAN stores into the project database
        itself (no save command).
    """
    try:
        arts = _parse_articles(articles)
        if handles is not None and not isinstance(handles, (list, tuple)):
            raise SchematicValueError("handles must be a list of strings (use handle= for one).")
        hl = []
        for v in ([handle] if handle else []) + list(handles or []):
            if not isinstance(v, str) or not v.strip():
                raise SchematicValueError("handles entries must be non-empty strings, got %r." % (v,))
            if v.strip() not in hl:
                hl.append(v.strip())
        if not hl:
            raise SchematicValueError("Pass handle='...' or handles=[...] (functions from live_read_function_properties).")
        mx = cs_int(max_items, "max_items", minimum=1, maximum=500)
        if len(hl) > mx:
            raise SchematicValueError("%d handles, more than max_items=%d - nothing written." % (len(hl), mx))
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _ADD_ART_BODY,
            V8HANDLES=_cs_str_array(hl),
            V8PAGE='"%s"' % cs_escape(cs_text(page, "page", allow_empty=True)),
            V8ANR=_cs_str_array([a[0] for a in arts]),
            V8AVAR=_cs_str_array([a[1] for a in arts]),
            V8ACNT=_cs_int_array([a[2] for a in arts]),
            V8DUP=cs_bool(allow_duplicate),
            V8DRY=cs_bool(dry_run),
            V8ROLLBACK=cs_bool(rollback_on_error),
        )
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    # Same parts database preflight as replace_article (v5): nothing reaches
    # EPLAN when the database would open a modal dialog.
    blocked = _scripted._parts_db_guard(allow_unverified_db)
    if blocked:
        out = dict(blocked)
        out["error"] = "add articles NOT run, nothing written. " + str(out.get("error") or out.get("message") or "")
        out["nothingWritten"] = True
        return out
    in_db = {}
    for pn in dict.fromkeys(a[0] for a in arts):
        in_db[pn] = _article_in_db(pn, allow_unverified_db)
    missing = [pn for pn, f in in_db.items() if f is False]
    if missing and not allow_missing_article:
        return {"success": False, "errorType": "ARTICLE_NOT_IN_DB", "nothingWritten": True, "articleInDb": in_db,
                "error": ("Nothing written: %s not in the parts database (parts_db_get_part: found false). EPLAN "
                          "would add an article without description / manufacturer. Pass "
                          "allow_missing_article=true to add anyway." % ", ".join(missing))}

    out = _shape_keep(_execute_script(
        _script(_cls("AddArt"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS + _HELPERS_ART),
        timeout=timeout_seconds,
    ), keep=("items", "failedAt", "rolledBack", "rollbackErrors", "addedCount", "project"))
    out["articleInDb"] = in_db
    if not out.get("success"):
        return out
    out["dryRun"] = bool(dry_run)
    counts, warnings = {}, []
    for it in out.get("items") or []:
        for pa in it.get("plan") or []:
            key = pa.get("result") or pa.get("action") or "?"
            counts[key] = counts.get(key, 0) + 1
        if it.get("isMainFunction") is False:
            warnings.append("%s is not the main function - articles normally go on the main function." % it.get("name"))
        if it.get("connectionsChanged"):
            warnings.append("%s: EPLAN changed the connection points / functions when the article was added "
                            "- check the page." % it.get("name"))
        if not dry_run and it.get("verified") is False:
            warnings.append("%s: read-back does not show every article (or an existing one moved)." % it.get("name"))
    out["counts"] = counts
    undecided = [pn for pn, f in in_db.items() if f is None]
    if undecided:
        warnings.append("Could not tell whether %s is in the parts database - check with parts_db_get_part."
                        % ", ".join(undecided))
    if missing:
        warnings.append("%s %s NOT in the parts database (allow_missing_article)."
                        % (", ".join(missing), "was" if not dry_run else "would be added although"))
    if warnings:
        out["warnings"] = warnings
    if out.get("failedAt"):
        fa = out["failedAt"]
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
        out["error"] = ("Adding %s to %s failed: %s. %s" % (
            fa.get("partNr"), fa.get("handle"), fa.get("error"),
            "Articles added by this call were removed again." if rollback_on_error and not out.get("rollbackErrors")
            else "Articles added before it stay - check the functions."))
    return out


# ---------------------------------------------------------------------------
# v9  hide / remove the text that shows a property on a symbol
# ---------------------------------------------------------------------------
# inbox 2026-10-30_eplan_skrij_property_placement.md (V2, ET-1): the
# Siemens PLC boxes show "Type number" (22002, module + BaseUnit) under
# "Siemens" as DOCKED ContextPropertyPlacement texts that carry the SAME handle
# as the box - live_remove_placements(handles=[box]) would remove the box. This
# tool works on the symbol's PropertyPlacements list instead.
# API 2.9: PropertyPlacement.IsSetAsVisible (Placement.Visibility Invisible),
# GraphicalPlacement.IsVisible, PropertyPlacement.Remove(), RelationIndex,
# TextBase.IsDocked, SymbolReference.UseLocalPropertyPlacements /
# SwitchLocalPropertyPlacements(bool) (texts of the symbol variant are read-only).

_HELPERS_HIDE = r'''
    // ---- v9: live_hide_function_property ----------------------------------------
    // index > 0: DisplayedProperty.Index, or - for article texts that keep Index 0 -
    // PropertyPlacement.RelationIndex (22002 of the 1st / 2nd article of the box).
    static bool PpMatch(object pp, int id, int idx, string rel, int relIdx)
    {
        if (PpPropId(pp) != id) return false;
        int ri = PpInt(pp, "RelationIndex", 0);
        if (idx > 0)
        {
            int pi = PpPropIdx(pp);
            if (!(pi == idx || (pi == 0 && ri == idx))) return false;
        }
        if (rel.Length > 0 && SafeText(TryRead(pp, "Relation", null)) != rel) return false;
        if (relIdx > 0 && ri != relIdx) return false;
        return true;
    }

    // Identity of a text that survives SwitchLocalPropertyPlacements (local copies).
    static string PpSig(object pp)
    {
        return PpPropId(pp) + "[" + PpPropIdx(pp) + "]" + SafeText(TryRead(pp, "Relation", null)) +
            "#" + PpInt(pp, "RelationIndex", 0);
    }

    static string PpState(object pp)
    {
        return PpSig(pp) + (PpShown(pp) ? " shown" : " hidden");
    }

    static Dictionary<string, object> PpBrief(object pp)
    {
        Dictionary<string, object> f = DumpPropPl(pp);
        Dictionary<string, object> d = new Dictionary<string, object>();
        foreach (string k in new string[] { "clrType", "propertyId", "propertyIndex", "relation", "relationIndex",
                                            "propertyName", "text", "visible", "setAsVisible", "docked" })
            if (f.ContainsKey(k)) d[k] = f[k];
        return d;
    }

    static string PpMakeHidden(object pp)
    {
        List<string> routes = new List<string>();
        PropertyInfo sv = GetWritable(pp.GetType(), "IsSetAsVisible");
        if (sv != null && sv.PropertyType.IsEnum)
        {
            try { sv.SetValue(pp, Enum.Parse(sv.PropertyType, "Invisible"), null); routes.Add("IsSetAsVisible=Invisible"); }
            catch (TargetInvocationException tie) { routes.Add("IsSetAsVisible threw " + Flatten(tie.InnerException)); }
            catch (Exception ex) { routes.Add("IsSetAsVisible threw " + Flatten(ex)); }
        }
        PropertyInfo iv = GetWritable(pp.GetType(), "IsVisible");
        if (PpShown(pp) && iv != null)
        {
            try { iv.SetValue(pp, false, null); routes.Add("IsVisible=false"); }
            catch (TargetInvocationException tie) { routes.Add("IsVisible threw " + Flatten(tie.InnerException)); }
            catch (Exception ex) { routes.Add("IsVisible threw " + Flatten(ex)); }
        }
        if (sv == null && iv == null)
            throw new Exception("PropertyPlacement has neither a writable IsSetAsVisible nor IsVisible. " +
                MemberList(pp.GetType(), false));
        if (PpShown(pp))
            throw new Exception("Property placement " + PpSig(pp) + " is still visible after the write (" +
                string.Join("; ", routes.ToArray()) + "; IsSetAsVisible=" + PpVisibility(pp) + ").");
        return string.Join("; ", routes.ToArray());
    }
'''


_HIDE_PP_BODY = '''
            string[] v9handles = V9HANDLES;
            string v9page = V9PAGE;
            string[] v9pnames = V9PNAMES;
            int[] v9pidx = V9PIDX;
            int[] v9pnum = V9PNUM;
            string v9action = V9ACTION;
            string v9rel = V9REL;
            int v9relIdx = V9RELIDX;
            bool v9local = V9LOCAL;
            bool v9dry = V9DRY;
            bool v9rollback = V9ROLLBACK;
            bool v9verbose = V9VERBOSE;

            // ---- 1. resolve every handle BEFORE anything is written ----------
            // A docked ContextPropertyPlacement carries the SAME handle as its box -
            // only an object with PropertyPlacements (the symbol) is taken.
            List<string> wanted = new List<string>(v9handles);
            Dictionary<string, object> byH = new Dictionary<string, object>();
            if (v9page.Length > 0)
            {
                object pg = FindPage(project, v9page);
                foreach (object pl in PagePlacements(pg))
                {
                    if (pl == null) continue;
                    string h0 = Handle(pl);
                    if (h0 != null && wanted.Contains(h0) && !byH.ContainsKey(h0) &&
                        TryRead(pl, "PropertyPlacements", null) is IEnumerable) byH[h0] = pl;
                }
            }
            else
            {
                foreach (object fn in AllFunctions(project))
                {
                    if (fn == null) continue;
                    string h0 = Handle(fn);
                    if (h0 != null && wanted.Contains(h0) && !byH.ContainsKey(h0)) byH[h0] = fn;
                    if (byH.Count == wanted.Count) break;
                }
            }
            List<string> missing = new List<string>();
            foreach (string h in wanted) if (!byH.ContainsKey(h)) missing.Add(h);
            if (missing.Count > 0)
                throw new Exception("Nothing changed: " + missing.Count + " symbol handle(s) not found" +
                    (v9page.Length > 0 ? " on page '" + v9page + "'" : " among the project's functions") + ": " +
                    string.Join(", ", missing.ToArray()) + ". Handles are session-scoped - re-read them " +
                    "(live_read_function_properties / live_read_page).");
            foreach (string h in wanted)
                if (!(TryRead(byH[h], "PropertyPlacements", null) is IEnumerable))
                    throw new Exception("Nothing changed: " + h + " is a " + byH[h].GetType().Name +
                        " - only symbols (SymbolReference: functions, PLC boxes, terminals ...) carry property placements.");

            // ---- 2. plan: per symbol, the texts of the properties ------------
            List<object> items = new List<object>();
            List<object[]> plan = new List<object[]>();
            foreach (string h in wanted)
            {
                object fn = byH[h];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["handle"] = h;
                it["clrType"] = fn.GetType().Name;
                string nm = SafeText(TryRead(fn, "Name", null));
                if (nm != null && nm.Length > 0) it["name"] = nm;
                List<int[]> specs = new List<int[]>();
                List<string> labels = new List<string>();
                for (int k = 0; k < v9pnum.Length; k++)
                {
                    int id = v9pnum[k];
                    if (id < 0)
                    {
                        object props = FnProps(fn);
                        PropertyInfo pi = FnPropInfo(props, v9pnames[k], v9pidx[k]);
                        if (pi == null)
                            throw new Exception("Nothing changed: " + props.GetType().Name + " of " + h + " declares no '" +
                                v9pnames[k] + "'. A property number (e.g. 22002) is accepted too.");
                        object pv;
                        try { pv = v9pidx[k] > 0 ? pi.GetValue(props, new object[] { v9pidx[k] }) : pi.GetValue(props, null); }
                        catch (TargetInvocationException tie) { throw new Exception("Nothing changed: reading " + v9pnames[k] + " of " + h + ": " + Flatten(tie.InnerException)); }
                        object anyId = pv == null ? null : TryRead(pv, "Id", null);
                        id = PpInt(anyId, "AsInt", -1);
                        if (id < 0) throw new Exception("Nothing changed: " + v9pnames[k] + " of " + h + " gives no PropertyValue.Id.AsInt.");
                    }
                    int[] sp = new int[2];
                    sp[0] = id; sp[1] = v9pidx[k];
                    specs.Add(sp);
                    labels.Add((v9pnames[k].Length > 0 ? v9pnames[k] : id.ToString()) + (v9pidx[k] > 0 ? "[" + v9pidx[k] + "]" : ""));
                }
                List<object> hits = new List<object>();
                List<string> othersBefore = new List<string>();
                int[] perSpec = new int[specs.Count];
                int total = 0, toWrite = 0;
                foreach (object pp in (IEnumerable)TryRead(fn, "PropertyPlacements", null))
                {
                    if (pp == null) continue;
                    total++;
                    int hitAt = -1;
                    for (int k = 0; k < specs.Count; k++)
                        if (PpMatch(pp, specs[k][0], specs[k][1], v9rel, v9relIdx)) { hitAt = k; break; }
                    if (hitAt < 0) { othersBefore.Add(PpState(pp)); continue; }
                    perSpec[hitAt]++;
                    Dictionary<string, object> d = v9verbose ? DumpPropPl(pp) : PpBrief(pp);
                    string act = v9action == "remove" ? "remove" : (PpShown(pp) ? "hide" : "alreadyHidden");
                    if (act != "alreadyHidden") toWrite++;
                    d["action"] = act;
                    hits.Add(d);
                }
                List<string> nf = new List<string>();
                for (int k = 0; k < specs.Count; k++) if (perSpec[k] == 0) nf.Add(labels[k]);
                if (nf.Count > 0) it["notFound"] = nf;
                it["texts"] = total;
                it["matched"] = hits;
                it["useLocalPropertyPlacements"] = ReadBool(fn, "UseLocalPropertyPlacements", true);
                it["action"] = hits.Count == 0 ? "notFound" : (toWrite > 0 ? v9action : "alreadyHidden");
                items.Add(it);
                plan.Add(new object[] { h, fn, specs, othersBefore, it, toWrite > 0 });
            }
            results["items"] = items;
            results["dryRun"] = v9dry;

            // ---- 3. write (one LockingStep, all or nothing with rollback) ---
            if (!v9dry)
            {
                List<object[]> undo = new List<object[]>();   // {kind, object, property, old value}
                List<string> notRestorable = new List<string>();
                Dictionary<string, object> failedAt = null;
                foreach (object[] p in plan)
                {
                    string h = (string)p[0];
                    object fn = p[1];
                    List<int[]> specs = (List<int[]>)p[2];
                    List<string> othersBefore = (List<string>)p[3];
                    Dictionary<string, object> it = (Dictionary<string, object>)p[4];
                    if (!(bool)p[5]) { it["result"] = (string)it["action"]; continue; }
                    string step = "start";
                    bool switched = false;
                    try
                    {
                        // Texts of the symbol VARIANT cannot be changed - local copies first
                        // (as live_place_function_property; the drawing does not change).
                        if (!ReadBool(fn, "UseLocalPropertyPlacements", true))
                        {
                            if (!v9local)
                                throw new Exception("the symbol uses its variant's property placements and make_local=false");
                            step = "SwitchLocalPropertyPlacements(true)";
                            Call(RequireMethod(fn.GetType(), "SwitchLocalPropertyPlacements", new string[] { "Boolean" }, false), fn, new object[] { true });
                            undo.Add(new object[] { "local", fn, null, null });
                            switched = true;
                            it["switchedToLocal"] = true;
                        }
                        step = "find";
                        List<object> targets = new List<object>();
                        foreach (object pp in (IEnumerable)TryRead(fn, "PropertyPlacements", null))
                        {
                            if (pp == null) continue;
                            foreach (int[] sp in specs)
                                if (PpMatch(pp, sp[0], sp[1], v9rel, v9relIdx)) { targets.Add(pp); break; }
                        }
                        int nHidden = 0, nRemoved = 0;
                        foreach (object pp in targets)
                        {
                            if (v9action == "remove")
                            {
                                step = "Remove";
                                if (!switched) notRestorable.Add(h + " " + PpSig(pp));
                                Call(RequireMethod(pp.GetType(), "Remove", new string[] { }, false), pp, null);
                                nRemoved++;
                            }
                            else if (PpShown(pp))
                            {
                                step = "hide";
                                undo.Add(new object[] { "set", pp, "IsSetAsVisible", TryRead(pp, "IsSetAsVisible", null) });
                                undo.Add(new object[] { "set", pp, "IsVisible", TryRead(pp, "IsVisible", null) });
                                it["hiddenRoute"] = PpMakeHidden(pp);
                                nHidden++;
                            }
                        }
                        // ---- read back: the property is no longer shown, nothing else changed
                        step = "verify";
                        List<string> othersAfter = new List<string>();
                        List<string> stillShown = new List<string>();
                        int remaining = 0;
                        foreach (object pp in (IEnumerable)TryRead(fn, "PropertyPlacements", null))
                        {
                            if (pp == null || !IsValidObj(pp)) continue;
                            bool hit = false;
                            foreach (int[] sp in specs) if (PpMatch(pp, sp[0], sp[1], v9rel, v9relIdx)) { hit = true; break; }
                            if (!hit) { othersAfter.Add(PpState(pp)); continue; }
                            remaining++;
                            if (PpShown(pp)) stillShown.Add(PpSig(pp));
                        }
                        othersBefore.Sort(StringComparer.Ordinal);
                        othersAfter.Sort(StringComparer.Ordinal);
                        bool same = string.Join("|", othersBefore.ToArray()) == string.Join("|", othersAfter.ToArray());
                        it["othersUnchanged"] = same;
                        if (!same)
                        {
                            it["othersBefore"] = othersBefore;
                            it["othersAfter"] = othersAfter;
                            throw new Exception("other texts of the symbol changed (count or visibility) - see othersBefore / othersAfter");
                        }
                        if (stillShown.Count > 0)
                            throw new Exception("still shown after the write: " + string.Join(", ", stillShown.ToArray()));
                        if (v9action == "remove" && remaining > 0)
                            throw new Exception(remaining + " text(s) of the property still exist after Remove");
                        if (!IsValidObj(fn) || Handle(fn) != h)
                            throw new Exception("the symbol itself is no longer valid");
                        it["symbolKept"] = true;
                        it["verified"] = true;
                        it["result"] = v9action == "remove" ? "removed" : "hidden";
                        if (nHidden > 0) it["hiddenCount"] = nHidden;
                        if (nRemoved > 0) it["removedCount"] = nRemoved;
                        if (v9verbose) it["after"] = DumpPropPls(fn, 200);
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["handle"] = h;
                        failedAt["step"] = step;
                        failedAt["error"] = Flatten(ex);
                        break;
                    }
                }
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (notRestorable.Count > 0) results["notRestorable"] = notRestorable;
                    if (v9rollback)
                    {
                        List<string> rbErr = new List<string>();
                        int rb = 0;
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            object[] u = undo[k];
                            try
                            {
                                string kind = (string)u[0];
                                if (kind == "local")
                                    Call(RequireMethod(u[1].GetType(), "SwitchLocalPropertyPlacements", new string[] { "Boolean" }, false), u[1], new object[] { false });
                                else if (IsValidObj(u[1]) && !object.Equals(TryRead(u[1], (string)u[2], null), u[3]))
                                    PpRestore(u[1], (string)u[2], u[3]);   // only what really changed
                                rb++;
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
            }
'''


_HIDE_ACTIONS = ("hide", "remove")


def _hide_specs(property_id):
    """'22002' / '22002[2]' / '22002[*]' / 'FUNC_X' / 'FUNC_X[1]' / 22002 / list of them
    -> [(name, index, number)]; index 0 = every index of the property."""
    vals = list(property_id) if isinstance(property_id, (list, tuple)) else [property_id]
    if not vals:
        raise SchematicValueError("property_id: give at least one property.")
    if len(vals) > 20:
        raise SchematicValueError("property_id: at most 20 properties in one call, got %d." % len(vals))
    out = []
    for v in vals:
        if isinstance(v, str) and v.strip().endswith("[*]"):
            v = v.strip()[:-3]
        spec = _pp_property(v)
        if spec not in out:
            out.append(spec)
    return out


def live_hide_function_property(property_id=None, handles: list = None, handle: str = None,
                                page: str = None, action: str = "hide", relation: str = None,
                                relation_index: int = None, make_local: bool = True,
                                dry_run: bool = True, allow_real_project: bool = False,
                                rollback_on_error: bool = True, max_items: int = 100,
                                verbose: bool = False, timeout_seconds: float = 180.0) -> dict:
    """
    v9: HIDE (or remove) the text that shows a property of a symbol on the
    sheet - property placement (Eigenschaftsplatzierung), the counterpart of
    live_place_function_property. Hides the property TEXT only: the symbol
    (PLC box, function, terminal), its other texts, the property VALUE,
    articles and connections stay. WRITES (unless dry_run, the default) -
    scratch-only unless allow_real_project=True (dry_run only reads).

    Works on the symbol's own list (SymbolReference.PropertyPlacements), so it
    also reaches the DOCKED texts of a macro (clrType ContextPropertyPlacement,
    e.g. "Type number" 22002 of the module and of the BaseUnit under
    "Siemens" on an ET 200SP box). Those carry the SAME handle as the box in
    live_read_page - never pass that handle to live_remove_placements (it
    would remove the box).

    For each symbol given by handle and each text of the property:
      - shown  -> hidden: IsSetAsVisible=Invisible (+ IsVisible=false)
        ("hidden"; also for texts set ByLayer - the layer is NOT changed),
      - already hidden -> nothing ("alreadyHidden"),
      - action="remove" -> PropertyPlacement.Remove() ("removed").
    A symbol that uses its variant's texts (UseLocalPropertyPlacements false)
    is switched to local texts first (SwitchLocalPropertyPlacements(true),
    the drawing does not change) - make_local=False refuses instead.
    Many symbols in ONE call (handles=[...], one LockingStep); every handle
    and property is checked before the first write; read back per symbol:
    no text of the property is shown any more ("verified"), every other text
    keeps its visibility ("othersUnchanged"), the symbol is still there
    ("symbolKept") - else everything of this call is undone
    (rollback_on_error; a hidden text gets its old visibility back, a symbol
    switched to local texts goes back to its variant's texts).

    Args:
        property_id: the property - a number as string or int ("22002"),
            optionally indexed ("22002[1]", "22002[2]"; "22002" or "22002[*]"
            = every index), or a FunctionPropertyList name
            ("FUNC_PLCGROUP_STARTADDRESS_1_INPUTS", "FUNC_X[2]"); or a LIST of
            them (up to 20), e.g. ["22002", "FUNC_PLCGROUP_STARTADDRESS_1_INPUTS",
            "FUNC_PLCGROUP_STARTADDRESS_1_OUTPUTS"]. The index matches
            DisplayedProperty.Index, or - for article texts with Index 0 -
            PropertyPlacement.RelationIndex (1st / 2nd article).
        handles / handle: the SYMBOLS (box / function handles from
            live_read_page or live_read_function_properties; session-scoped).
            At most max_items (default 100).
        page: optional - resolve the handles on this page only.
        action: "hide" (default) or "remove". Remove cannot be undone on a
            symbol that already had local texts ("notRestorable") - prefer hide.
        relation, relation_index: optional filters (PropertyPlacement.Relation
            name, e.g. "MainFunctionArticle"; RelationIndex).
        make_local: see above (default True).
        dry_run: default True - per symbol "matched" texts with "action"
            (hide / alreadyHidden / remove), "notFound"; nothing changed.
        allow_real_project: must be True to write outside the scratch root.
        rollback_on_error: default True.
        verbose: full text records (as live_read_page) and "after" per symbol.
        timeout_seconds: default 180.

    Returns:
        {"success", "dryRun", "action", "counts" {per symbol result/action: n},
         "textsMatched", "textsHidden"/"textsRemoved", "items": [{handle, name,
         clrType, texts, matched [{propertyId, propertyIndex, relation,
         relationIndex, text, visible, setAsVisible, docked, action}],
         notFound, useLocalPropertyPlacements, action, and after a real run:
         result hidden/removed/alreadyHidden/notFound, verified, othersUnchanged,
         symbolKept, switchedToLocal}], "warnings"}. A failed write: success
        false, errorType "ROLLED_BACK" (or "PARTIAL"), "failedAt" {handle,
        step, error}. Check with live_read_page(...,
        include_property_placements=True): the text has visible false.
    """
    try:
        specs = _hide_specs(property_id)
        if handles is not None and not isinstance(handles, (list, tuple)):
            raise SchematicValueError("handles must be a list of strings (use handle= for one).")
        hl = []
        for v in ([handle] if handle else []) + list(handles or []):
            if not isinstance(v, str) or not v.strip():
                raise SchematicValueError("handles entries must be non-empty strings, got %r." % (v,))
            if v.strip() not in hl:
                hl.append(v.strip())
        if not hl:
            raise SchematicValueError("Pass handle='...' or handles=[...] (symbols from live_read_page / "
                                      "live_read_function_properties).")
        mx = cs_int(max_items, "max_items", minimum=1, maximum=500)
        if len(hl) > mx:
            raise SchematicValueError("%d handles, more than max_items=%d - nothing changed." % (len(hl), mx))
        if action not in _HIDE_ACTIONS:
            raise SchematicValueError("action must be 'hide' or 'remove', got %r." % (action,))
        rel = ""
        if relation:
            if not isinstance(relation, str) or not _PP_IDENT.match(relation.strip()):
                raise SchematicValueError("relation must be a PropertyPlacement.RelationType name, got %r." % (relation,))
            rel = relation.strip()
        rix = 0 if relation_index is None else cs_int(relation_index, "relation_index", minimum=1, maximum=999)
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _HIDE_PP_BODY,
            V9HANDLES=_cs_str_array(hl),
            V9PAGE='"%s"' % cs_escape(cs_text(page, "page", allow_empty=True)),
            V9PNAMES=_cs_str_array([n for n, _, _ in specs]),
            V9PIDX=_cs_int_array([i for _, i, _ in specs]),
            V9PNUM=_cs_int_array([n for _, _, n in specs]),
            V9ACTION='"%s"' % action,
            V9REL='"%s"' % rel,
            V9RELIDX=str(rix),
            V9LOCAL=cs_bool(make_local),
            V9DRY=cs_bool(dry_run),
            V9ROLLBACK=cs_bool(rollback_on_error),
            V9VERBOSE=cs_bool(verbose),
        )
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    out = _shape_keep(_execute_script(
        _script(_cls("HideProp"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS + _HELPERS_PP + _HELPERS_HIDE),
        timeout=timeout_seconds,
    ), keep=("items", "failedAt", "rolledBack", "rollbackErrors", "notRestorable", "project"))
    if not out.get("success"):
        return out
    out["dryRun"] = bool(dry_run)
    out["action"] = action
    counts, warnings, nf = {}, [], []
    matched = written = 0
    for it in out.get("items") or []:
        key = it.get("result") or it.get("action") or "?"
        counts[key] = counts.get(key, 0) + 1
        for t in it.get("matched") or []:
            matched += 1
            if t.get("action") in ("hide", "remove"):
                written += 1
            disp, _ = multilang_display(t.get("text"))
            if disp is not None:
                t["displayText"] = disp
        if it.get("notFound"):
            nf.append("%s: %s" % (it.get("name") or it.get("handle"), ", ".join(it["notFound"])))
    out["counts"] = counts
    out["textsMatched"] = matched
    out["textsRemoved" if action == "remove" else "textsHidden"] = written
    if nf:
        warnings.append("No text of the property on %d symbol(s) (nothing to hide there): %s"
                        % (len(nf), "; ".join(nf[:20])))
    if warnings:
        out["warnings"] = warnings
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
        fa = out["failedAt"]
        out["error"] = ("Writing failed at %s (step %s): %s. %s" % (
            fa.get("handle"), fa.get("step"), fa.get("error"),
            "All changes of this call were undone." if rollback_on_error and not out.get("rollbackErrors")
            and not out.get("notRestorable") else "Some changes stay - check the page."))
    return out


# ---------------------------------------------------------------------------
# v10  texts on the sheet (Text / PathText) and batch property writes
# ---------------------------------------------------------------------------
# inbox 2026-11-03_eplan_besedilo_poti_pathtext_branje_urejanje.md :
# the path function text (PathText, magenta under every device) feeds
# FUNC_TEXT_AUTOMATIC (#20031) of the functions of its path whose FUNC_TEXT is
# empty - and so the ET 200SP cards. live_read_page(include_text_content=True)
# reads it, live_set_texts changes it. inbox 2026-11-03_eplan_set_function_
# properties_brez_paketa.md / 2026-11-02_..._paketno.md: live_set_function_
# properties(items=[...]) writes different values to many functions in ONE call.
#
# API 2.9 (eplan.help): Graphics.PathText : Text : TextBase : GraphicalPlacement;
# Text.Contents (MultiLangString get/set - invalid on a PropertyPlacement);
# TextBase.GetDisplayString / Height / Justification / TextColorId (-16002 =
# from layer) / Rotation / LanguageMode / FixedLanguage; GraphicalPlacement.Layer
# (GraphicalLayer); MultiLangString() / GetAsString / SetAsString /
# AddString(ISOCode.Language, string) / DeleteString(ISOCode.Language);
# ISOCode.Language L___ (0, language independent "??_??"), L_sl_SI, L_en_US ...;
# FunctionPropertyList.FUNC_TEXT_AUTOMATIC #20031 read-only: "the manually
# entered function text or, if it is empty, the path function text" - EPLAN
# evaluates it, there is no setter and no action to refresh it.

_HELPERS_V10 = r'''
    // ---- v10: Text / PathText contents -------------------------------------
    static Dictionary<Type, int> _txKind = new Dictionary<Type, int>();

    // 0 = no text, 1 = Text, 2 = PathText. A PropertyPlacement (a TextBase, not
    // a Text: setting its Contents is invalid) is never taken.
    static int TxKind(object o)
    {
        if (o == null) return 0;
        Type t0 = o.GetType();
        int k;
        if (_txKind.TryGetValue(t0, out k)) return k;
        k = 0;
        bool path = false;
        Type t = t0;
        while (t != null)
        {
            if (t.Name == "PropertyPlacement") { k = 0; break; }
            if (t.Name == "PathText") path = true;
            if (t.Name == "Text" && (t.Namespace == null || t.Namespace.EndsWith(".Graphics"))) { k = path ? 2 : 1; break; }
            t = t.BaseType;
        }
        _txKind[t0] = k;
        return k;
    }

    static IEnumerable AllPagesOf(object project)
    {
        Type finderType = FindType("Eplan.EplApi.DataModel.DMObjectsFinder");
        object finder = Activator.CreateInstance(finderType, new object[] { project });
        Type filterType = FindType("Eplan.EplApi.DataModel.PagesFilter");
        object filter = Activator.CreateInstance(filterType);
        MethodInfo getPages = RequireMethod(finderType, "GetPages", new string[] { filterType.Name }, false);
        object r = Call(getPages, finder, new object[] { filter });
        if (r == null) throw new Exception("GetPages returned null.");
        return (IEnumerable)r;
    }

    // MultiLangString in EPLAN's string form "sl_SI@text;en_US@text;" ("??_??" = language independent).
    static string MlsRaw(object mls)
    {
        if (mls == null) return "";
        MethodInfo ga = MethodByShape(mls.GetType(), "GetAsString", new string[] { }, false);
        if (ga != null)
        {
            try { object s = ga.Invoke(mls, null); if (s != null) return s.ToString(); }
            catch { }
        }
        string t = SafeText(mls);
        return t == null ? "" : t;
    }

    // {language, text} segments; null = not in the MultiLangString form (plain text).
    static List<string[]> MlsSegs(string raw)
    {
        List<string[]> segs = new List<string[]>();
        if (raw == null || raw.Length == 0) return segs;
        int i = 0;
        while (i < raw.Length)
        {
            if (i + 6 > raw.Length || raw[i + 2] != '_' || raw[i + 5] != '@') return null;
            for (int k = 0; k < 5; k++)
            {
                if (k == 2) continue;
                char c = raw[i + k];
                if (!(c == '?' || char.IsLetter(c))) return null;
            }
            int end = raw.IndexOf(';', i + 6);
            if (end < 0) end = raw.Length;
            string[] g = new string[2];
            g[0] = raw.Substring(i, 5);
            g[1] = raw.Substring(i + 6, end - i - 6);
            segs.Add(g);
            i = end + 1;
        }
        return segs;
    }

    // the text of one language; null = no entry (plain text counts as "??_??")
    static string MlsEntry(string raw, string lang)
    {
        List<string[]> s = MlsSegs(raw);
        if (s == null) return lang == "??_??" ? raw : null;
        foreach (string[] g in s) if (g[0] == lang) return g[1];
        return null;
    }

    static bool MlsEntryAny(string raw, string want)
    {
        List<string[]> s = MlsSegs(raw);
        if (s == null) return false;
        foreach (string[] g in s) if (g[1] == want) return true;
        return false;
    }

    // whitespace removed: FUNC_TEXT_AUTOMATIC drops line breaks when displayed
    static string MlsNorm(string s)
    {
        if (s == null) return "";
        StringBuilder b = new StringBuilder();
        foreach (char c in s) if (!char.IsWhiteSpace(c)) b.Append(c);
        return b.ToString();
    }

    static List<string> MlsTexts(string raw)
    {
        List<string> l = new List<string>();
        if (raw == null || raw.Length == 0) return l;
        List<string[]> s = MlsSegs(raw);
        if (s == null) { string n0 = MlsNorm(raw); if (n0.Length > 0) l.Add(n0); return l; }
        foreach (string[] g in s)
        {
            string n = MlsNorm(g[1]);
            if (n.Length > 0 && !l.Contains(n)) l.Add(n);
        }
        return l;
    }

    static bool MlsShares(string a, string b)
    {
        List<string> lb = MlsTexts(b);
        foreach (string x in MlsTexts(a)) if (lb.Contains(x)) return true;
        return false;
    }

    // same text in every language (order and a trailing ';' aside)
    static bool MlsSame(string a, string b)
    {
        List<string[]> sa = MlsSegs(a);
        List<string[]> sb2 = MlsSegs(b);
        if (sa == null || sb2 == null) return a == b;
        Dictionary<string, string> da = new Dictionary<string, string>();
        foreach (string[] g in sa) da[g[0]] = g[1];
        Dictionary<string, string> db = new Dictionary<string, string>();
        foreach (string[] g in sb2) db[g[0]] = g[1];
        if (da.Count != db.Count) return false;
        foreach (KeyValuePair<string, string> kv in da)
        {
            string v;
            if (!db.TryGetValue(kv.Key, out v) || v != kv.Value) return false;
        }
        return true;
    }

    static object LangEnum(Type langType, string code)
    {
        string n = code == "??_??" ? "L___" : "L_" + code;
        try { return Enum.Parse(langType, n); }
        catch
        {
            throw new Exception("language '" + code + "' is not a " + langType.FullName + " value (" + n + ").");
        }
    }

    // The new contents as a FRESH MultiLangString (the type of Text.Contents):
    // mode "raw" = text is a whole MultiLangString; else the entry of `lang` is set,
    // the other languages kept (keep) or dropped.
    static object MlsBuild(Type mlsType, string oldRaw, string mode, string lang, bool keep, string text, out string route)
    {
        object m = Activator.CreateInstance(mlsType);
        MethodInfo setAs = RequireMethod(mlsType, "SetAsString", new string[] { "String" }, false);
        if (mode == "raw")
        {
            Call(setAs, m, new object[] { text });
            route = "MultiLangString.SetAsString(raw)";
            return m;
        }
        route = "";
        if (keep && oldRaw.Length > 0) { Call(setAs, m, new object[] { oldRaw }); route = "SetAsString(old)+"; }
        MethodInfo add = RequireMethod(mlsType, "AddString", new string[] { "Language", "String" }, false);
        Type lt = add.GetParameters()[0].ParameterType;
        object lv = LangEnum(lt, lang);
        MethodInfo del = MethodByShape(mlsType, "DeleteString", new string[] { "Language" }, false);
        if (del != null && route.Length > 0)
        {
            try { del.Invoke(m, new object[] { lv }); route += "DeleteString+"; } catch { }
        }
        Call(add, m, new object[] { lv, text });
        route += "AddString(" + lt.Name + "." + lv + ")";
        return m;
    }

    static string TxDisplayed(object t)
    {
        MethodInfo gd = MethodByShape(t.GetType(), "GetDisplayString", new string[] { }, false);
        if (gd == null) return null;
        try { object s = gd.Invoke(t, null); return s == null ? null : SafeText(s); }
        catch { return null; }
    }

    static string TxLayer(object t)
    {
        object ly = TryRead(t, "Layer", null);
        if (ly == null) return null;
        string n = SafeText(TryRead(ly, "Name", null));
        return n != null && n.Length > 0 ? n : SafeText(ly);
    }

    static double TxNum(object t, string name, double dflt)
    {
        object v = TryRead(t, name, null);
        if (v == null) return dflt;
        try { return Convert.ToDouble(v); } catch { return dflt; }
    }

    // what a contents change must leave alone (height only when not requested)
    static string TxGeom(object t, bool withHeight)
    {
        double[] xy = PlXY(t);
        string s = xy == null ? "-" : Math.Round(xy[0], 3) + "/" + Math.Round(xy[1], 3);
        if (withHeight) s += " h" + Math.Round(TxNum(t, "Height", -1), 3);
        s += " j" + SafeText(TryRead(t, "Justification", null)) + " l" + TxLayer(t) +
             " c" + SafeText(TryRead(t, "TextColorId", null)) + " v" + SafeText(TryRead(t, "IsSetAsVisible", null)) +
             " r" + SafeText(TryRead(t, "Rotation", null));
        return s;
    }

    static Dictionary<string, object> TxRecord(object t)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["kind"] = TxKind(t) == 2 ? "PathText" : "Text";
        d["raw"] = MlsRaw(TryRead(t, "Contents", null));
        string disp = TxDisplayed(t);
        if (disp != null) d["displayed"] = disp;
        object loc = TryRead(t, "Location", null);
        if (loc != null) { try { d["location"] = PtDict(loc); } catch { } }
        double h = TxNum(t, "Height", -1);
        if (h >= 0) d["height"] = Math.Round(h, 4);
        object j = TryRead(t, "Justification", null);
        if (j != null) d["justification"] = SafeText(j);
        string ly = TxLayer(t);
        if (ly != null) d["layer"] = ly;
        object c = TryRead(t, "TextColorId", null);
        if (c != null) { try { d["textColorId"] = Convert.ToInt32(c); } catch { } }
        object v = TryRead(t, "IsSetAsVisible", null);
        if (v != null) d["setAsVisible"] = SafeText(v);
        object r = TryRead(t, "Rotation", null);
        if (r != null) { try { d["rotation"] = Math.Round(Convert.ToDouble(r), 4); } catch { } }
        object lm = TryRead(t, "LanguageMode", null);
        if (lm != null) d["languageMode"] = SafeText(lm);
        object fl = TryRead(t, "FixedLanguage", null);
        if (fl != null) d["fixedLanguage"] = SafeText(fl);
        return d;
    }

    // ---- v10: functions that show a path function text -----------------------
    static string FnTextProp(object fn, string name)
    {
        try
        {
            string t;
            if (ReadFnProp(FnProps(fn), name, 0, out t) && t != null && !t.StartsWith("<error")) return t;
        }
        catch { }
        return "";
    }

    // {fn, handle, name, page, x (NaN = none), FUNC_TEXT, FUNC_TEXT_AUTOMATIC}
    static object[] FnTextRow(object fn, string h)
    {
        double[] xy = PlXY(fn);
        object[] r = new object[7];
        r[0] = fn;
        r[1] = h;
        r[2] = SafeText(TryRead(fn, "Name", null));
        r[3] = FnPageName(fn);
        r[4] = xy == null ? double.NaN : xy[0];
        r[5] = FnTextProp(fn, "FUNC_TEXT");
        r[6] = FnTextProp(fn, "FUNC_TEXT_AUTOMATIC");
        return r;
    }

    // the functions on these pages (pages null = every function of the project)
    static List<object[]> FnTextRows(object project, List<object> pages)
    {
        List<object[]> rows = new List<object[]>();
        Dictionary<string, bool> seen = new Dictionary<string, bool>();
        if (pages == null)
        {
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                string h = Handle(fn);
                if (h == null || seen.ContainsKey(h)) continue;
                seen[h] = true;
                rows.Add(FnTextRow(fn, h));
            }
            return rows;
        }
        foreach (object pg in pages)
            foreach (object pl in PagePlacements(pg))
            {
                if (pl == null || !IsFunctionType(pl)) continue;
                string h = Handle(pl);
                if (h == null || seen.ContainsKey(h)) continue;
                seen[h] = true;
                rows.Add(FnTextRow(pl, h));
            }
        return rows;
    }

    static double FnDist(object[] r, string pageName, double x)
    {
        if ((string)r[3] != pageName || double.IsNaN((double)r[4]) || double.IsNaN(x)) return 1e9;
        return Math.Abs((double)r[4] - x);
    }

    // functions with an EMPTY FUNC_TEXT whose FUNC_TEXT_AUTOMATIC shows `raw`, nearest first
    // (the same text under other paths matches too - "dx" tells them apart)
    static List<object> FnShowing(List<object[]> rows, string raw, string pageName, double x, int max, out int total)
    {
        List<object[]> hits = new List<object[]>();
        foreach (object[] r in rows)
        {
            if (MlsTexts((string)r[5]).Count > 0) continue;   // its own function text wins
            if (!MlsShares((string)r[6], raw)) continue;
            hits.Add(r);
        }
        total = hits.Count;
        hits.Sort(delegate(object[] a, object[] b) { return FnDist(a, pageName, x).CompareTo(FnDist(b, pageName, x)); });
        List<object> list = new List<object>();
        foreach (object[] r in hits)
        {
            if (list.Count >= max) break;
            Dictionary<string, object> d = new Dictionary<string, object>();
            d["handle"] = r[1];
            d["name"] = r[2];
            if ((string)r[3] != pageName) d["page"] = r[3];
            else if (!double.IsNaN((double)r[4]) && !double.IsNaN(x)) d["dx"] = Math.Round((double)r[4] - x, 2);
            list.Add(d);
        }
        return list;
    }

    // ---- v10: batch property writes (live_set_function_properties items) -----
    static string FnWrite(object fn, string name, int idx, string val)
    {
        object props = FnProps(fn);
        if (idx > 0)
        {
            PropertyInfo ipi = GetPropInfoIdx(props.GetType(), name);
            object pv = ipi.GetValue(props, new object[] { idx });
            Call(RequireMethod(pv.GetType(), "Set", new string[] { "String" }, false), pv, new object[] { val });
            return "indexed getter+Set(string)";
        }
        return SetLiveProp(props, name, val);
    }

    // previous value back; a MultiLangString form ("??_??@x;") through Set(MultiLangString)
    static string FnRestore(object fn, string name, int idx, string prev)
    {
        List<string[]> segs = MlsSegs(prev);
        if (segs != null && segs.Count > 0)
        {
            object props = FnProps(fn);
            PropertyInfo pi = idx > 0 ? GetPropInfoIdx(props.GetType(), name) : GetPropInfo(props.GetType(), name);
            object pv = pi == null ? null : (idx > 0 ? pi.GetValue(props, new object[] { idx }) : pi.GetValue(props, null));
            MethodInfo setM = pv == null ? null : MethodByShape(pv.GetType(), "Set", new string[] { "MultiLangString" }, false);
            if (setM != null)
            {
                Type mt = setM.GetParameters()[0].ParameterType;
                object m = Activator.CreateInstance(mt);
                Call(RequireMethod(mt, "SetAsString", new string[] { "String" }, false), m, new object[] { prev });
                Call(setM, pv, new object[] { m });
                return "Set(MultiLangString)";
            }
        }
        return FnWrite(fn, name, idx, prev);
    }
'''


# live_read_page(include_text_content=True): appended to the read body.
_READ_TEXT_BODY = r'''
            Dictionary<string, Dictionary<string, object>> v10byH = new Dictionary<string, Dictionary<string, object>>();
            foreach (object v10o in (List<object>)state["placements"])
            {
                Dictionary<string, object> v10d = v10o as Dictionary<string, object>;
                if (v10d == null || !v10d.ContainsKey("handle") || v10d["handle"] == null) continue;
                string v10k = v10d["handle"].ToString();
                if (!v10byH.ContainsKey(v10k)) v10byH[v10k] = v10d;
            }
            int v10n = 0;
            List<object[]> v10paths = new List<object[]>();
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null || TxKind(pl) == 0) continue;
                string h = Handle(pl);
                if (h == null || !v10byH.ContainsKey(h)) continue;
                Dictionary<string, object> rec;
                try { rec = TxRecord(pl); }
                catch (Exception ex) { rec = new Dictionary<string, object>(); rec["error"] = Flatten(ex); }
                v10byH[h]["textContent"] = rec;
                v10n++;
                if (TxKind(pl) == 2) v10paths.Add(new object[] { pl, rec });
            }
            results["textContentCount"] = v10n;
            results["pathTextCount"] = v10paths.Count;
            int v10fmax = V10FMAX;
            if (v10fmax > 0 && v10paths.Count > 0)
            {
                List<object> v10pages = new List<object>();
                v10pages.Add(page);
                List<object[]> v10rows = FnTextRows(project, v10pages);
                string v10pn = PropText(page, "Name");
                foreach (object[] v10pt in v10paths)
                {
                    Dictionary<string, object> v10rec = (Dictionary<string, object>)v10pt[1];
                    if (!v10rec.ContainsKey("raw")) continue;
                    double[] v10xy = PlXY(v10pt[0]);
                    int v10tot;
                    v10rec["functionsShowingIt"] = FnShowing(v10rows, (string)v10rec["raw"], v10pn,
                        v10xy == null ? double.NaN : v10xy[0], v10fmax, out v10tot);
                    v10rec["functionsShowingItCount"] = v10tot;
                }
                results["pageFunctionsChecked"] = v10rows.Count;
            }
'''


def _cs_lit(value):
    """v10: a C# string literal; non-ASCII as \\uXXXX (en dash, č ...) - no codepage involved."""
    out = []
    for ch in cs_escape(value):
        cp = ord(ch)
        if cp < 0x80:
            out.append(ch)
        elif cp <= 0xFFFF:
            out.append("\\u%04x" % cp)
        else:
            cp -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)))
    return '"%s"' % "".join(out)


def _cs_lit_array(values):
    return "new string[] { " + ", ".join(_cs_lit(v) for v in values) + " }"


def _cs_double_array(values):
    return "new double[] { " + ", ".join(cs_double(v, "value") for v in values) + " }"


def _text_display(raw):
    """display text of a MultiLangString form (first non-empty language), else raw itself."""
    disp, by_lang = multilang_display(raw)
    return (raw if disp is None else disp), by_lang


# ---------------------------------------------------------------------------
# v10  live_set_texts
# ---------------------------------------------------------------------------

_SET_TEXTS_BODY = r'''
            string[] tH = V10H;
            string[] tT = V10T;
            string[] tL = V10L;
            string[] tP = V10P;
            bool[] tK = V10K;
            double[] tHt = V10HT;
            bool tDry = V10DRY;
            bool tRb = V10RB;
            string tFn = V10FN;
            int tFmax = V10FMAX;
            int tChMax = V10CHMAX;
            bool tVerbose = V10VERB;

            // ---- 1. resolve every handle BEFORE anything is written ---------------
            Dictionary<string, bool> wanted = new Dictionary<string, bool>();
            foreach (string h in tH) wanted[h] = true;
            Dictionary<string, object> byH = new Dictionary<string, object>();
            Dictionary<string, object> pgOf = new Dictionary<string, object>();
            List<string> pnames = new List<string>();
            foreach (string p in tP) if (p.Length > 0 && !pnames.Contains(p)) pnames.Add(p);
            foreach (string pn in pnames)
            {
                object pg = FindPage(project, pn);
                foreach (object pl in PagePlacements(pg))
                {
                    if (pl == null || TxKind(pl) == 0) continue;
                    string h0 = Handle(pl);
                    if (h0 != null && wanted.ContainsKey(h0) && !byH.ContainsKey(h0)) { byH[h0] = pl; pgOf[h0] = pg; }
                }
            }
            if (byH.Count < wanted.Count)
            {
                foreach (object pg in AllPagesOf(project))
                {
                    if (pg == null) continue;
                    foreach (object pl in PagePlacements(pg))
                    {
                        if (pl == null || TxKind(pl) == 0) continue;
                        string h0 = Handle(pl);
                        if (h0 != null && wanted.ContainsKey(h0) && !byH.ContainsKey(h0)) { byH[h0] = pl; pgOf[h0] = pg; }
                    }
                    if (byH.Count == wanted.Count) break;
                }
            }
            List<string> missing = new List<string>();
            foreach (string h in tH) if (!byH.ContainsKey(h) && !missing.Contains(h)) missing.Add(h);
            if (missing.Count > 0)
                throw new Exception("Nothing changed: " + missing.Count + " handle(s) are no Text / PathText of this project: " +
                    string.Join(", ", missing.ToArray()) + ". Handles are session-scoped - re-read them with " +
                    "live_read_page(types=[Text, PathText], include_text_content=true). Symbols and property texts are refused.");

            // ---- 2. plan: the new contents of every text, checked BEFORE the first write
            List<string> problems = new List<string>();
            List<object> items = new List<object>();
            List<object[]> plan = new List<object[]>();   // {i, text, old raw, new MLS, Contents property, height requested, unchanged}
            for (int i = 0; i < tH.Length; i++)
            {
                object t = byH[tH[i]];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["i"] = i;
                it["handle"] = tH[i];
                it["kind"] = TxKind(t) == 2 ? "PathText" : "Text";
                it["page"] = PropText(pgOf[tH[i]], "Name");
                items.Add(it);
                if (tP[i].Length > 0 && (string)it["page"] != tP[i])
                {
                    problems.Add("item " + i + ": " + tH[i] + " is on page '" + it["page"] + "', not '" + tP[i] + "'");
                    continue;
                }
                PropertyInfo cp = GetWritable(t.GetType(), "Contents");
                if (cp == null)
                {
                    problems.Add("item " + i + ": " + t.GetType().Name + " has no writable Contents. " + MemberList(t.GetType(), false));
                    continue;
                }
                string oldRaw = MlsRaw(TryRead(t, "Contents", null));
                it["before"] = oldRaw;
                string disp0 = TxDisplayed(t);
                if (disp0 != null) it["displayedBefore"] = disp0;
                string lang = tL[i];
                if (lang == "auto")
                {
                    List<string[]> segs = MlsSegs(oldRaw);
                    List<string> langs = new List<string>();
                    if (segs != null) foreach (string[] g in segs) if (g[1].Length > 0 && !langs.Contains(g[0])) langs.Add(g[0]);
                    if (langs.Count == 0) lang = "??_??";
                    else if (langs.Count == 1) lang = langs[0];
                    else
                    {
                        it["languages"] = langs;
                        problems.Add("item " + i + ": the text has " + langs.Count + " languages (" + string.Join(", ", langs.ToArray()) +
                            ") - give language = one of them, ??_?? or raw");
                        continue;
                    }
                }
                it["language"] = lang;
                string route;
                object newM;
                try { newM = MlsBuild(cp.PropertyType, oldRaw, lang == "raw" ? "raw" : "lang", lang, tK[i], tT[i], out route); }
                catch (Exception ex) { problems.Add("item " + i + ": " + Flatten(ex)); continue; }
                string newRaw = MlsRaw(newM);
                it["after"] = newRaw;
                it["route"] = route;
                bool okPred = lang == "raw" ? MlsSame(newRaw, tT[i]) : MlsEntry(newRaw, lang) == tT[i];
                if (!okPred)
                {
                    problems.Add("item " + i + ": the new contents '" + newRaw + "' would not hold the requested text in " + lang);
                    continue;
                }
                bool hReq = tHt[i] > 0;
                if (hReq) it["heightRequested"] = tHt[i];
                bool same = MlsSame(newRaw, oldRaw) && (!hReq || Math.Abs(TxNum(t, "Height", -1) - tHt[i]) < 0.0005);
                it["action"] = same ? "unchanged" : "change";
                if (tVerbose) it["record"] = TxRecord(t);
                plan.Add(new object[] { i, t, oldRaw, newM, cp, hReq, same });
            }
            results["items"] = items;
            results["dryRun"] = tDry;
            if (problems.Count > 0)
                throw new Exception("Nothing changed: " + string.Join("; ", problems.GetRange(0, Math.Min(20, problems.Count)).ToArray()) +
                    (problems.Count > 20 ? " (+" + (problems.Count - 20) + " more)" : ""));

            // ---- 3. functions of the path texts (FUNC_TEXT_AUTOMATIC) before the write
            bool anyPath = false;
            foreach (object[] p in plan) if (TxKind(p[1]) == 2 && !(bool)p[6]) anyPath = true;
            List<object[]> rows = null;
            if (tFn != "none" && anyPath)
            {
                List<object> pgs = null;
                if (tFn == "page")
                {
                    pgs = new List<object>();
                    foreach (object[] p in plan)
                    {
                        if (TxKind(p[1]) != 2) continue;
                        object pg = pgOf[tH[(int)p[0]]];
                        if (!pgs.Contains(pg)) pgs.Add(pg);
                    }
                }
                rows = FnTextRows(project, pgs);
                results["functionsChecked"] = rows.Count;
                results["functionsScope"] = tFn;
                foreach (object[] p in plan)
                {
                    if (TxKind(p[1]) != 2 || (bool)p[6]) continue;
                    Dictionary<string, object> it = (Dictionary<string, object>)items[(int)p[0]];
                    double[] txy = PlXY(p[1]);
                    int tot;
                    List<object> sh = FnShowing(rows, (string)p[2], (string)it["page"], txy == null ? double.NaN : txy[0], tFmax, out tot);
                    it["functionsShowingOldCount"] = tot;
                    if (sh.Count > 0) it["functionsShowingOld"] = sh;
                }
            }

            // ---- 4. write, read back, undo everything of this call on an error ----
            if (!tDry)
            {
                List<object[]> undo = new List<object[]>();   // {text, Contents property, old raw, old height} - height back too if it moved
                Dictionary<string, object> failedAt = null;
                foreach (object[] p in plan)
                {
                    int i = (int)p[0];
                    object t = p[1];
                    Dictionary<string, object> it = (Dictionary<string, object>)items[i];
                    if ((bool)p[6]) { it["result"] = "unchanged"; continue; }
                    bool hReq = (bool)p[5];
                    string step = "Contents";
                    try
                    {
                        string geom0 = TxGeom(t, !hReq);
                        undo.Add(new object[] { t, p[4], p[2], TxNum(t, "Height", -1), i });
                        PropertyInfo cp = (PropertyInfo)p[4];
                        try { cp.SetValue(t, p[3], null); }
                        catch (TargetInvocationException tie) { throw new Exception("Contents: " + Flatten(tie.InnerException)); }
                        if (hReq)
                        {
                            step = "Height";
                            PropertyInfo hp = RequireWritable(t.GetType(), "Height");
                            try { hp.SetValue(t, tHt[i], null); }
                            catch (TargetInvocationException tie) { throw new Exception("Height: " + Flatten(tie.InnerException)); }
                        }
                        step = "verify";
                        string got = MlsRaw(TryRead(t, "Contents", null));
                        it["after"] = got;
                        string lang = (string)it["language"];
                        bool ok = lang == "raw" ? MlsSame(got, tT[i]) : MlsEntry(got, lang) == tT[i];
                        if (!ok) throw new Exception("read back '" + got + "' does not hold the requested text in " + lang);
                        if (tK[i] && lang != "raw")
                        {
                            List<string[]> olds = MlsSegs((string)p[2]);
                            if (olds != null)
                                foreach (string[] g in olds)
                                    if (g[0] != lang && MlsEntry(got, g[0]) != g[1])
                                        throw new Exception("language " + g[0] + " changed: '" + g[1] + "' -> '" + MlsEntry(got, g[0]) + "'");
                        }
                        string geom1 = TxGeom(t, !hReq);
                        it["othersUnchanged"] = geom0 == geom1;
                        if (geom0 != geom1)
                        {
                            it["geomBefore"] = geom0;
                            it["geomAfter"] = geom1;
                            throw new Exception("location / height / justification / layer / colour / visibility changed with the contents");
                        }
                        if (hReq && Math.Abs(TxNum(t, "Height", -1) - tHt[i]) > 0.0005)
                            throw new Exception("height reads " + TxNum(t, "Height", -1) + ", not " + tHt[i]);
                        string disp = TxDisplayed(t);
                        if (disp != null)
                        {
                            it["displayed"] = disp;
                            it["displayedMatches"] = lang == "raw" ? MlsTexts(tT[i]).Contains(MlsNorm(disp)) : MlsNorm(disp) == MlsNorm(tT[i]);
                        }
                        it["verified"] = true;
                        it["result"] = "changed";
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["i"] = i;
                        failedAt["handle"] = tH[i];
                        failedAt["step"] = step;
                        failedAt["error"] = Flatten(ex);
                        break;
                    }
                }
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (tRb)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            object[] u = undo[k];
                            Dictionary<string, object> itU = (Dictionary<string, object>)items[(int)u[4]];
                            try
                            {
                                // only what really changed (the failed write may have changed nothing)
                                PropertyInfo cp = (PropertyInfo)u[1];
                                bool textMoved = !MlsSame(MlsRaw(TryRead(u[0], "Contents", null)), (string)u[2]);
                                bool hMoved = (double)u[3] > 0 && Math.Abs(TxNum(u[0], "Height", -1) - (double)u[3]) > 0.0005;
                                if (textMoved)
                                {
                                    object m = Activator.CreateInstance(cp.PropertyType);
                                    if (((string)u[2]).Length > 0)
                                        Call(RequireMethod(cp.PropertyType, "SetAsString", new string[] { "String" }, false), m, new object[] { u[2] });
                                    cp.SetValue(u[0], m, null);
                                }
                                if (hMoved) RequireWritable(u[0].GetType(), "Height").SetValue(u[0], (double)u[3], null);
                                if (textMoved || hMoved) { rb++; itU["result"] = "rolledBack"; }
                                else itU["result"] = "notWritten";
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); itU["result"] = "rollbackFailed"; }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
                else if (rows != null)
                {
                    // FUNC_TEXT_AUTOMATIC is evaluated by EPLAN - read it again
                    List<object> changed = new List<object>();
                    int nChanged = 0;
                    Dictionary<int, int> perItem = new Dictionary<int, int>();
                    foreach (object[] r in rows)
                    {
                        string now = FnTextProp(r[0], "FUNC_TEXT_AUTOMATIC");
                        if (now == (string)r[6]) continue;
                        nChanged++;
                        int which = -1;
                        foreach (object[] p in plan)
                            if (!(bool)p[6] && TxKind(p[1]) == 2 && MlsShares(now, MlsRaw(p[3]))) { which = (int)p[0]; break; }
                        if (which >= 0) { int c0; perItem.TryGetValue(which, out c0); perItem[which] = c0 + 1; }
                        if (changed.Count >= tChMax) continue;
                        Dictionary<string, object> d = new Dictionary<string, object>();
                        d["handle"] = r[1];
                        d["name"] = r[2];
                        d["page"] = r[3];
                        d["before"] = r[6];
                        d["after"] = now;
                        if (which >= 0) d["item"] = which;
                        changed.Add(d);
                    }
                    foreach (KeyValuePair<int, int> kv in perItem)
                        ((Dictionary<string, object>)items[kv.Key])["functionsChangedCount"] = kv.Value;
                    results["functionsChangedCount"] = nChanged;
                    results["functionsChanged"] = changed;
                }
            }
'''

_TEXT_LANG = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
_TEXT_ITEM_KEYS = frozenset({"handle", "text", "language", "page", "height", "keep_other_languages"})
_TEXT_FN_SCOPES = ("page", "project", "none")


def _text_language(value, what):
    v = "auto" if value is None else value
    if not isinstance(v, str):
        raise SchematicValueError("%s must be a string, got %r." % (what, v))
    v = v.strip()
    if v in ("auto", "raw", "??_??") or _TEXT_LANG.match(v):
        return v
    raise SchematicValueError(
        "%s must be 'auto' (the one language the text has, '??_??' when it has none), '??_??' "
        "(language independent), a language such as 'sl_SI' / 'en_US', or 'raw' (text is a whole "
        "MultiLangString 'sl_SI@..;en_US@..;'), got %r." % (what, value))


def live_set_texts(items: list = None, handle: str = None, text: str = None, page: str = None,
                   language: str = "auto", keep_other_languages: bool = True,
                   dry_run: bool = True, allow_real_project: bool = False,
                   rollback_on_error: bool = True, check_functions: str = "page",
                   function_list_limit: int = 3, max_items: int = 200, verbose: bool = False,
                   timeout_seconds: float = 300.0) -> dict:
    """
    v10: CHANGE THE CONTENTS of existing free texts on the sheet - Text and
    PathText (path function text / Pfadfunktionstext, the magenta text under a
    device) - by handle, many in one call. WRITES (unless dry_run, the
    default) - scratch-only unless allow_real_project=True (dry_run only reads).

    Only Text.Contents (MultiLangString) changes; location, layer, colour,
    justification, visibility and height stay (height only if given). A path
    function text feeds FUNC_TEXT_AUTOMATIC (#20031, "Function text
    (automatic)") of the functions of its path whose FUNC_TEXT is empty, and
    so the PLC cards / overview connection points; EPLAN evaluates that
    property itself (read-only, no setter, no refresh action in API 2.9) -
    this tool reads it before and after and reports what changed. Property
    texts of a symbol (PropertyPlacement) are refused - use
    live_set_function_properties for the property value.

    Handles: live_read_page(page, types=["Text", "PathText"],
    include_text_content=True) - "textContent" gives the contents and the
    functions that show a path text. Handles are session-scoped.

    Args:
        items: [{"handle", "text", "language"?, "page"?, "height"?,
            "keep_other_languages"?}] (up to max_items, default 200) - or
            handle= + text= for one text.
        language: default for the items - "auto" (default: the one language
            the text already has, "??_??" when it has none; a text with
            several languages is refused - name one), "??_??" (language
            independent), "sl_SI" / "en_US" / ... (that entry), or "raw" (text
            is the whole MultiLangString "sl_SI@a;en_US@b;", every language
            replaced). The text must not contain ";" (it separates languages)
            except with "raw". Line breaks: "\\n".
        keep_other_languages: default True - entries of other languages
            stay; False keeps only the written one.
        page: optional, default page of the items - resolve the handles on
            this page only (faster; without it every page is searched).
        dry_run: default True - per item "before", "language", "after" (the
            new MultiLangString), "action" change/unchanged, and for a path
            text "functionsShowingOldCount" / "functionsShowingOld" (functions
            with an empty FUNC_TEXT whose FUNC_TEXT_AUTOMATIC shows the old
            text, nearest first, "dx" = x distance - the same text under
            another path matches too); nothing changed.
        allow_real_project: must be True to write outside the scratch root.
        rollback_on_error: default True - an error or a failed read-back
            undoes every text of this call (contents and height).
        check_functions: "page" (default: functions on the pages of the
            changed path texts), "project" (every function - also the overview
            PLC points of a card page) or "none".
        function_list_limit: functions listed per path text / changed
            (default 3; changed list: 20 x this).
        verbose: full text record ("record") per item.
        timeout_seconds: default 300.

    Returns:
        {"success", "dryRun", "counts" {change/unchanged | changed/unchanged},
         "items": [{i, handle, kind Text/PathText, page, before, textBefore,
         language, after, textAfter, action, result changed/unchanged,
         verified, othersUnchanged, displayed, displayedMatches,
         functionsShowingOldCount, functionsShowingOld, functionsChangedCount}],
         "functionsChecked", "functionsChangedCount", "functionsChanged"
         [{handle, name, page, before, after, item}], "warnings"}.
        "before" is the old MultiLangString: language="raw", text=before
        restores a text. A failed write: success false, errorType
        "ROLLED_BACK" (or "PARTIAL"), "failedAt" {i, handle, step, error}.
    """
    try:
        if items is not None and not isinstance(items, (list, tuple)):
            raise SchematicValueError("items must be a list of {handle, text, language?, page?, height?}.")
        raw_items = list(items or [])
        if handle is not None or text is not None:
            if handle is None or text is None:
                raise SchematicValueError("Give handle= and text= together (or items=[...]).")
            raw_items.append({"handle": handle, "text": text})
        if not raw_items:
            raise SchematicValueError("Nothing to change: give items=[{handle, text}] or handle= + text=.")
        mx = cs_int(max_items, "max_items", minimum=1, maximum=1000)
        if len(raw_items) > mx:
            raise SchematicValueError("%d items, more than max_items=%d - nothing changed." % (len(raw_items), mx))
        dflt_lang = _text_language(language, "language")
        dflt_page = cs_text(page, "page", allow_empty=True)
        if check_functions not in _TEXT_FN_SCOPES:
            raise SchematicValueError("check_functions must be 'page', 'project' or 'none', got %r." % (check_functions,))
        fmax = cs_int(function_list_limit, "function_list_limit", minimum=0, maximum=50)
        hs, ts, ls, ps, ks, hts = [], [], [], [], [], []
        for n, it in enumerate(raw_items):
            if not isinstance(it, dict):
                raise SchematicValueError("items[%d] must be a dict {handle, text, ...}, got %r." % (n, it))
            extra = set(it) - _TEXT_ITEM_KEYS
            if extra:
                raise SchematicValueError("items[%d]: unknown key(s) %s (allowed: %s)."
                                          % (n, sorted(extra), ", ".join(sorted(_TEXT_ITEM_KEYS))))
            h = it.get("handle")
            if not isinstance(h, str) or not h.strip():
                raise SchematicValueError("items[%d]: handle must be a non-empty string." % n)
            h = h.strip()
            if h in hs:
                raise SchematicValueError("items[%d]: handle %s is given twice." % (n, h))
            t = it.get("text")
            if not isinstance(t, str):
                raise SchematicValueError("items[%d]: text must be a string (\"\" clears it), got %r." % (n, t))
            t = cs_text(t, "items[%d].text" % n, allow_empty=True)
            lang = _text_language(it.get("language", dflt_lang), "items[%d].language" % n)
            if lang == "raw":
                disp, _ = multilang_display(t)
                if t and disp is None:
                    raise SchematicValueError("items[%d]: language 'raw' needs the MultiLangString form "
                                              "'sl_SI@text;en_US@text;', got %r." % (n, t))
            elif ";" in t:
                raise SchematicValueError("items[%d]: ';' separates languages in EPLAN's MultiLangString - "
                                          "not allowed in a text (use language='raw' for a whole MultiLangString)." % n)
            pg = it.get("page", dflt_page)
            pg = cs_text(pg, "items[%d].page" % n, allow_empty=True)
            hv = it.get("height")
            hts.append(-1.0 if hv is None else float(cs_double(hv, "items[%d].height" % n)))
            if hv is not None and not 0 < hts[-1] <= 100:
                raise SchematicValueError("items[%d]: height must be > 0 and <= 100 mm." % n)
            keep = it.get("keep_other_languages", keep_other_languages)
            hs.append(h)
            ts.append(t)
            ls.append(lang)
            ps.append(pg)
            ks.append(bool(keep))
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _SET_TEXTS_BODY,
            V10H=_cs_lit_array(hs), V10T=_cs_lit_array(ts), V10L=_cs_lit_array(ls), V10P=_cs_lit_array(ps),
            V10K=_cs_bool_array(ks), V10HT=_cs_double_array(hts), V10DRY=cs_bool(dry_run),
            V10RB=cs_bool(rollback_on_error), V10FN='"%s"' % check_functions, V10FMAX=str(fmax),
            V10CHMAX=str(max(fmax, 1) * 20), V10VERB=cs_bool(verbose))
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    out = _shape_keep(_execute_script(
        _script(_cls("SetTexts"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS + _HELPERS_V10),
        timeout=timeout_seconds,
    ), keep=("items", "failedAt", "rolledBack", "rollbackErrors", "project"))
    for it in out.get("items") or []:
        for src, dst in (("before", "textBefore"), ("after", "textAfter")):
            if isinstance(it.get(src), str):
                it[dst], _ = _text_display(it[src])
    if not out.get("success"):
        return out
    out["dryRun"] = bool(dry_run)
    counts, warnings = {}, []
    for it in out.get("items") or []:
        key = it.get("result") or it.get("action") or "?"
        counts[key] = counts.get(key, 0) + 1
    out["counts"] = counts
    nomatch = [str(it.get("handle")) for it in out.get("items") or [] if it.get("displayedMatches") is False]
    if nomatch:
        warnings.append("Displayed text differs from the written text on %d text(s) (%s) - another language entry "
                        "is displayed (project display language); write that language or language='??_??' with "
                        "keep_other_languages=false." % (len(nomatch), ", ".join(nomatch[:10])))
    paths = [it for it in out.get("items") or [] if it.get("kind") == "PathText" and it.get("result") == "changed"]
    if paths and check_functions != "none" and not out.get("functionsChangedCount") and not out.get("failedAt"):
        warnings.append("No FUNC_TEXT_AUTOMATIC changed within this call (%d function(s) checked, scope %s). EPLAN "
                        "evaluates it itself - re-read with live_read_function_properties(properties="
                        "[\"FUNC_TEXT\", \"FUNC_TEXT_AUTOMATIC\"]); a function with its own FUNC_TEXT keeps it."
                        % (out.get("functionsChecked") or 0, check_functions))
    if dry_run and any(it.get("kind") == "PathText" and it.get("action") == "change" for it in out.get("items") or []):
        warnings.append("Path function texts feed FUNC_TEXT_AUTOMATIC of the functions of their path whose FUNC_TEXT "
                        "is empty (see functionsShowingOld) - a function with its own FUNC_TEXT does not change.")
    if warnings:
        out["warnings"] = warnings
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
        fa = out["failedAt"]
        out["error"] = "Writing failed at item %s (%s, step %s): %s. %s" % (
            fa.get("i"), fa.get("handle"), fa.get("step"), fa.get("error"),
            "All texts of this call were restored." if rollback_on_error and not out.get("rollbackErrors")
            else "Some changes stay - check the page.")
    return out


# ---------------------------------------------------------------------------
# v10  live_set_function_properties(items=[...])
# ---------------------------------------------------------------------------

_SET_ITEMS_BODY = r'''
            string[] iH = V10IH;
            string[] iN = V10IN;
            string[] iP = V10IP;
            string[] iC = V10IC;
            int[] iLim = V10ILIM;
            int[] rI = V10RI;
            string[] rN = V10RN;
            int[] rX = V10RX;
            string[] rV = V10RV;
            bool pDry = V10DRY;
            bool pRb = V10RB;
            bool pVerbose = V10VERB;

            // ---- 1. the project's functions, indexed once (handle, exact name) --
            Dictionary<string, object> fByH = new Dictionary<string, object>();
            Dictionary<string, List<object>> fByN = new Dictionary<string, List<object>>();
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                string h = Handle(fn);
                if (h != null && !fByH.ContainsKey(h)) fByH[h] = fn;
                string n = PropText(fn, "Name");
                if (n == null) continue;
                List<object> l;
                if (!fByN.TryGetValue(n, out l)) { l = new List<object>(); fByN[n] = l; }
                l.Add(fn);
            }

            // ---- 2. resolve and validate EVERY item before the first write -----
            List<string> problems = new List<string>();
            List<List<object>> targets = new List<List<object>>();
            Dictionary<string, int> usedBy = new Dictionary<string, int>();
            int nFn = 0;
            for (int k = 0; k < iH.Length; k++)
            {
                List<object> cand = new List<object>();
                if (iH[k].Length > 0)
                {
                    object f;
                    if (fByH.TryGetValue(iH[k], out f) && FnMatches(f, iN[k], "", iP[k], iC[k], "", "")) cand.Add(f);
                }
                else
                {
                    List<object> l;
                    if (fByN.TryGetValue(iN[k], out l))
                        foreach (object f in l) if (FnMatches(f, "", "", iP[k], iC[k], "", "")) cand.Add(f);
                }
                targets.Add(cand);
                string sel = (iH[k].Length > 0 ? iH[k] : iN[k]) + (iP[k].Length > 0 ? " @" + iP[k] : "") + (iC[k].Length > 0 ? " :" + iC[k] : "");
                if (cand.Count == 0) { problems.Add("item " + k + " (" + sel + "): no function"); continue; }
                if (cand.Count > iLim[k]) { problems.Add("item " + k + " (" + sel + "): " + cand.Count + " functions match, limit " + iLim[k]); continue; }
                nFn += cand.Count;
                foreach (object f in cand)
                {
                    string fh = Handle(f);
                    int prevK;
                    if (fh != null && usedBy.TryGetValue(fh, out prevK))
                        problems.Add("item " + k + " (" + sel + "): function " + fh + " is also the target of item " + prevK);
                    else if (fh != null) usedBy[fh] = k;
                    object props = FnProps(f);
                    for (int r = 0; r < rI.Length; r++)
                    {
                        if (rI[r] != k) continue;
                        if (FnPropInfo(props, rN[r], rX[r]) == null)
                            problems.Add("item " + k + " (" + sel + "): property " + rN[r] + (rX[r] > 0 ? "[" + rX[r] + "]" : "") +
                                " is not declared by " + props.GetType().Name);
                    }
                }
                if (problems.Count > 50) break;
            }
            results["matchedFunctions"] = nFn;
            if (problems.Count > 0)
                throw new Exception("Nothing written - " + problems.Count + " problem(s): " +
                    string.Join("; ", problems.GetRange(0, Math.Min(20, problems.Count)).ToArray()));

            // ---- 3. write (or count), read back; undo all of this call on an error
            int written = 0, unchanged = 0, verified = 0, nUnver = 0;
            List<object> unver = new List<object>();
            List<object> details = new List<object>();
            List<object[]> undo = new List<object[]>();   // {fn, name, idx, previous}
            Dictionary<string, object> failedAt = null;
            for (int k = 0; k < iH.Length && failedAt == null; k++)
            {
                foreach (object f in targets[k])
                {
                    List<object> plist = null;
                    if (pVerbose)
                    {
                        Dictionary<string, object> d = new Dictionary<string, object>();
                        d["item"] = k;
                        d["name"] = PropText(f, "Name");
                        d["handle"] = Handle(f);
                        d["page"] = FnPageName(f);
                        plist = new List<object>();
                        d["properties"] = plist;
                        details.Add(d);
                    }
                    string step = "";
                    try
                    {
                        for (int r = 0; r < rI.Length; r++)
                        {
                            if (rI[r] != k) continue;
                            string key = rX[r] > 0 ? rN[r] + "[" + rX[r] + "]" : rN[r];
                            step = key;
                            string before;
                            ReadFnProp(FnProps(f), rN[r], rX[r], out before);
                            if (before == null) before = "";
                            Dictionary<string, object> pd = null;
                            if (plist != null)
                            {
                                pd = new Dictionary<string, object>();
                                pd["property"] = key;
                                pd["previous"] = before;
                                pd["requested"] = rV[r];
                                plist.Add(pd);
                            }
                            if (before == rV[r] || MlsEntryAny(before, rV[r]))
                            {
                                unchanged++;
                                if (pd != null) pd["result"] = "unchanged";
                                continue;
                            }
                            if (pDry)
                            {
                                written++;
                                if (pd != null) pd["result"] = "wouldWrite";
                                continue;
                            }
                            undo.Add(new object[] { f, rN[r], rX[r], before });
                            string route = FnWrite(f, rN[r], rX[r], rV[r]);
                            written++;
                            string after;
                            ReadFnProp(FnProps(f), rN[r], rX[r], out after);
                            bool ok = after == rV[r] || MlsEntryAny(after, rV[r]);
                            if (ok) verified++;
                            else
                            {
                                nUnver++;
                                if (unver.Count < 50)
                                {
                                    Dictionary<string, object> u = new Dictionary<string, object>();
                                    u["item"] = k;
                                    u["handle"] = Handle(f);
                                    u["name"] = PropText(f, "Name");
                                    u["property"] = key;
                                    u["requested"] = rV[r];
                                    u["new"] = after;
                                    unver.Add(u);
                                }
                            }
                            if (pd != null) { pd["route"] = route; pd["new"] = after; pd["verified"] = ok; pd["result"] = "written"; }
                        }
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["item"] = k;
                        failedAt["handle"] = Handle(f);
                        failedAt["name"] = PropText(f, "Name");
                        failedAt["property"] = step;
                        failedAt["error"] = Flatten(ex);
                        break;
                    }
                }
            }
            Dictionary<string, object> counts = new Dictionary<string, object>();
            counts["items"] = iH.Length;
            counts["functions"] = nFn;
            counts[pDry ? "wouldWrite" : "written"] = written;
            counts["unchanged"] = unchanged;
            if (!pDry) { counts["verified"] = verified; counts["unverified"] = nUnver; }
            results["counts"] = counts;
            results["dryRun"] = pDry;
            if (unver.Count > 0) results["unverified"] = unver;
            if (pVerbose) results["details"] = details;
            if (failedAt != null)
            {
                results["failedAt"] = failedAt;
                if (pRb)
                {
                    int rb = 0;
                    List<string> rbErr = new List<string>();
                    for (int q = undo.Count - 1; q >= 0; q--)
                    {
                        object[] u = undo[q];
                        try
                        {
                            string cur;
                            ReadFnProp(FnProps(u[0]), (string)u[1], (int)u[2], out cur);
                            if (cur == (string)u[3]) continue;   // the failed write changed nothing
                            FnRestore(u[0], (string)u[1], (int)u[2], (string)u[3]);
                            rb++;
                        }
                        catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                    }
                    results["rolledBack"] = rb;
                    if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                }
            }
'''

_FP_ITEM_KEYS = frozenset({"handle", "name", "page", "connection_designation", "values", "limit"})


def _set_function_properties_items(items, allow_real_project, timeout_seconds, dry_run,
                                   rollback_on_error, verbose, max_items):
    """v10: live_set_function_properties(items=[...]) - one script, one LockingStep."""
    try:
        if not isinstance(items, (list, tuple)) or not items:
            raise SchematicValueError("items must be a non-empty list of {handle | name (+page, "
                                      "connection_designation), values: {property: value}, limit?}.")
        mx = cs_int(max_items, "max_items", minimum=1, maximum=2000)
        if len(items) > mx:
            raise SchematicValueError("%d items, more than max_items=%d - nothing written." % (len(items), mx))
        ih, inm, ip, ic, ilim = [], [], [], [], []
        ri, rn, rx, rv = [], [], [], []
        for k, it in enumerate(items):
            if not isinstance(it, dict):
                raise SchematicValueError("items[%d] must be a dict, got %r." % (k, it))
            extra = set(it) - _FP_ITEM_KEYS
            if extra:
                raise SchematicValueError("items[%d]: unknown key(s) %s (allowed: %s)."
                                          % (k, sorted(extra), ", ".join(sorted(_FP_ITEM_KEYS))))
            h = cs_text(it.get("handle"), "items[%d].handle" % k, allow_empty=True).strip()
            nm = cs_text(it.get("name"), "items[%d].name" % k, allow_empty=True).strip()
            if not h and not nm:
                raise SchematicValueError("items[%d]: give handle or name (+ page / connection_designation)." % k)
            vals = it.get("values")
            if not isinstance(vals, dict) or not vals:
                raise SchematicValueError("items[%d]: values must be a non-empty dict {property: value}." % k)
            ih.append(h)
            inm.append(nm)
            ip.append(cs_text(it.get("page"), "items[%d].page" % k, allow_empty=True))
            ic.append(cs_text(None if it.get("connection_designation") is None else str(it["connection_designation"]),
                              "items[%d].connection_designation" % k, allow_empty=True))
            ilim.append(cs_int(it.get("limit", 1), "items[%d].limit" % k, minimum=1, maximum=50))
            for key, v in vals.items():
                if v is None:
                    raise SchematicValueError("items[%d]: value for %s must not be None (use \"\" to clear)." % (k, key))
                (pname, pidx), = _parse_props([key], "items[%d].values" % k)
                ri.append(k)
                rn.append(pname)
                rx.append(pidx)
                rv.append(cs_text(str(v), "items[%d] value for %s" % (k, key), allow_empty=True))
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _SET_ITEMS_BODY,
            V10IH=_cs_lit_array(ih), V10IN=_cs_lit_array(inm), V10IP=_cs_lit_array(ip), V10IC=_cs_lit_array(ic),
            V10ILIM=_cs_int_array(ilim), V10RI=_cs_int_array(ri), V10RN=_cs_str_array(rn), V10RX=_cs_int_array(rx),
            V10RV=_cs_lit_array(rv), V10DRY=cs_bool(dry_run), V10RB=cs_bool(rollback_on_error),
            V10VERB=cs_bool(verbose))
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    out = _shape_keep(_execute_script(
        _script(_cls("SetFnPropsItems"), body,
                extra_helpers=_HELPERS_SCHEMATIC + _HELPERS_PROPS + _HELPERS_V10),
        timeout=timeout_seconds,
    ), keep=("counts", "failedAt", "rolledBack", "rollbackErrors", "details", "project"))
    if not out.get("success"):
        return out
    out["dryRun"] = bool(dry_run)
    if out.get("unverified"):
        out["warning"] = ("Read-back differs from the requested value on %d write(s) - see 'unverified' (EPLAN may "
                          "reformat a value or refuse it silently)." % (out.get("counts") or {}).get("unverified", 0))
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
        fa = out["failedAt"]
        out["error"] = "Writing failed at item %s (%s, %s): %s. %s" % (
            fa.get("item"), fa.get("name") or fa.get("handle"), fa.get("property"), fa.get("error"),
            "All writes of this call were undone." if rollback_on_error and not out.get("rollbackErrors")
            else "Some writes stay - check with live_read_function_properties.")
    return out
