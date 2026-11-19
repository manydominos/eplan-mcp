"""
v11 - copy / paste between EPLAN projects and batch editing after the copy.

User request 2026-11-06: "as many tools as possible to copy elements from one
EPLAN schematic into a new one - in EPLAN most of the work is copy, paste and
fixing the connections; as fast as possible". Inbox notes:
2026-11-05_eplan_plc_tocka_ne_poveze_s_kartico.md,
2026-11-05_eplan_plc_tocka_definicija_funkcije.md,
2026-11-04_eplan_odstrani_artikel_funkcije.md,
2026-11-04_eplan_okence_plc_zasuk_variante_simbola.md,
2026-10-29_eplan_premik_funkcije_na_drugo_stran.md and the compact-output notes.

Native tools (the same reflection scaffold as live.py / schematic.py / live_props.py;
no eplan_execute_custom_script):

    live_copy_pages              pages of a project (open or closed .elk) into a
                                 project: Page.CopyTo(Project, PagePropertyList,
                                 NumerationMode, bool) - or a page macro round
                                 trip (PageMacro.Create + Insert.PageMacro);
                                 page names by rule (keep / replace / structure /
                                 map / next free), device tags kept / adapted /
                                 renumbered, optional device-tag replace after
                                 the copy, optional backup before it
    live_copy_objects            placements of a page (handles / rectangle /
                                 device tags / whole page) onto a page of the
                                 same or another project with an offset, N copies
                                 in one call - WindowMacro.Create (temporary .ema)
                                 + Insert.WindowMacro, or Placement.CopyTo
    live_save_macro              a selection as window macro (.ema) or pages as
                                 page macro (.emp)
    live_insert_page_macro       a page macro (.emp) into the project, page names
                                 by rule (Insert.PageMacro)
    live_rename_devices          batch device-tag rename: find / replace, map,
                                 next free number (one live_set_device_tags call)
    live_set_function_definition change the function definition (Function.
                                 FunctionDefinition set) - e.g. "PLC connection
                                 point, power supply" -> "... I/O"
    live_remove_function_articles remove article references (Function.
                                 RemoveArticleReference) - card swap = remove +
                                 live_add_function_articles
    live_set_symbol_variant      another variant / symbol of a placed function
                                 (Function.SetSymbolVariant)
    live_list_functions          compact table (columns + rows) of functions
    live_check_connections       duplicates, PLC points without card channel /
                                 with another function definition, symbols
                                 without connections

API 2.9 (eplan.help, URLs in SPREMEMBE.md): Page.CopyTo(Project,
PagePropertyList, PageMacro.Enums.NumerationMode, bool) -> Page (null when the
page exists and bOverwrite is false); PageMacro.Create(string, Page[],
MultiLangString) ("all pages have to be from the same project");
Insert.PageMacro(PageMacro, Project, bool[], NumerationMode) -> StorableObject[]
("set a new name on the pages in PageMacro.Pages"); WindowMacro.Create(string,
int, Placement[], MultiLangString); Insert.WindowMacro(string, int, Page,
PointD, MoveKind) and (string, RepresentationType, int, Page, PointD, MoveKind,
NumerationMode); Placement.CopyTo(Group, bool); Function.FunctionDefinition
{get; set;}; FunctionDefinition(Project, Category, short, short);
Function.SetSymbolVariant(SymbolVariant, bool); Function.RemoveArticleReference;
ProjectManager.OpenProject(string, OpenMode ReadOnly/Standard), OpenProjects,
Project.Close(). NumerationMode (PageMacro): Ignore (device tags unchanged),
None (designations follow the page), Number, NumberWithQuestionMark,
NumberPreferPrefix; WindowMacro has no Ignore.

NOTHING here was run against a live EPLAN 2.9 (development VM without EPLAN):
every reflective lookup that can miss throws with what the type declares, every
call returns its route / signature and times ("timing"), so the first live run
documents itself.
"""

import os
import re
import time

from ._base import cs_escape
from .live import _script
from .scripted import _execute_script
from . import scripted as _scripted
from . import live_props as _lp
from . import backup as _backup
from .schematic import _HELPERS_SCHEMATIC, _HELPERS_MOVE, _fill, _shape, _cls, _err
from .live_props import (_HELPERS_PROPS, _HELPERS_ART, _HELPERS_V10, _shape_keep, _cs_lit,
                         _cs_lit_array, _cs_int_array, multilang_display)
from .fixtures import SCRATCH_ROOT
from .schematic_model import SchematicValueError, cs_bool, cs_double, cs_int, cs_text

__all__ = [
    "live_copy_pages",
    "live_copy_objects",
    "live_save_macro",
    "live_insert_page_macro",
    "live_rename_devices",
    "live_set_function_definition",
    "live_remove_function_articles",
    "live_set_symbol_variant",
    "live_list_functions",
    "live_check_connections",
]


# ---------------------------------------------------------------------------
# Python-side helpers (tested offline)
# ---------------------------------------------------------------------------

# "==FA=PLANT++INST+LOC&DOC#USER/counter" - EPLAN's page name prefixes.
_PAGE_RE = re.compile(
    r"^(?:==(?P<fa>[^=+&#/]*))?(?:=(?P<pl>[^=+&#/]*))?(?:\+\+(?P<inst>[^+&#/]*))?"
    r"(?:\+(?P<lo>[^+&#/]*))?(?:&(?P<doc>[^&#/]*))?(?:#(?P<us>[^#/]*))?/(?P<ct>[^/]+)$")
_PART_KEYS = ("fa", "pl", "inst", "lo", "doc", "us", "ct")

# numbering words -> enum member names (PageMacro / WindowMacro NumerationMode)
_PAGE_NUMBERING = {
    "keep": "Ignore", "ignore": "Ignore",
    "adapt": "None", "none": "None",
    "number": "Number",
    "number_question": "NumberWithQuestionMark",
    "number_prefix": "NumberPreferPrefix",
}
_MACRO_NUMBERING = {
    "default": "default",
    "keep": "None", "none": "None",
    "number": "Number",
    "number_question": "NumberWithQuestionMark",
    "number_prefix": "NumberPreferPrefix",
}


def split_page_name(name):
    """'=AB-C+DI/201' -> {'fa': '', 'pl': 'AB-C', ..., 'ct': '201'}; SchematicValueError if not a page name."""
    if not isinstance(name, str):
        raise SchematicValueError("page name must be a string, got %r." % (name,))
    m = _PAGE_RE.match(name.strip())
    if not m:
        raise SchematicValueError(
            "%r is not an EPLAN page name '==FA=PLANT++INST+LOC&DOC#USER/counter' "
            "(e.g. '=AB-C+DI/201')." % (name,))
    return {k: (m.group(k) or "") for k in _PART_KEYS}


def join_page_name(parts):
    p = parts
    return (("==" + p["fa"] if p["fa"] else "") + ("=" + p["pl"] if p["pl"] else "")
            + ("++" + p["inst"] if p["inst"] else "") + ("+" + p["lo"] if p["lo"] else "")
            + ("&" + p["doc"] if p["doc"] else "") + ("#" + p["us"] if p["us"] else "")
            + "/" + p["ct"])


def _structure(name):
    return name.rsplit("/", 1)[0] if "/" in name else ""


def _counter_key(ct):
    m = re.match(r"^(\d+)(.*)$", ct or "")
    return (int(m.group(1)), m.group(2).lower()) if m else (-1, (ct or "").lower())


def _used_counters(names, structure):
    used = set()
    for n in names:
        if "/" not in n or _structure(n).upper() != structure.upper():
            continue
        ct = n.rsplit("/", 1)[1]
        if ct.isdigit():
            used.add(int(ct))
    return used


def _check_rule(rule):
    if rule is None:
        return {}
    if isinstance(rule, str):
        if rule.strip().lower() in ("keep", ""):
            return {}
        if rule.strip().lower() == "next_free":
            return {"next_free": True, "replace": [], "step": 1}
        raise SchematicValueError("page_rename must be a dict or 'keep' / 'next_free', got %r." % (rule,))
    if not isinstance(rule, dict):
        raise SchematicValueError("page_rename must be a dict, got %r." % (rule,))
    allowed = {"map", "replace", "structure", "next_free", "start", "step"}
    extra = set(rule) - allowed
    if extra:
        raise SchematicValueError("page_rename: unknown key(s) %s (allowed: %s)." % (sorted(extra), ", ".join(sorted(allowed))))
    out = dict(rule)
    if out.get("map") is not None:
        if not isinstance(out["map"], dict):
            raise SchematicValueError("page_rename.map must be {source page: target page}.")
        for k, v in out["map"].items():
            cs_text(k, "page_rename.map")
            split_page_name(cs_text(v, "page_rename.map"))
    out["replace"] = _check_reps(out.get("replace"), "page_rename.replace")
    if out.get("structure") is not None:
        st = out["structure"]
        if not isinstance(st, str):
            raise SchematicValueError("page_rename.structure must be a string such as '=AB-C+DO'.")
        split_page_name(cs_text(st, "page_rename.structure") + "/1")
    if out.get("start") is not None:
        out["start"] = cs_int(out["start"], "page_rename.start", minimum=0, maximum=999999)
    out["step"] = cs_int(out.get("step", 1), "page_rename.step", minimum=1, maximum=1000)
    return out


def page_targets(src_names, rule, target_names):
    """Target page names for the source pages (rule already checked by _check_rule)."""
    rule = rule or {}
    mp = rule.get("map") or {}
    reps = rule.get("replace") or []
    struct = rule.get("structure")
    nf = bool(rule.get("next_free"))
    start = rule.get("start")
    step = rule.get("step", 1)
    used_by = {}
    last_by = {}
    out = []
    for s in src_names:
        if s in mp:
            t = mp[s]
            split_page_name(t)
            out.append(t)
            continue
        t = s
        for r in reps:
            t = t.replace(r["from"], r["to"])
        if struct is not None:
            t = struct + "/" + t.rsplit("/", 1)[1]
        if nf:
            st = _structure(t)
            key = st.upper()
            if key not in used_by:
                used_by[key] = _used_counters(target_names, st)
            used = used_by[key]
            if key in last_by:
                n = last_by[key] + step
            elif start is not None:
                n = start
            else:
                n = (max(used) + 1) if used else 1
            while n in used:
                n += 1
            used.add(n)
            last_by[key] = n
            t = st + "/" + str(n)
        split_page_name(t)
        out.append(t)
    return out


def select_pages(names, pages=None, page_range=None, page_contains=None):
    """Source pages in project order: exact list / range (same structure: by counter) / substring."""
    chosen = [n for n, v in (("pages", pages), ("page_range", page_range), ("page_contains", page_contains)) if v]
    if len(chosen) != 1:
        raise SchematicValueError("Pass exactly one of pages=[...], page_range=[from, to] or page_contains='...' (got %s)."
                                  % (", ".join(chosen) or "none"))
    if pages:
        if isinstance(pages, str):
            pages = [pages]
        miss = [p for p in pages if p not in names]
        if miss:
            near = [n for n in names if any(m.split("/")[-1] == n.split("/")[-1] for m in miss)][:10]
            raise SchematicValueError("Source page(s) not found: %s. Same counter elsewhere: %s"
                                      % (", ".join(miss), ", ".join(near) or "-"))
        if len(set(pages)) != len(pages):
            raise SchematicValueError("pages lists a page twice.")
        return list(pages)
    if page_contains:
        sel = [n for n in names if page_contains.lower() in n.lower()]
        if not sel:
            raise SchematicValueError("No source page contains %r." % (page_contains,))
        return sel
    if isinstance(page_range, dict):
        page_range = [page_range.get("from"), page_range.get("to")]
    if not isinstance(page_range, (list, tuple)) or len(page_range) != 2:
        raise SchematicValueError("page_range must be [from page, to page].")
    a, b = page_range
    for p in (a, b):
        if p not in names:
            raise SchematicValueError("page_range: page %r not found in the source project." % (p,))
    if _structure(a).upper() == _structure(b).upper():
        st = _structure(a).upper()
        lo, hi = sorted([_counter_key(a.rsplit("/", 1)[1]), _counter_key(b.rsplit("/", 1)[1])])
        sel = [n for n in names if _structure(n).upper() == st and lo <= _counter_key(n.rsplit("/", 1)[1]) <= hi]
        return sorted(sel, key=lambda n: _counter_key(n.rsplit("/", 1)[1]))
    i, j = sorted([names.index(a), names.index(b)])
    return names[i:j + 1]


def _str_list(v, what, max_n=5000):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        raise SchematicValueError("%s must be a list of strings." % what)
    out = []
    for x in v:
        if not isinstance(x, str) or not x.strip():
            raise SchematicValueError("%s entries must be non-empty strings, got %r." % (what, x))
        out.append(cs_text(x, what).strip())
    if len(out) > max_n:
        raise SchematicValueError("%s: at most %d entries." % (what, max_n))
    return out


def _project_path(p, what):
    if p is None or p == "":
        return ""
    p = cs_text(p, what).strip()
    if not p.lower().endswith(".elk"):
        raise SchematicValueError("%s must be the project's .elk file, got %r." % (what, p))
    if not os.path.isabs(p):
        raise SchematicValueError("%s must be an absolute path, got %r." % (what, p))
    if not os.path.isfile(p):
        raise SchematicValueError("%s does not exist on the EPLAN machine: %r." % (what, p))
    return p


def _num(value, table, what):
    key = str(value if value is not None else "").strip().lower()
    if key not in table:
        raise SchematicValueError("%s must be one of %s, got %r." % (what, ", ".join(sorted(table)), value))
    return table[key]


def _rect(region):
    if isinstance(region, dict):
        region = [region.get(k) for k in ("x1", "y1", "x2", "y2")]
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise SchematicValueError("region must be [x1, y1, x2, y2] (page mm), got %r." % (region,))
    x1, y1, x2, y2 = (float(cs_double(v, "region")) for v in region)
    r = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
    if r[2] - r[0] <= 0 or r[3] - r[1] <= 0:
        raise SchematicValueError("region has no area: %r." % (region,))
    return r


def _timed(script, timeout):
    t0 = time.perf_counter()
    raw = _execute_script(script, timeout=timeout)
    return raw, int((time.perf_counter() - t0) * 1000)


def _replace_all(text, reps):
    for r in reps:
        text = text.replace(r["from"], r["to"])
    return text


def _check_reps(reps, what):
    if reps is None:
        return []
    if isinstance(reps, dict):
        reps = [reps]
    if not isinstance(reps, (list, tuple)):
        raise SchematicValueError("%s must be a list of {\"from\", \"to\"}." % what)
    for r in reps:
        if not isinstance(r, dict) or not isinstance(r.get("from"), str) or not isinstance(r.get("to"), str) or not r["from"]:
            raise SchematicValueError("%s entries must be {\"from\": \"...\", \"to\": \"...\"} (from not empty)." % what)
        cs_text(r["from"], what)
        cs_text(r["to"], what, allow_empty=True)
    return list(reps)


def _limit(out, key, n):
    v = out.get(key)
    if isinstance(v, list) and len(v) > n:
        out[key] = v[:n]
        out[key + "Truncated"] = len(v)

# ---------------------------------------------------------------------------
# C# helpers (spliced ahead of [Start])
# ---------------------------------------------------------------------------

