"""
Many placements at once: remove or move a whole macro, a device, a list of
handles or everything inside a rectangle in ONE call (EPLAN 2.9 and later).

    live_remove_placements  remove many placements in one call (list of
                            handles, a rectangle, or one macro / device)
    live_move_placements    move placements (one macro / device, a list, a
                            rectangle) by setting Placement.Location - nothing
                            removed or re-inserted

Also the C# helpers the function-property and copy tools (live_props.py,
live_copy.py) share with these two (_HELPERS_LOCAL): device tags of Function
SUBCLASSES (a PLC connection point is a Terminal, a PLC box a PLC), bounding
boxes and groups of placements, and property placements (the texts that show
a property of a symbol). They are kept here, next to their first users,
instead of growing schematic._HELPERS_SCHEMATIC; every script that needs them
is built with _HELPERS_SCHEMATIC + _HELPERS_LOCAL.

A property text's Location is RELATIVE to its symbol's insertion point
(EPLAN 2.9), so live_move_placements does not move such texts a second time.

Same reflection scaffold as schematic.py (no `using Eplan.EplApi.DataModel;`),
C# 5 only, scratch-only writes unless allow_real_project=True.
"""

import json
import os
import re
import time
import uuid

from ._base import cs_escape
from .live import _script
from .scripted import _execute_script
from .schematic import _HELPERS_SCHEMATIC, _guard_prelude, _fill, _shape, _cls, _err
from .schematic_model import SchematicValueError, cs_bool, cs_double, cs_int, cs_text

__all__ = [
    "live_remove_placements",
    "live_move_placements",
]


