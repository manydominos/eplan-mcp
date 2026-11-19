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
                                 allow_missing_article: bool = False) -> dict:
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

    Returns:
        {"success", "project", "matched", "modified", "details": [{"name",
         "handle", "page", "properties": [{"property", "previous", "requested",
         "new", "verified", "route"}], "article": {"from", "to", "previous",
         "after", "route"}}], "articleInDb"}. "previous" makes every edit
        reversible. A failure mid-way keeps "details" of what was already
        written. EPLAN stores the change in the project database itself (no
        save command; get_current_project reports "databaseLastWrite").
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