_HELPERS_V11 = r"""
    // ==== v11 (live_copy.py) ==================================================
    static string V11Norm(string s)
    {
        if (s == null) return "";
        string n = s.Replace("/", "\\").Trim().TrimEnd('\\').ToUpperInvariant();
        if (n.EndsWith(".ELK") || n.EndsWith(".EDB") || n.EndsWith(".ELS") || n.EndsWith(".ELR")) n = n.Substring(0, n.Length - 4);
        return n;
    }

    static string V11ProjPath(object p)
    {
        if (p == null) return "";
        string[] names = new string[] { "ProjectLinkFilePath", "ProjectFullName", "ProjectDirectoryPath" };
        foreach (string n in names)
        {
            string s = PropText(p, n);
            if (s != null && s.Length > 0) return s;
        }
        return "";
    }

    static bool V11SameProject(object p, string path)
    {
        string want = V11Norm(path);
        if (want.Length == 0 || p == null) return false;
        string[] names = new string[] { "ProjectLinkFilePath", "ProjectFullName", "ProjectDirectoryPath" };
        foreach (string n in names)
            if (V11Norm(PropText(p, n)) == want) return true;
        return false;
    }

    static bool V11Same(object a, object b)
    {
        if (a == null || b == null) return false;
        if (object.ReferenceEquals(a, b)) return true;
        string pa = V11Norm(V11ProjPath(a));
        return pa.Length > 0 && pa == V11Norm(V11ProjPath(b));
    }

    static object V11Enum(Type t, string name)
    {
        try { return Enum.Parse(t, name, true); }
        catch { throw new Exception(t.Name + " has no member '" + name + "' (members: " + string.Join(", ", Enum.GetNames(t)) + ")."); }
    }

    // The project at path: the current one, one already open, or opened now
    // (ProjectManager.OpenProject(path, ReadOnly / Standard)); "openedByTool" says which.
    static object V11Project(object current, string path, bool readOnly, Dictionary<string, object> info)
    {
        info["openedByTool"] = false;
        if (path == null || path.Length == 0 || V11SameProject(current, path))
        {
            info["path"] = V11ProjPath(current);
            info["name"] = PropText(current, "ProjectName");
            return current;
        }
        object pm = Activator.CreateInstance(FindType("Eplan.EplApi.DataModel.ProjectManager"));
        object ops = TryRead(pm, "OpenProjects", null);
        if (ops is IEnumerable)
            foreach (object p in (IEnumerable)ops)
                if (p != null && V11SameProject(p, path))
                {
                    info["path"] = V11ProjPath(p);
                    info["name"] = PropText(p, "ProjectName");
                    info["wasOpen"] = true;
                    return p;
                }
        MethodInfo op = null;
        foreach (MethodInfo mi in pm.GetType().GetMethods(BindingFlags.Public | BindingFlags.Instance))
        {
            if (mi.Name != "OpenProject") continue;
            ParameterInfo[] ps = mi.GetParameters();
            if (ps.Length == 2 && ps[0].ParameterType == typeof(string) && ps[1].ParameterType.IsEnum) { op = mi; break; }
        }
        if (op == null)
            throw new Exception("ProjectManager has no OpenProject(String, OpenMode). " + MemberList(pm.GetType(), true));
        object mode = V11Enum(op.GetParameters()[1].ParameterType, readOnly ? "ReadOnly" : "Standard");
        System.Diagnostics.Stopwatch sw = System.Diagnostics.Stopwatch.StartNew();
        object proj = Call(op, pm, new object[] { path, mode });
        if (proj == null) throw new Exception("ProjectManager.OpenProject returned null for " + path);
        info["path"] = V11ProjPath(proj);
        info["name"] = PropText(proj, "ProjectName");
        info["openedByTool"] = true;
        info["openMode"] = readOnly ? "ReadOnly" : "Standard";
        info["openMs"] = sw.ElapsedMilliseconds;
        return proj;
    }

    static string V11Close(object p)
    {
        try
        {
            MethodInfo c = MethodByShape(p.GetType(), "Close", new string[] { }, false);
            if (c == null) return "no Project.Close()";
            Call(c, p, null);
            return "closed";
        }
        catch (Exception ex) { return "close failed: " + Flatten(ex); }
    }

    static Dictionary<string, object> V11PageIndex(object project)
    {
        Dictionary<string, object> d = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
        foreach (object p in AllPagesOf(project))
        {
            if (p == null) continue;
            string n = PropText(p, "Name");
            if (n != null && !d.ContainsKey(n)) d[n] = p;
        }
        return d;
    }

    static List<object> V11PageRows(object project)
    {
        List<object> rows = new List<object>();
        foreach (object p in AllPagesOf(project))
        {
            if (p == null) continue;
            string n = PropText(p, "Name");
            string t = PropText(p, "PageType");
            string[] r = new string[2];
            r[0] = n == null ? "" : n;
            r[1] = t == null ? "" : t;
            rows.Add(r);
        }
        return rows;
    }

    // A fresh PagePropertyList with the name parts (only the non-empty ones) and the counter as text.
    static object V11PageName(string[] parts)
    {
        Type pplType = FindType("Eplan.EplApi.DataModel.PagePropertyList");
        object ppl = Activator.CreateInstance(pplType);
        string[] keys = new string[] { "DESIGNATION_FUNCTIONALASSIGNMENT", "DESIGNATION_PLANT",
            "DESIGNATION_PLACEOFINSTALLATION", "DESIGNATION_LOCATION", "DESIGNATION_DOCTYPE", "DESIGNATION_USERDEFINED" };
        for (int i = 0; i < keys.Length; i++)
            if (parts[i].Length > 0) SetProp(ppl, keys[i], parts[i]);
        try { SetProp(ppl, "PAGE_COUNTER", parts[6]); }
        catch (Exception ex)
        {
            int n;
            if (!int.TryParse(parts[6], out n)) throw new Exception("PAGE_COUNTER '" + parts[6] + "': " + Flatten(ex));
            SetProp(ppl, "PAGE_COUNTER", n);
        }
        return ppl;
    }

    static void V11SetName(object o, string name)
    {
        PropertyInfo pi = RequireWritable(o.GetType(), "Name");
        try { pi.SetValue(o, name, null); }
        catch (TargetInvocationException tie) { throw new Exception("Name = '" + name + "': " + Flatten(tie.InnerException)); }
    }

    static void V11Remove(object o)
    {
        Call(RequireMethod(o.GetType(), "Remove", new string[] { }, false), o, null);
    }

    static double[] V11Box(List<object> pls)
    {
        double[] box = null;
        foreach (object pl in pls)
        {
            double[] bb = BBoxOf(pl);
            if (bb == null) continue;
            if (box == null) { box = new double[4]; box[0] = bb[0]; box[1] = bb[1]; box[2] = bb[2]; box[3] = bb[3]; }
            else
            {
                box[0] = Math.Min(box[0], bb[0]); box[1] = Math.Min(box[1], bb[1]);
                box[2] = Math.Max(box[2], bb[2]); box[3] = Math.Max(box[3], bb[3]);
            }
        }
        return box;
    }

    static bool V11IsPp(object o)
    {
        return o != null && o.GetType().Name.IndexOf("PropertyPlacement", StringComparison.Ordinal) >= 0;
    }

    // Selection on one page: "handles" | "region" | "devices" | "page". Group objects
    // (their members are copied), property texts (they come with their symbol) and
    // - unless incConn - logical Connection objects (EPLAN regenerates them) are left out.
    static List<object> V11Select(object page, string mode, string[] handles, string[] tags, double[] rect,
        string rmatch, string[] types, string[] exHandles, bool incBox, bool incGroup, bool incConn,
        Dictionary<string, object> info)
    {
        double tol = 0.01;
        List<object> all = new List<object>();
        Dictionary<string, object> byH = new Dictionary<string, object>();
        foreach (object pl in PagePlacements(page))
        {
            if (pl == null) continue;
            all.Add(pl);
            string h0 = Handle(pl);
            if (h0 != null && !byH.ContainsKey(h0)) byH[h0] = pl;
        }
        Dictionary<string, object> pick = new Dictionary<string, object>();
        Dictionary<string, string> why = new Dictionary<string, string>();
        List<string> order = new List<string>();
        if (mode == "handles")
        {
            List<string> missing = new List<string>();
            foreach (string h in handles)
            {
                object pl;
                if (byH.TryGetValue(h, out pl)) AddPick(pick, why, order, pl, "handle");
                else missing.Add(h);
            }
            if (missing.Count > 0)
                throw new Exception("Nothing done: " + missing.Count + " handle(s) are not on page '" + PropText(page, "Name") +
                    "': " + string.Join(", ", missing.ToArray()) + ". Handles are session-scoped; re-read the page.");
        }
        else if (mode == "region")
        {
            foreach (object pl in all)
            {
                if (!TypeIn(pl, types)) continue;
                bool hit = rmatch == "location" ? PointInside(pl, rect, tol) : BoxInside(BBoxOf(pl), rect, tol);
                if (hit) AddPick(pick, why, order, pl, "region");
            }
        }
        else if (mode == "devices")
        {
            List<string> found = new List<string>();
            foreach (object pl in all)
            {
                if (!IsFunctionType(pl)) continue;
                string n = SafeText(TryRead(pl, "Name", null));
                foreach (string t in tags)
                {
                    if (t.Length == 0 || !NameMatches(n, t)) continue;
                    AddPick(pick, why, order, pl, "device");
                    if (!found.Contains(t)) found.Add(t);
                    break;
                }
            }
            List<string> nf = new List<string>();
            foreach (string t in tags) if (!found.Contains(t)) nf.Add(t);
            if (nf.Count > 0)
            {
                List<string> names = new List<string>();
                foreach (object pl in all)
                {
                    if (!IsFunctionType(pl)) continue;
                    string n = DtPart(SafeText(TryRead(pl, "Name", null)));
                    if (n.Length > 0 && !names.Contains(n) && names.Count < 40) names.Add(n);
                }
                throw new Exception("Nothing done: device tag(s) not on page '" + PropText(page, "Name") + "': " +
                    string.Join(", ", nf.ToArray()) + ". Device tags on the page: " + string.Join(" | ", names.ToArray()));
            }
            if (incBox)
            {
                foreach (string h in new List<string>(order))
                {
                    if (!IsBoxPl(pick[h])) continue;
                    double[] bb = BBoxOf(pick[h]);
                    if (bb == null) continue;
                    foreach (object pl in all) if (BoxInside(BBoxOf(pl), bb, tol)) AddPick(pick, why, order, pl, "insideBox");
                }
            }
            if (incGroup)
            {
                foreach (string h in new List<string>(order))
                {
                    object g = IsGroupPl(pick[h]) ? pick[h] : TopGroup(pick[h]);
                    if (g == null) continue;
                    List<object> mem = new List<object>();
                    GroupMembers(g, mem, 0);
                    foreach (object m in mem) AddPick(pick, why, order, m, "group");
                }
            }
            if (types.Length > 0)
            {
                List<string> keep = new List<string>();
                foreach (string h in order) if (TypeIn(pick[h], types)) keep.Add(h); else { pick.Remove(h); why.Remove(h); }
                order = keep;
            }
        }
        else
        {
            foreach (object pl in all) if (TypeIn(pl, types)) AddPick(pick, why, order, pl, "page");
        }
        int exN = 0;
        foreach (string h in exHandles)
            if (pick.ContainsKey(h)) { pick.Remove(h); why.Remove(h); order.Remove(h); exN++; }
        List<object> sel = new List<object>();
        Dictionary<string, int> byType = new Dictionary<string, int>();
        int skG = 0, skP = 0, skC = 0;
        foreach (string h in order)
        {
            object pl = pick[h];
            string tn = pl.GetType().Name;
            if (IsGroupPl(pl)) { skG++; continue; }
            if (V11IsPp(pl)) { skP++; continue; }
            if (!incConn && tn == "Connection") { skC++; continue; }
            sel.Add(pl);
            int c;
            byType.TryGetValue(tn, out c);
            byType[tn] = c + 1;
        }
        info["selectedCount"] = sel.Count;
        info["countsByType"] = byType;
        if (exN > 0) info["excluded"] = exN;
        if (skG + skP + skC > 0)
        {
            Dictionary<string, object> sk = new Dictionary<string, object>();
            sk["groupObjects"] = skG;
            sk["propertyTexts"] = skP;
            sk["logicalConnections"] = skC;
            info["notCopiedDirectly"] = sk;
        }
        double[] box = V11Box(sel);
        if (box != null) info["selectionBox"] = MinMax(box);
        return sel;
    }

    // WindowMacro.Create(String, Int32, Placement[], MultiLangString): the selection as .ema.
    // Returns the macro's RepresentationType (null when unreadable).
    static object V11WriteWindowMacro(string path, List<object> sel, string descr, int variant)
    {
        Type wmT = FindType("Eplan.EplApi.DataModel.MasterData.WindowMacro");
        MethodInfo cr = RequireMethod(wmT, "Create", new string[] { "String", "Int32", "Placement[]", "MultiLangString" }, false);
        ParameterInfo[] ps = cr.GetParameters();
        Array arr = Array.CreateInstance(ps[2].ParameterType.GetElementType(), sel.Count);
        for (int i = 0; i < sel.Count; i++) arr.SetValue(sel[i], i);
        object mls = null;
        if (descr != null && descr.Length > 0)
        {
            mls = Activator.CreateInstance(ps[3].ParameterType);
            Call(RequireMethod(ps[3].ParameterType, "SetAsString", new string[] { "String" }, false), mls, new object[] { descr });
        }
        object wm = Activator.CreateInstance(wmT);
        object rep = null;
        try
        {
            Call(cr, wm, new object[] { path, variant, arr, mls });
            rep = TryRead(wm, "RepresentationType", null);
        }
        finally
        {
            IDisposable d = wm as IDisposable;
            if (d != null) { try { d.Dispose(); } catch { } }
        }
        if (!File.Exists(path)) throw new Exception("WindowMacro.Create wrote no file " + path);
        return rep;
    }

    // Insert.WindowMacro at an absolute point. num "default" = the 5-argument form
    // (as live_insert_window_macro); else the form with RepresentationType + NumerationMode.
    static object[] V11InsertWindowMacro(string path, object rep, object page, double x, double y, string num, out string sig)
    {
        Type insT = FindType("Eplan.EplApi.HEServices.Insert");
        MethodInfo m;
        object[] args;
        if (num == "default")
        {
            m = RequireMethod(insT, "WindowMacro", new string[] { "String", "Int32", "Page", "PointD", "MoveKind" }, false);
            ParameterInfo[] ps = m.GetParameters();
            args = new object[] { path, 0, page, MakePoint(ps[3].ParameterType, x, y), V11Enum(ps[4].ParameterType, "Absolute") };
        }
        else
        {
            m = RequireMethod(insT, "WindowMacro",
                new string[] { "String", "RepresentationType", "Int32", "Page", "PointD", "MoveKind", "NumerationMode" }, false);
            ParameterInfo[] ps = m.GetParameters();
            object rt = rep;
            if (rt == null) throw new Exception("The macro's RepresentationType is unknown - use numbering 'default'.");
            if (rt.GetType() != ps[1].ParameterType) rt = V11Enum(ps[1].ParameterType, rt.ToString());
            args = new object[] { path, rt, 0, page, MakePoint(ps[4].ParameterType, x, y),
                V11Enum(ps[5].ParameterType, "Absolute"), V11Enum(ps[6].ParameterType, num) };
        }
        sig = m.ToString();
        object ins = Activator.CreateInstance(insT);
        object ret;
        try { ret = Call(m, ins, args); }
        finally
        {
            IDisposable d = ins as IDisposable;
            if (d != null) { try { d.Dispose(); } catch { } }
        }
        List<object> outl = new List<object>();
        if (ret is IEnumerable) foreach (object o in (IEnumerable)ret) if (o != null) outl.Add(o);
        return outl.ToArray();
    }

    // The outline of the inserted objects must start at (wantX, wantY). EPLAN's insertion
    // point of a macro is not documented, so it is measured and the copy shifted when off.
    static double[] V11Align(object[] ins, double wantX, double wantY)
    {
        List<object> pls = new List<object>();
        foreach (object o in ins)
        {
            if (o == null || IsGroupPl(o) || V11IsPp(o) || o.GetType().Name == "Connection") continue;
            if (TryRead(o, "Location", null) == null) continue;
            pls.Add(o);
        }
        double[] b = V11Box(pls);
        if (b == null) return null;
        double cx = wantX - b[0], cy = wantY - b[1];
        if (Math.Abs(cx) < 0.05 && Math.Abs(cy) < 0.05) return null;
        foreach (object o in pls)
        {
            double[] l = LocXY(o);
            if (l == null) continue;
            double[] ob = BBoxOf(o);
            List<object[]> pps = new List<object[]>();
            object arr = TryRead(o, "PropertyPlacements", null);
            if (arr is IEnumerable)
                foreach (object pp in (IEnumerable)arr)
                    if (pp != null) { double[] p0 = LocXY(pp); if (p0 != null) pps.Add(new object[] { pp, p0 }); }
            SetLocXY(o, l[0] + cx, l[1] + cy);
            foreach (object[] q in pps)
            {
                double[] was = (double[])q[1];
                double[] now = LocXY(q[0]);
                if (now == null || PpLooksRelative(was, l, ob)) continue;
                if (Near(now[0], was[0], 0.05) && Near(now[1], was[1], 0.05)) SetLocXY(q[0], was[0] + cx, was[1] + cy);
            }
        }
        double[] r = new double[2];
        r[0] = cx; r[1] = cy;
        return r;
    }

    static void V11Describe(object[] ins, Dictionary<string, object> co, int max, List<string> fnHandles, List<string> dtsAll)
    {
        int n = 0, fns = 0;
        List<string> dts = new List<string>();
        foreach (object o in ins)
        {
            if (o == null) continue;
            n++;
            if (!IsFunctionType(o)) continue;
            fns++;
            string h = Handle(o);
            if (h != null && fnHandles != null) fnHandles.Add(h);
            string dt = DtPart(SafeText(TryRead(o, "Name", null)));
            if (dt.Length > 0 && !dts.Contains(dt)) dts.Add(dt);
            if (dt.Length > 0 && dtsAll != null && !dtsAll.Contains(dt)) dtsAll.Add(dt);
        }
        co["insertedCount"] = n;
        co["functions"] = fns;
        co["deviceTagCount"] = dts.Count;
        co["deviceTags"] = dts.Count > max ? dts.GetRange(0, max) : dts;
    }

    // Device tags that functions OUTSIDE the given handles / pages also carry (merge / cross references).
    static Dictionary<string, object> V11Shared(object project, List<string> dts, Dictionary<string, bool> mine, Dictionary<string, bool> minePages)
    {
        Dictionary<string, object> shared = new Dictionary<string, object>();
        if (dts.Count == 0) return shared;
        Dictionary<string, bool> want = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        foreach (string d in dts) want[d] = true;
        foreach (object fn in AllFunctions(project))
        {
            if (fn == null) continue;
            if (mine != null) { string h = Handle(fn); if (h != null && mine.ContainsKey(h)) continue; }
            if (minePages != null)
            {
                object pg = TryRead(fn, "Page", null);
                if (pg != null) { string ph = Handle(pg); if (ph != null && minePages.ContainsKey(ph)) continue; }
            }
            string dt = DtPart(SafeText(TryRead(fn, "Name", null)));
            if (dt.Length == 0 || !want.ContainsKey(dt)) continue;
            shared[dt] = shared.ContainsKey(dt) ? (int)shared[dt] + 1 : 1;
        }
        return shared;
    }

    static List<string> V11DefTexts(object fd)
    {
        List<string> list = new List<string>();
        if (fd == null) return list;
        object nm = TryRead(fd, "Name", null);
        string raw = nm == null ? "" : (nm.GetType().Name.IndexOf("MultiLangString", StringComparison.Ordinal) >= 0 ? MlsRaw(nm) : SafeText(nm));
        if (raw == null) raw = "";
        List<string[]> segs = raw.IndexOf('@') > 0 ? MlsSegs(raw) : null;
        if (segs != null) { foreach (string[] g in segs) if (g[1].Length > 0 && !list.Contains(g[1])) list.Add(g[1]); }
        else if (raw.Length > 0) list.Add(raw);
        return list;
    }

    static Dictionary<string, object> V11DefInfo(object fd)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        if (fd == null) { d["none"] = true; return d; }
        List<string> t = V11DefTexts(fd);
        d["name"] = t.Count > 0 ? t[0] : "";
        object c = TryRead(fd, "Category", null);
        if (c != null) d["category"] = SafeText(c);
        object g = TryRead(fd, "Group", null);
        if (g != null) d["group"] = SafeText(g);
        object i = TryRead(fd, "Id", null);
        if (i != null) d["id"] = SafeText(i);
        return d;
    }

    static string V11DefKey(object fd)
    {
        if (fd == null) return "";
        Dictionary<string, object> d = V11DefInfo(fd);
        return (d.ContainsKey("category") ? d["category"] : "") + "/" + (d.ContainsKey("group") ? d["group"] : "") + "/" +
            (d.ContainsKey("id") ? d["id"] : "");
    }

    static string V11NormName(string s)
    {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder();
        foreach (char ch in s.ToLowerInvariant()) if (!char.IsWhiteSpace(ch)) sb.Append(ch);
        return sb.ToString();
    }

    static object V11MakeDef(object project, string cat, int grp, int id)
    {
        Type fdT = FindType("Eplan.EplApi.DataModel.FunctionDefinition");
        foreach (ConstructorInfo ci in fdT.GetConstructors())
        {
            ParameterInfo[] ps = ci.GetParameters();
            if (ps.Length != 4 || ps[0].ParameterType.Name != "Project" || !ps[1].ParameterType.IsEnum || ps[2].ParameterType != typeof(short)) continue;
            object ce = V11Enum(ps[1].ParameterType, cat);
            try { return ci.Invoke(new object[] { project, ce, (short)grp, (short)id }); }
            catch (TargetInvocationException tie)
            { throw new Exception("FunctionDefinition(" + cat + ", " + grp + ", " + id + "): " + Flatten(tie.InnerException)); }
        }
        throw new Exception("FunctionDefinition has no (Project, Category, Int16, Int16) constructor. " + MemberList(fdT, true));
    }

    static object V11MakeVariant(object project, string lib, string sym, int nr)
    {
        Type libType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolLibrary");
        Type symType = FindType("Eplan.EplApi.DataModel.MasterData.Symbol");
        Type varType = FindType("Eplan.EplApi.DataModel.MasterData.SymbolVariant");
        ConstructorInfo libCtor = libType.GetConstructor(new Type[] { project.GetType(), typeof(string) });
        if (libCtor == null)
        {
            foreach (ConstructorInfo ci in libType.GetConstructors())
            {
                ParameterInfo[] ps = ci.GetParameters();
                if (ps.Length == 2 && ps[0].ParameterType.Name == "Project" && ps[1].ParameterType == typeof(string)) { libCtor = ci; break; }
            }
        }
        if (libCtor == null) throw new Exception("SymbolLibrary has no (Project, string) ctor. " + MemberList(libType, true));
        object l;
        try { l = libCtor.Invoke(new object[] { project, lib }); }
        catch (TargetInvocationException tie) { throw new Exception("Symbol library " + lib + ": " + Flatten(tie.InnerException)); }
        ConstructorInfo symCtor = symType.GetConstructor(new Type[] { libType, typeof(string) });
        if (symCtor == null) throw new Exception("Symbol has no (SymbolLibrary, string) ctor. " + MemberList(symType, true));
        object s;
        try { s = symCtor.Invoke(new object[] { l, sym }); }
        catch (TargetInvocationException tie) { throw new Exception("Symbol " + sym + " in " + lib + ": " + Flatten(tie.InnerException)); }
        ConstructorInfo varCtor = varType.GetConstructor(new Type[] { symType, typeof(int) });
        if (varCtor != null)
        {
            try { return varCtor.Invoke(new object[] { s, nr }); }
            catch (TargetInvocationException tie) { throw new Exception("Symbol " + sym + " has no variant " + nr + ": " + Flatten(tie.InnerException)); }
        }
        object v = Activator.CreateInstance(varType);
        Call(RequireMethod(varType, "Initialize", new string[] { symType.Name, "Int32" }, false), v, new object[] { s, nr });
        return v;
    }

    static string[] V11SvInfo(object fn)
    {
        object sv = TryRead(fn, "SymbolVariant", null);
        if (sv == null) return null;
        string[] r = new string[3];
        r[0] = SafeText(TryRead(sv, "SymbolLibraryName", null));
        r[1] = SafeText(TryRead(sv, "SymbolName", null));
        r[2] = SafeText(TryRead(sv, "VariantNr", null));
        if (r[0] == null) r[0] = "";
        if (r[1] == null) r[1] = "";
        if (r[2] == null) r[2] = "";
        return r;
    }

    static bool V11IsOverview(object page, Dictionary<string, bool> cache)
    {
        if (page == null) return false;
        string h = Handle(page);
        bool v;
        if (h != null && cache.TryGetValue(h, out v)) return v;
        string t = PropText(page, "PageType");
        v = t != null && t.IndexOf("Overview", StringComparison.OrdinalIgnoreCase) >= 0;
        if (h != null) cache[h] = v;
        return v;
    }

    static bool V11IsPlcPoint(object fn)
    {
        string tn = fn.GetType().Name;
        if (tn == "PLC" || IsBoxPl(fn)) return false;
        string cat = SafeText(TryRead(fn, "Category", null));
        if (cat != null && cat.IndexOf("PLC", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        object fd = TryRead(fn, "FunctionDefinition", null);
        if (fd != null)
        {
            string c2 = SafeText(TryRead(fd, "Category", null));
            if (c2 != null && c2.IndexOf("PLC", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        }
        return false;
    }

    static int V11Count(object o)
    {
        int n = 0;
        if (o is IEnumerable) foreach (object x in (IEnumerable)o) if (x != null) n++;
        return n;
    }

    // Functions of the scope: given pages (Page.Functions), pages whose name contains pcont, or the project.
    static List<object> V11ScopeFunctions(object project, string[] pages, string pcont, Dictionary<string, object> info)
    {
        List<object> list = new List<object>();
        if (pages.Length > 0 || pcont.Length > 0)
        {
            List<object> pgs = new List<object>();
            if (pages.Length > 0) foreach (string pn in pages) pgs.Add(FindPage(project, pn));
            else foreach (object p in AllPagesOf(project)) if (p != null && Matches(PropText(p, "Name"), pcont)) pgs.Add(p);
            info["pagesInScope"] = pgs.Count;
            foreach (object p in pgs)
            {
                object fns = TryRead(p, "Functions", null);
                if (fns is IEnumerable) foreach (object fn in (IEnumerable)fns) if (fn != null) list.Add(fn);
            }
        }
        else foreach (object fn in AllFunctions(project)) if (fn != null) list.Add(fn);
        return list;
    }
"""

