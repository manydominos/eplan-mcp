"""
Parts database checks for the live writers that touch articles, without
`using Eplan.EplApi.MasterData;` (EPLAN 2.9 and later).

EPLAN 2.9's script compiler does not reference the MasterData assembly, so a
script that names MDPartsManagement / MDPart statically fails with CS0234
("namespace Eplan.EplApi.MasterData does not exist"). The scripts here resolve
the types at RUN time from the loaded (or loadable) assembly and call them by
reflection, so they compile on 2.9 and on newer versions alike.

Used by live_props.live_set_function_properties (replace_article),
live_props.live_add_function_articles and live_copy.live_remove_function_articles:

    parts_db_guard(allow_unverified_db)  None = the parts database can be
        opened without a modal dialog; otherwise the refusal to return. The
        preflight (preflight_script) never opens the database: it reads
        PartsService.PartsDatabase and checks that the 64-bit ACE OLE DB
        provider is registered for an Access database - on EPLAN 2.9 64-bit
        without it, opening the database shows the MODAL "Microsoft Office 64
        bits is required ..." and blocks EPLAN until someone clicks OK.
    article_in_db(part_nr, ...)  True / False when the database answered,
        None when it could not tell.

The parts_db_* tools in scripted.py are unchanged; this module does not
replace them.
"""

import os
import re
import uuid

from ._base import cs_escape