_HELPERS_LOCAL = r'''
    // ---- v5: device tags of Function SUBCLASSES ------------------------------
    // A PLC connection point is a Terminal, a PLC box a PLC: both derive from
    // the object model class Function and carry a device tag (
    // FunctionBase.Name is { get; set; } in API 2.9, Terminal/PLC inherit it).
    static bool IsFunctionType(object o)
    {
        if (o == null) return false;
        Type t = o.GetType();
        while (t != null)
        {
            if (t.Name == "Function" && (t.Namespace == null || t.Namespace.StartsWith("Eplan.EplApi.DataModel")))
                return true;
            t = t.BaseType;
        }
        return false;
    }

    // "=AB-C+DI-510K3:9" -> "=AB-C+DI-510K3": the device tag without a
    // ":designation" suffix after the last '-' (terminals, PLC points).
    static string DtPart(string name)
    {
        if (name == null) return "";
        int dash = name.LastIndexOf('-');
        int colon = name.IndexOf(':', dash < 0 ? 0 : dash);
        return colon < 0 ? name : name.Substring(0, colon);
    }

    // "=AB-C+DI-510K3" -> "510K3" (the last device tag segment).
    static string ShortTag(string dt)
    {
        if (dt == null) return "";
        int dash = dt.LastIndexOf('-');
        return dash < 0 ? dt : dt.Substring(dash + 1);
    }

    // New device tag in the structure of the old one: "510K4" on
    // "=AB-C+DI-510K3" -> "=AB-C+DI-510K4". A tag with '=' or '+' is taken whole.
    static string ComposeDt(string oldDt, string tag)
    {
        if (tag.IndexOf('=') >= 0 || tag.IndexOf('+') >= 0) return tag;
        int dash = oldDt == null ? -1 : oldDt.LastIndexOf('-');
        string structure = dash < 0 ? "" : oldDt.Substring(0, dash);
        return structure + "-" + tag.TrimStart('-');
    }

    // Name selector: the exact name, the device tag without ":x", or - for a
    // selector without '=' / '+' - the last device tag segment ("510K3").
    static bool NameMatches(string name, string sel)
    {
        if (sel == null || sel.Length == 0) return true;
        if (name == null) return false;
        if (name == sel) return true;
        string dt = DtPart(name);
        if (dt == sel) return true;
        if (sel.IndexOf('=') < 0 && sel.IndexOf('+') < 0)
            return string.Equals(ShortTag(dt), ShortTag(sel), StringComparison.OrdinalIgnoreCase);
        return false;
    }

    // ---- v7: removing many placements (live_remove_placements) ---------------
    // Bounding box as {minX, minY, maxX, maxY}; a placement without
    // GetBoundingBox falls back to its Location (a point). null = no geometry.
    static double[] BBoxOf(object pl)
    {
        double[] b = null;
        MethodInfo bbox = MethodByShape(pl.GetType(), "GetBoundingBox", new string[] { }, false);
        if (bbox != null)
        {
            try
            {
                object bb = bbox.Invoke(pl, null);
                if (bb is IEnumerable)
                    foreach (object p in (IEnumerable)bb)
                        if (p != null) b = BBoxAdd(b, p);
            }
            catch { b = null; }
        }
        if (b == null)
        {
            object loc = TryRead(pl, "Location", null);
            if (loc != null) { try { b = BBoxAdd(null, loc); } catch { b = null; } }
        }
        return b;
    }

    static double[] BBoxAdd(double[] b, object pt)
    {
        Dictionary<string, object> d = PtDict(pt);
        double x = Convert.ToDouble(d["x"]), y = Convert.ToDouble(d["y"]);
        if (b == null)
        {
            // (not an array initializer: test_routed_offline counts those in every schematic script)
            b = new double[4];
            b[0] = x; b[1] = y; b[2] = x; b[3] = y;
            return b;
        }
        if (x < b[0]) b[0] = x;
        if (y < b[1]) b[1] = y;
        if (x > b[2]) b[2] = x;
        if (y > b[3]) b[3] = y;
        return b;
    }

    // inner fully inside outer (tolerance tol, mm).
    static bool BoxInside(double[] inner, double[] outer, double tol)
    {
        if (inner == null || outer == null) return false;
        return inner[0] >= outer[0] - tol && inner[1] >= outer[1] - tol &&
               inner[2] <= outer[2] + tol && inner[3] <= outer[3] + tol;
    }

    static bool PointInside(object pl, double[] outer, double tol)
    {
        object loc = TryRead(pl, "Location", null);
        if (loc == null) return false;
        Dictionary<string, object> d = PtDict(loc);
        double x = Convert.ToDouble(d["x"]), y = Convert.ToDouble(d["y"]);
        return x >= outer[0] - tol && y >= outer[1] - tol && x <= outer[2] + tol && y <= outer[3] + tol;
    }

    static bool IsGraphicPl(object o)
    {
        Type t = o.GetType();
        string ns = t.Namespace == null ? "" : t.Namespace;
        if (ns.EndsWith(".Graphics")) return true;
        switch (t.Name)
        {
            case "Line": case "Arc": case "Circle": case "Polyline": case "PolyLine":
            case "Rectangle": case "Ellipse": case "Hatch": case "Image": case "Text":
                return true;
        }
        return false;
    }

    // PLC box, black box, ... - a placement that encloses other functions.
    static bool IsBoxPl(object o)
    {
        if (o == null || IsGraphicPl(o) || IsGroupPl(o)) return false;
        string t = o.GetType().Name;
        return t == "PLC" || t.EndsWith("Box");
    }

    static bool IsGroupPl(object o)
    {
        Type t = o == null ? null : o.GetType();
        while (t != null)
        {
            if (t.Name == "Group") return true;
            t = t.BaseType;
        }
        return false;
    }

    static bool TypeIn(object o, string[] types)
    {
        if (types == null || types.Length == 0) return true;
        string tn = o.GetType().Name;
        foreach (string w in types)
            if (string.Equals(tn, w, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    // Placement.Group (API 2.9: the group the placement belongs to, or null),
    // walked up to the outermost group.
    static object TopGroup(object pl)
    {
        object g = TryRead(pl, "Group", null);
        int guard = 0;
        while (g != null && guard++ < 20)
        {
            object up = TryRead(g, "Group", null);
            if (up == null) break;
            g = up;
        }
        return g;
    }

    // Group.SubPlacements (API 2.9), recursively; nested groups included.
    static void GroupMembers(object grp, List<object> into, int depth)
    {
        if (grp == null || depth > 20) return;
        object subs = TryRead(grp, "SubPlacements", null);
        if (!(subs is IEnumerable)) return;
        foreach (object m in (IEnumerable)subs)
        {
            if (m == null) continue;
            into.Add(m);
            if (IsGroupPl(m)) GroupMembers(m, into, depth + 1);
        }
    }

    static bool IsValidObj(object o)
    {
        object v = TryRead(o, "IsValid", null);
        if (v == null) return true;
        try { return Convert.ToBoolean(v); } catch { return true; }
    }

    static void AddPick(Dictionary<string, object> objs, Dictionary<string, string> why,
                        List<string> order, object pl, string reason)
    {
        if (pl == null) return;
        string h = Handle(pl);
        if (h == null) return;
        if (objs.ContainsKey(h)) return;
        objs[h] = pl;
        why[h] = reason;
        order.Add(h);
    }

    static Dictionary<string, object> PickRecord(object pl, string h, string reason, bool full)
    {
        Dictionary<string, object> d = full ? DumpPlacement(pl, false) : new Dictionary<string, object>();
        d["handle"] = h;
        d["clrType"] = pl.GetType().Name;
        if (!full)
        {
            string n = SafeText(TryRead(pl, "Name", null));
            if (n != null && n.Length > 0) d["name"] = n;
        }
        d["reason"] = reason;
        return d;
    }

    static bool TagHit(string name, string[] tags)
    {
        if (name == null || name.Length == 0 || tags == null) return false;
        foreach (string t in tags)
            if (t != null && t.Length > 0 && NameMatches(name, t)) return true;
        return false;
    }

    // ---- v8: property placements (texts that SHOW a property of a symbol) ----
    // API 2.9: SymbolReference.PropertyPlacements (PropertyPlacement[]) and
    // UseLocalPropertyPlacements; PropertyPlacement.DisplayedProperty
    // (AnyPropertyId: AsInt, Index, Definition.Name), IsSetAsVisible
    // (Placement.Visibility: Visible / Invisible / ByLayer / Undetermined),
    // IsVisible, GetDisplayString(), Contents, Location, Height, Justification,
    // IsDocked, Relation. Shared by live_read_page and
    // live_place_function_property, so a write and a later read agree.
    static int PpInt(object o, string name, int dflt)
    {
        if (o == null) return dflt;
        object v = TryRead(o, name, null);
        if (v == null) return dflt;
        try { return Convert.ToInt32(v); } catch { return dflt; }
    }

    static int PpPropId(object pp)
    {
        return PpInt(TryRead(pp, "DisplayedProperty", null), "AsInt", -1);
    }

    static int PpPropIdx(object pp)
    {
        return PpInt(TryRead(pp, "DisplayedProperty", null), "Index", 0);
    }

    // IsSetAsVisible as its enum name, or null when the member is absent.
    static string PpVisibility(object pp)
    {
        object v = TryRead(pp, "IsSetAsVisible", null);
        return v == null ? null : SafeText(v);
    }

    // Shown on the sheet: "Visible", or "ByLayer" with IsVisible true. NOTE (API 2.9):
    // IsVisible also answers true for a hidden text when GED shows invisible elements.
    static bool PpShown(object pp)
    {
        string s = PpVisibility(pp);
        bool isVis = false;
        object iv = TryRead(pp, "IsVisible", null);
        if (iv != null) { try { isVis = Convert.ToBoolean(iv); } catch { isVis = false; } }
        if (s == null) return isVis;
        if (s == "Visible") return true;
        if (s == "ByLayer") return isVis;
        return false;
    }

    // The text as EPLAN draws it: GetDisplayString(), else Contents (MultiLangString).
    static string PpText(object pp, out string source)
    {
        source = null;
        MethodInfo gd = MethodByShape(pp.GetType(), "GetDisplayString", new string[] { }, false);
        if (gd != null)
        {
            try
            {
                object s = gd.Invoke(pp, null);
                if (s != null) { source = "GetDisplayString"; return SafeText(s); }
            }
            catch { }
        }
        object c = TryRead(pp, "Contents", null);
        if (c != null)
        {
            MethodInfo ga = MethodByShape(c.GetType(), "GetAsString", new string[] { }, false);
            if (ga != null)
            {
                try
                {
                    object s = ga.Invoke(c, null);
                    if (s != null) { source = "Contents.GetAsString"; return SafeText(s); }
                }
                catch { }
            }
            string t = SafeText(c);
            if (t != null) { source = "Contents"; return t; }
        }
        return null;
    }

    static Dictionary<string, object> MinMax(double[] b)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        List<double> mn = new List<double>();
        mn.Add(Math.Round(b[0], 4)); mn.Add(Math.Round(b[1], 4));
        List<double> mx = new List<double>();
        mx.Add(Math.Round(b[2], 4)); mx.Add(Math.Round(b[3], 4));
        d["min"] = mn;
        d["max"] = mx;
        return d;
    }

    static Dictionary<string, object> DumpPropPl(object pp)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["handle"] = Handle(pp);
        d["clrType"] = pp.GetType().Name;
        object id = TryRead(pp, "DisplayedProperty", null);
        if (id != null)
        {
            int n = PpInt(id, "AsInt", -1);
            if (n >= 0) d["propertyId"] = n;
            int ix = PpInt(id, "Index", 0);
            if (ix > 0) d["propertyIndex"] = ix;
            object def = TryRead(id, "Definition", null);
            if (def != null)
            {
                string nm = SafeText(TryRead(def, "Name", null));
                if (nm != null && nm.Length > 0) d["propertyName"] = nm;
            }
        }
        string src;
        string txt = PpText(pp, out src);
        if (txt != null) { d["text"] = txt; d["textSource"] = src; }
        string vis = PpVisibility(pp);
        if (vis != null) d["setAsVisible"] = vis;
        object iv = TryRead(pp, "IsVisible", null);
        if (iv != null) { try { d["isVisible"] = Convert.ToBoolean(iv); } catch { } }
        d["visible"] = PpShown(pp);
        object loc = TryRead(pp, "Location", null);
        if (loc != null) { try { d["location"] = PtDict(loc); } catch { } }
        MethodInfo bbm = MethodByShape(pp.GetType(), "GetBoundingBox", new string[] { }, false);
        if (bbm != null)
        {
            try
            {
                object bb = bbm.Invoke(pp, null);
                double[] b = null;
                if (bb is IEnumerable) foreach (object p in (IEnumerable)bb) if (p != null) b = BBoxAdd(b, p);
                if (b != null) d["boundingBox"] = MinMax(b);
            }
            catch { }
        }
        object h = TryRead(pp, "Height", null);
        if (h != null) { try { d["height"] = Math.Round(Convert.ToDouble(h), 4); } catch { } }
        object j = TryRead(pp, "Justification", null);
        if (j != null) d["justification"] = SafeText(j);
        object dk = TryRead(pp, "IsDocked", null);
        if (dk != null) { try { d["docked"] = Convert.ToBoolean(dk); } catch { } }
        object rel = TryRead(pp, "Relation", null);
        if (rel != null) d["relation"] = SafeText(rel);
        object rix = TryRead(pp, "RelationIndex", null);   // v9
        if (rix != null) { try { int r = Convert.ToInt32(rix); if (r > 0) d["relationIndex"] = r; } catch { } }
        return d;
    }

    // null = this placement carries no property placements (not a SymbolReference).
    static List<object> DumpPropPls(object sr, int max)
    {
        object arr = TryRead(sr, "PropertyPlacements", null);
        if (!(arr is IEnumerable)) return null;
        List<object> list = new List<object>();
        foreach (object pp in (IEnumerable)arr)
        {
            if (pp == null) continue;
            if (list.Count >= max) break;
            try { list.Add(DumpPropPl(pp)); }
            catch (Exception ex)
            {
                Dictionary<string, object> e = new Dictionary<string, object>();
                e["error"] = Flatten(ex);
                list.Add(e);
            }
        }
        return list;
    }

    // ---- v9: frame of a symbol text's Location ---------------------------------
    // EPLAN 2.9 (live V2): PropertyPlacement.Location of a symbol's texts
    // is RELATIVE to the symbol's insertion point (SymbolReference.Location) - the
    // device tag of a PLC box from a macro reads (-1,0). Used by
    // live_place_function_property (write) and live_move_placements (no 2nd move).
    static double[] PlXY(object pl)
    {
        object loc = pl == null ? null : TryRead(pl, "Location", null);
        if (loc == null) return null;
        try
        {
            Dictionary<string, object> d = PtDict(loc);
            double[] r = new double[2];
            r[0] = Convert.ToDouble(d["x"]); r[1] = Convert.ToDouble(d["y"]);
            return r;
        }
        catch { return null; }
    }

    static double DistToBox(double x, double y, double[] b)
    {
        double dx = x < b[0] ? b[0] - x : (x > b[2] ? x - b[2] : 0);
        double dy = y < b[1] ? b[1] - y : (y > b[3] ? y - b[3] : 0);
        return Math.Sqrt(dx * dx + dy * dy);
    }

    // true: Location l of a text of the symbol inserted at s (box = its outline on
    // the page) reads as relative - l + s lies at least as near the outline as l.
    static bool PpLooksRelative(double[] l, double[] s, double[] box)
    {
        if (l == null || s == null || box == null) return false;
        return DistToBox(l[0] + s[0], l[1] + s[1], box) <= DistToBox(l[0], l[1], box);
    }

    // The frame the symbol's existing texts use: "relative" / "page" / "" (no
    // evidence: no texts, or the insertion point so near the origin that both read alike).
    static string PpFrameOf(object sr, out int nRel, out int nPage)
    {
        nRel = 0; nPage = 0;
        double[] s = PlXY(sr);
        double[] box = BBoxOf(sr);
        object arr = TryRead(sr, "PropertyPlacements", null);
        if (s == null || box == null || !(arr is IEnumerable)) return "";
        foreach (object pp in (IEnumerable)arr)
        {
            if (pp == null) continue;
            double[] l = PlXY(pp);
            if (l == null) continue;
            double dr = DistToBox(l[0] + s[0], l[1] + s[1], box), dp = DistToBox(l[0], l[1], box);
            if (Math.Abs(dr - dp) < 1.0) continue;
            if (dr < dp) nRel++; else nPage++;
        }
        if (nRel == 0 && nPage == 0) return "";
        return nRel >= nPage ? "relative" : "page";
    }
'''

# Every script of live_placements / live_props / live_copy: the schematic helpers
# plus the shared ones above.
_HELPERS_SCHEMATIC_X = _HELPERS_SCHEMATIC + _HELPERS_LOCAL


# ---------------------------------------------------------------------------
# live_read_page(include_property_placements / include_text_content)
# ---------------------------------------------------------------------------