_ALL_HELPERS = _HELPERS_SCHEMATIC + _HELPERS_MOVE + _HELPERS_PROPS + _HELPERS_ART + _HELPERS_V10 + _HELPERS_V11


def _guard_target(allow_real):
    """GuardScratch on the TARGET project (tproject), not on the focused one."""
    return '            GuardScratch(tproject, %s, "%s");\n' % (cs_bool(allow_real), cs_escape(SCRATCH_ROOT))


def _guard_current(allow_real):
    return '            GuardScratch(project, %s, "%s");\n' % (cs_bool(allow_real), cs_escape(SCRATCH_ROOT))


# Project resolution + closing of a source opened by the tool. Target first
# (writable), then the source (read-only unless it is the target).
_PROJ_OPEN = """
            System.Diagnostics.Stopwatch v11sw = System.Diagnostics.Stopwatch.StartNew();
            Dictionary<string, object> v11tInfo = new Dictionary<string, object>();
            Dictionary<string, object> v11sInfo = new Dictionary<string, object>();
            object tproject = V11Project(project, V11TGTPATH, false, v11tInfo);
            object sproject = V11SameProject(tproject, V11SRCPATH) ? tproject : V11Project(project, V11SRCPATH, true, v11sInfo);
            if (V11Same(sproject, tproject) && !v11sInfo.ContainsKey("path")) { v11sInfo["path"] = V11ProjPath(tproject); v11sInfo["sameAsTarget"] = true; }
            results["source"] = v11sInfo;
            results["target"] = v11tInfo;
            try
            {
"""

_PROJ_CLOSE = """
            }
            finally
            {
                string v11close = V11CLOSESRC;
                bool v11opened = v11sInfo.ContainsKey("openedByTool") && (bool)v11sInfo["openedByTool"];
                if (!V11Same(sproject, tproject) && (v11close == "yes" || (v11close == "ifopened" && v11opened)))
                    results["sourceClosed"] = V11Close(sproject);
                else if (v11opened) results["sourceLeftOpen"] = true;
                results["eplanMs"] = v11sw.ElapsedMilliseconds;
            }
"""


def _proj_fill(body, src, tgt, close):
    """Wrap a body with project resolution; src/tgt '' = current project."""
    return _PROJ_OPEN + body + _PROJ_CLOSE, dict(
        V11SRCPATH=_cs_lit(src), V11TGTPATH=_cs_lit(tgt), V11CLOSESRC='"%s"' % close)


def _timing(out, total_ms, extra=None):
    t = {"totalMs": total_ms}
    if isinstance(out.get("eplanMs"), (int, float)):
        t["eplanMs"] = out.pop("eplanMs")
    if extra:
        t.update({k: v for k, v in extra.items() if v is not None})
    out["timing"] = t
    return out

# ---------------------------------------------------------------------------
# 1. live_copy_pages
# ---------------------------------------------------------------------------

_INVENTORY_BODY = r'''
                results["sourcePages"] = V11PageRows(sproject);
                if (!V11Same(sproject, tproject)) results["targetPages"] = V11PageRows(tproject);
                else results["sameProject"] = true;
                string v11mp = V11MACRO;
                if (v11mp.Length > 0)
                {
                    Type pmT = FindType("Eplan.EplApi.DataModel.MasterData.PageMacro");
                    object pmac = Activator.CreateInstance(pmT);
                    try
                    {
                        Call(RequireMethod(pmT, "Open", new string[] { "String", "Project" }, false), pmac, new object[] { v11mp, tproject });
                        List<object> mps = new List<object>();
                        object mpages = TryRead(pmac, "Pages", null);
                        if (mpages is IEnumerable)
                            foreach (object mp in (IEnumerable)mpages)
                            {
                                if (mp == null) continue;
                                string[] r = new string[2];
                                r[0] = PropText(mp, "Name") ?? "";
                                r[1] = PropText(mp, "PageType") ?? "";
                                mps.Add(r);
                            }
                        results["macroPages"] = mps;
                    }
                    finally
                    {
                        IDisposable dm = pmac as IDisposable;
                        if (dm != null) { try { dm.Dispose(); } catch { } }
                    }
                }
'''

_COPY_PAGES_BODY = r'''
                V11GUARD
                string[] cSrc = V11SRC;
                string[] cTgt = V11TGT;
                string[] pFa = V11PFA;
                string[] pPl = V11PPL;
                string[] pIn = V11PIN;
                string[] pLo = V11PLO;
                string[] pDo = V11PDO;
                string[] pUs = V11PUS;
                string[] pCt = V11PCT;
                string numName = V11NUM;
                bool ow = V11OW;
                string route = V11ROUTE;
                bool rbk = V11RB;
                bool mergeCheck = V11MERGE;
                int listMax = V11LIST;
                string keepMacro = V11KEEP;

                Dictionary<string, object> sIdx = V11PageIndex(sproject);
                Dictionary<string, object> tIdx0 = V11PageIndex(tproject);
                List<string> miss = new List<string>();
                foreach (string s in cSrc) if (!sIdx.ContainsKey(s)) miss.Add(s);
                if (miss.Count > 0) throw new Exception("Nothing copied: source page(s) not found: " + string.Join(" | ", miss.ToArray()));
                List<string> exist = new List<string>();
                foreach (string t in cTgt) if (tIdx0.ContainsKey(t)) exist.Add(t);
                if (exist.Count > 0 && !ow)
                    throw new Exception("Nothing copied: target page(s) already exist (overwrite=false): " + string.Join(" | ", exist.ToArray()));
                if (exist.Count > 0) results["overwritten"] = exist;

                Type pageT = FindType("Eplan.EplApi.DataModel.Page");
                MethodInfo copyTo = MethodByShape(pageT, "CopyTo", new string[] { "Project", "PagePropertyList", "NumerationMode", "Boolean" }, false);
                string used = route == "auto" ? (copyTo != null ? "copyto" : "pagemacro") : route;
                results["route"] = used;
                List<object> done = new List<object>();
                List<object> newPages = new List<object>();
                List<object> created = new List<object>();
                List<string> mism = new List<string>();
                Dictionary<string, object> failedAt = null;

                if (used == "copyto")
                {
                    if (copyTo == null) throw new Exception("Page has no CopyTo(Project, PagePropertyList, NumerationMode, Boolean). " + MemberList(pageT, true));
                    results["copySignature"] = copyTo.ToString();
                    object nm = V11Enum(copyTo.GetParameters()[2].ParameterType, numName);
                    for (int i = 0; i < cSrc.Length; i++)
                    {
                        System.Diagnostics.Stopwatch psw = System.Diagnostics.Stopwatch.StartNew();
                        string step = "name";
                        try
                        {
                            object ppl = V11PageName(new string[] { pFa[i], pPl[i], pIn[i], pLo[i], pDo[i], pUs[i], pCt[i] });
                            step = "CopyTo";
                            object np = Call(copyTo, sIdx[cSrc[i]], new object[] { tproject, ppl, nm, ow });
                            if (np == null) throw new Exception("Page.CopyTo returned null (the page exists and overwrite is false)");
                            string got = PropText(np, "Name");
                            newPages.Add(np);
                            if (!exist.Contains(cTgt[i])) created.Add(np);
                            Dictionary<string, object> r = new Dictionary<string, object>();
                            r["from"] = cSrc[i];
                            r["to"] = got;
                            r["ms"] = psw.ElapsedMilliseconds;
                            if (!string.Equals(got, cTgt[i], StringComparison.OrdinalIgnoreCase)) { r["wanted"] = cTgt[i]; mism.Add(cTgt[i] + " -> " + got); }
                            done.Add(r);
                        }
                        catch (Exception ex)
                        {
                            failedAt = new Dictionary<string, object>();
                            failedAt["i"] = i;
                            failedAt["from"] = cSrc[i];
                            failedAt["to"] = cTgt[i];
                            failedAt["step"] = step;
                            failedAt["error"] = Flatten(ex);
                            break;
                        }
                    }
                }
                else
                {
                    Type pmT = FindType("Eplan.EplApi.DataModel.MasterData.PageMacro");
                    object pmac = Activator.CreateInstance(pmT);
                    MethodInfo pmCreate = RequireMethod(pmT, "Create", new string[] { "String", "Page[]", "MultiLangString" }, false);
                    Array parr = Array.CreateInstance(pmCreate.GetParameters()[1].ParameterType.GetElementType(), cSrc.Length);
                    for (int i = 0; i < cSrc.Length; i++) parr.SetValue(sIdx[cSrc[i]], i);
                    string tmp = Path.Combine(Path.GetTempPath(), "eplan_mcp_pages_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".emp");
                    string step = "PageMacro.Create";
                    System.Diagnostics.Stopwatch psw = System.Diagnostics.Stopwatch.StartNew();
                    try
                    {
                        Call(pmCreate, pmac, new object[] { tmp, parr, null });
                        results["macroCreateMs"] = psw.ElapsedMilliseconds;
                        step = "rename macro pages";
                        object mpages = RequireReadable(pmT, "Pages").GetValue(pmac, null);
                        int k = 0;
                        if (mpages is IEnumerable)
                            foreach (object mp in (IEnumerable)mpages)
                            {
                                if (mp == null) continue;
                                if (k < cTgt.Length) V11SetName(mp, cTgt[k]);
                                k++;
                            }
                        if (k != cTgt.Length) throw new Exception("the page macro holds " + k + " pages, " + cTgt.Length + " expected");
                        step = "Insert.PageMacro";
                        Type insT = FindType("Eplan.EplApi.HEServices.Insert");
                        MethodInfo ipm = RequireMethod(insT, "PageMacro", new string[] { "PageMacro", "Project", "Boolean[]", "NumerationMode" }, false);
                        results["copySignature"] = ipm.ToString();
                        object nm = V11Enum(ipm.GetParameters()[3].ParameterType, numName);
                        bool[] owa = new bool[cTgt.Length];
                        for (int i = 0; i < owa.Length; i++) owa[i] = ow;
                        object ins = Activator.CreateInstance(insT);
                        object ret;
                        try { ret = Call(ipm, ins, new object[] { pmac, tproject, owa, nm }); }
                        finally
                        {
                            IDisposable di = ins as IDisposable;
                            if (di != null) { try { di.Dispose(); } catch { } }
                        }
                        int unplaced = 0, insPages = 0;
                        if (ret is IEnumerable)
                            foreach (object o in (IEnumerable)ret)
                            {
                                if (o == null) continue;
                                if (o.GetType().Name == "Page") insPages++; else unplaced++;
                            }
                        results["macroReturnedPages"] = insPages;
                        results["unplacedFunctions"] = unplaced;
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["step"] = step;
                        failedAt["error"] = Flatten(ex);
                    }
                    finally
                    {
                        IDisposable dm = pmac as IDisposable;
                        if (dm != null) { try { dm.Dispose(); } catch { } }
                        if (keepMacro.Length > 0 && File.Exists(tmp))
                        {
                            try { File.Copy(tmp, keepMacro, true); results["macroSaved"] = keepMacro; }
                            catch (Exception exk) { results["macroSaveError"] = Flatten(exk); }
                        }
                        try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }
                    }
                    Dictionary<string, object> tIdx1 = V11PageIndex(tproject);
                    for (int i = 0; i < cTgt.Length; i++)
                    {
                        object np;
                        Dictionary<string, object> r = new Dictionary<string, object>();
                        r["from"] = cSrc[i];
                        if (tIdx1.TryGetValue(cTgt[i], out np))
                        {
                            newPages.Add(np);
                            if (!exist.Contains(cTgt[i])) created.Add(np);
                            r["to"] = PropText(np, "Name");
                        }
                        else { r["to"] = null; r["wanted"] = cTgt[i]; mism.Add(cTgt[i] + " -> (not found)"); }
                        done.Add(r);
                    }
                    results["insertMs"] = psw.ElapsedMilliseconds;
                }

                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (rbk)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = created.Count - 1; k >= 0; k--)
                        {
                            try { if (IsValidObj(created[k])) { V11Remove(created[k]); rb++; } }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        results["rolledBackPages"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
                results["pages"] = done;
                results["copiedCount"] = newPages.Count;
                if (mism.Count > 0) results["nameMismatch"] = mism;

                if (failedAt == null)
                {
                    Dictionary<string, bool> newH = new Dictionary<string, bool>();
                    List<string> dts = new List<string>();
                    int fnCount = 0;
                    foreach (object np in newPages)
                    {
                        string ph = Handle(np);
                        if (ph != null) newH[ph] = true;
                        object fns = TryRead(np, "Functions", null);
                        if (!(fns is IEnumerable)) continue;
                        foreach (object fn in (IEnumerable)fns)
                        {
                            if (fn == null) continue;
                            fnCount++;
                            string dt = DtPart(SafeText(TryRead(fn, "Name", null)));
                            if (dt.Length > 0 && !dts.Contains(dt)) dts.Add(dt);
                        }
                    }
                    results["functions"] = fnCount;
                    results["deviceTagCount"] = dts.Count;
                    results["deviceTags"] = dts.Count > listMax ? dts.GetRange(0, listMax) : dts;
                    if (mergeCheck)
                    {
                        Dictionary<string, object> sh = V11Shared(tproject, dts, null, newH);
                        results["sharedDeviceTagCount"] = sh.Count;
                        Dictionary<string, object> shl = new Dictionary<string, object>();
                        foreach (KeyValuePair<string, object> kv in sh) { if (shl.Count >= listMax) break; shl[kv.Key] = kv.Value; }
                        results["sharedDeviceTags"] = shl;
                    }
                }
'''


def _inventory(src, tgt, macro, close, timeout):
    body, subs = _proj_fill(_INVENTORY_BODY, src, tgt, close)
    subs["V11MACRO"] = _cs_lit(macro or "")
    raw, ms = _timed(_script(_cls("V11Inv"), _fill(body, **subs), extra_helpers=_ALL_HELPERS), timeout)
    out = _shape(raw)
    out["_ms"] = ms
    return out


def _is_current_target(out):
    t = out.get("target") or {}
    return not t.get("openedByTool") and not t.get("wasOpen")