# Static helpers shared by the four read scripts. C# 5 only. Every
# GetProperty call pins Type.EmptyTypes (ARTICLE_* members are declared plain
# AND [int]-indexed, so a bare lookup throws AmbiguousMatchException).
_HELPERS = r'''
    static string Flatten(Exception ex)
    {
        StringBuilder sb = new StringBuilder();
        Exception e = ex;
        int depth = 0;
        while (e != null && depth < 6)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name);
            sb.Append(": ");
            sb.Append(e.Message);
            e = e.InnerException;
            depth++;
        }
        return sb.ToString();
    }

    // MasterData is usually NOT loaded until something needs it (on 2025.0.3
    // MasterDatau is lazy, see introspect.api_types). Loaded set first, then
    // Assembly.Load by name, then LoadFrom the EPLAN Bin folder.
    static Assembly[] _asms;
    static Type FindMdType(string fullName)
    {
        if (_asms == null) _asms = AppDomain.CurrentDomain.GetAssemblies();
        foreach (Assembly a in _asms)
        {
            try { Type t = a.GetType(fullName); if (t != null) return t; } catch { }
        }
        string[] names = new string[] {
            "Eplan.EplApi.MasterDatau", "Eplan.EplApi.MasterDataNetu", "Eplan.EplApi.MasterData" };
        List<string> tried = new List<string>();
        foreach (string n in names)
        {
            Assembly a = null;
            try { a = Assembly.Load(n); }
            catch (Exception exLoad)
            {
                tried.Add(n + " (Load: " + exLoad.GetType().Name + ")");
                try
                {
                    string p = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, n + ".dll");
                    if (File.Exists(p)) a = Assembly.LoadFrom(p);
                }
                catch (Exception exFrom) { tried.Add(n + " (LoadFrom: " + exFrom.GetType().Name + ")"); }
            }
            if (a == null) continue;
            try
            {
                Type t = a.GetType(fullName);
                if (t != null) { _asms = AppDomain.CurrentDomain.GetAssemblies(); return t; }
                tried.Add(n + " (loaded, type absent)");
            }
            catch { }
        }
        throw new Exception("Could not resolve " + fullName + ". Tried: " +
            string.Join(", ", tried.ToArray()));
    }

    static PropertyInfo FindUnambiguous(Type t, string name)
    {
        BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.DeclaredOnly;
        Type cur = t;
        while (cur != null)
        {
            PropertyInfo pi = null;
            try { pi = cur.GetProperty(name, bf, null, null, Type.EmptyTypes, null); } catch { }
            if (pi != null && pi.CanRead) return pi;
            cur = cur.BaseType;
        }
        return null;
    }

    // Flatten to a STRING before it goes into results: an MDPropertyValue in
    // the results dict hangs the JSON serialiser (see parts_db_get_part).
    static string Str(object value)
    {
        if (value == null) return "";
        try { string s = value.ToString(); return s == null ? "" : s; }
        catch { return ""; }
    }

    static readonly Dictionary<string, string> FriendlyToArticle = BuildFriendlyMap();

    static Dictionary<string, string> BuildFriendlyMap()
    {
        var m = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        m["Description1"] = "ARTICLE_DESCR1";
        m["Description2"] = "ARTICLE_DESCR2";
        m["Description3"] = "ARTICLE_DESCR3";
        m["Manufacturer"] = "ARTICLE_MANUFACTURER";
        m["Supplier"] = "ARTICLE_SUPPLIER";
        m["OrderNr"] = "ARTICLE_ORDERNR";
        m["TypeNr"] = "ARTICLE_TYPENR";
        return m;
    }

    // 1. a member of MDPart itself (PartNr, ProductGroup, ...),
    // 2. a field of its property list (ARTICLE_*, or a friendly alias).
    static string ReadPartProperty(object part, string propName)
    {
        PropertyInfo direct = FindUnambiguous(part.GetType(), propName);
        if (direct != null && direct.Name != "Properties")
            return Str(direct.GetValue(part, null));

        string articleName = propName;
        if (FriendlyToArticle.ContainsKey(propName)) articleName = FriendlyToArticle[propName];

        // Most-derived "Properties" = MDPartsDatabaseItemPropertyList.
        PropertyInfo plp = FindUnambiguous(part.GetType(), "Properties");
        object pl = plp == null ? null : plp.GetValue(part, null);
        if (pl != null)
        {
            PropertyInfo listProp = FindUnambiguous(pl.GetType(), articleName);
            if (listProp != null) return Str(listProp.GetValue(pl, null));
        }
        throw new MissingMemberException(
            "No property '" + propName + "' on MDPart or its property list " +
            "(tried '" + articleName + "'). Descriptive fields are ARTICLE_*-" +
            "prefixed; PartNr/ProductGroup/ProductSubGroup/GenericProductGroup " +
            "are members of MDPart itself.");
    }

    static object OpenDb()
    {
        Type mgmtType = FindMdType("Eplan.EplApi.MasterData.MDPartsManagement");
        object mgmt = Activator.CreateInstance(mgmtType);
        MethodInfo open = mgmtType.GetMethod("OpenDatabase", Type.EmptyTypes);
        if (open == null) throw new Exception("MDPartsManagement has no OpenDatabase().");
        object db;
        try { db = open.Invoke(mgmt, null); }
        catch (TargetInvocationException tie) { throw new Exception("OpenDatabase: " + Flatten(tie.InnerException)); }
        if (db == null) throw new Exception("OpenDatabase() returned null.");
        return db;
    }

    static void CloseDb(object db)
    {
        IDisposable d = db as IDisposable;
        if (d != null) { try { d.Dispose(); } catch { } }
    }

    static IEnumerable PartsOf(object db)
    {
        PropertyInfo pp = FindUnambiguous(db.GetType(), "Parts");
        if (pp == null) throw new Exception("MDPartsDatabase has no readable Parts.");
        object v = pp.GetValue(db, null);
        if (v == null) throw new Exception("MDPartsDatabase.Parts returned null.");
        return (IEnumerable)v;
    }

    // Substring filter on a MEMBER of MDPart (as the old LINQ .Where did).
    static bool PassesFilter(object part, string filterProp, string filterValue)
    {
        if (filterProp.Length == 0) return true;
        PropertyInfo fpi = FindUnambiguous(part.GetType(), filterProp);
        if (fpi == null)
            throw new Exception("filter_property '" + filterProp + "' is not a member of " +
                part.GetType().Name + " (filter on PartNr, ProductGroup, ProductSubGroup, Variant ...).");
        return Str(fpi.GetValue(part, null)).Contains(filterValue);
    }
'''

_HEAD = '''using System;
using System.IO;
using System.Text;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Eplan.EplApi.Scripting;

public class '''