# v8 (inbox 2026-10-30_eplan_live_read_page_brez_prikazanih_lastnosti.md): with
# include_property_placements=True every symbol placement (Function, PLC box,
# Terminal, ...) gets "propertyPlacements" - the texts that show its properties.
# Appended to the read body only when asked, so the default read is unchanged.
_READ_PP_BODY = '''
            Dictionary<string, Dictionary<string, object>> v8byH = new Dictionary<string, Dictionary<string, object>>();
            foreach (object v8o in (List<object>)state["placements"])
            {
                Dictionary<string, object> v8d = v8o as Dictionary<string, object>;
                if (v8d == null || !v8d.ContainsKey("handle") || v8d["handle"] == null) continue;
                string v8k = v8d["handle"].ToString();
                if (!v8byH.ContainsKey(v8k)) v8byH[v8k] = v8d;
            }
            int v8ppCount = 0;
            int v8withPp = 0;
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null) continue;
                string h = Handle(pl);
                if (h == null || !v8byH.ContainsKey(h)) continue;
                if (pl.GetType().Name.IndexOf("PropertyPlacement", StringComparison.Ordinal) >= 0)
                {
                    v8byH[h]["propertyPlacement"] = DumpPropPl(pl);
                    continue;
                }
                List<object> pps = DumpPropPls(pl, V8PPMAX);
                if (pps == null) continue;
                v8byH[h]["propertyPlacements"] = pps;
                object ul = TryRead(pl, "UseLocalPropertyPlacements", null);
                if (ul != null) { try { v8byH[h]["useLocalPropertyPlacements"] = Convert.ToBoolean(ul); } catch { } }
                v8ppCount += pps.Count;
                v8withPp++;
            }
            results["propertyPlacementCount"] = v8ppCount;
            results["placementsWithPropertyPlacements"] = v8withPp;
'''


def _pp_display(out):
    """v8: "displayText" for a property placement text in EPLAN's MultiLangString form."""
    try:
        from .live_props import multilang_display
    except ImportError:  # pragma: no cover
        return
    for pl in out.get("placements") or []:
        recs = list(pl.get("propertyPlacements") or [])
        if isinstance(pl.get("propertyPlacement"), dict):
            recs.append(pl["propertyPlacement"])
        for pp in recs:
            disp, _ = multilang_display(pp.get("text"))
            if disp is not None:
                pp["displayText"] = disp


def _text_display(out):
    """v10: "text" (first language) and "byLanguage" of every textContent."""
    try:
        from .live_props import multilang_display
    except ImportError:  # pragma: no cover
        return
    for pl in out.get("placements") or []:
        rec = pl.get("textContent")
        if not isinstance(rec, dict) or not isinstance(rec.get("raw"), str):
            continue
        disp, by_lang = multilang_display(rec["raw"])
        rec["text"] = rec["raw"] if disp is None else disp
        if by_lang is not None:
            rec["byLanguage"] = by_lang


# ---------------------------------------------------------------------------
# remove many placements
# ---------------------------------------------------------------------------

def _page_file(kind, page_dict, page_name):
    """Write a page read (page_before / page_after / removed list) to a JSON file
    in the local work folder (never the code folder)."""
    from .scripted import _WORK_ROOT
    folder = os.path.join(_WORK_ROOT, kind)
    os.makedirs(folder, exist_ok=True)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(page_name or "page")).strip("_") or "page"
    path = os.path.join(folder, "%s_%s_%s.json" % (base, time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4]))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(page_dict, f, ensure_ascii=False, indent=1)
    return path


def _counts_by_type(page_dict):
    counts = {}
    for pl in (page_dict or {}).get("placements") or []:
        t = pl.get("clrType") or "?"
        counts[t] = counts.get(t, 0) + 1
    return counts


def _cs_str_array(values, what):
    if not values:
        return "new string[0]"
    return "new string[] { %s }" % ", ".join('"%s"' % cs_escape(cs_text(v, what)) for v in values)