def live_copy_pages(pages: list = None, page_range: list = None, page_contains: str = None,
                    source_project: str = None, target_project: str = None,
                    page_rename: dict = None, devices: str = "keep", overwrite: bool = False,
                    route: str = "auto", device_replace: list = None, allow_merge: bool = False,
                    dry_run: bool = True, allow_real_project: bool = False,
                    backup_before: bool = False, backup_dir: str = None,
                    keep_macro_path: str = None, close_source: bool = True,
                    rollback_on_error: bool = True, check_shared_tags: bool = True,
                    list_limit: int = 40, timeout_seconds: float = 900.0) -> dict:
    """
    v11: COPY PAGES (with everything on them) from a source project - the open
    one or ANOTHER .elk, also a closed one (opened read-only for the copy and
    closed again) - into the target project (the open one or target_project).
    Many pages in ONE call. WRITES (unless dry_run, the default) - the target
    must be a scratch project unless allow_real_project=True.

    Route "copyto" = Page.CopyTo(Project, PagePropertyList, NumerationMode,
    overwrite) per page (API 2.9); "pagemacro" = PageMacro.Create (temporary
    .emp) + Insert.PageMacro with the new page names; "auto" (default) =
    copyto if the method exists.

    Which pages (ONE of): pages=["=AB-C+DI/201", ...] | page_range=["=AB-C+DI/200",
    "=AB-C+DI/215"] (same structure: by counter, else project order) |
    page_contains="+DI/".

    page_rename (target names; default: same names):
        {"replace": [{"from": "+DI/", "to": "+DO/"}]}   string replace
        {"structure": "=AB-C+DO"}                         new structure, same counter
        {"map": {"=AB-C+DI/201": "=AB-C+DO/301"}}         explicit
        {"next_free": true, "start": 300, "step": 1}      next free counters in the
                                                          (new) structure of the target
        Keys combine: map wins, then replace, structure, next_free.

    devices (device tags on the copied pages; NumerationMode): "keep" (Ignore -
    unchanged: in the SAME project they merge with the originals = cross
    references kept), "adapt" (None - the structure designations follow the
    new page), "number" / "number_question" / "number_prefix" (renumbered by
    the project's numbering settings = cross references to the originals broken).
    device_replace=[{"from": "510K", "to": "610K"}]: after the copy, device tags
    on the new pages are renamed (live_rename_devices, one call; target must be
    the open project). allow_merge: let device_replace produce a tag that exists.

    Safety: every source page must exist, every target name must be free
    (unless overwrite=True - overwritten pages cannot be rolled back; use
    backup_before), target names unique - all checked before the first copy.
    dry_run=True (default) returns the plan ("plan": ["src -> target"]) and
    nothing is written. backup_before=True: eplan_backup_project of the target
    (.zw1 in backup_dir, default the project's folder) before the copy.
    rollback_on_error: pages created by this call are removed on an error.
    keep_macro_path: route pagemacro - keep the .emp there.

    Returns (compact): {"success", "dryRun", "plan" | "pages" [{from, to, ms}],
    "copiedCount", "route", "functions", "deviceTagCount", "deviceTags",
    "sharedDeviceTagCount"/"sharedDeviceTags" (tags that also exist on other
    pages of the target = merged / cross-referenced), "nameMismatch",
    "renameDevices" (device_replace result), "backup", "timing" {totalMs,
    inventoryMs, eplanMs, backupMs}, "source", "target"}. Error after a partial
    copy: errorType ROLLED_BACK / PARTIAL with "failedAt".
    """
    t0 = time.perf_counter()
    try:
        src = _project_path(source_project, "source_project")
        tgt = _project_path(target_project, "target_project")
        rule = _check_rule(page_rename)
        num = _num(devices, _PAGE_NUMBERING, "devices")
        if route not in ("auto", "copyto", "pagemacro"):
            raise SchematicValueError("route must be 'auto', 'copyto' or 'pagemacro', got %r." % (route,))
        reps = _check_reps(device_replace, "device_replace")
        ll = cs_int(list_limit, "list_limit", minimum=0, maximum=2000)
        keep = ""
        if keep_macro_path:
            keep = cs_text(keep_macro_path, "keep_macro_path").strip()
            if not keep.lower().endswith(".emp") or not os.path.isabs(keep):
                raise SchematicValueError("keep_macro_path must be an absolute .emp path.")
            if not os.path.isdir(os.path.dirname(keep)):
                raise SchematicValueError("keep_macro_path: folder does not exist: %r." % os.path.dirname(keep))
        if backup_dir is not None:
            backup_dir = cs_text(backup_dir, "backup_dir").strip()
            if not os.path.isdir(backup_dir):
                raise SchematicValueError("backup_dir does not exist: %r." % backup_dir)
        if pages is None and page_range is None and page_contains is None:
            raise SchematicValueError("Pass pages=[...], page_range=[from, to] or page_contains='...'.")
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    # ---- phase 1: page lists of source and target (one script) -------------
    inv = _inventory(src, tgt, "", "ifopened" if dry_run and close_source else "no", timeout_seconds)
    if not inv.get("success"):
        inv["error"] = "Reading the page lists failed: %s" % inv.get("error")
        inv.pop("_ms", None)
        return _timing(inv, int((time.perf_counter() - t0) * 1000))
    src_names = [r[0] for r in inv.get("sourcePages") or [] if r and r[0]]
    same = bool(inv.get("sameProject"))
    tgt_names = src_names if same else [r[0] for r in inv.get("targetPages") or [] if r and r[0]]
    opened_src = bool((inv.get("source") or {}).get("openedByTool"))
    timing_extra = {"inventoryMs": inv.get("_ms")}
    try:
        sel = select_pages(src_names, pages, page_range, page_contains)
        targets = page_targets(sel, rule, tgt_names)
    except SchematicValueError as exc:
        out = _err(exc)
        out.update({"source": inv.get("source"), "target": inv.get("target")})
        if opened_src and not dry_run:
            out["sourceLeftOpen"] = True
        return _timing(out, int((time.perf_counter() - t0) * 1000), timing_extra)
    dup = sorted({t for t in targets if targets.count(t) > 1})
    tset = {n.upper() for n in tgt_names}
    exists = [t for t in targets if t.upper() in tset]
    plan = ["%s -> %s" % (s, t) for s, t in zip(sel, targets)]
    base = {"source": inv.get("source"), "target": inv.get("target"), "pageCount": len(sel),
            "devices": devices, "numerationMode": num}
    problems = []
    if dup:
        problems.append("target page names repeat: %s" % ", ".join(dup))
    if exists and not overwrite:
        problems.append("%d target page(s) already exist (overwrite=false): %s" % (len(exists), ", ".join(exists[:15])))
    if dry_run or problems:
        out = dict(base, success=not problems, dryRun=bool(dry_run), plan=plan)
        if exists:
            out["existingTargets"] = exists
        if problems:
            out["error"] = ("Nothing copied: " + "; ".join(problems) +
                            ". Use page_rename (replace / structure / next_free) or overwrite=true.")
            out["errorType"] = "TARGET_EXISTS" if exists and not dup else "BAD_NAMES"
        if opened_src and not dry_run:
            out["sourceLeftOpen"] = True
        _limit(out, "plan", max(ll, 1))
        return _timing(out, int((time.perf_counter() - t0) * 1000), timing_extra)

    # ---- backup (optional) ------------------------------------------------
    backup_info = None
    if backup_before:
        tpath = (inv.get("target") or {}).get("path") or ""
        if not tpath.lower().endswith(".elk"):
            cand = re.sub(r"\.edb$", "", tpath, flags=re.I) + ".elk"
            tpath = cand if os.path.isfile(cand) else tpath
        if not tpath.lower().endswith(".elk"):
            return _timing({"success": False, "errorType": "BACKUP_FAILED", "nothingCopied": True,
                            "error": "Backup requested, but the target's .elk path is unknown (%r) - nothing copied." % tpath},
                           int((time.perf_counter() - t0) * 1000), timing_extra)
        bdir = backup_dir or os.path.dirname(tpath)
        arch = "%s_pred_kopijo_%s.zw1" % (os.path.splitext(os.path.basename(tpath))[0], time.strftime("%Y%m%d_%H%M%S"))
        tb = time.perf_counter()
        b = _backup.backup_project(destination_path=bdir, archive_name=arch, project_name=tpath, backup_method="BACKUP")
        timing_extra["backupMs"] = int((time.perf_counter() - tb) * 1000)
        backup_info = {"archive": os.path.join(bdir, arch), "success": bool(isinstance(b, dict) and b.get("success"))}
        if not backup_info["success"]:
            backup_info["error"] = (b or {}).get("error") or (b or {}).get("message") if isinstance(b, dict) else str(b)
            return _timing({"success": False, "errorType": "BACKUP_FAILED", "nothingCopied": True, "backup": backup_info,
                            "error": "Backup before the copy failed - nothing copied."},
                           int((time.perf_counter() - t0) * 1000), timing_extra)

    # ---- phase 2: copy ------------------------------------------------------
    parts = [split_page_name(t) for t in targets]
    body, subs = _proj_fill(_COPY_PAGES_BODY, src, tgt, "yes" if (opened_src and close_source) else "no")
    subs.update(
        V11GUARD=_guard_target(allow_real_project),
        V11SRC=_cs_lit_array(sel), V11TGT=_cs_lit_array(targets),
        V11PFA=_cs_lit_array([p["fa"] for p in parts]), V11PPL=_cs_lit_array([p["pl"] for p in parts]),
        V11PIN=_cs_lit_array([p["inst"] for p in parts]), V11PLO=_cs_lit_array([p["lo"] for p in parts]),
        V11PDO=_cs_lit_array([p["doc"] for p in parts]), V11PUS=_cs_lit_array([p["us"] for p in parts]),
        V11PCT=_cs_lit_array([p["ct"] for p in parts]),
        V11NUM='"%s"' % num, V11OW=cs_bool(overwrite), V11ROUTE='"%s"' % route,
        V11RB=cs_bool(rollback_on_error), V11MERGE=cs_bool(check_shared_tags), V11LIST=str(max(ll, 1)),
        V11KEEP=_cs_lit(keep))
    raw, ms = _timed(_script(_cls("V11CopyPages"), _fill(body, **subs), extra_helpers=_ALL_HELPERS), timeout_seconds)
    out = _shape_keep(raw, keep=("pages", "failedAt", "rolledBackPages", "rollbackErrors", "copiedCount",
                                 "route", "source", "target", "eplanMs", "sourceClosed"))
    for k, v in base.items():
        out.setdefault(k, v)
    out["dryRun"] = False
    if backup_info:
        out["backup"] = backup_info
    timing_extra["copyMs"] = ms
    if out.get("failedAt"):
        fa = out["failedAt"]
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error and not out.get("rollbackErrors") else "PARTIAL"
        out["error"] = "Copy failed at %s (step %s): %s. %s" % (
            fa.get("from") or "the page macro", fa.get("step"), fa.get("error"),
            "Pages created by this call were removed." if out["errorType"] == "ROLLED_BACK"
            else "Pages copied before it stay - check the target.")
    if out.get("success") and out.get("nameMismatch"):
        out.setdefault("warnings", []).append(
            "%d page(s) got another name than planned (project structure settings?) - see nameMismatch."
            % len(out["nameMismatch"]))
    if out.get("success") and out.get("sharedDeviceTagCount"):
        out.setdefault("warnings", []).append(
            "%d device tag(s) of the copied pages also exist on other pages of the target - those devices are "
            "merged / cross-referenced with the originals (devices='number' or device_replace separates them)."
            % out["sharedDeviceTagCount"])
    # ---- device tag replace on the new pages --------------------------------
    if out.get("success") and reps:
        if not _is_current_target(out):
            out.setdefault("warnings", []).append("device_replace skipped: the target is not the open project - "
                                                  "open it and call live_rename_devices(pages=..., replace=...).")
        else:
            newp = [p.get("to") for p in out.get("pages") or [] if p.get("to")]
            tr = time.perf_counter()
            rn = live_rename_devices(pages=newp, replace=reps, dry_run=False, allow_merge=allow_merge,
                                     allow_real_project=allow_real_project, timeout_seconds=timeout_seconds)
            timing_extra["renameMs"] = int((time.perf_counter() - tr) * 1000)
            out["renameDevices"] = rn
            if not rn.get("success"):
                out["success"] = False
                out["errorType"] = "RENAME_FAILED"
                out["error"] = "Pages copied, device_replace failed: %s" % rn.get("error")
    _limit(out, "pages", max(ll, 1))
    return _timing(out, int((time.perf_counter() - t0) * 1000), timing_extra)

# ---------------------------------------------------------------------------
# 2. live_copy_objects / live_save_macro
# ---------------------------------------------------------------------------

_SELECT_PART = r'''
                object spage = FindPage(sproject, V11PAGE);
                results["page"] = PropText(spage, "Name");
                Dictionary<string, object> selInfo = new Dictionary<string, object>();
                List<object> sel = V11Select(spage, V11MODE, V11HANDLES, V11TAGS,
                    new double[] { V11RX1, V11RY1, V11RX2, V11RY2 }, V11RMATCH, V11TYPES, V11EXH,
                    V11INCBOX, V11INCGROUP, V11INCCONN, selInfo);
                foreach (KeyValuePair<string, object> kv in selInfo) results[kv.Key] = kv.Value;
                if (sel.Count == 0) throw new Exception("Nothing selected on page '" + PropText(spage, "Name") + "' - nothing done.");
                if (sel.Count > V11MAX) throw new Exception("TOO_MANY: " + sel.Count + " placements selected, more than max_items=" + V11MAX + " - nothing done.");
                double[] sbox = V11Box(sel);
                if (sbox == null) throw new Exception("The selection has no geometry (no bounding box / location).");
'''

_COPY_OBJECTS_BODY = _SELECT_PART + r'''
                string tpn = V11TPAGE;
                object tpage = tpn.Length > 0 ? FindPage(tproject, tpn) : (V11Same(sproject, tproject) ? spage : FindPage(tproject, PropText(spage, "Name")));
                results["targetPage"] = PropText(tpage, "Name");
                double bdx = V11DX, bdy = V11DY;
                if (V11ABS) { bdx = V11TOX - sbox[0]; bdy = V11TOY - sbox[1]; }
                double sdx = V11SDX, sdy = V11SDY;
                int copies = V11COPIES;
                int listMax = V11LIST;
                double pw = 0, ph = 0;
                object psize = TryRead(tpage, "Size", null);
                if (psize != null) { Dictionary<string, object> ps0 = PtDict(psize); pw = Convert.ToDouble(ps0["x"]); ph = Convert.ToDouble(ps0["y"]); results["pageSize"] = ps0; }
                List<object> selBoxes = new List<object>();
                foreach (object pl in sel) if (IsBoxPl(pl)) selBoxes.Add(pl);
                List<double[]> tBoxes = new List<double[]>();
                List<string> tBoxNames = new List<string>();
                foreach (object pl in PagePlacements(tpage))
                {
                    if (pl == null || !IsBoxPl(pl)) continue;
                    double[] bb = BBoxOf(pl);
                    if (bb == null) continue;
                    tBoxes.Add(bb);
                    tBoxNames.Add(SafeText(TryRead(pl, "Name", null)) ?? "");
                }
                List<object> plan = new List<object>();
                List<double[]> planned = new List<double[]>();
                List<string> overl = new List<string>();
                bool outside = false;
                for (int k = 0; k < copies; k++)
                {
                    double ox = bdx + k * sdx, oy = bdy + k * sdy;
                    double[] nb = Shifted(sbox, ox, oy);
                    Dictionary<string, object> pd = new Dictionary<string, object>();
                    pd["copy"] = k + 1;
                    pd["dx"] = Math.Round(ox, 4);
                    pd["dy"] = Math.Round(oy, 4);
                    pd["box"] = MinMax(nb);
                    if (pw > 1 && ph > 1 && (nb[0] < -0.01 || nb[1] < -0.01 || nb[2] > pw + 0.01 || nb[3] > ph + 0.01)) { outside = true; pd["outsidePage"] = true; }
                    List<double[]> mine = new List<double[]>();
                    foreach (object b in selBoxes)
                    {
                        double[] mb = Shifted(BBoxOf(b), ox, oy);
                        if (mb == null) continue;
                        mine.Add(mb);
                        for (int e = 0; e < tBoxes.Count; e++)
                            if (Overlap(mb, tBoxes[e], 0.01)) { overl.Add("copy " + (k + 1) + ": " + (SafeText(TryRead(b, "Name", null)) ?? "box") + " on " + tBoxNames[e]); break; }
                        foreach (double[] pb in planned)
                            if (Overlap(mb, pb, 0.01)) { overl.Add("copy " + (k + 1) + ": overlaps an earlier copy"); break; }
                    }
                    planned.AddRange(mine);
                    plan.Add(pd);
                }
                results["plan"] = plan;
                results["outsidePage"] = outside;
                if (overl.Count > 0) results["overlapsBoxes"] = overl.Count > 20 ? overl.GetRange(0, 20) : overl;
                string refused = null, refusedWhy = null;
                if (outside && !V11ALLOWOUT) { refused = "OUTSIDE_PAGE"; refusedWhy = "A copy would lie outside the page - nothing copied (allow_outside_page=true to force)."; }
                if (refused == null && overl.Count > 0 && !V11ALLOWOVER) { refused = "OVERLAP"; refusedWhy = overl.Count + " copied box(es) would overlap a box (EPLAN nests device tags inside a box) - nothing copied (allow_overlap=true to force)."; }
                if (refused != null) { results["refused"] = refused; results["refusedReason"] = refusedWhy; }

                if (!V11DRY && refused == null)
                {
                    V11GUARD
                    string route = V11ROUTE;
                    string num = V11NUM;
                    bool rbk = V11RB;
                    List<object> insertedAll = new List<object>();
                    List<object> copiesOut = new List<object>();
                    List<object> handlesPerCopy = new List<object>();
                    List<string> dtsAll = new List<string>();
                    Dictionary<string, object> failedAt = null;
                    string tmp = null;
                    object rep = null;
                    try
                    {
                        if (route == "macro")
                        {
                            System.Diagnostics.Stopwatch msw = System.Diagnostics.Stopwatch.StartNew();
                            tmp = Path.Combine(Path.GetTempPath(), "eplan_mcp_copy_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".ema");
                            rep = V11WriteWindowMacro(tmp, sel, null, 0);
                            results["macroMs"] = msw.ElapsedMilliseconds;
                            if (rep != null) results["representationType"] = rep.ToString();
                        }
                        for (int k = 0; k < copies; k++)
                        {
                            double ox = bdx + k * sdx, oy = bdy + k * sdy;
                            System.Diagnostics.Stopwatch csw = System.Diagnostics.Stopwatch.StartNew();
                            Dictionary<string, object> co = new Dictionary<string, object>();
                            co["copy"] = k + 1;
                            string step = route == "macro" ? "Insert.WindowMacro" : "Placement.CopyTo";
                            try
                            {
                                object[] made;
                                if (route == "macro")
                                {
                                    string sig;
                                    made = V11InsertWindowMacro(tmp, rep, tpage, sbox[0] + ox, sbox[1] + oy, num, out sig);
                                    results["insertSignature"] = sig;
                                    foreach (object o in made) insertedAll.Add(o);
                                }
                                else
                                {
                                    List<object> mk = new List<object>();
                                    foreach (object pl in sel)
                                    {
                                        MethodInfo cp = MethodByShape(pl.GetType(), "CopyTo", new string[] { "Group", "Boolean" }, false);
                                        if (cp == null) throw new Exception(pl.GetType().Name + " has no CopyTo(Group, Boolean). " + MemberList(pl.GetType(), true));
                                        object c = Call(cp, pl, new object[] { tpage, true });
                                        if (c != null) { mk.Add(c); insertedAll.Add(c); }
                                    }
                                    made = mk.ToArray();
                                }
                                if (made.Length == 0) throw new Exception("nothing was inserted (macro without a representation for this page type?)");
                                step = "position";
                                double[] corr = V11Align(made, sbox[0] + ox, sbox[1] + oy);
                                if (corr != null) co["correction"] = XYd(corr[0], corr[1]);
                                List<string> fh = new List<string>();
                                V11Describe(made, co, listMax, fh, dtsAll);
                                handlesPerCopy.Add(fh);
                                co["ms"] = csw.ElapsedMilliseconds;
                                copiesOut.Add(co);
                            }
                            catch (Exception ex)
                            {
                                failedAt = new Dictionary<string, object>();
                                failedAt["copy"] = k + 1;
                                failedAt["step"] = step;
                                failedAt["error"] = Flatten(ex);
                                break;
                            }
                        }
                    }
                    catch (Exception ex0)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["step"] = "WindowMacro.Create";
                        failedAt["error"] = Flatten(ex0);
                    }
                    finally
                    {
                        string keepM = V11KEEP;
                        if (tmp != null && keepM.Length > 0 && File.Exists(tmp))
                        {
                            try { File.Copy(tmp, keepM, true); results["macroSaved"] = keepM; }
                            catch (Exception exk) { results["macroSaveError"] = Flatten(exk); }
                        }
                        if (tmp != null) { try { if (File.Exists(tmp)) File.Delete(tmp); } catch { } }
                    }
                    results["copies"] = copiesOut;
                    results["functionHandles"] = handlesPerCopy;
                    results["insertedTotal"] = insertedAll.Count;
                    if (failedAt != null)
                    {
                        results["failedAt"] = failedAt;
                        if (rbk)
                        {
                            int rb = 0;
                            List<string> rbErr = new List<string>();
                            for (int k = insertedAll.Count - 1; k >= 0; k--)
                            {
                                object o = insertedAll[k];
                                try { if (IsValidObj(o) && !V11IsPp(o)) { V11Remove(o); rb++; } }
                                catch (Exception exr) { if (rbErr.Count < 10) rbErr.Add(Flatten(exr)); }
                            }
                            results["rolledBack"] = rb;
                            if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                        }
                    }
                    else if (V11SHARED)
                    {
                        Dictionary<string, bool> mineH = new Dictionary<string, bool>();
                        foreach (object o in insertedAll) { string h = Handle(o); if (h != null) mineH[h] = true; }
                        Dictionary<string, object> sh = V11Shared(tproject, dtsAll, mineH, null);
                        results["sharedDeviceTagCount"] = sh.Count;
                        Dictionary<string, object> shl = new Dictionary<string, object>();
                        foreach (KeyValuePair<string, object> kv in sh) { if (shl.Count >= listMax) break; shl[kv.Key] = kv.Value; }
                        results["sharedDeviceTags"] = shl;
                    }
                }
'''

def _selection_subs(page, handles, region, device_tags, whole_page, region_match, types,
                    exclude_handles, include_box_area, include_group, include_connections, max_items):
    hl = _str_list(handles, "handles")
    tags = _str_list(device_tags, "device_tags", 500)
    chosen = [n for n, v in (("handles", hl), ("region", region), ("device_tags", tags), ("whole_page", whole_page)) if v]
    if len(chosen) != 1:
        raise SchematicValueError("Pass exactly one selector - handles=[...], region=[x1, y1, x2, y2], "
                                  "device_tags=[...] or whole_page=true (got %s)." % (", ".join(chosen) or "none"))
    rect = _rect(region) if region else [0.0, 0.0, 0.0, 0.0]
    if region_match not in ("inside", "location"):
        raise SchematicValueError("region_match must be 'inside' or 'location', got %r." % (region_match,))
    mode = "handles" if hl else "region" if region else "devices" if tags else "page"
    return mode, dict(
        V11PAGE=_cs_lit(cs_text(page, "page")),
        V11MODE='"%s"' % mode,
        V11HANDLES=_cs_lit_array(hl), V11TAGS=_cs_lit_array(tags),
        V11RX1=cs_double(rect[0], "region"), V11RY1=cs_double(rect[1], "region"),
        V11RX2=cs_double(rect[2], "region"), V11RY2=cs_double(rect[3], "region"),
        V11RMATCH='"%s"' % region_match,
        V11TYPES=_cs_lit_array(_str_list(types, "types", 50)),
        V11EXH=_cs_lit_array(_str_list(exclude_handles, "exclude_handles")),
        V11INCBOX=cs_bool(include_box_area), V11INCGROUP=cs_bool(include_group),
        V11INCCONN=cs_bool(include_connections),
        V11MAX=str(cs_int(max_items, "max_items", minimum=1, maximum=20000)))


def _macro_out_path(path, ext, overwrite, what):
    p = cs_text(path, what).strip()
    if not os.path.isabs(p) or not p.lower().endswith(ext):
        raise SchematicValueError("%s must be an absolute %s path, got %r." % (what, ext, path))
    if not os.path.isdir(os.path.dirname(p)):
        raise SchematicValueError("%s: folder does not exist: %r." % (what, os.path.dirname(p)))
    if os.path.exists(p) and not overwrite:
        raise SchematicValueError("%s exists: %r (overwrite_macro=true to replace it)." % (what, p))
    return p