_TAIL = '''
        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''


def _wrap(cls_prefix, run_body):
    return (_HEAD + cls_prefix + "_" + uuid.uuid4().hex[:6] + "\n{" + _HELPERS +
            "\n    [Start]\n    public void Run()\n    {\n"
            "        var results = new Dictionary<string, object>();\n" +
            run_body + _TAIL)


# Output keys of parts_db_get_part -> where the value lives. Unchanged shape.
_GET_PART_FIELDS = (
    ("PartNr", "ARTICLE_PARTNR"),
    ("Description1", "ARTICLE_DESCR1"),
    ("Description2", "ARTICLE_DESCR2"),
    ("Description3", "ARTICLE_DESCR3"),
    ("Manufacturer", "ARTICLE_MANUFACTURER"),
    ("Supplier", "ARTICLE_SUPPLIER"),
    ("OrderNr", "ARTICLE_ORDERNR"),
    ("ProductGroup", "ProductGroup"),
    ("ProductSubGroup", "ProductSubGroup"),
    # MDPart has NO "ProductTopGroup" member (that is the enum TYPE); the
    # member is GenericProductGroup.
    ("GenericProductGroup", "GenericProductGroup"),
)


def get_part_script(part_number):
    keys = ", ".join('"%s"' % k for k, _ in _GET_PART_FIELDS)
    srcs = ", ".join('"%s"' % s for _, s in _GET_PART_FIELDS)
    body = '''        object db = null;
        try
        {
            string wanted = "%s";
            string[] outKeys = new string[] { %s };
            string[] srcNames = new string[] { %s };
            db = OpenDb();
            object part = null;
            MethodInfo getPart = db.GetType().GetMethod("GetPart", new Type[] { typeof(string) });
            if (getPart != null)
            {
                try { part = getPart.Invoke(db, new object[] { wanted }); } catch { part = null; }
            }
            if (part == null)
            {
                foreach (object p in PartsOf(db))
                {
                    if (p == null) continue;
                    PropertyInfo pn = FindUnambiguous(p.GetType(), "PartNr");
                    if (pn != null && Str(pn.GetValue(p, null)) == wanted) { part = p; break; }
                }
            }
            if (part != null)
            {
                var partDict = new Dictionary<string, object>();
                for (int i = 0; i < outKeys.Length; i++)
                {
                    try { partDict[outKeys[i]] = ReadPartProperty(part, srcNames[i]); }
                    catch (Exception exProp) { partDict[outKeys[i]] = "<error: " + exProp.GetType().Name + ">"; }
                }
                results["success"] = true;
                results["found"] = true;
                results["part"] = partDict;
            }
            else
            {
                results["success"] = true;
                results["found"] = false;
            }
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }
        finally { if (db != null) CloseDb(db); }
''' % (cs_escape(part_number), keys, srcs)
    return _wrap("PartsGet", body)


# =============================================================================
# v4: preflight - which parts database, can EPLAN open it? (never opens it)
# =============================================================================
# inbox 2026-10-28_eplan_parts_db_blokira_streznik.md: on EPLAN 2.9 64-bit
# without 64-bit Office, OpenDatabase() shows the MODAL "...ESS_part001.mdb
# database cannot be opened. Microsoft Office 64 bits is required for a 64-bit
# version of the EPLAN Platform." and the script (and the MCP server) waited
# for a click. This script reads only:
#   - HEServices.PartsService.PartsDatabase (string; "the complete filename of
#     the current parts database or the connection string if an SQL
#     connection is selected" - eplan.help API 2.9), path variables resolved
#     with Base.PathMap.SubstitutePath, both by reflection;
#   - the OLE DB providers Microsoft.ACE.OLEDB.12.0 / 16.0 in the registry view
#     of the EPLAN process (64-bit view for 64-bit EPLAN): ProgID -> CLSID ->
#     InprocServer32 -> the DLL must exist. A ProgID alone is not enough: the
#     ProgID key is shared by both registry views, the CLSID is not, so a
#     32-bit ACE would otherwise look available.
# MDPartsManagement / MasterData is not touched.

_PREFLIGHT = r'''using System;
using System.IO;
using System.Text;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Microsoft.Win32;
using Eplan.EplApi.Scripting;

public class PartsPreflight_CLSID
{
    static string Flatten(Exception ex)
    {
        StringBuilder sb = new StringBuilder();
        Exception e = ex;
        int depth = 0;
        while (e != null && depth < 6)
        {
            if (sb.Length > 0) sb.Append(" <- ");
            sb.Append(e.GetType().Name);
            sb.Append(": ");
            sb.Append(e.Message);
            e = e.InnerException;
            depth++;
        }
        return sb.ToString();
    }