def _str_list(value, what, maximum=2000):
    """None / "a" / ["a", "b"] -> list of non-empty unique strings (order kept)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise SchematicValueError("%s must be a list of strings, got %s." % (what, type(value).__name__))
    out = []
    for v in value:
        if not isinstance(v, str) or not v.strip():
            raise SchematicValueError("%s entries must be non-empty strings, got %r." % (what, v))
        v = v.strip()
        if v not in out:
            out.append(v)
    if len(out) > maximum:
        raise SchematicValueError("%s: at most %d entries, got %d." % (what, maximum, len(out)))
    return out


_REMOVE_MANY_BODY = '''
            object page = FindPage(project, PAGENAME);
            results["page"] = PropText(page, "Name");
            string v7mode = V7MODE;
            string[] v7handles = V7HANDLES;
            string[] v7exTags = V7EXTAGS;
            string[] v7exHandles = V7EXHANDLES;
            string[] v7types = V7TYPES;
            double[] v7rect = new double[] { V7RX1, V7RY1, V7RX2, V7RY2 };
            string v7macro = V7MACRO;
            bool v7dry = V7DRY;
            bool v7full = V7FULL;
            int v7max = V7MAX;
            double tol = 0.01;

            // Every placement of the page, by handle (stable within one session).
            List<object> all = new List<object>();
            Dictionary<string, object> byHandle = new Dictionary<string, object>();
            foreach (object pl in PagePlacements(page))
            {
                if (pl == null) continue;
                all.Add(pl);
                string h0 = Handle(pl);
                if (h0 != null && !byHandle.ContainsKey(h0)) byHandle[h0] = pl;
            }
            results["placementCountBefore"] = all.Count;
            if (V7SAVEPAGE && !v7dry) results["page_before"] = ReadPage(page, 5000, false, null);

            Dictionary<string, object> pick = new Dictionary<string, object>();
            Dictionary<string, string> why = new Dictionary<string, string>();
            List<string> order = new List<string>();
            List<string> warnings = new List<string>();
            string refused = null;
            string refusedWhy = null;

            if (v7mode == "handles")
            {
                List<string> missing = new List<string>();
                foreach (string h in v7handles)
                {
                    object pl;
                    if (byHandle.TryGetValue(h, out pl)) AddPick(pick, why, order, pl, "handle");
                    else missing.Add(h);
                }
                if (missing.Count > 0)
                    throw new Exception("Nothing removed: " + missing.Count + " handle(s) are not on page '" +
                        PropText(page, "Name") + "': " + string.Join(", ", missing.ToArray()) +
                        ". Handles are session-scoped; re-read the page (live_read_page).");
                if (v7types.Length > 0)
                    foreach (string h in order)
                        if (!TypeIn(pick[h], v7types))
                            throw new Exception("Nothing removed: handle " + h + " is a " + pick[h].GetType().Name +
                                ", not one of types [" + string.Join(", ", v7types) + "].");
            }
            else if (v7mode == "region")
            {
                foreach (object pl in all)
                {
                    if (!TypeIn(pl, v7types)) continue;
                    bool hit = V7REGIONMATCH == "location" ? PointInside(pl, v7rect, tol) : BoxInside(BBoxOf(pl), v7rect, tol);
                    if (hit) AddPick(pick, why, order, pl, "region");
                }
            }
            else
            {
                // macro_of: one device / macro instance. Anchor = the functions with
                // the device tag (box + its connection points), then their group
                // (Placement.Group, if the macro was inserted grouped), then
                // everything lying fully inside the box outline.
                string tag = v7macro;
                List<object> anchors = new List<object>();
                if (v7macro.IndexOf('/') >= 0)
                {
                    object a;
                    if (!byHandle.TryGetValue(v7macro, out a))
                        throw new Exception("Nothing removed: no placement with handle '" + v7macro + "' on page '" +
                            PropText(page, "Name") + "'. Re-read the page.");
                    anchors.Add(a);
                    tag = DtPart(SafeText(TryRead(a, "Name", null)));
                }
                if (tag != null && tag.Trim().Length > 0)
                    foreach (object pl in all)
                        if (IsFunctionType(pl) && TagHit(SafeText(TryRead(pl, "Name", null)), new string[] { tag }) && !anchors.Contains(pl))
                            anchors.Add(pl);
                if (anchors.Count == 0)
                {
                    List<string> names = new List<string>();
                    foreach (object pl in all)
                    {
                        if (!IsFunctionType(pl)) continue;
                        string n = DtPart(SafeText(TryRead(pl, "Name", null)));
                        if (n.Length > 0 && !names.Contains(n) && names.Count < 40) names.Add(n);
                    }
                    throw new Exception("Nothing removed: no function with device tag '" + v7macro + "' on page '" +
                        PropText(page, "Name") + "'. Device tags on the page: " + string.Join(" | ", names.ToArray()));
                }
                results["macroTag"] = tag;
                foreach (object a in anchors) AddPick(pick, why, order, a, "device");
                if (V7INCBOX)
                {
                    List<object> boxes = new List<object>();
                    foreach (object a in anchors) if (IsBoxPl(a)) boxes.Add(a);
                    List<object> outline = new List<object>();
                    foreach (object b in boxes)
                    {
                        double[] bb = BBoxOf(b);
                        if (bb == null) continue;
                        Dictionary<string, object> o = new Dictionary<string, object>();
                        o["handle"] = Handle(b);
                        o["min"] = new double[] { bb[0], bb[1] };
                        o["max"] = new double[] { bb[2], bb[3] };
                        outline.Add(o);
                        foreach (object pl in all) if (BoxInside(BBoxOf(pl), bb, tol)) AddPick(pick, why, order, pl, "insideBox");
                    }
                    results["boxOutline"] = outline;
                    if (boxes.Count == 0) warnings.Add("No box (PLC / *Box) among the functions of '" + tag +
                        "' - only the functions" + (V7INCGROUP ? " and their group" : "") + " are selected; graphics and texts " +
                        "of the macro are not. Add them with a region call.");
                }
                if (V7INCGROUP)
                {
                    // the group(s) of everything selected so far (box, its points, graphics inside it)
                    foreach (string h in new List<string>(order))
                    {
                        object g = IsGroupPl(pick[h]) ? pick[h] : TopGroup(pick[h]);
                        if (g == null) continue;
                        AddPick(pick, why, order, g, "group");
                        List<object> mem = new List<object>();
                        GroupMembers(g, mem, 0);
                        foreach (object m in mem) AddPick(pick, why, order, m, "group");
                    }
                }
                if (v7types.Length > 0)
                {
                    List<string> keep = new List<string>();
                    foreach (string h in order) if (TypeIn(pick[h], v7types)) keep.Add(h); else { pick.Remove(h); why.Remove(h); }
                    order = keep;
                }
            }

            // ---- exclusions: handles and device tags that must stay ----------
            Dictionary<string, object> ex = new Dictionary<string, object>();
            Dictionary<string, string> exWhy = new Dictionary<string, string>();
            List<string> exOrder = new List<string>();
            List<string> exHandlesMissing = new List<string>();
            foreach (string h in v7exHandles)
            {
                object pl;
                if (byHandle.TryGetValue(h, out pl)) AddPick(ex, exWhy, exOrder, pl, "excludeHandle");
                else exHandlesMissing.Add(h);
            }
            List<string> exFunctions = new List<string>();
            List<string> exTagsFound = new List<string>();
            foreach (object pl in all)
            {
                if (!IsFunctionType(pl)) continue;
                string n = SafeText(TryRead(pl, "Name", null));
                if (!TagHit(n, v7exTags)) continue;
                AddPick(ex, exWhy, exOrder, pl, "excludeTag");
                if (!exFunctions.Contains(n)) exFunctions.Add(n);
                foreach (string t in v7exTags) if (NameMatches(n, t) && !exTagsFound.Contains(t)) exTagsFound.Add(t);
            }
            List<string> exTagsMissing = new List<string>();
            foreach (string t in v7exTags) if (!exTagsFound.Contains(t)) exTagsMissing.Add(t);
            if (V7EXEXPAND)
            {
                List<object> seeds = new List<object>();
                foreach (string h in exOrder) seeds.Add(ex[h]);
                foreach (object s in seeds)
                {
                    object g = TopGroup(s);
                    if (g != null)
                    {
                        AddPick(ex, exWhy, exOrder, g, "excludeGroup");
                        List<object> mem = new List<object>();
                        GroupMembers(g, mem, 0);
                        foreach (object m in mem) AddPick(ex, exWhy, exOrder, m, "excludeGroup");
                    }
                    if (IsBoxPl(s))
                    {
                        double[] bb = BBoxOf(s);
                        if (bb != null) foreach (object pl in all) if (BoxInside(BBoxOf(pl), bb, tol)) AddPick(ex, exWhy, exOrder, pl, "insideExcludedBox");
                    }
                }
            }
            results["excludedFunctions"] = exFunctions;
            results["excludedCount"] = exOrder.Count;
            if (exTagsMissing.Count > 0) warnings.Add("exclude_device_tags not found on this page (nothing to protect there): " +
                string.Join(", ", exTagsMissing.ToArray()));
            if (exHandlesMissing.Count > 0) warnings.Add("exclude_handles not on this page: " + string.Join(", ", exHandlesMissing.ToArray()));

            List<string> hits = new List<string>();
            foreach (string h in order) if (ex.ContainsKey(h)) hits.Add(h);
            if (hits.Count > 0)
            {
                if (v7mode == "region")
                {
                    List<string> keep = new List<string>();
                    foreach (string h in order) if (!ex.ContainsKey(h)) keep.Add(h); else { pick.Remove(h); why.Remove(h); }
                    order = keep;
                    results["excludedFromSelection"] = hits.Count;
                }
                else
                {
                    List<string> desc = new List<string>();
                    foreach (string h in hits)
                    {
                        if (desc.Count >= 20) break;
                        string n = SafeText(TryRead(pick[h], "Name", null));
                        desc.Add(pick[h].GetType().Name + " " + h + (n == null || n.Length == 0 ? "" : " '" + n + "'") + " (" + exWhy[h] + ")");
                    }
                    refused = "EXCLUDED_HIT";
                    refusedWhy = hits.Count + " selected placement(s) belong to an excluded device / handle - nothing removed: " +
                        string.Join("; ", desc.ToArray());
                }
            }

            // ---- a group object goes only with ALL its members ----------------
            List<string> groupsKept = new List<string>();
            List<Dictionary<string, object>> groupInfo = new List<Dictionary<string, object>>();
            Dictionary<string, bool> seenGroups = new Dictionary<string, bool>();
            foreach (string h in new List<string>(order))
            {
                object g = IsGroupPl(pick[h]) ? pick[h] : TopGroup(pick[h]);
                if (g == null) continue;
                string gh = Handle(g);
                if (gh == null || seenGroups.ContainsKey(gh)) continue;
                seenGroups[gh] = true;
                List<object> mem = new List<object>();
                GroupMembers(g, mem, 0);
                int selN = 0;
                foreach (object m in mem) { string mh = Handle(m); if (mh != null && pick.ContainsKey(mh)) selN++; }
                Dictionary<string, object> gi = new Dictionary<string, object>();
                gi["handle"] = gh;
                gi["members"] = mem.Count;
                gi["selectedMembers"] = selN;
                groupInfo.Add(gi);
                if (pick.ContainsKey(gh) && selN < mem.Count)
                {
                    pick.Remove(gh); why.Remove(gh); order.Remove(gh);
                    groupsKept.Add(gh);
                }
            }
            results["groups"] = groupInfo;
            if (groupsKept.Count > 0) results["groupsKept"] = groupsKept;

            if (refused == null && order.Count > v7max)
            {
                refused = "TOO_MANY";
                refusedWhy = order.Count + " placements selected, more than max_items=" + v7max +
                    " - nothing removed. Narrow the selection or raise max_items.";
            }

            // ---- record of the selection (before anything is removed) ---------
            List<object> items = new List<object>();
            Dictionary<string, int> byType = new Dictionary<string, int>();
            Dictionary<string, int> byReason = new Dictionary<string, int>();
            double[] selBox = null;
            foreach (string h in order)
            {
                object pl = pick[h];
                items.Add(PickRecord(pl, h, why[h], v7full));
                string tn = pl.GetType().Name;
                byType[tn] = byType.ContainsKey(tn) ? byType[tn] + 1 : 1;
                byReason[why[h]] = byReason.ContainsKey(why[h]) ? byReason[why[h]] + 1 : 1;
                double[] bb = BBoxOf(pl);
                if (bb != null)
                {
                    if (selBox == null) selBox = new double[] { bb[0], bb[1], bb[2], bb[3] };
                    else { selBox[0] = Math.Min(selBox[0], bb[0]); selBox[1] = Math.Min(selBox[1], bb[1]);
                           selBox[2] = Math.Max(selBox[2], bb[2]); selBox[3] = Math.Max(selBox[3], bb[3]); }
                }
            }
            results["selectedCount"] = order.Count;
            results["countsByType"] = byType;
            results["countsByReason"] = byReason;
            results["items"] = items;
            if (selBox != null)
            {
                Dictionary<string, object> sb = new Dictionary<string, object>();
                sb["min"] = new double[] { Math.Round(selBox[0], 4), Math.Round(selBox[1], 4) };
                sb["max"] = new double[] { Math.Round(selBox[2], 4), Math.Round(selBox[3], 4) };
                results["selectionBox"] = sb;
            }
            if (refused != null) { results["refused"] = refused; results["refusedReason"] = refusedWhy; }
            if (order.Count == 0) warnings.Add("Nothing selected.");
            results["warnings"] = warnings;

            if (!v7dry && refused == null && order.Count > 0)
            {
                // Leaves first, boxes after them, group objects last.
                List<string> seq = new List<string>();
                foreach (string h in order) if (!IsBoxPl(pick[h]) && !IsGroupPl(pick[h])) seq.Add(h);
                foreach (string h in order) if (IsBoxPl(pick[h])) seq.Add(h);
                foreach (string h in order) if (IsGroupPl(pick[h])) seq.Add(h);
                List<string> removedH = new List<string>();
                List<string> goneBefore = new List<string>();
                Dictionary<string, int> routes = new Dictionary<string, int>();
                Dictionary<string, object> touchedGroups = new Dictionary<string, object>();
                foreach (string h in seq)
                {
                    object pl = pick[h];
                    if (!IsValidObj(pl)) { goneBefore.Add(h); continue; }
                    object g = IsGroupPl(pl) ? null : TryRead(pl, "Group", null);
                    string route = "Remove";
                    try
                    {
                        MethodInfo rm = RequireMethod(pl.GetType(), "Remove", new string[] { }, false);
                        Call(rm, pl, null);
                    }
                    catch (Exception ex1)
                    {
                        MethodInfo rs = g == null ? null : MethodByShape(g.GetType(), "RemoveSubPlacement", new string[] { "" }, false);
                        if (rs == null)
                        {
                            Dictionary<string, object> fa = new Dictionary<string, object>();
                            fa["handle"] = h; fa["clrType"] = pl.GetType().Name; fa["error"] = Flatten(ex1);
                            results["failedAt"] = fa;
                            break;
                        }
                        try { Call(rs, g, new object[] { pl }); route = "Group.RemoveSubPlacement"; }
                        catch (Exception ex2)
                        {
                            Dictionary<string, object> fa = new Dictionary<string, object>();
                            fa["handle"] = h; fa["clrType"] = pl.GetType().Name;
                            fa["error"] = Flatten(ex1) + " / Group.RemoveSubPlacement: " + Flatten(ex2);
                            results["failedAt"] = fa;
                            break;
                        }
                    }
                    routes[route] = routes.ContainsKey(route) ? routes[route] + 1 : 1;
                    removedH.Add(h);
                    if (g != null) { string gh = Handle(g); if (gh != null && !pick.ContainsKey(gh)) touchedGroups[gh] = g; }
                }
                // A group emptied by this call is removed too (it would stay as an invisible empty object).
                List<string> emptyGroups = new List<string>();
                foreach (KeyValuePair<string, object> kv in touchedGroups)
                {
                    if (!IsValidObj(kv.Value)) continue;
                    List<object> mem = new List<object>();
                    GroupMembers(kv.Value, mem, 0);
                    int alive = 0;
                    foreach (object m in mem) if (IsValidObj(m)) alive++;
                    if (alive > 0) continue;
                    try { Call(RequireMethod(kv.Value.GetType(), "Remove", new string[] { }, false), kv.Value, null); emptyGroups.Add(kv.Key); }
                    catch (Exception exg) { warnings.Add("Empty group " + kv.Key + " not removed: " + Flatten(exg)); }
                }
                results["removedCount"] = removedH.Count;
                results["removedRoutes"] = routes;
                if (goneBefore.Count > 0) results["goneWithOthers"] = goneBefore;
                if (emptyGroups.Count > 0) results["emptyGroupsRemoved"] = emptyGroups;
                results["partial"] = results.ContainsKey("failedAt");

                // ---- read back --------------------------------------------------
                int after = 0;
                Dictionary<string, bool> present = new Dictionary<string, bool>();
                List<string> exNamesAfter = new List<string>();
                foreach (object pl in PagePlacements(page))
                {
                    if (pl == null) continue;
                    after++;
                    string h = Handle(pl);
                    if (h != null) present[h] = true;
                    if (IsFunctionType(pl))
                    {
                        string n = SafeText(TryRead(pl, "Name", null));
                        if (n != null && exFunctions.Contains(n) && !exNamesAfter.Contains(n)) exNamesAfter.Add(n);
                    }
                }
                List<string> still = new List<string>();
                foreach (string h in removedH) if (present.ContainsKey(h)) still.Add(h);
                List<string> exLost = new List<string>();
                foreach (string n in exFunctions) if (!exNamesAfter.Contains(n)) exLost.Add(n);
                results["placementCountAfter"] = after;
                results["stillPresent"] = still;
                results["excludedStillPresent"] = exNamesAfter.Count;
                if (exLost.Count > 0) results["excludedLost"] = exLost;
                if (V7SAVEPAGE) results["page_after"] = ReadPage(page, 5000, false, null);
            }
'''


def live_remove_placements(page: str, handles: list = None, region: list = None,
                           macro_of: str = None,
                           exclude_device_tags: list = None, exclude_handles: list = None,
                           types: list = None, region_match: str = "inside",
                           include_group: bool = True, include_box_area: bool = True,
                           exclude_expand: bool = True, max_items: int = 200,
                           dry_run: bool = True, allow_real_project: bool = False,
                           save_page_files: bool = True, verbose: bool = False,
                           list_limit: int = 60,
                           timeout_seconds: float = 180.0) -> dict:
    """
    v7: remove MANY placements of one page in one call. WRITES (unless
    dry_run, the default) - scratch-only unless allow_real_project=True
    (dry_run only reads and needs no allow_real_project).

    Use it to clear an old macro (PLC box + connection points + graphics +
    texts) instead of live_remove_placement in a loop. Pass EXACTLY ONE
    selector:

      handles=[...]      these placements (all must be on the page, otherwise
                         nothing is removed).
      region=[x1, y1, x2, y2]
                         every placement lying FULLY inside the rectangle (page
                         mm; corner order free). region_match="location": the
                         placement's insertion point inside is enough.
      macro_of="500S1"   one device / macro instance: every function with that
                         device tag on the page (box + its connection points;
                         "500S1", "=AB-C+ET-1-500S1" or the box handle
                         "218/34/.../0"), + everything lying fully inside the
                         box outline (include_box_area: graphics, texts, bus
                         terminal), + the group of all of these
                         (include_group: EPLAN Placement.Group, if the macro
                         was inserted grouped).
                         Texts/graphics OUTSIDE the box are not part of it -
                         see "boxOutline" and add them with a region call.

    Safety (nothing is removed when a check fails; no rollback is needed):
      exclude_device_tags=["500K3"] / exclude_handles=[...]: these stay. With
        exclude_expand (default) also their group and everything inside an
        excluded box. region: excluded placements are left out of the
        selection ("excludedFromSelection"). handles / macro_of: a selection
        that touches an excluded placement is REFUSED as a whole
        (errorType EXCLUDED_HIT).
      max_items (default 200): more selected placements -> refused
        (errorType TOO_MANY).
      types=["PolyLine", "Text"]: handles - every handle must be one of them;
        region / macro_of - only these types are selected.
      A group object is removed only together with ALL its members.
      dry_run=True (DEFAULT): selects and reports, removes nothing. Look at
        countsByType / items, then call again with dry_run=False.

    Args:
        page: Page name, e.g. "=AB-C+ET-1/900".
        handles / region / macro_of: the selector (exactly one).
        exclude_device_tags, exclude_handles, exclude_expand: see above.
        types: optional CLR type names (see above).
        region_match: "inside" (default, bounding box fully inside) or
            "location" (insertion point inside).
        include_group, include_box_area: macro_of parts (default True).
        max_items: 1..5000, default 200.
        dry_run: default True.
        allow_real_project: must be True to write outside the scratch root.
        save_page_files: default True (not in dry_run): full page_before,
            page_after and the full list of removed placements go to JSON
            files on the EPLAN machine ("pageBeforeFile", "pageAfterFile",
            "itemsFile") - the record of what was destroyed.
        verbose: True = every item with the full placement record and the
            whole list in the answer.
        list_limit: items returned in the answer (default 60; the rest only
            in itemsFile). 0 = counts only.
        timeout_seconds: default 180.

    Returns:
        {"success", "page", "mode", "dryRun", "selectedCount", "countsByType",
         "countsByReason", "items": [{handle, clrType, name, reason}],
         "itemsTruncated", "selectionBox", "boxOutline", "groups",
         "excludedFunctions", "excludedCount", "excludedFromSelection",
         "warnings", "placementCountBefore",
         and after a real run: "removedCount", "placementCountAfter",
         "stillPresent" (must be []), "excludedStillPresent", "excludedLost",
         "partial", "failedAt", "pageBeforeFile", "pageAfterFile", "itemsFile"}.
        A refused call: success false, "errorType" EXCLUDED_HIT / TOO_MANY,
        with the same report. "partial": true means EPLAN refused one removal
        midway - "removedCount" placements are gone, "failedAt" says which
        one stopped it (no rollback; restore from the backup if needed).
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        hl = _str_list(handles, "handles")
        ext = _str_list(exclude_device_tags, "exclude_device_tags", 200)
        exh = _str_list(exclude_handles, "exclude_handles")
        tl = _str_list(types, "types", 50)
        chosen = [n for n, v in (("handles", hl), ("region", region), ("macro_of", macro_of)) if v]
        if len(chosen) != 1:
            raise SchematicValueError(
                "Pass exactly one selector - handles=[...], region=[x1, y1, x2, y2] or "
                "macro_of='500S1' (got %s)." % (", ".join(chosen) or "none"))
        rect = [0.0, 0.0, 0.0, 0.0]
        if region:
            if isinstance(region, dict):
                region = [region.get(k) for k in ("x1", "y1", "x2", "y2")]
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                raise SchematicValueError("region must be [x1, y1, x2, y2] (page mm), got %r." % (region,))
            x1, y1, x2, y2 = (float(cs_double(v, "region")) for v in region)
            rect = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
            if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
                raise SchematicValueError("region has no area: %r." % (region,))
        macro = cs_escape(cs_text(macro_of, "macro_of")) if macro_of else ""
        if region_match not in ("inside", "location"):
            raise SchematicValueError("region_match must be 'inside' or 'location', got %r." % (region_match,))
        mx = cs_int(max_items, "max_items", minimum=1, maximum=5000)
        ll = cs_int(list_limit, "list_limit", minimum=0, maximum=5000)
        # dry_run reads only - no scratch guard (as live_set_device_tags)
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _REMOVE_MANY_BODY,
            PAGENAME='"%s"' % page_cs,
            V7MODE='"%s"' % ("handles" if hl else "region" if region else "macro"),
            V7HANDLES=_cs_str_array(hl, "handles"),
            V7EXTAGS=_cs_str_array(ext, "exclude_device_tags"),
            V7EXHANDLES=_cs_str_array(exh, "exclude_handles"),
            V7TYPES=_cs_str_array(tl, "types"),
            V7RX1=cs_double(rect[0], "region"), V7RY1=cs_double(rect[1], "region"),
            V7RX2=cs_double(rect[2], "region"), V7RY2=cs_double(rect[3], "region"),
            V7MACRO='"%s"' % macro,
            V7DRY=cs_bool(dry_run),
            V7FULL=cs_bool(verbose),
            V7MAX=str(mx),
            V7SAVEPAGE=cs_bool(save_page_files),
            V7REGIONMATCH='"%s"' % region_match,
            V7INCGROUP=cs_bool(include_group),
            V7INCBOX=cs_bool(include_box_area),
            V7EXEXPAND=cs_bool(exclude_expand),
        )
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    out = _shape(_execute_script(
        _script(_cls("RemoveMany"), body, extra_helpers=_HELPERS_SCHEMATIC_X),
        timeout=timeout_seconds,
    ))
    if not out.get("success"):
        return out
    out["mode"] = "handles" if hl else "region" if region else "macro_of"
    out["dryRun"] = bool(dry_run)
    items = out.get("items") or []
    pb, pa = out.pop("page_before", None), out.pop("page_after", None)
    if save_page_files and not dry_run:
        for key, data, kind in (("pageBeforeFile", pb, "page_before"), ("pageAfterFile", pa, "page_after"),
                                ("itemsFile", {"page": out.get("page"), "items": items}, "removed_items")):
            if data is None:
                continue
            try:
                out[key] = _page_file(kind, data, out.get("page"))
            except (OSError, TypeError, ValueError) as exc:
                out[key + "Error"] = str(exc)
    if not verbose and len(items) > ll:
        out["items"] = items[:ll]
        out["itemsTruncated"] = len(items)
    if not out.get("warnings"):
        out.pop("warnings", None)
    if out.get("refused"):
        out["errorType"] = out.pop("refused")
        out["error"] = out.pop("refusedReason", "refused")
        if dry_run:
            out["wouldRefuse"] = out.pop("errorType")
            out["wouldRefuseReason"] = out.pop("error")
        else:
            out["success"] = False
            out["removedCount"] = 0
    elif not dry_run:
        if out.get("partial"):
            out["success"] = False
            out["errorType"] = "PARTIAL"
            out["error"] = ("EPLAN refused a removal midway: %s placement(s) removed, then stopped at %s. "
                            "No rollback - restore from the backup if needed."
                            % (out.get("removedCount"), (out.get("failedAt") or {}).get("handle")))
        elif out.get("stillPresent"):
            out["success"] = False
            out["errorType"] = "NOT_REMOVED"
            out["error"] = "Placement(s) still on the page after Remove: %s" % out["stillPresent"]
        elif out.get("excludedLost"):
            out["success"] = False
            out["errorType"] = "EXCLUDED_LOST"
            out["error"] = ("Excluded function(s) no longer on the page after the removal: %s - check the page "
                            "(EPLAN removed them together with a selected placement)." % out["excludedLost"])
    return out