def live_copy_objects(page: str, handles: list = None, region: list = None, device_tags: list = None,
                      whole_page: bool = False, target_page: str = None,
                      source_project: str = None, target_project: str = None,
                      dx: float = None, dy: float = None, to_x: float = None, to_y: float = None,
                      copies: int = 1, step_dx: float = None, step_dy: float = None,
                      route: str = "macro", numbering: str = "default",
                      device_replace: list = None, device_replace_per_copy: list = None,
                      allow_merge: bool = False,
                      region_match: str = "inside", types: list = None, exclude_handles: list = None,
                      include_box_area: bool = True, include_group: bool = True,
                      include_connections: bool = False,
                      allow_overlap: bool = False, allow_outside_page: bool = False,
                      save_macro_path: str = None, overwrite_macro: bool = False,
                      max_items: int = 2000, dry_run: bool = True, allow_real_project: bool = False,
                      rollback_on_error: bool = True, check_shared_tags: bool = True,
                      close_source: bool = True, list_limit: int = 20, verbose: bool = False,
                      timeout_seconds: float = 600.0) -> dict:
    """
    v11: COPY AND PASTE placements - a PLC card window, a device with its box,
    a rectangle, a list of handles or a whole page - onto the same page, another
    page or a page of ANOTHER project, shifted by the offset (dx, dy) or so that the
    selection's lower-left corner lands on (to_x, to_y); copies=N pastes N
    times in one call (each further copy shifted by step_dx/step_dy, default
    dx/dy). WRITES (unless dry_run, the default) - the target project must be a
    scratch project unless allow_real_project=True.

    Selection (ONE of, on `page` of the source project): handles=[...] |
    region=[x1, y1, x2, y2] (region_match inside / location, types filter) |
    device_tags=["510S1", "=AB-C+ET-1-510K3"] (the functions + everything inside
    their box + their group: include_box_area / include_group) | whole_page=true.
    exclude_handles leaves placements out. Group objects are not copied
    themselves (their members are), symbol texts come with their symbol,
    logical connections are regenerated by EPLAN (include_connections=true
    copies them too).

    route "macro" (default, like EPLAN's copy/paste): WindowMacro.Create of
    the selection into a temporary .ema, Insert.WindowMacro per copy at the
    target point; the result is measured and shifted if EPLAN placed it
    elsewhere ("correction"). numbering: "default" (insert as
    live_insert_window_macro - device tags from the macro / project settings),
    "keep" (NumerationMode None = do not modify), "number" /
    "number_question" / "number_prefix" (renumber - breaks the link to the
    originals). route "placement": Placement.CopyTo(page) of every selected
    placement (same device tags, no macro).

    device_replace=[{"from": "510S1", "to": "510S2"}] renames the device tags
    of EVERY copy; device_replace_per_copy=[[{"from": "K1", "to": "K2"}],
    [{"from": "K1", "to": "K3"}]] one list per copy (live_rename_devices on the
    inserted functions; target must be the open project).

    Safety: a copied box overlapping a box on the target page (EPLAN nests the
    tags) or a copy outside the page is refused (allow_overlap /
    allow_outside_page); max_items (default 2000). rollback_on_error removes
    everything this call inserted. save_macro_path keeps the .ema (= "save the
    selection as window macro").

    Returns (compact): {"success", "dryRun", "selectedCount", "countsByType",
    "selectionBox", "plan" [{copy, dx, dy, box}], "copies" [{copy,
    insertedCount, functions, deviceTagCount, deviceTags, correction, ms}],
    "sharedDeviceTagCount"/"sharedDeviceTags" (tags that also exist elsewhere
    = merged / cross-referenced with the originals), "renameDevices",
    "macroSaved", "timing", "source", "target"}; verbose adds
    "functionHandles" per copy. Refused: errorType OVERLAP / OUTSIDE_PAGE /
    TOO_MANY (dry_run: "wouldRefuse"). Failed: ROLLED_BACK / PARTIAL + failedAt.
    """
    t0 = time.perf_counter()
    try:
        src = _project_path(source_project, "source_project")
        tgt = _project_path(target_project, "target_project")
        mode, subs = _selection_subs(page, handles, region, device_tags, whole_page, region_match, types,
                                     exclude_handles, include_box_area, include_group, include_connections, max_items)
        absolute = to_x is not None or to_y is not None
        if absolute and (to_x is None or to_y is None):
            raise SchematicValueError("to_x and to_y go together.")
        if absolute and (dx is not None or dy is not None):
            raise SchematicValueError("Pass dx/dy OR to_x/to_y, not both.")
        n = cs_int(copies, "copies", minimum=1, maximum=200)
        same_page = not target_page and not tgt and not src
        if not absolute and dx is None and dy is None and same_page:
            raise SchematicValueError("A copy on the same page needs dx/dy or to_x/to_y (it would land on the original).")
        ddx = 0.0 if dx is None else float(cs_double(dx, "dx"))
        ddy = 0.0 if dy is None else float(cs_double(dy, "dy"))
        sdx = float(cs_double(step_dx, "step_dx")) if step_dx is not None else ddx
        sdy = float(cs_double(step_dy, "step_dy")) if step_dy is not None else ddy
        if n > 1 and sdx == 0 and sdy == 0:
            raise SchematicValueError("copies > 1 needs step_dx / step_dy (or dx / dy) - the copies would lie on each other.")
        if route not in ("macro", "placement"):
            raise SchematicValueError("route must be 'macro' or 'placement', got %r." % (route,))
        num = _num(numbering, _MACRO_NUMBERING, "numbering")
        if route == "placement" and num != "default":
            raise SchematicValueError("numbering applies to route 'macro' only.")
        keep = ""
        if save_macro_path:
            if route != "macro":
                raise SchematicValueError("save_macro_path needs route 'macro'.")
            keep = _macro_out_path(save_macro_path, ".ema", overwrite_macro, "save_macro_path")
        reps = _check_reps(device_replace, "device_replace")
        per = []
        if device_replace_per_copy is not None:
            if reps:
                raise SchematicValueError("Pass device_replace OR device_replace_per_copy.")
            if not isinstance(device_replace_per_copy, (list, tuple)) or len(device_replace_per_copy) != n:
                raise SchematicValueError("device_replace_per_copy must hold one list per copy (%d)." % n)
            per = [_check_reps(x, "device_replace_per_copy[%d]" % i) for i, x in enumerate(device_replace_per_copy)]
        ll = cs_int(list_limit, "list_limit", minimum=0, maximum=2000)
        body, psubs = _proj_fill(_COPY_OBJECTS_BODY, src, tgt, "ifopened" if close_source else "no")
        subs.update(psubs)
        subs.update(
            V11TPAGE=_cs_lit(cs_text(target_page, "target_page", allow_empty=True)),
            V11DX=cs_double(ddx, "dx"), V11DY=cs_double(ddy, "dy"),
            V11ABS=cs_bool(absolute),
            V11TOX=cs_double(0.0 if to_x is None else to_x, "to_x"),
            V11TOY=cs_double(0.0 if to_y is None else to_y, "to_y"),
            V11SDX=cs_double(sdx, "step_dx"), V11SDY=cs_double(sdy, "step_dy"),
            V11COPIES=str(n), V11LIST=str(max(ll, 1)),
            V11ALLOWOUT=cs_bool(allow_outside_page), V11ALLOWOVER=cs_bool(allow_overlap),
            V11DRY=cs_bool(dry_run), V11GUARD=_guard_target(allow_real_project),
            V11ROUTE='"%s"' % route, V11NUM='"%s"' % num, V11RB=cs_bool(rollback_on_error),
            V11KEEP=_cs_lit(keep), V11SHARED=cs_bool(check_shared_tags))
        script = _script(_cls("V11CopyObj"), _fill(body, **subs), extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)

    raw, ms = _timed(script, timeout_seconds)
    out = _shape_keep(raw, keep=("copies", "failedAt", "rolledBack", "rollbackErrors", "selectedCount",
                                 "plan", "source", "target", "eplanMs", "insertedTotal"))
    out["mode"] = mode
    out["dryRun"] = bool(dry_run)
    extra = {"scriptMs": ms}
    if not out.get("success") and "TOO_MANY:" in str(out.get("error", "")):
        out["errorType"] = "TOO_MANY"
    if out.get("refused"):
        rt, rw = out.pop("refused"), out.pop("refusedReason", "refused")
        if dry_run:
            out["wouldRefuse"], out["wouldRefuseReason"] = rt, rw
        else:
            out.update(success=False, errorType=rt, error=rw, insertedTotal=0)
    elif not dry_run and out.get("failedAt"):
        fa = out["failedAt"]
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error and not out.get("rollbackErrors") else "PARTIAL"
        out["error"] = "Copy %s failed (step %s): %s. %s" % (
            fa.get("copy", ""), fa.get("step"), fa.get("error"),
            "Everything inserted by this call was removed." if out["errorType"] == "ROLLED_BACK"
            else "Copies inserted before it stay - check the page.")
    handles_per = out.get("functionHandles") or []
    if out.get("success") and not dry_run and (reps or per):
        if not _is_current_target(out):
            out.setdefault("warnings", []).append("device_replace skipped: the target is not the open project.")
        else:
            results = []
            tr = time.perf_counter()
            for i, hs in enumerate(handles_per):
                rr = reps or (per[i] if i < len(per) else [])
                if not rr or not hs:
                    continue
                rn = live_rename_devices(handles=hs, replace=rr, dry_run=False, allow_merge=allow_merge,
                                         allow_real_project=allow_real_project, timeout_seconds=timeout_seconds)
                results.append({"copy": i + 1, "success": rn.get("success"), "mapping": rn.get("mapping"),
                                "error": rn.get("error")})
                if not rn.get("success"):
                    out["success"] = False
                    out["errorType"] = "RENAME_FAILED"
                    out["error"] = "Copy %d inserted, device_replace failed: %s" % (i + 1, rn.get("error"))
                    break
            out["renameDevices"] = results
            extra["renameMs"] = int((time.perf_counter() - tr) * 1000)
    if out.get("success") and out.get("sharedDeviceTagCount"):
        out.setdefault("warnings", []).append(
            "%d device tag(s) of the copies also exist elsewhere in the target - merged / cross-referenced "
            "(numbering='number' or device_replace to separate)." % out["sharedDeviceTagCount"])
    if not verbose:
        out.pop("functionHandles", None)
    _limit(out, "plan", max(ll, 1))
    return _timing(out, int((time.perf_counter() - t0) * 1000), extra)

_SAVE_WINDOW_BODY = _SELECT_PART + r'''
                System.Diagnostics.Stopwatch msw = System.Diagnostics.Stopwatch.StartNew();
                string outp = V11OUT;
                string tmp = Path.Combine(Path.GetTempPath(), "eplan_mcp_save_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".ema");
                object rep = V11WriteWindowMacro(tmp, sel, V11DESCR, V11VARIANT);
                File.Copy(tmp, outp, true);
                try { File.Delete(tmp); } catch { }
                results["macro"] = outp;
                results["bytes"] = new FileInfo(outp).Length;
                if (rep != null) results["representationType"] = rep.ToString();
                results["macroMs"] = msw.ElapsedMilliseconds;
'''

_SAVE_PAGES_BODY = r'''
                string[] pgs = V11PAGES;
                Dictionary<string, object> sIdx = V11PageIndex(sproject);
                List<string> miss = new List<string>();
                foreach (string s in pgs) if (!sIdx.ContainsKey(s)) miss.Add(s);
                if (miss.Count > 0) throw new Exception("Page(s) not found: " + string.Join(" | ", miss.ToArray()));
                System.Diagnostics.Stopwatch msw = System.Diagnostics.Stopwatch.StartNew();
                Type pmT = FindType("Eplan.EplApi.DataModel.MasterData.PageMacro");
                MethodInfo pmCreate = RequireMethod(pmT, "Create", new string[] { "String", "Page[]", "MultiLangString" }, false);
                ParameterInfo[] ps = pmCreate.GetParameters();
                Array parr = Array.CreateInstance(ps[1].ParameterType.GetElementType(), pgs.Length);
                for (int i = 0; i < pgs.Length; i++) parr.SetValue(sIdx[pgs[i]], i);
                object mls = null;
                string descr = V11DESCR;
                if (descr.Length > 0)
                {
                    mls = Activator.CreateInstance(ps[2].ParameterType);
                    Call(RequireMethod(ps[2].ParameterType, "SetAsString", new string[] { "String" }, false), mls, new object[] { descr });
                }
                string outp = V11OUT;
                string tmp = Path.Combine(Path.GetTempPath(), "eplan_mcp_save_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".emp");
                object pmac = Activator.CreateInstance(pmT);
                try { Call(pmCreate, pmac, new object[] { tmp, parr, mls }); }
                finally
                {
                    IDisposable dm = pmac as IDisposable;
                    if (dm != null) { try { dm.Dispose(); } catch { } }
                }
                if (!File.Exists(tmp)) throw new Exception("PageMacro.Create wrote no file.");
                File.Copy(tmp, outp, true);
                try { File.Delete(tmp); } catch { }
                results["macro"] = outp;
                results["pageCount"] = pgs.Length;
                results["bytes"] = new FileInfo(outp).Length;
                results["macroMs"] = msw.ElapsedMilliseconds;
'''