    static Type FindLoadedType(string fullName, string[] loadNames)
    {
        foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
        {
            try { Type t = a.GetType(fullName); if (t != null) return t; } catch { }
        }
        foreach (string n in loadNames)
        {
            try
            {
                Assembly a = Assembly.Load(n);
                if (a == null) continue;
                Type t = a.GetType(fullName);
                if (t != null) return t;
            }
            catch { }
        }
        return null;
    }

    static Dictionary<string, object> Provider(RegistryKey root, string progId)
    {
        Dictionary<string, object> d = new Dictionary<string, object>();
        d["progId"] = progId;
        d["available"] = false;
        try
        {
            string clsid = null;
            using (RegistryKey k = root.OpenSubKey(progId + "\\CLSID"))
            {
                if (k != null) clsid = k.GetValue("") as string;
            }
            d["registered"] = clsid != null;
            if (clsid == null) return d;
            d["clsid"] = clsid;
            string dll = null;
            using (RegistryKey s = root.OpenSubKey("CLSID\\" + clsid + "\\InprocServer32"))
            {
                if (s != null) dll = s.GetValue("") as string;
            }
            d["inprocServer"] = dll == null ? "" : dll;
            if (dll == null) return d;
            bool exists = false;
            try { exists = File.Exists(Environment.ExpandEnvironmentVariables(dll)); } catch { }
            d["dllExists"] = exists;
            d["available"] = exists;
        }
        catch (Exception ex) { d["error"] = Flatten(ex); }
        return d;
    }