# ---------------------------------------------------------------------------
# v8  move placements (one macro / device, a list, a rectangle)
# ---------------------------------------------------------------------------
# Coordinator 2026-10-30 (user: "module lepo razporedi po straneh"). The
# selection is the SAME C# as live_remove_placements (v7) - its part before
# "record of the selection" is reused verbatim, so both tools pick exactly the
# same placements. Moving = Placement.Location set (API 2.9, get/set) on each
# selected placement; nothing is removed or re-inserted, so handles, device
# tags, properties and connection points stay. Another page: Placement.Page
# set (API 2.9: "assigns it to a page ... removes it from its previous page";
# not for PropertyPlacement) - NOT verified on 2.9, read back and rolled back.

_SELECT_MARK = "            // ---- record of the selection"
assert _REMOVE_MANY_BODY.count(_SELECT_MARK) == 1
_SELECT_BODY = _REMOVE_MANY_BODY.split(_SELECT_MARK)[0]

_HELPERS_MOVE = r'''
    // ---- v8: live_move_placements ------------------------------------------
    static double[] Shifted(double[] b, double dx, double dy)
    {
        if (b == null) return null;
        double[] r = new double[4];
        r[0] = b[0] + dx; r[1] = b[1] + dy; r[2] = b[2] + dx; r[3] = b[3] + dy;
        return r;
    }

    static bool Overlap(double[] a, double[] b, double tol)
    {
        if (a == null || b == null) return false;
        return a[0] < b[2] - tol && b[0] < a[2] - tol && a[1] < b[3] - tol && b[1] < a[3] - tol;
    }

    static double[] LocXY(object pl)
    {
        object loc = TryRead(pl, "Location", null);
        if (loc == null) return null;
        Dictionary<string, object> d = PtDict(loc);
        double[] r = new double[2];
        r[0] = Convert.ToDouble(d["x"]); r[1] = Convert.ToDouble(d["y"]);
        return r;
    }

    static void SetLocXY(object pl, double x, double y)
    {
        PropertyInfo pi = GetWritable(pl.GetType(), "Location");
        if (pi == null) throw new Exception(pl.GetType().Name + " has no writable Location. " + MemberList(pl.GetType(), false));
        try { pi.SetValue(pl, MakePoint(pi.PropertyType, x, y), null); }
        catch (TargetInvocationException tie) { throw new Exception("Location: " + Flatten(tie.InnerException)); }
    }

    static void SetPageOf(object pl, object page)
    {
        PropertyInfo pi = GetWritable(pl.GetType(), "Page");
        if (pi == null) throw new Exception(pl.GetType().Name + " has no writable Page. " + MemberList(pl.GetType(), false));
        try { pi.SetValue(pl, page, null); }
        catch (TargetInvocationException tie) { throw new Exception("Page: " + Flatten(tie.InnerException)); }
    }

    static bool Near(double a, double b, double tol) { return Math.Abs(a - b) <= tol; }

    static Dictionary<string, object> XYd(double x, double y)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["x"] = Math.Round(x, 4);
        d["y"] = Math.Round(y, 4);
        return d;
    }
'''