def live_save_macro(macro_path: str, page: str = None, handles: list = None, region: list = None,
                    device_tags: list = None, whole_page: bool = False, pages: list = None,
                    description: str = None, variant: int = 0, source_project: str = None,
                    region_match: str = "inside", types: list = None, exclude_handles: list = None,
                    include_box_area: bool = True, include_group: bool = True,
                    include_connections: bool = False, overwrite_macro: bool = False,
                    max_items: int = 5000, close_source: bool = True,
                    timeout_seconds: float = 300.0) -> dict:
    """
    v11: SAVE a selection as a WINDOW macro (.ema) or pages as a PAGE macro
    (.emp) - from the open project or another .elk (opened read-only). Writes
    only the macro file, never the project (no allow_real_project needed).

    Window macro (macro_path *.ema): page + ONE selector as live_copy_objects
    (handles / region / device_tags / whole_page) - WindowMacro.Create(path,
    variant, Placement[], description). Insert it with live_insert_window_macro.
    Page macro (macro_path *.emp): pages=[...] (all from one project) -
    PageMacro.Create(path, Page[], description). Insert it with
    live_insert_page_macro.

    Returns {"success", "macro", "bytes", "selectedCount" / "pageCount",
    "countsByType", "selectionBox", "representationType", "timing"}.
    """
    t0 = time.perf_counter()
    try:
        src = _project_path(source_project, "source_project")
        p = cs_text(macro_path, "macro_path").strip()
        is_page = p.lower().endswith(".emp")
        outp = _macro_out_path(p, ".emp" if is_page else ".ema", overwrite_macro, "macro_path")
        descr = cs_text(description, "description", allow_empty=True)
        if is_page:
            pl = _str_list(pages, "pages", 2000)
            if not pl:
                raise SchematicValueError("A page macro (.emp) needs pages=[...].")
            if page or handles or region or device_tags or whole_page:
                raise SchematicValueError("A page macro takes pages=[...] only (no page / selector).")
            body, subs = _proj_fill(_SAVE_PAGES_BODY, src, "", "ifopened" if close_source else "no")
            subs.update(V11PAGES=_cs_lit_array(pl), V11OUT=_cs_lit(outp), V11DESCR=_cs_lit(descr))
        else:
            if pages:
                raise SchematicValueError("pages=[...] is for a page macro (.emp); a window macro (.ema) takes page + selector.")
            _, subs = _selection_subs(page, handles, region, device_tags, whole_page, region_match, types,
                                      exclude_handles, include_box_area, include_group, include_connections, max_items)
            body, psubs = _proj_fill(_SAVE_WINDOW_BODY, src, "", "ifopened" if close_source else "no")
            subs.update(psubs)
            subs.update(V11OUT=_cs_lit(outp), V11DESCR=_cs_lit(descr),
                        V11VARIANT=str(cs_int(variant, "variant", minimum=0, maximum=15)))
        script = _script(_cls("V11SaveMacro"), _fill(body, **subs), extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    raw, ms = _timed(script, timeout_seconds)
    out = _shape(raw)
    return _timing(out, int((time.perf_counter() - t0) * 1000), {"scriptMs": ms})


# ---------------------------------------------------------------------------
# 3. live_insert_page_macro
# ---------------------------------------------------------------------------

_INSERT_PM_BODY = r'''
                V11GUARD
                string[] want = V11TGT;
                bool ow = V11OW;
                bool rbk = V11RB;
                string after = V11AFTER;
                Dictionary<string, object> tIdx0 = V11PageIndex(tproject);
                List<string> exist = new List<string>();
                foreach (string t in want) if (tIdx0.ContainsKey(t)) exist.Add(t);
                if (exist.Count > 0 && !ow) throw new Exception("Nothing inserted: page(s) exist (overwrite=false): " + string.Join(" | ", exist.ToArray()));
                Type pmT = FindType("Eplan.EplApi.DataModel.MasterData.PageMacro");
                object pmac = Activator.CreateInstance(pmT);
                System.Diagnostics.Stopwatch isw = System.Diagnostics.Stopwatch.StartNew();
                Dictionary<string, object> failedAt = null;
                try
                {
                    Call(RequireMethod(pmT, "Open", new string[] { "String", "Project" }, false), pmac, new object[] { V11MACRO, tproject });
                    object mpages = RequireReadable(pmT, "Pages").GetValue(pmac, null);
                    int k = 0;
                    if (mpages is IEnumerable)
                        foreach (object mp in (IEnumerable)mpages)
                        {
                            if (mp == null) continue;
                            if (k < want.Length && !string.Equals(PropText(mp, "Name"), want[k], StringComparison.OrdinalIgnoreCase)) V11SetName(mp, want[k]);
                            k++;
                        }
                    if (k != want.Length) throw new Exception("the macro holds " + k + " pages, " + want.Length + " names planned - re-run dry_run");
                    Type insT = FindType("Eplan.EplApi.HEServices.Insert");
                    object ins = Activator.CreateInstance(insT);
                    object ret;
                    try
                    {
                        if (after.Length > 0)
                        {
                            MethodInfo ipm = RequireMethod(insT, "PageMacro", new string[] { "PageMacro", "Page", "Project", "Boolean", "NumerationMode" }, false);
                            results["insertSignature"] = ipm.ToString();
                            ret = Call(ipm, ins, new object[] { pmac, FindPage(tproject, after), tproject, ow, V11Enum(ipm.GetParameters()[4].ParameterType, V11NUM) });
                        }
                        else
                        {
                            MethodInfo ipm = RequireMethod(insT, "PageMacro", new string[] { "PageMacro", "Project", "Boolean[]", "NumerationMode" }, false);
                            results["insertSignature"] = ipm.ToString();
                            bool[] owa = new bool[want.Length];
                            for (int i = 0; i < owa.Length; i++) owa[i] = ow;
                            ret = Call(ipm, ins, new object[] { pmac, tproject, owa, V11Enum(ipm.GetParameters()[3].ParameterType, V11NUM) });
                        }
                    }
                    finally
                    {
                        IDisposable di = ins as IDisposable;
                        if (di != null) { try { di.Dispose(); } catch { } }
                    }
                    int insPages = 0, unplaced = 0;
                    if (ret is IEnumerable)
                        foreach (object o in (IEnumerable)ret)
                        {
                            if (o == null) continue;
                            if (o.GetType().Name == "Page") insPages++; else unplaced++;
                        }
                    results["returnedPages"] = insPages;
                    results["unplacedFunctions"] = unplaced;
                }
                catch (Exception ex)
                {
                    failedAt = new Dictionary<string, object>();
                    failedAt["error"] = Flatten(ex);
                }
                finally
                {
                    IDisposable dm = pmac as IDisposable;
                    if (dm != null) { try { dm.Dispose(); } catch { } }
                }
                results["insertMs"] = isw.ElapsedMilliseconds;
                Dictionary<string, object> tIdx1 = V11PageIndex(tproject);
                List<object> pagesOut = new List<object>();
                List<object> created = new List<object>();
                List<string> missing = new List<string>();
                foreach (string t in want)
                {
                    object np;
                    if (tIdx1.TryGetValue(t, out np)) { pagesOut.Add(PropText(np, "Name")); if (!exist.Contains(t)) created.Add(np); }
                    else missing.Add(t);
                }
                int newCount = 0;
                foreach (KeyValuePair<string, object> kv in tIdx1) if (!tIdx0.ContainsKey(kv.Key)) newCount++;
                results["pages"] = pagesOut;
                results["newPagesInProject"] = newCount;
                if (missing.Count > 0) results["notFoundAfterInsert"] = missing;
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (rbk)
                    {
                        int rb = 0;
                        foreach (object pc in created) { try { if (IsValidObj(pc)) { V11Remove(pc); rb++; } } catch { } }
                        results["rolledBackPages"] = rb;
                    }
                }
'''


def live_insert_page_macro(macro_path: str, page_rename: dict = None, devices: str = "keep",
                           overwrite: bool = False, insert_after_page: str = None,
                           dry_run: bool = True, allow_real_project: bool = False,
                           rollback_on_error: bool = True, list_limit: int = 40,
                           timeout_seconds: float = 600.0) -> dict:
    """
    v11: INSERT a PAGE macro (.emp) into the open project with new page names -
    Insert.PageMacro (API 2.9; the names are set on PageMacro.Pages before the
    insert). WRITES (unless dry_run, the default) - scratch-only unless
    allow_real_project=True.

    page_rename and devices exactly as live_copy_pages (rules on the macro's
    page names; devices keep / adapt / number / number_question /
    number_prefix). insert_after_page: insert after this page (the overload with
    a page; the macro then takes that page's structure). dry_run: "plan"
    (macro page -> new name) and the names that already exist.

    Returns {"success", "dryRun", "plan", "pages", "newPagesInProject",
    "unplacedFunctions", "notFoundAfterInsert", "timing"}; failed: ROLLED_BACK /
    PARTIAL + failedAt.
    """
    t0 = time.perf_counter()
    try:
        p = cs_text(macro_path, "macro_path").strip()
        if not p.lower().endswith(".emp") or not os.path.isabs(p) or not os.path.isfile(p):
            raise SchematicValueError("macro_path must be an existing absolute .emp file, got %r." % (macro_path,))
        rule = _check_rule(page_rename)
        num = _num(devices, _PAGE_NUMBERING, "devices")
        after = cs_text(insert_after_page, "insert_after_page", allow_empty=True)
        ll = cs_int(list_limit, "list_limit", minimum=1, maximum=2000)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    inv = _inventory("", "", p, "no", timeout_seconds)
    timing_extra = {"inventoryMs": inv.pop("_ms", None)}
    if not inv.get("success"):
        inv["error"] = "Reading the macro / page list failed: %s" % inv.get("error")
        return _timing(inv, int((time.perf_counter() - t0) * 1000), timing_extra)
    mnames = [r[0] for r in inv.get("macroPages") or [] if r]
    tnames = [r[0] for r in inv.get("sourcePages") or [] if r and r[0]]
    try:
        if not mnames:
            raise SchematicValueError("The macro holds no pages (or PageMacro.Pages is unreadable).")
        targets = page_targets(mnames, rule, tnames)
    except SchematicValueError as exc:
        return _timing(_err(exc), int((time.perf_counter() - t0) * 1000), timing_extra)
    tset = {n.upper() for n in tnames}
    exists = [t for t in targets if t.upper() in tset]
    dup = sorted({t for t in targets if targets.count(t) > 1})
    plan = ["%s -> %s" % (a, b) for a, b in zip(mnames, targets)]
    problems = []
    if dup:
        problems.append("target page names repeat: %s" % ", ".join(dup))
    if exists and not overwrite:
        problems.append("%d page(s) exist (overwrite=false): %s" % (len(exists), ", ".join(exists[:15])))
    if dry_run or problems:
        out = {"success": not problems, "dryRun": bool(dry_run), "plan": plan, "macroPageCount": len(mnames)}
        if exists:
            out["existingTargets"] = exists
        if problems:
            out["errorType"] = "TARGET_EXISTS" if exists and not dup else "BAD_NAMES"
            out["error"] = "Nothing inserted: " + "; ".join(problems) + "."
        _limit(out, "plan", ll)
        return _timing(out, int((time.perf_counter() - t0) * 1000), timing_extra)
    body, subs = _proj_fill(_INSERT_PM_BODY, "", "", "no")
    subs.update(V11GUARD=_guard_target(allow_real_project), V11TGT=_cs_lit_array(targets),
                V11OW=cs_bool(overwrite), V11RB=cs_bool(rollback_on_error), V11AFTER=_cs_lit(after),
                V11MACRO=_cs_lit(p), V11NUM='"%s"' % num)
    raw, ms = _timed(_script(_cls("V11InsPm"), _fill(body, **subs), extra_helpers=_ALL_HELPERS), timeout_seconds)
    out = _shape_keep(raw, keep=("pages", "failedAt", "rolledBackPages", "notFoundAfterInsert", "eplanMs"))
    out["dryRun"] = False
    out["plan"] = plan
    timing_extra["insertScriptMs"] = ms
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error else "PARTIAL"
        out["error"] = "Insert.PageMacro failed: %s" % out["failedAt"].get("error")
    elif out.get("success") and out.get("notFoundAfterInsert"):
        out.setdefault("warnings", []).append("Pages not found under the planned names after the insert: %s"
                                              % ", ".join(out["notFoundAfterInsert"][:10]))
    _limit(out, "plan", ll)
    _limit(out, "pages", ll)
    return _timing(out, int((time.perf_counter() - t0) * 1000), timing_extra)

# ---------------------------------------------------------------------------
# 4. live_rename_devices
# ---------------------------------------------------------------------------

_LIST_FN_BODY = r'''
            string[] pgs = V11PAGES;
            string pcont = V11PCONT;
            string[] hs = V11HANDLES;
            string[] tags = V11TAGS;
            string prefix = V11PREFIX;
            Dictionary<string, object> info = new Dictionary<string, object>();
            List<object> fns;
            if (hs.Length > 0)
            {
                Dictionary<string, bool> want = new Dictionary<string, bool>();
                foreach (string h in hs) want[h] = true;
                fns = new List<object>();
                foreach (object fn in AllFunctions(project))
                {
                    if (fn == null) continue;
                    string h = Handle(fn);
                    if (h != null && want.ContainsKey(h)) fns.Add(fn);
                }
            }
            else fns = V11ScopeFunctions(project, pgs, pcont, info);
            List<object> rows = new List<object>();
            foreach (object fn in fns)
            {
                string n = SafeText(TryRead(fn, "Name", null)) ?? "";
                if (tags.Length > 0 && !TagHit(n, tags)) continue;
                string[] r = new string[6];
                r[0] = Handle(fn) ?? "";
                r[1] = n;
                r[2] = DtPart(n);
                r[3] = IsBoxPl(fn) ? "box" : "";
                object mf = TryRead(fn, "IsMainFunction", null);
                bool isMain = false;
                if (mf != null) { try { isMain = Convert.ToBoolean(mf); } catch { } }
                r[4] = isMain ? "main" : "";
                r[5] = FnPageName(fn);
                rows.Add(r);
            }
            results["rows"] = rows;
            foreach (KeyValuePair<string, object> kv in info) results[kv.Key] = kv.Value;
            if (prefix.Length > 0)
            {
                List<string> used = new List<string>();
                foreach (object fn in AllFunctions(project))
                {
                    if (fn == null) continue;
                    string st = ShortTag(DtPart(SafeText(TryRead(fn, "Name", null))));
                    if (st.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) && !used.Contains(st)) used.Add(st);
                }
                results["usedWithPrefix"] = used;
            }
'''


def _short(dt):
    return dt.rsplit("-", 1)[-1] if "-" in dt else dt


def _with_short(dt, short):
    if "-" not in dt:
        return short
    return dt[:len(dt) - len(_short(dt))] + short.lstrip("-")


def tag_for(old, new):
    """The "tag" live_set_device_tags needs so that ComposeDtV6(old, tag) == new, or None.

    live_set_device_tags takes a tag with '=' / '+' whole and otherwise replaces
    only the part after the LAST '-' of the old device tag - so "ET-1.3" ->
    "ET-1.4" must be sent as "1.4" ("ET-1.4" would give "ET-ET-1.4").
    """
    if "=" in new or "+" in new:
        return new
    if "-" not in old:
        return new
    prefix = old[:old.rfind("-")] + "-"
    if new.startswith(prefix) and len(new) > len(prefix) and not new[len(prefix):].startswith("-"):
        return new[len(prefix):]
    return None


def rename_plan(rows, replace=None, mapping=None, next_free=None, used_with_prefix=None):
    """{old device tag: {"handle" (box / main / first), "functions", "to"}} - only tags that change."""
    groups = {}
    order = []
    for r in rows:
        h, name, dt, box, main, page = (list(r) + [""] * 6)[:6]
        if not dt:
            continue
        if dt not in groups:
            groups[dt] = {"handles": [], "box": None, "main": None}
            order.append(dt)
        g = groups[dt]
        g["handles"].append(h)
        if box and not g["box"]:
            g["box"] = h
        if main and not g["main"]:
            g["main"] = h
    new = {}
    for dt in order:
        for k, v in (mapping or {}).items():
            hit = (k == dt or k.lstrip("-") == dt.lstrip("-")
                   or ("=" not in k and "+" not in k and _short(dt) == k.lstrip("-")))
            if hit:
                new[dt] = v if ("=" in v or "+" in v) else _with_short(dt, v)
                break
    for dt in order:
        if dt in new or not replace:
            continue
        t = _replace_all(dt, replace)
        if t != dt:
            new[dt] = t
    if next_free:
        prefix = next_free["prefix"]
        digits = next_free.get("digits", 0)
        used = set()
        for st in used_with_prefix or []:
            rest = st[len(prefix):]
            if st[:len(prefix)].upper() == prefix.upper() and rest.isdigit():
                used.add(int(rest))
        n = next_free.get("start", 1)
        for dt in order:
            if dt in new:
                continue
            while n in used:
                n += 1
            used.add(n)
            new[dt] = _with_short(dt, prefix + (str(n).zfill(digits) if digits else str(n)))
    plan = {}
    for dt in order:
        if dt not in new or new[dt] == dt:
            continue
        g = groups[dt]
        plan[dt] = {"handle": g["box"] or g["main"] or g["handles"][0], "functions": len(g["handles"]), "to": new[dt]}
    return plan


def live_rename_devices(pages: list = None, page_contains: str = None, handles: list = None,
                        device_tags: list = None, replace: list = None, mapping: dict = None,
                        next_free: dict = None, allow_merge: bool = False, dry_run: bool = True,
                        allow_real_project: bool = False, rollback_on_error: bool = True,
                        evaluate_pages: bool = True, name_route: str = "auto", verbose: bool = False,
                        list_limit: int = 60, timeout_seconds: float = 600.0) -> dict:
    """
    v11: BATCH RENAME device tags after a copy - every device (box + all its
    points / functions with the same tag) in the scope gets its new tag in ONE
    live_set_device_tags call (box before points, merge check, verification,
    rollback). WRITES (unless dry_run, the default) - scratch-only unless
    allow_real_project=True. Works on the open project.

    Scope (optional, combine): pages=[...] | page_contains="+DI/" | handles=[...]
    (functions, e.g. those live_copy_objects inserted); device_tags=[...] narrows
    to these devices (name / device tag / last segment "510K3"). Without scope:
    the whole project.

    New tag (one of / combined in this order):
        mapping={"510K3": "610K3", "=AB-C+DI-K1": "=AB-C+DO-K7"}  explicit (a
                 value without '=' / '+' replaces only the last segment)
        replace=[{"from": "510", "to": "610"}]   string replace in the device tag
        next_free={"prefix": "K", "start": 1, "digits": 0}  next free number
                 with that prefix in the project (structure kept)

    Returns (compact): {"success", "dryRun", "mapping": ["old -> new (n
    functions)"], "devices", "functions", "setDeviceTags" {merges, modified,
    unverified, rollback, warnings ...}, "timing"}. A new tag that already exists
    is refused (merge) unless allow_merge=True.
    """
    t0 = time.perf_counter()
    try:
        pl = _str_list(pages, "pages", 2000)
        hl = _str_list(handles, "handles", 20000)
        tl = _str_list(device_tags, "device_tags", 2000)
        pc = cs_text(page_contains, "page_contains", allow_empty=True)
        reps = _check_reps(replace, "replace")
        mp = {}
        if mapping is not None:
            if not isinstance(mapping, dict) or not mapping:
                raise SchematicValueError("mapping must be {old tag: new tag}.")
            for k, v in mapping.items():
                cs_text(k, "mapping key")
                cs_text(v, "mapping value")
            mp = dict(mapping)
        nf = None
        if next_free is not None:
            if not isinstance(next_free, dict) or not isinstance(next_free.get("prefix"), str) or not next_free["prefix"]:
                raise SchematicValueError("next_free must be {\"prefix\": \"K\", \"start\": 1, \"digits\": 0}.")
            nf = {"prefix": cs_text(next_free["prefix"], "next_free.prefix"),
                  "start": cs_int(next_free.get("start", 1), "next_free.start", minimum=0, maximum=10 ** 7),
                  "digits": cs_int(next_free.get("digits", 0), "next_free.digits", minimum=0, maximum=8)}
        if not (reps or mp or nf):
            raise SchematicValueError("Give replace=[...], mapping={...} or next_free={...}.")
        ll = cs_int(list_limit, "list_limit", minimum=1, maximum=5000)
        body = _fill(_LIST_FN_BODY, V11PAGES=_cs_lit_array(pl), V11PCONT=_cs_lit(pc), V11HANDLES=_cs_lit_array(hl),
                     V11TAGS=_cs_lit_array(tl), V11PREFIX=_cs_lit(nf["prefix"] if nf else ""))
        script = _script(_cls("V11ListFn"), body, extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    raw, ms = _timed(script, timeout_seconds)
    lst = _shape(raw)
    if not lst.get("success"):
        return _timing(lst, int((time.perf_counter() - t0) * 1000), {"listMs": ms})
    rows = lst.get("rows") or []
    plan = rename_plan(rows, reps, mp, nf, lst.get("usedWithPrefix"))
    news = [v["to"] for v in plan.values()]
    clash = sorted({t for t in news if news.count(t) > 1})
    out = {"success": True, "dryRun": bool(dry_run), "functions": len(rows), "devices": len(plan),
           "mapping": ["%s -> %s (%d)" % (k, v["to"], v["functions"]) for k, v in plan.items()]}
    if clash and not allow_merge:
        out.update(success=False, errorType="MERGE", error="Several devices would get the same tag: %s - nothing renamed "
                   "(allow_merge=true to merge)." % ", ".join(clash))
        _limit(out, "mapping", ll)
        return _timing(out, int((time.perf_counter() - t0) * 1000), {"listMs": ms})
    if not plan:
        out["note"] = "No device tag in the scope changes - nothing to do."
        return _timing(out, int((time.perf_counter() - t0) * 1000), {"listMs": ms})
    items, bad = [], []
    for k, v in plan.items():
        tg = tag_for(k, v["to"])
        if tg is None:
            bad.append("%s -> %s" % (k, v["to"]))
        items.append({"handle": v["handle"], "tag": tg, "scope": "device"})
    if bad:
        out.update(success=False, errorType="BAD_TAG", error=(
            "Nothing renamed: %s keep(s) no structure ('=' / '+') and change(s) more than the part after the last "
            "'-' - live_set_device_tags cannot express that. Give the full structured tag (mapping "
            "{\"old\": \"=PLANT+LOC-NEW\"}) or change only the last segment." % ", ".join(bad[:10])))
        _limit(out, "mapping", ll)
        return _timing(out, int((time.perf_counter() - t0) * 1000), {"listMs": ms})
    ts = time.perf_counter()
    res = _lp.live_set_device_tags(items, allow_merge=allow_merge, dry_run=dry_run,
                                   rollback_on_error=rollback_on_error, allow_real_project=allow_real_project,
                                   timeout_seconds=timeout_seconds, name_route=name_route,
                                   evaluate_pages=evaluate_pages, verbose=verbose)
    sd_ms = int((time.perf_counter() - ts) * 1000)
    if not verbose and isinstance(res, dict):
        res = {k: v for k, v in res.items() if k not in ("functions", "planned", "details", "undo", "project")}
    out["setDeviceTags"] = res
    if not res.get("success"):
        out["success"] = False
        out["errorType"] = res.get("errorType") or "SET_DEVICE_TAGS"
        out["error"] = res.get("error")
    elif dry_run and res.get("wouldRefuse"):
        out["wouldRefuse"] = res.get("wouldRefuse")
    _limit(out, "mapping", ll)
    return _timing(out, int((time.perf_counter() - t0) * 1000), {"listMs": ms, "setDeviceTagsMs": sd_ms})

# ---------------------------------------------------------------------------
# 5. live_set_function_definition
# ---------------------------------------------------------------------------

_SET_DEF_BODY = r'''
            string[] fH = V11H;
            string[] fLike = V11LIKE;
            string[] fName = V11DNAME;
            string[] fCat = V11CAT;
            int[] fGrp = V11GRP;
            int[] fId = V11ID;
            bool dry = V11DRY;
            bool rbk = V11RB;
            bool anyName = false;
            foreach (string s in fName) if (s.Length > 0) anyName = true;
            Dictionary<string, bool> want = new Dictionary<string, bool>();
            foreach (string h in fH) want[h] = true;
            foreach (string h in fLike) if (h.Length > 0) want[h] = true;
            Dictionary<string, object> byH = new Dictionary<string, object>();
            Dictionary<string, object[]> defByName = new Dictionary<string, object[]>();
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                string h = Handle(fn);
                if (h != null && want.ContainsKey(h) && !byH.ContainsKey(h)) byH[h] = fn;
                if (anyName)
                {
                    object fd = TryRead(fn, "FunctionDefinition", null);
                    if (fd == null) continue;
                    foreach (string t in V11DefTexts(fd))
                    {
                        string k = V11NormName(t);
                        if (k.Length > 0 && !defByName.ContainsKey(k)) defByName[k] = new object[] { fd, h };
                    }
                }
                else if (byH.Count == want.Count) break;
            }
            List<string> missing = new List<string>();
            foreach (string h in want.Keys) if (!byH.ContainsKey(h)) missing.Add(h);
            if (missing.Count > 0)
                throw new Exception("Nothing changed: handle(s) not among the project's functions: " + string.Join(", ", missing.ToArray()) + ". Handles are session-scoped - re-read them.");
            List<object> items = new List<object>();
            List<object[]> plan = new List<object[]>();
            List<string> problems = new List<string>();
            for (int i = 0; i < fH.Length; i++)
            {
                object fn = byH[fH[i]];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["i"] = i;
                it["handle"] = fH[i];
                it["name"] = SafeText(TryRead(fn, "Name", null)) ?? "";
                it["page"] = FnPageName(fn);
                object old = TryRead(fn, "FunctionDefinition", null);
                it["before"] = V11DefInfo(old);
                items.Add(it);
                object nd = null;
                try
                {
                    if (fLike[i].Length > 0)
                    {
                        nd = TryRead(byH[fLike[i]], "FunctionDefinition", null);
                        if (nd == null) throw new Exception("like_handle " + fLike[i] + " has no FunctionDefinition");
                        it["from"] = "like " + fLike[i];
                    }
                    else if (fName[i].Length > 0)
                    {
                        object[] hit;
                        if (!defByName.TryGetValue(V11NormName(fName[i]), out hit))
                        {
                            List<string> names = new List<string>();
                            foreach (string k in defByName.Keys) { if (names.Count >= 40) break; names.Add(k); }
                            throw new Exception("no function of the project uses a definition named '" + fName[i] + "' (spaces ignored). Names in the project: " + string.Join(" | ", names.ToArray()));
                        }
                        nd = hit[0];
                        it["from"] = "name, as on " + hit[1];
                    }
                    else
                    {
                        nd = V11MakeDef(project, fCat[i], fGrp[i], fId[i]);
                        it["from"] = "category/group/id";
                    }
                }
                catch (Exception ex) { problems.Add("item " + i + ": " + Flatten(ex)); continue; }
                it["after"] = V11DefInfo(nd);
                bool same = V11DefKey(old) == V11DefKey(nd) && V11DefKey(nd).Length > 2;
                it["action"] = same ? "unchanged" : "change";
                if (GetWritable(fn.GetType(), "FunctionDefinition") == null)
                    problems.Add("item " + i + ": " + fn.GetType().Name + " has no writable FunctionDefinition. " + MemberList(fn.GetType(), false));
                plan.Add(new object[] { i, fn, old, nd, same });
            }
            results["items"] = items;
            if (problems.Count > 0)
                throw new Exception("Nothing changed: " + string.Join("; ", problems.GetRange(0, Math.Min(20, problems.Count)).ToArray()));
            if (!dry)
            {
                V11GUARDCUR
                List<object[]> undo = new List<object[]>();
                Dictionary<string, object> failedAt = null;
                foreach (object[] p in plan)
                {
                    int i = (int)p[0];
                    Dictionary<string, object> it = (Dictionary<string, object>)items[i];
                    if ((bool)p[4]) { it["result"] = "unchanged"; continue; }
                    object fn = p[1];
                    try
                    {
                        List<string> c0 = ConnDesignations(FnProps(fn));
                        PropertyInfo pi = GetWritable(fn.GetType(), "FunctionDefinition");
                        undo.Add(new object[] { fn, p[2], i });
                        try { pi.SetValue(fn, p[3], null); }
                        catch (TargetInvocationException tie) { throw new Exception("FunctionDefinition: " + Flatten(tie.InnerException)); }
                        object now = TryRead(fn, "FunctionDefinition", null);
                        it["after"] = V11DefInfo(now);
                        if (V11DefKey(now) != V11DefKey(p[3])) throw new Exception("read back " + V11DefKey(now) + ", not " + V11DefKey(p[3]));
                        string n1 = SafeText(TryRead(fn, "Name", null)) ?? "";
                        if (n1 != (string)it["name"]) it["nameAfter"] = n1;
                        List<string> c1 = ConnDesignations(FnProps(fn));
                        if (string.Join("|", c0.ToArray()) != string.Join("|", c1.ToArray()))
                        {
                            it["connectionDesignationsBefore"] = c0;
                            it["connectionDesignationsAfter"] = c1;
                        }
                        it["result"] = "changed";
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["i"] = i;
                        failedAt["handle"] = it["handle"];
                        failedAt["error"] = Flatten(ex);
                        break;
                    }
                }
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (rbk)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            object fn = undo[k][0];
                            Dictionary<string, object> itU = (Dictionary<string, object>)items[(int)undo[k][2]];
                            try
                            {
                                if (undo[k][1] != null && V11DefKey(TryRead(fn, "FunctionDefinition", null)) != V11DefKey(undo[k][1]))
                                {
                                    GetWritable(fn.GetType(), "FunctionDefinition").SetValue(fn, undo[k][1], null);
                                    rb++;
                                    itU["result"] = "rolledBack";
                                }
                                else itU["result"] = "notWritten";
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); itU["result"] = "rollbackFailed"; }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
            }
'''

_DEF_KEYS = frozenset({"handle", "like_handle", "definition_name", "category", "group", "id"})


def live_set_function_definition(items: list = None, handles: list = None, like_handle: str = None,
                                 definition_name: str = None, category: str = None, group: int = None,
                                 id: int = None, dry_run: bool = True, allow_real_project: bool = False,
                                 rollback_on_error: bool = True, verbose: bool = False,
                                 timeout_seconds: float = 300.0) -> dict:
    """
    v11: CHANGE THE FUNCTION DEFINITION of functions (Function.FunctionDefinition
    {get; set;}, API 2.9) - e.g. a PLC connection point "PLC connection point,
    power supply" -> "PLC connection point, I / O" so it pairs with the channel
    of the card (inbox 2026-11-05_eplan_plc_tocka_definicija_funkcije.md:
    FUNC_GROUP is read-only, S063113). WRITES (unless dry_run, the default) -
    scratch-only unless allow_real_project=True.

    The new definition (per item, or for all handles):
        like_handle="682/17/36635/0"  the definition of another function (e.g.
                                      the card's overview channel) - safest
        definition_name="PLC connection point, I / O"  a definition some
                                      function of the project already uses
                                      (any language, spaces ignored)
        category="PLCTerminal", group=1, id=2  FunctionDefinition(Project,
                                      Category, short, short) from the project's
                                      definition library (live_list_functions
                                      fields=["defkey"] shows category/group/id)

    items=[{"handle", "like_handle" | "definition_name" | "category"+"group"+"id"}]
    or handles=[...] + one definition for all. Everything is resolved before the
    first write; read back (category/group/id), name and connection points
    reported when they change; rollback on error. Find mismatching PLC points
    with live_check_connections(checks=["plc"]).

    Returns {"success", "dryRun", "counts", "items" (compact: "handle name:
    old -> new [result]"; verbose: before/after {name, category, group, id},
    nameAfter, connection points), "timing"}; failed: ROLLED_BACK / PARTIAL +
    failedAt.
    """
    t0 = time.perf_counter()
    try:
        its = []
        if items is not None:
            if not isinstance(items, (list, tuple)) or not items:
                raise SchematicValueError("items must be a non-empty list.")
            for n, it in enumerate(items):
                if not isinstance(it, dict):
                    raise SchematicValueError("items[%d] must be a dict." % n)
                extra = set(it) - _DEF_KEYS
                if extra:
                    raise SchematicValueError("items[%d]: unknown key(s) %s." % (n, sorted(extra)))
                its.append(dict(it))
        for h in _str_list(handles, "handles", 2000):
            its.append({"handle": h, "like_handle": like_handle, "definition_name": definition_name,
                        "category": category, "group": group, "id": id})
        if not its:
            raise SchematicValueError("Give items=[...] or handles=[...] with one definition.")
        if len(its) > 2000:
            raise SchematicValueError("At most 2000 functions per call.")
        H, L, N, C, G, I = [], [], [], [], [], []
        for n, it in enumerate(its):
            h = it.get("handle")
            if not isinstance(h, str) or not h.strip():
                raise SchematicValueError("items[%d]: handle is required." % n)
            if h.strip() in H:
                raise SchematicValueError("items[%d]: handle %s twice." % (n, h))
            lk = it.get("like_handle") or ""
            dn = it.get("definition_name") or ""
            cat = it.get("category") or ""
            given = [x for x in (lk, dn, cat) if x]
            if len(given) != 1:
                raise SchematicValueError("items[%d]: give ONE of like_handle, definition_name or category+group+id." % n)
            if cat and (it.get("group") is None or it.get("id") is None):
                raise SchematicValueError("items[%d]: category needs group and id." % n)
            if cat and not re.match(r"^[A-Za-z0-9_]+$", str(cat)):
                raise SchematicValueError("items[%d]: category must be an enum name or number." % n)
            H.append(h.strip())
            L.append(cs_text(lk, "like_handle", allow_empty=True).strip())
            N.append(cs_text(dn, "definition_name", allow_empty=True))
            C.append(str(cat))
            G.append(cs_int(it.get("group") or 0, "group", minimum=0, maximum=32767))
            I.append(cs_int(it.get("id") or 0, "id", minimum=0, maximum=32767))
        body = _fill(_SET_DEF_BODY, V11H=_cs_lit_array(H), V11LIKE=_cs_lit_array(L), V11DNAME=_cs_lit_array(N),
                     V11CAT=_cs_lit_array(C), V11GRP=_cs_int_array(G), V11ID=_cs_int_array(I),
                     V11DRY=cs_bool(dry_run), V11RB=cs_bool(rollback_on_error),
                     V11GUARDCUR=_guard_current(allow_real_project))
        script = _script(_cls("V11SetDef"), body, extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    raw, ms = _timed(script, timeout_seconds)
    out = _shape_keep(raw, keep=("items", "failedAt", "rolledBack", "rollbackErrors", "project"))
    out["dryRun"] = bool(dry_run)
    counts = {}
    for it in out.get("items") or []:
        k = it.get("result") or it.get("action") or "?"
        counts[k] = counts.get(k, 0) + 1
    out["counts"] = counts
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error and not out.get("rollbackErrors") else "PARTIAL"
        out["error"] = "Setting the definition failed at %s: %s" % (out["failedAt"].get("handle"), out["failedAt"].get("error"))
    if not verbose and out.get("success"):
        out["items"] = ["%s %s: %s -> %s [%s]%s" % (
            it.get("handle"), it.get("name"), (it.get("before") or {}).get("name"), (it.get("after") or {}).get("name"),
            it.get("result") or it.get("action"), (" name now " + it["nameAfter"]) if it.get("nameAfter") else "")
            for it in out.get("items") or []]
    return _timing(out, int((time.perf_counter() - t0) * 1000), {"scriptMs": ms})

# ---------------------------------------------------------------------------
# 6. live_remove_function_articles
# ---------------------------------------------------------------------------

_REMOVE_ART_BODY = r'''
            string[] rH = V11H;
            string[] rPn = V11PN;
            bool all = V11ALL;
            bool dry = V11DRY;
            bool rbk = V11RB;
            bool ignoreMissing = V11IGN;
            Dictionary<string, bool> want = new Dictionary<string, bool>();
            foreach (string h in rH) want[h] = true;
            Dictionary<string, object> byH = new Dictionary<string, object>();
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                string h = Handle(fn);
                if (h != null && want.ContainsKey(h) && !byH.ContainsKey(h)) byH[h] = fn;
                if (byH.Count == want.Count) break;
            }
            List<string> missing = new List<string>();
            foreach (string h in rH) if (!byH.ContainsKey(h)) missing.Add(h);
            if (missing.Count > 0) throw new Exception("Nothing removed: handle(s) not found: " + string.Join(", ", missing.ToArray()));
            List<object> items = new List<object>();
            List<object[]> plan = new List<object[]>();
            List<string> problems = new List<string>();
            foreach (string h in rH)
            {
                object fn = byH[h];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["handle"] = h;
                it["name"] = SafeText(TryRead(fn, "Name", null)) ?? "";
                List<object> before = ArtList(fn);
                it["articlesBefore"] = before;
                List<object> refs = new List<object>();
                object ar = TryRead(fn, "ArticleReferences", null);
                if (ar is IEnumerable) foreach (object r in (IEnumerable)ar) if (r != null) refs.Add(r);
                List<object> rm = new List<object>();
                List<string> hit = new List<string>();
                foreach (object r in refs)
                {
                    string pn = SafeText(TryRead(r, "PartNr", null)) ?? "";
                    bool take = all;
                    foreach (string w in rPn) if (string.Equals(pn, w, StringComparison.OrdinalIgnoreCase)) take = true;
                    if (!take) continue;
                    rm.Add(r);
                    if (!hit.Contains(pn.ToUpperInvariant())) hit.Add(pn.ToUpperInvariant());
                }
                if (!all)
                    foreach (string w in rPn)
                        if (!hit.Contains(w.ToUpperInvariant()) && !ignoreMissing)
                            problems.Add(h + " '" + it["name"] + "' has no article " + w);
                List<string> rmNames = new List<string>();
                foreach (object r in rm) rmNames.Add(SafeText(TryRead(r, "PartNr", null)) ?? "");
                it["remove"] = rmNames;
                it["action"] = rm.Count > 0 ? "remove" : "nothing";
                items.Add(it);
                plan.Add(new object[] { fn, rm, it });
            }
            results["items"] = items;
            if (problems.Count > 0) throw new Exception("Nothing removed: " + string.Join("; ", problems.GetRange(0, Math.Min(20, problems.Count)).ToArray()) + " (ignore_missing=true to skip).");
            if (!dry)
            {
                V11GUARDCUR
                List<object[]> undo = new List<object[]>();
                Dictionary<string, object> failedAt = null;
                int removed = 0;
                foreach (object[] p in plan)
                {
                    object fn = p[0];
                    Dictionary<string, object> it = (Dictionary<string, object>)p[2];
                    foreach (object r in (List<object>)p[1])
                    {
                        string pn = SafeText(TryRead(r, "PartNr", null)) ?? "";
                        string vn = SafeText(TryRead(r, "VariantNr", null)) ?? "1";
                        int cnt = 1;
                        object c0 = TryRead(r, "Count", null);
                        if (c0 != null) { try { cnt = Convert.ToInt32(c0); } catch { } }
                        try
                        {
                            MethodInfo rmM = MethodByShape(fn.GetType(), "RemoveArticleReference", new string[] { "" }, false);
                            if (rmM == null) throw new Exception(fn.GetType().Name + " has no RemoveArticleReference(1). " + MemberList(fn.GetType(), true));
                            Call(rmM, fn, new object[] { r });
                            undo.Add(new object[] { fn, pn, vn, cnt });
                            removed++;
                        }
                        catch (Exception ex)
                        {
                            failedAt = new Dictionary<string, object>();
                            failedAt["handle"] = it["handle"];
                            failedAt["partNr"] = pn;
                            failedAt["error"] = Flatten(ex);
                            break;
                        }
                    }
                    if (failedAt != null) break;
                }
                results["removedCount"] = removed;
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (rbk)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            try
                            {
                                MethodInfo m = MethodByShape(undo[k][0].GetType(), "AddArticleReference", new string[] { "String", "String", "UInt32" }, false);
                                if (m == null) throw new Exception("no AddArticleReference(String, String, UInt32)");
                                Call(m, undo[k][0], new object[] { undo[k][1], undo[k][2], (uint)(int)undo[k][3] });
                                rb++;
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
                foreach (object[] p in plan)
                {
                    Dictionary<string, object> it = (Dictionary<string, object>)p[2];
                    List<object> after = ArtList(p[0]);
                    it["articlesAfter"] = after;
                    bool ok = true;
                    if (failedAt == null)
                        foreach (string pn in (List<string>)it["remove"])
                        {
                            int nb = 0, na = 0;
                            foreach (object a in (List<object>)it["articlesBefore"]) if (string.Equals((string)((Dictionary<string, object>)a)["partNr"], pn, StringComparison.OrdinalIgnoreCase)) nb++;
                            foreach (object a in after) if (string.Equals((string)((Dictionary<string, object>)a)["partNr"], pn, StringComparison.OrdinalIgnoreCase)) na++;
                            if (na >= nb && nb > 0) ok = false;
                        }
                    it["verified"] = ok;
                }
            }
'''


def live_remove_function_articles(handles: list = None, handle: str = None, part_numbers: list = None,
                                  all_articles: bool = False, ignore_missing: bool = False,
                                  dry_run: bool = True, allow_real_project: bool = False,
                                  rollback_on_error: bool = True, allow_unverified_db: bool = False,
                                  verbose: bool = False, timeout_seconds: float = 240.0) -> dict:
    """
    v11: REMOVE article references (parts) from functions -
    Function.RemoveArticleReference (API 2.9), e.g. the combined article of a
    window macro (inbox 2026-11-04_eplan_odstrani_artikel_funkcije.md). Card
    swap = this + live_add_function_articles (or live_set_function_properties
    replace_article for a plain part-number swap). WRITES (unless dry_run, the
    default) - scratch-only unless allow_real_project=True.

    part_numbers=["SIE.6ES7134-6JF00-0CA1+BU15-P16+A10+2B"] (case-insensitive;
    every one must be on every function unless ignore_missing) or
    all_articles=True. Same parts-database preflight as
    live_add_function_articles before a write (no modal dialog). Rollback
    re-adds what this call removed (AddArticleReference(partNr, variant, count)).

    Returns {"success", "dryRun", "removedCount", "counts", "items" (compact:
    "handle name: remove [..], left [..]"; verbose: full before/after),
    "timing"}; failed: ROLLED_BACK / PARTIAL + failedAt.
    """
    t0 = time.perf_counter()
    try:
        hl = _str_list(([handle] if handle else []) + list(handles or []), "handles", 2000)
        hl = list(dict.fromkeys(hl))
        if not hl:
            raise SchematicValueError("Give handle= or handles=[...].")
        pns = _str_list(part_numbers, "part_numbers", 50)
        if bool(pns) == bool(all_articles):
            raise SchematicValueError("Give part_numbers=[...] OR all_articles=true.")
        body = _fill(_REMOVE_ART_BODY, V11H=_cs_lit_array(hl), V11PN=_cs_lit_array(pns), V11ALL=cs_bool(all_articles),
                     V11DRY=cs_bool(dry_run), V11RB=cs_bool(rollback_on_error), V11IGN=cs_bool(ignore_missing),
                     V11GUARDCUR=_guard_current(allow_real_project))
        script = _script(_cls("V11RmArt"), body, extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    if not dry_run:
        blocked = _scripted._parts_db_guard(allow_unverified_db)
        if blocked:
            out = dict(blocked)
            out["error"] = "remove articles NOT run, nothing written. " + str(out.get("error") or out.get("message") or "")
            out["nothingWritten"] = True
            return out
    raw, ms = _timed(script, timeout_seconds)
    out = _shape_keep(raw, keep=("items", "failedAt", "rolledBack", "rollbackErrors", "removedCount", "project"))
    out["dryRun"] = bool(dry_run)
    counts = {}
    for it in out.get("items") or []:
        if isinstance(it, dict):
            counts[it.get("action", "?")] = counts.get(it.get("action", "?"), 0) + 1
    out["counts"] = counts
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error and not out.get("rollbackErrors") else "PARTIAL"
        out["error"] = "Removing %s from %s failed: %s" % (out["failedAt"].get("partNr"), out["failedAt"].get("handle"),
                                                           out["failedAt"].get("error"))
    elif out.get("success") and not dry_run:
        bad = [it.get("handle") for it in out.get("items") or [] if it.get("verified") is False]
        if bad:
            out["warnings"] = ["Read-back still shows a removed article on %s." % ", ".join(bad)]
    if not verbose and out.get("success"):
        def left(it):
            src = it.get("articlesAfter") if "articlesAfter" in it else it.get("articlesBefore")
            return [a.get("partNr") for a in src or []]
        out["items"] = ["%s %s: remove %s, %s %s" % (it.get("handle"), it.get("name"), it.get("remove"),
                                                     "has" if dry_run else "left", left(it))
                        for it in out.get("items") or []]
    return _timing(out, int((time.perf_counter() - t0) * 1000), {"scriptMs": ms})

# ---------------------------------------------------------------------------
# 7. live_set_symbol_variant
# ---------------------------------------------------------------------------

_SET_SV_BODY = r'''
            string[] sH = V11H;
            int[] sV = V11V;
            string[] sLib = V11LIB;
            string[] sSym = V11SYM;
            bool useLogic = V11LOGIC;
            bool dry = V11DRY;
            bool rbk = V11RB;
            Dictionary<string, bool> want = new Dictionary<string, bool>();
            foreach (string h in sH) want[h] = true;
            Dictionary<string, object> byH = new Dictionary<string, object>();
            foreach (object fn in AllFunctions(project))
            {
                if (fn == null) continue;
                string h = Handle(fn);
                if (h != null && want.ContainsKey(h) && !byH.ContainsKey(h)) byH[h] = fn;
                if (byH.Count == want.Count) break;
            }
            List<string> missing = new List<string>();
            foreach (string h in sH) if (!byH.ContainsKey(h)) missing.Add(h);
            if (missing.Count > 0) throw new Exception("Nothing changed: handle(s) not found: " + string.Join(", ", missing.ToArray()));
            List<object> items = new List<object>();
            List<object[]> plan = new List<object[]>();
            List<string> problems = new List<string>();
            for (int i = 0; i < sH.Length; i++)
            {
                object fn = byH[sH[i]];
                Dictionary<string, object> it = new Dictionary<string, object>();
                it["handle"] = sH[i];
                it["name"] = SafeText(TryRead(fn, "Name", null)) ?? "";
                items.Add(it);
                string[] cur = V11SvInfo(fn);
                if (cur == null) { problems.Add(sH[i] + " has no SymbolVariant (not a placed symbol)"); continue; }
                it["before"] = cur[0] + "/" + cur[1] + ":" + cur[2];
                string lib = sLib[i].Length > 0 ? sLib[i] : cur[0];
                string sym = sSym[i].Length > 0 ? sSym[i] : cur[1];
                int nr = 0;
                if (sV[i] >= 0) nr = sV[i];
                else if (!int.TryParse(cur[2], out nr)) { problems.Add(sH[i] + ": current variant '" + cur[2] + "' is not a number - give variant"); continue; }
                it["after"] = lib + "/" + sym + ":" + nr;
                bool same = lib == cur[0] && sym == cur[1] && nr.ToString() == cur[2];
                it["action"] = same ? "unchanged" : "change";
                if (MethodByShape(fn.GetType(), "SetSymbolVariant", new string[] { "SymbolVariant", "Boolean" }, false) == null)
                    problems.Add(sH[i] + ": no SetSymbolVariant(SymbolVariant, Boolean). " + MemberList(fn.GetType(), true));
                plan.Add(new object[] { fn, cur, lib, sym, nr, same, it });
            }
            results["items"] = items;
            if (problems.Count > 0) throw new Exception("Nothing changed: " + string.Join("; ", problems.GetRange(0, Math.Min(20, problems.Count)).ToArray()));
            if (!dry)
            {
                V11GUARDCUR
                List<object[]> undo = new List<object[]>();
                Dictionary<string, object> failedAt = null;
                foreach (object[] p in plan)
                {
                    Dictionary<string, object> it = (Dictionary<string, object>)p[6];
                    if ((bool)p[5]) { it["result"] = "unchanged"; continue; }
                    object fn = p[0];
                    string[] cur = (string[])p[1];
                    try
                    {
                        List<string> c0 = ConnDesignations(FnProps(fn));
                        object nv = V11MakeVariant(project, (string)p[2], (string)p[3], (int)p[4]);
                        MethodInfo m = MethodByShape(fn.GetType(), "SetSymbolVariant", new string[] { "SymbolVariant", "Boolean" }, false);
                        undo.Add(new object[] { fn, cur, it });
                        Call(m, fn, new object[] { nv, useLogic });
                        string[] now = V11SvInfo(fn);
                        string got = now == null ? "" : now[0] + "/" + now[1] + ":" + now[2];
                        if (got != (string)it["after"]) throw new Exception("read back " + got + ", not " + it["after"]);
                        string n1 = SafeText(TryRead(fn, "Name", null)) ?? "";
                        if (n1 != (string)it["name"]) it["nameAfter"] = n1;
                        List<string> c1 = ConnDesignations(FnProps(fn));
                        if (string.Join("|", c0.ToArray()) != string.Join("|", c1.ToArray())) { it["connectionDesignationsBefore"] = c0; it["connectionDesignationsAfter"] = c1; }
                        it["result"] = "changed";
                    }
                    catch (Exception ex)
                    {
                        failedAt = new Dictionary<string, object>();
                        failedAt["handle"] = it["handle"];
                        failedAt["error"] = Flatten(ex);
                        break;
                    }
                }
                if (failedAt != null)
                {
                    results["failedAt"] = failedAt;
                    if (rbk)
                    {
                        int rb = 0;
                        List<string> rbErr = new List<string>();
                        for (int k = undo.Count - 1; k >= 0; k--)
                        {
                            object fn = undo[k][0];
                            string[] was = (string[])undo[k][1];
                            try
                            {
                                string[] now = V11SvInfo(fn);
                                if (now != null && now[0] == was[0] && now[1] == was[1] && now[2] == was[2]) continue;
                                object ov = V11MakeVariant(project, was[0], was[1], Convert.ToInt32(was[2]));
                                Call(MethodByShape(fn.GetType(), "SetSymbolVariant", new string[] { "SymbolVariant", "Boolean" }, false), fn, new object[] { ov, useLogic });
                                rb++;
                                ((Dictionary<string, object>)undo[k][2])["result"] = "rolledBack";
                            }
                            catch (Exception exr) { rbErr.Add(Flatten(exr)); }
                        }
                        results["rolledBack"] = rb;
                        if (rbErr.Count > 0) results["rollbackErrors"] = rbErr;
                    }
                }
            }
'''


def live_set_symbol_variant(items: list = None, handles: list = None, variant: int = None,
                            library: str = None, symbol: str = None, use_logical_definition: bool = False,
                            dry_run: bool = True, allow_real_project: bool = False,
                            rollback_on_error: bool = True, timeout_seconds: float = 300.0) -> dict:
    """
    v11: ANOTHER VARIANT (rotation / mirror / pins down) or ANOTHER SYMBOL for
    placed functions - Function.SetSymbolVariant(SymbolVariant, bool) (API 2.9);
    the function keeps its device tag, properties and articles (inbox
    2026-11-04_eplan_okence_plc_zasuk_variante_simbola.md: PLC window with pins
    down). WRITES (unless dry_run, the default) - scratch-only unless
    allow_real_project=True.

    items=[{"handle", "variant"?, "library"?, "symbol"?}] or handles=[...] with
    variant / library / symbol for all (omitted = keep the current one).
    variant 0..15 (A = 0). use_logical_definition: take the pins / logic from
    the new symbol (SetSymbolVariant 2nd argument; default False keeps the
    function's own). Read back (library/symbol:variant), name and connection
    points reported, rollback on error. live_symbol_catalog lists variants;
    live_list_functions(fields=["handle", "symbol"]) shows the current ones.

    Returns {"success", "dryRun", "items" [{handle, name, before, after, action,
    result, nameAfter, connectionDesignationsAfter}], "counts", "timing"}.
    """
    t0 = time.perf_counter()
    try:
        its = list(items or [])
        for h in _str_list(handles, "handles", 2000):
            its.append({"handle": h, "variant": variant, "library": library, "symbol": symbol})
        if not its:
            raise SchematicValueError("Give items=[...] or handles=[...].")
        H, V, LB, SY = [], [], [], []
        for n, it in enumerate(its):
            if not isinstance(it, dict) or set(it) - {"handle", "variant", "library", "symbol"}:
                raise SchematicValueError("items[%d] must be {handle, variant?, library?, symbol?}." % n)
            h = it.get("handle")
            if not isinstance(h, str) or not h.strip():
                raise SchematicValueError("items[%d]: handle is required." % n)
            if h.strip() in H:
                raise SchematicValueError("items[%d]: handle %s twice." % (n, h))
            v = it.get("variant")
            lb = cs_text(it.get("library") or "", "library", allow_empty=True)
            sy = cs_text(it.get("symbol") or "", "symbol", allow_empty=True)
            if v is None and not lb and not sy:
                raise SchematicValueError("items[%d]: give variant and/or library + symbol." % n)
            H.append(h.strip())
            V.append(-1 if v is None else cs_int(v, "variant", minimum=0, maximum=15))
            LB.append(lb)
            SY.append(sy)
        body = _fill(_SET_SV_BODY, V11H=_cs_lit_array(H), V11V=_cs_int_array(V), V11LIB=_cs_lit_array(LB),
                     V11SYM=_cs_lit_array(SY), V11LOGIC=cs_bool(use_logical_definition), V11DRY=cs_bool(dry_run),
                     V11RB=cs_bool(rollback_on_error), V11GUARDCUR=_guard_current(allow_real_project))
        script = _script(_cls("V11SetSv"), body, extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    raw, ms = _timed(script, timeout_seconds)
    out = _shape_keep(raw, keep=("items", "failedAt", "rolledBack", "rollbackErrors", "project"))
    out["dryRun"] = bool(dry_run)
    counts = {}
    for it in out.get("items") or []:
        k = it.get("result") or it.get("action") or "?"
        counts[k] = counts.get(k, 0) + 1
    out["counts"] = counts
    if out.get("failedAt"):
        out["success"] = False
        out["errorType"] = "ROLLED_BACK" if rollback_on_error and not out.get("rollbackErrors") else "PARTIAL"
        out["error"] = "SetSymbolVariant failed at %s: %s" % (out["failedAt"].get("handle"), out["failedAt"].get("error"))
    return _timing(out, int((time.perf_counter() - t0) * 1000), {"scriptMs": ms})

# ---------------------------------------------------------------------------
# 8. live_list_functions (compact table)
# ---------------------------------------------------------------------------

_LIST_FIELDS = ("handle", "name", "dt", "conn", "page", "type", "box", "main", "def", "defkey",
                "visible", "x", "y", "articles", "symbol", "overview")

_LIST_TABLE_BODY = r'''
            string[] pgs = V11PAGES;
            string pcont = V11PCONT;
            string[] tags = V11TAGS;
            string ncont = V11NCONT;
            string[] types = V11TYPES;
            string[] fields = V11FIELDS;
            string[] pN = V11PN;
            int[] pI = V11PI;
            string kind = V11KIND;
            bool plcOnly = V11PLC;
            int limit = V11LIMIT;
            Dictionary<string, object> info = new Dictionary<string, object>();
            List<object> fns = V11ScopeFunctions(project, pgs, pcont, info);
            foreach (KeyValuePair<string, object> kv in info) results[kv.Key] = kv.Value;
            bool needPg = kind != "all";
            foreach (string f in fields) if (f == "overview") needPg = true;
            Dictionary<string, bool> ovCache = new Dictionary<string, bool>();
            List<object> rows = new List<object>();
            List<string> unknown = new List<string>();
            int matched = 0;
            foreach (object fn in fns)
            {
                string n = SafeText(TryRead(fn, "Name", null)) ?? "";
                if (tags.Length > 0 && !TagHit(n, tags)) continue;
                if (ncont.Length > 0 && !Matches(n, ncont)) continue;
                if (types.Length > 0 && !TypeIn(fn, types)) continue;
                bool ov = false;
                if (needPg) ov = V11IsOverview(TryRead(fn, "Page", null), ovCache);
                if (kind == "schematic" && ov) continue;
                if (kind == "overview" && !ov) continue;
                if (plcOnly && !V11IsPlcPoint(fn)) continue;
                matched++;
                if (rows.Count >= limit) continue;
                object[] r = new object[fields.Length + pN.Length];
                object props = null;
                for (int c = 0; c < fields.Length; c++)
                {
                    string f = fields[c];
                    object v = null;
                    try
                    {
                        if (f == "handle") v = Handle(fn);
                        else if (f == "name") v = n;
                        else if (f == "dt") v = DtPart(n);
                        else if (f == "conn") { if (props == null) props = FnProps(fn); v = string.Join(",", ConnDesignations(props).ToArray()); }
                        else if (f == "page") v = FnPageName(fn);
                        else if (f == "type") v = fn.GetType().Name;
                        else if (f == "box") v = IsBoxPl(fn);
                        else if (f == "main") { object m = TryRead(fn, "IsMainFunction", null); v = m != null && Convert.ToBoolean(m); }
                        else if (f == "def") { Dictionary<string, object> d = V11DefInfo(TryRead(fn, "FunctionDefinition", null)); v = d.ContainsKey("name") ? d["name"] : ""; }
                        else if (f == "defkey") v = V11DefKey(TryRead(fn, "FunctionDefinition", null));
                        else if (f == "visible") v = VisibleDt(fn);
                        else if (f == "x" || f == "y") { double[] l = LocXY(fn); if (l != null) v = Math.Round(f == "x" ? l[0] : l[1], 3); }
                        else if (f == "articles") { List<string> a = new List<string>(); foreach (object o in ArtList(fn)) a.Add((string)((Dictionary<string, object>)o)["partNr"]); v = string.Join(",", a.ToArray()); }
                        else if (f == "symbol") { string[] s = V11SvInfo(fn); v = s == null ? "" : s[0] + "/" + s[1] + ":" + s[2]; }
                        else if (f == "overview") v = ov;
                    }
                    catch (Exception ex) { v = "<error " + Flatten(ex) + ">"; }
                    r[c] = v;
                }
                if (pN.Length > 0)
                {
                    if (props == null) { try { props = FnProps(fn); } catch { props = null; } }
                    for (int c = 0; c < pN.Length; c++)
                    {
                        string t = null;
                        if (props != null)
                        {
                            bool ok = false;
                            try { ok = ReadFnProp(props, pN[c], pI[c], out t); } catch (Exception ex) { t = "<error " + Flatten(ex) + ">"; ok = true; }
                            if (!ok && !unknown.Contains(pN[c])) unknown.Add(pN[c]);
                        }
                        r[fields.Length + c] = t;
                    }
                }
                rows.Add(r);
            }
            results["rows"] = rows;
            results["matched"] = matched;
            if (unknown.Count > 0) results["unknownProperties"] = unknown;
'''


def live_list_functions(pages: list = None, page_contains: str = None, device_tags: list = None,
                        name_contains: str = None, types: list = None,
                        fields: list = None, properties: list = None, page_kind: str = "all",
                        plc_points_only: bool = False, display_text: bool = True, limit: int = 2000,
                        timeout_seconds: float = 300.0) -> dict:
    """
    v11: COMPACT TABLE of functions - one header ("columns") and one array per
    function ("rows"), only the columns asked for. READ-ONLY. Meant for many
    cards / pages per call (live_read_page / live_read_function_properties
    repeat keys and geometry per object).

    Scope: pages=[...] | page_contains="+ET-1/" | whole project; filters
    device_tags=[...] (name / device tag / "510S1"), name_contains, types
    (CLR type, e.g. ["PLC", "Terminal"]), page_kind "all" / "schematic" /
    "overview", plc_points_only.
    fields (default handle, name, conn, page): handle, name, dt (device tag),
    conn (connection point designations), page, type, box, main, def (function
    definition name), defkey (category/group/id), visible (displayed tag), x, y,
    articles, symbol (library/symbol:variant), overview (on an overview page).
    properties=["FUNC_PLCADDRESS", "FUNC_TEXT_AUTOMATIC",
    "FUNC_CONNECTIONDESIGNATION[1]"] add property columns (MultiLangString
    shown as text when display_text).

    Returns {"success", "columns", "rows", "matched", "returned", "truncated",
    "unknownProperties", "timing"}.
    """
    t0 = time.perf_counter()
    try:
        fl = _str_list(fields, "fields", 30) or ["handle", "name", "conn", "page"]
        bad = [f for f in fl if f not in _LIST_FIELDS]
        if bad:
            raise SchematicValueError("Unknown field(s) %s (allowed: %s)." % (bad, ", ".join(_LIST_FIELDS)))
        props = _lp._parse_props(properties, "properties") if properties else []
        if page_kind not in ("all", "schematic", "overview"):
            raise SchematicValueError("page_kind must be 'all', 'schematic' or 'overview'.")
        lim = cs_int(limit, "limit", minimum=1, maximum=50000)
        body = _fill(_LIST_TABLE_BODY, V11PAGES=_cs_lit_array(_str_list(pages, "pages", 2000)),
                     V11PCONT=_cs_lit(cs_text(page_contains, "page_contains", allow_empty=True)),
                     V11TAGS=_cs_lit_array(_str_list(device_tags, "device_tags", 2000)),
                     V11NCONT=_cs_lit(cs_text(name_contains, "name_contains", allow_empty=True)),
                     V11TYPES=_cs_lit_array(_str_list(types, "types", 50)),
                     V11FIELDS=_cs_lit_array(fl), V11PN=_cs_lit_array([p[0] for p in props]),
                     V11PI=_cs_int_array([p[1] for p in props]), V11KIND='"%s"' % page_kind,
                     V11PLC=cs_bool(plc_points_only), V11LIMIT=str(lim))
        script = _script(_cls("V11ListTbl"), body, extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    raw, ms = _timed(script, timeout_seconds)
    out = _shape(raw)
    if out.get("success"):
        cols = list(fl) + [("%s[%d]" % p) if p[1] else p[0] for p in props]
        rows = out.get("rows") or []
        if display_text and props:
            for r in rows:
                for c in range(len(fl), len(r)):
                    d, _ = multilang_display(r[c])
                    if d is not None:
                        r[c] = d
        out["columns"] = cols
        out["returned"] = len(rows)
        out["truncated"] = (out.get("matched") or 0) > len(rows)
        out.pop("project", None)
    return _timing(out, int((time.perf_counter() - t0) * 1000), {"scriptMs": ms})


# ---------------------------------------------------------------------------
# 9. live_check_connections
# ---------------------------------------------------------------------------

_CHECK_BODY = r'''
            string[] pgs = V11PAGES;
            string pcont = V11PCONT;
            bool cDup = V11DUP;
            bool cPlc = V11PLCCHK;
            bool cUnc = V11UNC;
            int lim = V11LIMIT;
            Dictionary<string, object> info = new Dictionary<string, object>();
            List<object> fns = V11ScopeFunctions(project, pgs, pcont, info);
            foreach (KeyValuePair<string, object> kv in info) results[kv.Key] = kv.Value;
            results["functionsChecked"] = fns.Count;
            Dictionary<string, bool> ovCache = new Dictionary<string, bool>();
            Dictionary<string, List<string>> dupKeys = new Dictionary<string, List<string>>();
            Dictionary<string, object[]> plc = new Dictionary<string, object[]>(StringComparer.OrdinalIgnoreCase);
            List<object> noConn = new List<object>();
            int noConnN = 0;
            foreach (object fn in fns)
            {
                string n = SafeText(TryRead(fn, "Name", null)) ?? "";
                if (n.Length == 0 || n == "+" || n == "=") continue;
                object pg = TryRead(fn, "Page", null);
                bool ov = V11IsOverview(pg, ovCache);
                string pn = pg == null ? "" : (PropText(pg, "Name") ?? "");
                string h = Handle(fn) ?? "";
                if (cDup && !ov && !IsBoxPl(fn))
                {
                    string conns = "";
                    try { conns = string.Join(",", ConnDesignations(FnProps(fn)).ToArray()); } catch { }
                    string key = n + "|" + conns;
                    List<string> l;
                    if (!dupKeys.TryGetValue(key, out l)) { l = new List<string>(); dupKeys[key] = l; }
                    l.Add(h + " @" + pn);
                }
                if (cPlc && V11IsPlcPoint(fn))
                {
                    object[] e;
                    if (!plc.TryGetValue(n, out e)) { e = new object[] { 0, 0, new List<string>(), new List<string>(), new List<string>() }; plc[n] = e; }
                    object fd = TryRead(fn, "FunctionDefinition", null);
                    Dictionary<string, object> di = V11DefInfo(fd);
                    string dk = V11DefKey(fd) + " " + (di.ContainsKey("name") ? (string)di["name"] : "");
                    if (ov) { e[1] = (int)e[1] + 1; if (!((List<string>)e[3]).Contains(dk)) ((List<string>)e[3]).Add(dk); }
                    else { e[0] = (int)e[0] + 1; if (!((List<string>)e[2]).Contains(dk)) ((List<string>)e[2]).Add(dk); }
                    ((List<string>)e[4]).Add(h + (ov ? " overview @" : " @") + pn);
                }
                if (cUnc && !ov && !IsBoxPl(fn) && PinCount(fn) > 0)
                {
                    if (V11Count(TryRead(fn, "Connections", null)) == 0)
                    {
                        noConnN++;
                        if (noConn.Count < lim) noConn.Add(h + " " + n + " @" + pn);
                    }
                }
            }
            if (cDup)
            {
                List<object> d = new List<object>();
                int dn = 0;
                foreach (KeyValuePair<string, List<string>> kv in dupKeys)
                {
                    if (kv.Value.Count < 2) continue;
                    dn++;
                    if (d.Count < lim) d.Add(kv.Key.Replace("|", " pins ") + ": " + string.Join("; ", kv.Value.ToArray()));
                }
                results["duplicateCount"] = dn;
                results["duplicates"] = d;
            }
            if (cPlc)
            {
                List<object> so = new List<object>();
                List<object> oo = new List<object>();
                List<object> mm = new List<object>();
                int soN = 0, ooN = 0, mmN = 0;
                foreach (KeyValuePair<string, object[]> kv in plc)
                {
                    object[] e = kv.Value;
                    int s = (int)e[0], o = (int)e[1];
                    List<string> where = (List<string>)e[4];
                    if (s > 0 && o == 0) { soN++; if (so.Count < lim) so.Add(kv.Key + ": " + string.Join("; ", where.ToArray())); }
                    else if (o > 0 && s == 0) { ooN++; if (oo.Count < lim) oo.Add(kv.Key); }
                    else if (s > 0 && o > 0)
                    {
                        List<string> a = (List<string>)e[2];
                        List<string> b = (List<string>)e[3];
                        bool same = a.Count == 1 && b.Count == 1 && a[0].Split(' ')[0] == b[0].Split(' ')[0];
                        if (!same) { mmN++; if (mm.Count < lim) mm.Add(kv.Key + ": schematic [" + string.Join(" | ", a.ToArray()) + "] overview [" + string.Join(" | ", b.ToArray()) + "] " + string.Join("; ", where.ToArray())); }
                    }
                }
                results["plcPoints"] = plc.Count;
                results["schematicOnlyCount"] = soN;
                results["schematicOnly"] = so;
                results["overviewOnlyCount"] = ooN;
                results["overviewOnly"] = oo;
                results["definitionMismatchCount"] = mmN;
                results["definitionMismatch"] = mm;
            }
            if (cUnc) { results["noConnectionsCount"] = noConnN; results["noConnections"] = noConn; }
'''

_CHECKS = ("duplicates", "plc", "unconnected")


def live_check_connections(pages: list = None, page_contains: str = None, checks: list = None,
                           generate_first: bool = False, limit: int = 50,
                           timeout_seconds: float = 600.0) -> dict:
    """
    v11: CHECK after copy / paste - READ-ONLY (generate_first runs EPLAN's
    "generate connections" action first, which updates the project's
    connections). One call, compact counts + short lists.

    checks (default all):
      "duplicates"  functions on schematic (non-overview) pages with the SAME
                    name AND connection points (e.g. two "ET-1.3:11") - boxes
                    left out
      "plc"         PLC connection points per name: "schematicOnly" (a point
                    without a channel on an overview / card page - no cross
                    reference), "overviewOnly" (unused card channels - often
                    normal), "definitionMismatch" (schematic point and card
                    channel have different function definitions: the case of
                    inbox 2026-11-05 - fix with live_set_function_definition
                    like_handle = the card channel)
      "unconnected" symbols with pins on schematic pages without any
                    connection (Function.Connections empty)
    Scope: pages=[...] | page_contains | whole project.

    Returns {"success", "functionsChecked", "duplicateCount", "duplicates",
    "plcPoints", "schematicOnlyCount", "schematicOnly", "overviewOnlyCount",
    "overviewOnly", "definitionMismatchCount", "definitionMismatch",
    "noConnectionsCount", "noConnections", "generate", "timing"}. Lists hold
    "handle @page" so the next call can fix them.
    """
    t0 = time.perf_counter()
    try:
        ck = _str_list(checks, "checks", 3) or list(_CHECKS)
        bad = [c for c in ck if c not in _CHECKS]
        if bad:
            raise SchematicValueError("Unknown check(s) %s (allowed: %s)." % (bad, ", ".join(_CHECKS)))
        pl = _str_list(pages, "pages", 2000)
        pc = cs_text(page_contains, "page_contains", allow_empty=True)
        lim = cs_int(limit, "limit", minimum=1, maximum=5000)
        body = _fill(_CHECK_BODY, V11PAGES=_cs_lit_array(pl), V11PCONT=_cs_lit(pc),
                     V11DUP=cs_bool("duplicates" in ck), V11PLCCHK=cs_bool("plc" in ck),
                     V11UNC=cs_bool("unconnected" in ck), V11LIMIT=str(lim))
        script = _script(_cls("V11Check"), body, extra_helpers=_ALL_HELPERS)
    except (SchematicValueError, TypeError, ValueError) as exc:
        return _err(exc)
    extra = {}
    gen = None
    if generate_first:
        from . import generate as _generate
        tg = time.perf_counter()
        gen = _generate.generate_connections(page_names=pl or None)
        extra["generateMs"] = int((time.perf_counter() - tg) * 1000)
    raw, ms = _timed(script, timeout_seconds)
    out = _shape(raw)
    if gen is not None:
        ok = bool(isinstance(gen, dict) and gen.get("success"))
        out["generate"] = {"success": ok}
        if not ok:
            out["generate"]["error"] = (gen.get("error") or gen.get("message")) if isinstance(gen, dict) else str(gen)
    out.pop("project", None)
    extra["scriptMs"] = ms
    return _timing(out, int((time.perf_counter() - t0) * 1000), extra)