    [Start]
    public void Run()
    {
        var results = new Dictionary<string, object>();
        results["is64BitProcess"] = Environment.Is64BitProcess;
        string db = null;
        try
        {
            Type ps = FindLoadedType("Eplan.EplApi.HEServices.PartsService",
                new string[] { "Eplan.EplApi.HEServicesu", "Eplan.EplApi.HEServicesNetu" });
            if (ps == null) throw new Exception("type Eplan.EplApi.HEServices.PartsService not found");
            object o = Activator.CreateInstance(ps);
            try
            {
                PropertyInfo pi = ps.GetProperty("PartsDatabase", BindingFlags.Public | BindingFlags.Instance,
                    null, typeof(string), Type.EmptyTypes, null);
                if (pi == null) throw new Exception("PartsService has no string property PartsDatabase");
                db = pi.GetValue(o, null) as string;
            }
            finally
            {
                IDisposable disp = o as IDisposable;
                if (disp != null) { try { disp.Dispose(); } catch { } }
            }
            results["database"] = db == null ? "" : db;
        }
        catch (Exception ex) { results["databaseError"] = Flatten(ex); }

        string resolved = db;
        if (db != null && db.IndexOf("$(") >= 0)
        {
            try
            {
                Type pm = FindLoadedType("Eplan.EplApi.Base.PathMap",
                    new string[] { "Eplan.EplApi.Baseu", "Eplan.EplApi.BaseNetu" });
                MethodInfo sub = pm == null ? null : pm.GetMethod("SubstitutePath",
                    BindingFlags.Public | BindingFlags.Static, null, new Type[] { typeof(string) }, null);
                if (sub == null) results["pathMapError"] = "PathMap.SubstitutePath(string) not found";
                else
                {
                    resolved = sub.Invoke(null, new object[] { db }) as string;
                    results["databaseResolved"] = resolved == null ? "" : resolved;
                }
            }
            catch (Exception ex) { results["pathMapError"] = Flatten(ex); }
        }
        if (!string.IsNullOrEmpty(resolved) && resolved.IndexOf('=') < 0)
        {
            try { results["databaseFileExists"] = File.Exists(resolved.Trim().Trim('"')); } catch { }
        }

        List<object> providers = new List<object>();
        try
        {
            RegistryKey root = RegistryKey.OpenBaseKey(RegistryHive.ClassesRoot,
                Environment.Is64BitProcess ? RegistryView.Registry64 : RegistryView.Registry32);
            try
            {
                providers.Add(Provider(root, "Microsoft.ACE.OLEDB.12.0"));
                providers.Add(Provider(root, "Microsoft.ACE.OLEDB.16.0"));
            }
            finally { root.Close(); }
        }
        catch (Exception ex) { results["registryError"] = Flatten(ex); }
        results["aceProviders"] = providers;
        results["success"] = true;

        string json = Newtonsoft.Json.JsonConvert.SerializeObject(results);
        File.WriteAllText(@"{{RESULT_PATH}}", json);
    }
}
'''


def preflight_script():
    """C# that reports the configured parts database and the ACE providers."""
    return _PREFLIGHT.replace("PartsPreflight_CLSID", "PartsPreflight_" + uuid.uuid4().hex[:6])


_ACCESS_EXT = (".mdb", ".accdb")
_SQL_HINT = re.compile(
    r"(?i)(?:^|;)\s*(data source|server|initial catalog|database|provider|"
    r"user id|integrated security|trusted_connection)\s*=")

_HINT_ACCESS = (
    "On the EPLAN machine install 64-bit Microsoft Office or the 64-bit "
    "'Microsoft Access Database Engine' (ACE OLEDB), or point EPLAN to an SQL "
    "parts database (Options > Settings > User > Management > Parts management). "
    "Until then the parts_db_* tools refuse instead of opening the database.")


def classify_parts_db(raw):
    """
    Verdict from the preflight envelope (_execute_script's return value).

    {"verdict": "ok" | "unavailable" | "unknown" | "not_connected",
     "kind": "access" | "sql" | "eplan" | None, "database", "reason", ...}
    "unavailable" is definitive (EPLAN would show the modal dialog);
    "unknown" means the check could not decide.
    """
    out = {"verdict": "unknown", "kind": None, "database": None, "reason": None}
    if not isinstance(raw, dict):
        out["reason"] = "unexpected preflight result: %r" % (raw,)
        return out
    info = raw.get("results") if isinstance(raw.get("results"), dict) else None
    if info is None:
        msg = str(raw.get("message") or raw.get("error") or "")
        if "not connected" in msg.lower():
            out["verdict"] = "not_connected"
            out["response"] = raw
            return out
        out["reason"] = "the preflight script gave no result: %s" % (raw.get("error") or msg or "?")
        out["preflight"] = {k: raw[k] for k in ("errorType", "message", "compile_errors") if k in raw}
        return out

    setting = info.get("database") or ""
    db = info.get("databaseResolved") or setting
    out["database"] = db or None
    if setting and setting != db:
        out["databaseSetting"] = setting
    out["is64BitProcess"] = info.get("is64BitProcess")
    providers = [p for p in (info.get("aceProviders") or []) if isinstance(p, dict)]
    out["aceProviders"] = providers
    for key in ("databaseError", "pathMapError", "registryError"):
        if info.get(key):
            out[key] = info[key]

    if not db:
        out["reason"] = "EPLAN did not report the parts database (%s)." % (
            info.get("databaseError") or "empty setting")
        return out

    path = db.strip().strip('"')
    ext = os.path.splitext(path)[1].lower()
    if ext in _ACCESS_EXT:
        out["kind"] = "access"
        if info.get("databaseFileExists") is False:
            out["verdict"] = "unavailable"
            out["reason"] = ("the Access parts database file does not exist: %s - EPLAN "
                             "would show 'database cannot be opened'." % path)
            return out
        if info.get("is64BitProcess") is False:
            out["reason"] = "32-bit EPLAN process: the 64-bit ACE check does not apply."
            return out
        if "registryError" in info:
            out["reason"] = "the OLE DB provider registry could not be read: %s" % info["registryError"]
            return out
        available = [p.get("progId") for p in providers if p.get("available")]
        if available:
            out["verdict"] = "ok"
            out["reason"] = "64-bit provider registered for EPLAN: %s" % ", ".join(available)
        else:
            out["verdict"] = "unavailable"
            out["reason"] = (
                "Access parts database, but no 64-bit Microsoft Access Database Engine "
                "(Microsoft.ACE.OLEDB.12.0 / 16.0) is registered for the EPLAN process. "
                "Opening it makes EPLAN show the MODAL message 'Microsoft Office 64 bits "
                "is required for a 64-bit version of the EPLAN Platform' and block until "
                "someone clicks OK.")
        return out
    if _SQL_HINT.search(path):
        out["kind"] = "sql"
        out["verdict"] = "ok"
        out["reason"] = "SQL Server parts database (no Access provider needed)."
        return out
    if ext == ".alk":
        out["kind"] = "eplan"
        if info.get("databaseFileExists") is False:
            out["verdict"] = "unavailable"
            out["reason"] = "the parts database file does not exist: %s" % path
        else:
            out["verdict"] = "ok"
            out["reason"] = "EPLAN parts database (.alk)."
        return out
    out["reason"] = "unrecognised parts database type: %s" % path
    return out


def guard_response(verdict, allow_unverified_db=False):
    """None = open the database; else the refusal (nothing was opened)."""
    v = verdict.get("verdict")
    if v == "ok":
        return None
    if v == "not_connected":
        return verdict.get("response") or {"success": False, "message": "Not connected"}
    if v == "unknown" and allow_unverified_db:
        return None
    details = {k: verdict[k] for k in ("database", "databaseSetting", "kind", "is64BitProcess",
                                       "aceProviders", "databaseError", "pathMapError",
                                       "registryError", "preflight") if verdict.get(k) is not None}
    if v == "unavailable":
        return dict({
            "success": False,
            "errorType": "PARTS_DB_UNAVAILABLE",
            "error": "Parts database NOT opened (it would block EPLAN with a modal "
                     "dialog): " + (verdict.get("reason") or ""),
            "partsDatabase": verdict.get("database"),
            "reason": verdict.get("reason"),
            "hint": _HINT_ACCESS if verdict.get("kind") == "access" else
                    "Fix the parts database setting in EPLAN, then call again.",
        }, **{"preflight": details})
    return dict({
        "success": False,
        "errorType": "PARTS_DB_UNVERIFIED",
        "error": "Parts database NOT opened: it could not be verified that EPLAN can "
                 "open it without a modal dialog - " + (verdict.get("reason") or ""),
        "partsDatabase": verdict.get("database"),
        "reason": verdict.get("reason"),
        "hint": "Call again with allow_unverified_db=true to try anyway. If EPLAN then "
                "shows a dialog, the call ends with EplanBlockedByDialog and the user has "
                "to answer it in EPLAN (eplan_list_dialogs / eplan_click_dialog_button).",
    }, **{"preflight": details})


# =============================================================================
# the guard and the article lookup used by the live writers
# =============================================================================

_PREFLIGHT_TTL_S = 30.0
_preflight_cache = {"at": 0.0, "verdict": None}


def _run(script):
    # Looked up per call so the offline tests' stub of scripted._execute_script applies.
    from . import scripted
    return scripted._execute_script(script)


def parts_db_preflight():
    """Verdict about the configured parts database (see classify_parts_db).

    Only definitive answers ("ok" / "unavailable") are cached, for 30 s: an
    article import checks one part number after another.
    """
    import time
    now = time.time()
    cached = _preflight_cache.get("verdict")
    if cached is not None and now - _preflight_cache.get("at", 0.0) < _PREFLIGHT_TTL_S:
        return cached
    verdict = classify_parts_db(_run(preflight_script()))
    if verdict.get("verdict") in ("ok", "unavailable"):
        _preflight_cache["at"] = now
        _preflight_cache["verdict"] = verdict
    return verdict


def parts_db_guard(allow_unverified_db=False):
    """None = go ahead; otherwise the refusal to return instead of opening the DB."""
    return guard_response(parts_db_preflight(), allow_unverified_db)


def get_part(part_number, allow_unverified_db=False):
    """Read one part from the parts database (guarded). Same shape as scripted.parts_db_get_part."""
    blocked = parts_db_guard(allow_unverified_db)
    if blocked:
        return blocked
    return _run(get_part_script(part_number))


def article_in_db(part_nr, allow_unverified_db=False):
    """True / False when the parts database answered, None when it could not tell."""
    try:
        raw = get_part(part_nr, allow_unverified_db=allow_unverified_db)
    except Exception:  # noqa: BLE001 - undecided, never a crash of the write
        return None
    if not isinstance(raw, dict) or raw.get("success") is False:
        return None
    res = raw.get("results") if isinstance(raw.get("results"), dict) else raw
    if res.get("success") is False:
        return None
    found = res.get("found")
    return found if isinstance(found, bool) else None