_MOVE_BODY = '''
            // ---- v8: move the selection --------------------------------------
            double mvdx = V8DX;
            double mvdy = V8DY;
            List<string> mv = new List<string>();
            List<string> groupsSkipped = new List<string>();
            foreach (string h in order)
            {
                object pl = pick[h];
                if (IsGroupPl(pl)) { groupsSkipped.Add(h); continue; }      // members move one by one
                if (pl.GetType().Name.IndexOf("PropertyPlacement", StringComparison.Ordinal) >= 0) continue;  // follow their symbol
                mv.Add(h);
            }
            if (groupsSkipped.Count > 0) results["groupObjectsNotMoved"] = groupsSkipped.Count;
            if (V8ABS)
            {
                object anc = null;
                string ah = V8ANCHOR;
                if (ah.Length > 0)
                {
                    if (!pick.ContainsKey(ah)) throw new Exception("Nothing moved: anchor_handle " + ah + " is not in the selection.");
                    anc = pick[ah];
                }
                else foreach (string h in mv) if (IsBoxPl(pick[h])) { anc = pick[h]; break; }
                if (anc == null) throw new Exception("Nothing moved: to_x/to_y need a box (PLC / *Box) in the selection or anchor_handle.");
                double[] al = LocXY(anc);
                if (al == null) throw new Exception("Nothing moved: the anchor has no Location.");
                mvdx = V8TOX - al[0];
                mvdy = V8TOY - al[1];
                Dictionary<string, object> ad = new Dictionary<string, object>();
                ad["handle"] = Handle(anc);
                ad["name"] = SafeText(TryRead(anc, "Name", null));
                ad["from"] = XYd(al[0], al[1]);
                ad["to"] = XYd(V8TOX, V8TOY);
                results["anchor"] = ad;
            }
            results["dx"] = Math.Round(mvdx, 4);
            results["dy"] = Math.Round(mvdy, 4);
            object tpage = page;
            string tpName = V8TPAGE;
            bool pageChange = false;
            if (tpName.Length > 0)
            {
                tpage = FindPage(project, tpName);
                pageChange = Handle(tpage) != Handle(page);
            }
            results["targetPage"] = PropText(tpage, "Name");
            results["pageChange"] = pageChange;

            // ---- planned geometry: outside the page, overlap with other boxes -
            double[] newSel = null;
            List<object> movingBoxes = new List<object>();
            foreach (string h in mv)
            {
                double[] nb = Shifted(BBoxOf(pick[h]), mvdx, mvdy);
                if (nb == null) continue;
                if (newSel == null) { newSel = new double[4]; newSel[0] = nb[0]; newSel[1] = nb[1]; newSel[2] = nb[2]; newSel[3] = nb[3]; }
                else { newSel[0] = Math.Min(newSel[0], nb[0]); newSel[1] = Math.Min(newSel[1], nb[1]);
                       newSel[2] = Math.Max(newSel[2], nb[2]); newSel[3] = Math.Max(newSel[3], nb[3]); }
                if (IsBoxPl(pick[h])) movingBoxes.Add(pick[h]);
            }
            if (newSel != null) results["newSelectionBox"] = MinMax(newSel);
            bool outside = false;
            object psize = TryRead(tpage, "Size", null);
            if (psize != null && newSel != null)
            {
                Dictionary<string, object> ps = PtDict(psize);
                double pw = Convert.ToDouble(ps["x"]), ph = Convert.ToDouble(ps["y"]);
                results["pageSize"] = ps;
                if (pw > 1 && ph > 1)
                    outside = newSel[0] < -0.01 || newSel[1] < -0.01 || newSel[2] > pw + 0.01 || newSel[3] > ph + 0.01;
            }
            results["outsidePage"] = outside;
            List<object> overlaps = new List<object>();
            foreach (object other in PagePlacements(tpage))
            {
                if (other == null || !IsBoxPl(other)) continue;
                string oh = Handle(other);
                if (oh != null && pick.ContainsKey(oh)) continue;
                double[] ob = BBoxOf(other);
                foreach (object b in movingBoxes)
                {
                    if (!Overlap(Shifted(BBoxOf(b), mvdx, mvdy), ob, 0.01)) continue;
                    Dictionary<string, object> o = new Dictionary<string, object>();
                    o["moving"] = SafeText(TryRead(b, "Name", null));
                    o["existing"] = SafeText(TryRead(other, "Name", null));
                    o["existingHandle"] = oh;
                    overlaps.Add(o);
                    if (overlaps.Count >= 20) break;
                }
                if (overlaps.Count >= 20) break;
            }
            results["overlapsExistingBoxes"] = overlaps;
            if (refused == null && outside && !V8ALLOWOUT)
            {
                refused = "OUTSIDE_PAGE";
                refusedWhy = "The moved selection would lie outside the page - nothing moved (allow_outside_page=true to force).";
            }
            if (refused == null && overlaps.Count > 0 && !V8ALLOWOVER)
            {
                refused = "OVERLAP";
                refusedWhy = overlaps.Count + " box(es) would overlap a box already on the page - nothing moved (allow_overlap=true to force).";
            }

            // ---- record of the selection ---------------------------------------
            List<object> items = new List<object>();
            Dictionary<string, int> byType = new Dictionary<string, int>();
            Dictionary<string, int> byReason = new Dictionary<string, int>();
            foreach (string h in mv)
            {
                object pl = pick[h];
                items.Add(PickRecord(pl, h, why[h], false));
                string tn = pl.GetType().Name;
                byType[tn] = byType.ContainsKey(tn) ? byType[tn] + 1 : 1;
                byReason[why[h]] = byReason.ContainsKey(why[h]) ? byReason[why[h]] + 1 : 1;
            }
            results["selectedCount"] = mv.Count;
            results["countsByType"] = byType;
            results["countsByReason"] = byReason;
            results["items"] = items;
            if (refused != null) { results["refused"] = refused; results["refusedReason"] = refusedWhy; }
            if (mv.Count == 0) warnings.Add("Nothing selected.");
            results["warnings"] = warnings;

            if (!v7dry && refused == null && mv.Count > 0)
            {
                // before: location, box, name, the symbol's property texts
                Dictionary<string, double[]> oldLoc = new Dictionary<string, double[]>();
                Dictionary<string, double[]> oldBox = new Dictionary<string, double[]>();
                Dictionary<string, string> oldName = new Dictionary<string, string>();
                Dictionary<string, List<object[]>> oldPp = new Dictionary<string, List<object[]>>();
                int connBefore = 0;
                foreach (object pl in PagePlacements(page)) if (pl != null && pl.GetType().Name.IndexOf("Connection", StringComparison.Ordinal) >= 0) connBefore++;
                foreach (string h in mv)
                {
                    object pl = pick[h];
                    oldLoc[h] = LocXY(pl);
                    oldBox[h] = BBoxOf(pl);
                    oldName[h] = SafeText(TryRead(pl, "Name", null));
                    object arr = TryRead(pl, "PropertyPlacements", null);
                    List<object[]> pps = new List<object[]>();
                    if (arr is IEnumerable)
                        foreach (object pp in (IEnumerable)arr)
                            if (pp != null) { double[] l = LocXY(pp); if (l != null) pps.Add(new object[] { pp, l }); }
                    oldPp[h] = pps;
                }
                List<string> done = new List<string>();
                List<object[]> ppDone = new List<object[]>();
                Dictionary<string, object> failedAt = null;
                List<string> notShifted = new List<string>();
                int ppFollowed = 0, ppMoved = 0, ppOther = 0;
                int ppRelative = 0;   // v9
                double mtol = 0.05;
                foreach (string h in mv)
                {
                    object pl = pick[h];
                    string step = "Location";
                    try
                    {
                        if (oldLoc[h] == null) throw new Exception("no readable Location");
                        if (pageChange) { step = "Page"; SetPageOf(pl, tpage); }
                        step = "Location";
                        done.Add(h);
                        SetLocXY(pl, oldLoc[h][0] + mvdx, oldLoc[h][1] + mvdy);
                        // read back: the whole outline must have shifted by (dx, dy)
                        double[] nb = BBoxOf(pl);
                        double[] ob = oldBox[h];
                        double[] nl = LocXY(pl);
                        bool ok = nl != null && Near(nl[0], oldLoc[h][0] + mvdx, mtol) && Near(nl[1], oldLoc[h][1] + mvdy, mtol);
                        if (ok && nb != null && ob != null)
                            ok = Near(nb[0], ob[0] + mvdx, mtol) && Near(nb[1], ob[1] + mvdy, mtol) &&
                                 Near(nb[2], ob[2] + mvdx, mtol) && Near(nb[3], ob[3] + mvdy, mtol);
                        if (!ok) notShifted.Add(h);
                        // the symbol's property texts: followed, or moved here
                        foreach (object[] q in oldPp[h])
                        {
                            double[] was = (double[])q[1];
                            double[] now = LocXY(q[0]);
                            if (now == null) continue;
                            // v9 (inbox 2026-10-30_eplan_move_placements_dvojni_premik_napisov_skatle):
                            // a text whose Location is RELATIVE to its symbol (EPLAN 2.9: the texts of a
                            // PLC box, device tag (-1,0)) follows the symbol with the SAME Location;
                            // v8 took the unchanged value for 'did not follow' and moved it twice.
                            if (PpLooksRelative(was, oldLoc[h], ob))
                            {
                                if (Near(now[0], was[0], mtol) && Near(now[1], was[1], mtol)) { ppFollowed++; ppRelative++; }
                                else if (Near(now[0], was[0] + mvdx, mtol) && Near(now[1], was[1] + mvdy, mtol)) ppFollowed++;
                                else ppOther++;
                                continue;
                            }
                            if (Near(now[0], was[0] + mvdx, mtol) && Near(now[1], was[1] + mvdy, mtol)) { ppFollowed++; continue; }
                            if (V8MOVEPP && Near(now[0], was[0], mtol) && Near(now[1], was[1], mtol))
                            {
                                step = "property text";
                                SetLocXY(q[0], was[0] + mvdx, was[1] + mvdy);
                                ppDone.Add(q);
                                ppMoved++;
                            }
                            else ppOther++;
                        }
                    }
                    catch (Exception exm)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["handle"] = h;
                        failedAt["clrType"] = pl.GetType().Name;
                        failedAt["step"] = step;
                        failedAt["error"] = Flatten(exm);
                        break;
                    }
                }
                if (failedAt == null && notShifted.Count > 0)
                {
                    failedAt = new Dictionary<string, object>();
                    failedAt["handle"] = notShifted[0];
                    failedAt["step"] = "verify";
                    failedAt["error"] = notShifted.Count + " placement(s) did not shift by (dx, dy) after Location was set";
                }
                if (notShifted.Count > 0) results["notShiftedAsExpected"] = notShifted;
                Dictionary<string, object> ppr = new Dictionary<string, object>();
                ppr["followed"] = ppFollowed; ppr["movedExplicitly"] = ppMoved; ppr["other"] = ppOther;
                if (ppRelative > 0) ppr["relativeKept"] = ppRelative;
                results["propertyTexts"] = ppr;
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (V8ROLLBACK)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = ppDone.Count - 1; k >= 0; k--)
                        {
                            try { double[] was = (double[])ppDone[k][1]; SetLocXY(ppDone[k][0], was[0], was[1]); rb++; }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        for (int k = done.Count - 1; k >= 0; k--)
                        {
                            string h = done[k];
                            try
                            {
                                if (pageChange) SetPageOf(pick[h], page);
                                SetLocXY(pick[h], oldLoc[h][0], oldLoc[h][1]);
                                rb++;
                            }
                            catch (Exception exr) { rbErr.Add(h + ": " + Flatten(exr)); }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
                else
                {
                    // same objects, same device tags, on the target page
                    List<string> lost = new List<string>();
                    List<string> renamed = new List<string>();
                    Dictionary<string, bool> onTarget = new Dictionary<string, bool>();
                    int connAfter = 0;
                    foreach (object pl in PagePlacements(tpage))
                    {
                        if (pl == null) continue;
                        string h0 = Handle(pl);
                        if (h0 != null) onTarget[h0] = true;
                        if (!pageChange && pl.GetType().Name.IndexOf("Connection", StringComparison.Ordinal) >= 0) connAfter++;
                    }
                    foreach (string h in mv)
                    {
                        if (!IsValidObj(pick[h]) || !onTarget.ContainsKey(h)) { lost.Add(h); continue; }
                        string n = SafeText(TryRead(pick[h], "Name", null));
                        if (n != oldName[h]) renamed.Add(h + " " + oldName[h] + " -> " + n);
                    }
                    results["movedCount"] = mv.Count;
                    results["notOnTargetPage"] = lost;
                    if (renamed.Count > 0) results["renamed"] = renamed;
                    results["connectionLinesBefore"] = connBefore;
                    if (!pageChange) results["connectionLinesAfter"] = connAfter;
                }
            }
'''


def live_move_placements(page: str, handles: list = None, region: list = None,
                         macro_of: str = None, dx: float = None, dy: float = None,
                         to_x: float = None, to_y: float = None, anchor_handle: str = None,
                         target_page: str = None,
                         exclude_device_tags: list = None, exclude_handles: list = None,
                         types: list = None, region_match: str = "inside",
                         include_group: bool = True, include_box_area: bool = True,
                         exclude_expand: bool = True, move_property_texts: bool = True,
                         allow_overlap: bool = False, allow_outside_page: bool = False,
                         max_items: int = 200, dry_run: bool = True,
                         allow_real_project: bool = False, rollback_on_error: bool = True,
                         list_limit: int = 60, timeout_seconds: float = 180.0) -> dict:
    """
    v8: MOVE placements on a sheet - one macro / device (PLC box + its
    connection points + graphics/texts in the box), a list of handles or a
    rectangle - by (dx, dy) or so that the box's insertion point lands on
    (to_x, to_y). Use it to lay modules out neatly on their pages. WRITES
    (unless dry_run, the default) - scratch-only unless
    allow_real_project=True (dry_run only reads).

    NOTHING is removed or re-inserted: every placement keeps its handle,
    device tag, properties, articles and connection points
    (Placement.Location set, API 2.9). Selection exactly as
    live_remove_placements: pass ONE of handles=[...], region=[x1, y1, x2,
    y2], macro_of="510S1" (device tag or box handle), with
    exclude_device_tags / exclude_handles / types / include_group /
    include_box_area / exclude_expand / region_match as there.

    Args:
        page: the page the placements are on, e.g. "=AB-C+ET-1/902".
        dx, dy: shift in mm (either may be omitted = 0).
        to_x, to_y: instead of dx/dy - the new insertion point (Location) of
            the box of the selection (the first PLC / *Box, or anchor_handle),
            e.g. 510S1 on ET-1/902 from (124, 268) to to_x=78, to_y=268.
        target_page: move to ANOTHER page (Placement.Page set, API 2.9) -
            NOT verified on EPLAN 2.9; read back ("notOnTargetPage") and
            rolled back on error. Omit to stay on the page.
        move_property_texts: a symbol's property texts (device tag, ...) that
            did not follow the symbol are moved by the same offset (default
            True; "propertyTexts" {followed, movedExplicitly, other,
            relativeKept}). v9: a text whose Location is RELATIVE to its
            symbol's insertion point (EPLAN 2.9: the texts of a PLC box from a
            macro, device tag at (-1, 0)) follows the symbol by itself and is
            never moved again ("relativeKept"; v8 moved those twice).
        allow_overlap: default False - a moved box overlapping another box
            already on the page -> refused (errorType OVERLAP,
            "overlapsExistingBoxes").
        allow_outside_page: default False - selection outside the page
            (Page.Size) -> refused (OUTSIDE_PAGE).
        max_items: default 200 (more -> TOO_MANY). dry_run: default True.
        allow_real_project: must be True to write outside the scratch root.
        rollback_on_error: default True - on an error, or when a placement
            did not shift by exactly (dx, dy) ("notShiftedAsExpected"), every
            placement moved by this call goes back.
        list_limit: items in the answer (default 60).

    Returns:
        {"success", "mode", "dryRun", "dx", "dy", "anchor" {handle, name,
         from, to}, "targetPage", "pageChange", "selectedCount",
         "countsByType", "countsByReason", "items", "newSelectionBox",
         "pageSize", "outsidePage", "overlapsExistingBoxes", "warnings",
         and after a real run: "movedCount", "notOnTargetPage" (must be []),
         "renamed" (must be absent), "propertyTexts", "connectionLinesBefore",
         "connectionLinesAfter"}. Refused: errorType OVERLAP / OUTSIDE_PAGE /
        TOO_MANY / EXCLUDED_HIT (dry_run: "wouldRefuse"). Failed write:
        errorType ROLLED_BACK (or PARTIAL), "failedAt" {handle, step, error}.
    """
    try:
        page_cs = cs_escape(cs_text(page, "page"))
        hl = _str_list(handles, "handles")
        ext = _str_list(exclude_device_tags, "exclude_device_tags", 200)
        exh = _str_list(exclude_handles, "exclude_handles")
        tl = _str_list(types, "types", 50)
        chosen = [n for n, v in (("handles", hl), ("region", region), ("macro_of", macro_of)) if v]
        if len(chosen) != 1:
            raise SchematicValueError(
                "Pass exactly one selector - handles=[...], region=[x1, y1, x2, y2] or "
                "macro_of='510S1' (got %s)." % (", ".join(chosen) or "none"))
        rect = [0.0, 0.0, 0.0, 0.0]
        if region:
            if isinstance(region, dict):
                region = [region.get(k) for k in ("x1", "y1", "x2", "y2")]
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                raise SchematicValueError("region must be [x1, y1, x2, y2] (page mm), got %r." % (region,))
            x1, y1, x2, y2 = (float(cs_double(v, "region")) for v in region)
            rect = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
            if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
                raise SchematicValueError("region has no area: %r." % (region,))
        macro = cs_escape(cs_text(macro_of, "macro_of")) if macro_of else ""
        if region_match not in ("inside", "location"):
            raise SchematicValueError("region_match must be 'inside' or 'location', got %r." % (region_match,))
        absolute = to_x is not None or to_y is not None
        if absolute and (to_x is None or to_y is None):
            raise SchematicValueError("to_x and to_y go together (the new insertion point of the box).")
        if absolute and (dx is not None or dy is not None):
            raise SchematicValueError("Pass dx/dy OR to_x/to_y, not both.")
        if not absolute and dx is None and dy is None:
            raise SchematicValueError("Nothing to do: pass dx and/or dy, or to_x and to_y.")
        if anchor_handle and not absolute:
            raise SchematicValueError("anchor_handle is only used with to_x / to_y.")
        mx = cs_int(max_items, "max_items", minimum=1, maximum=5000)
        ll = cs_int(list_limit, "list_limit", minimum=0, maximum=5000)
        body = ("" if dry_run else _guard_prelude(allow_real_project)) + _fill(
            _SELECT_BODY + _MOVE_BODY,
            PAGENAME='"%s"' % page_cs,
            V7MODE='"%s"' % ("handles" if hl else "region" if region else "macro"),
            V7HANDLES=_cs_str_array(hl, "handles"),
            V7EXTAGS=_cs_str_array(ext, "exclude_device_tags"),
            V7EXHANDLES=_cs_str_array(exh, "exclude_handles"),
            V7TYPES=_cs_str_array(tl, "types"),
            V7RX1=cs_double(rect[0], "region"), V7RY1=cs_double(rect[1], "region"),
            V7RX2=cs_double(rect[2], "region"), V7RY2=cs_double(rect[3], "region"),
            V7MACRO='"%s"' % macro,
            V7DRY=cs_bool(dry_run),
            V7FULL="false",
            V7MAX=str(mx),
            V7SAVEPAGE="false",
            V7REGIONMATCH='"%s"' % region_match,
            V7INCGROUP=cs_bool(include_group),
            V7INCBOX=cs_bool(include_box_area),
            V7EXEXPAND=cs_bool(exclude_expand),
            V8DX=cs_double(0.0 if dx is None else dx, "dx"),
            V8DY=cs_double(0.0 if dy is None else dy, "dy"),
            V8ABS=cs_bool(absolute),
            V8TOX=cs_double(0.0 if to_x is None else to_x, "to_x"),
            V8TOY=cs_double(0.0 if to_y is None else to_y, "to_y"),
            V8ANCHOR='"%s"' % cs_escape(cs_text(anchor_handle, "anchor_handle", allow_empty=True)),
            V8TPAGE='"%s"' % cs_escape(cs_text(target_page, "target_page", allow_empty=True)),
            V8MOVEPP=cs_bool(move_property_texts),
            V8ALLOWOVER=cs_bool(allow_overlap),
            V8ALLOWOUT=cs_bool(allow_outside_page),
            V8ROLLBACK=cs_bool(rollback_on_error),
        )
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    raw = _execute_script(
        _script(_cls("MovePl"), body, extra_helpers=_HELPERS_SCHEMATIC_X + _HELPERS_MOVE),
        timeout=timeout_seconds,
    )
    out = _shape(raw)
    if not out.get("success"):
        inner = raw.get("results") if isinstance(raw, dict) else None
        if isinstance(inner, dict):
            for k in ("items", "selectedCount", "dx", "dy"):
                if k in inner and k not in out:
                    out[k] = inner[k]
        return out
    out["mode"] = "handles" if hl else "region" if region else "macro_of"
    out["dryRun"] = bool(dry_run)
    items = out.get("items") or []
    if len(items) > ll:
        out["items"] = items[:ll]
        out["itemsTruncated"] = len(items)
    if not out.get("warnings"):
        out.pop("warnings", None)
    if out.get("refused"):
        out["errorType"] = out.pop("refused")
        out["error"] = out.pop("refusedReason", "refused")
        if dry_run:
            out["wouldRefuse"] = out.pop("errorType")
            out["wouldRefuseReason"] = out.pop("error")
        else:
            out["success"] = False
            out["movedCount"] = 0
    elif not dry_run:
        if out.get("failedAt"):
            fa = out["failedAt"]
            out["success"] = False
            out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
            out["error"] = ("Moving failed at %s (step %s): %s. %s" % (
                fa.get("handle"), fa.get("step"), fa.get("error"),
                "Every placement moved by this call was put back." if rollback_on_error and not out.get("rollbackErrors")
                else "Placements moved before it stay moved - check the page."))
        elif out.get("notOnTargetPage") or out.get("renamed"):
            out["success"] = False
            out["errorType"] = "NOT_ON_TARGET"
            out["error"] = ("After the move %s placement(s) are not on the target page and %s were renamed - "
                            "check the page." % (len(out.get("notOnTargetPage") or []), len(out.get("renamed") or [])))
    return out
