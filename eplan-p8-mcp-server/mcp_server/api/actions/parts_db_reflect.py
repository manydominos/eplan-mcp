"""
Parts database READ scripts without `using Eplan.EplApi.MasterData;` (EPLAN MCP v3, M-2).

EPLAN 2.9's script compiler does not reference the MasterData assembly, so
every parts_db_* script that named MDPartsManagement / MDPart statically failed
with CS0234 ("namespace Eplan.EplApi.MasterData does not exist") - which the
caller only ever saw as a timeout. Same trap and same cure as live.py for
DataModel: resolve the types at RUN time from the loaded (or loadable)
assembly and call them by reflection. Works on newer versions too, because
nothing is bound at compile time.

Builders only: each returns the C# text. The tool functions stay in
scripted.py (same names, parameters and result shape) so _execute_script is
the one in scripted - which is also what the offline tests stub.

parts_db_create / parts_db_update are unchanged (v3 scope: reads only).
"""

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


def _filter_literals(filter_property, filter_value):
    if filter_property and filter_value:
        return cs_escape(filter_property), cs_escape(filter_value)
    return "", ""


def query_script(filter_property, filter_value, return_properties, limit):
    """C# for parts_db_query. filter_property is validated by the caller."""
    fp, fv = _filter_literals(filter_property, filter_value)
    props_array = ", ".join('"%s"' % cs_escape(p) for p in return_properties)
    body = '''        var partsList = new List<Dictionary<string, object>>();
        object db = null;
        try
        {
            int limit = %d;
            string filterProp = "%s";
            string filterValue = "%s";
            string[] propsToGet = new string[] { %s };
            db = OpenDb();
            foreach (object part in PartsOf(db))
            {
                if (part == null) continue;
                if (partsList.Count >= limit) break;
                if (!PassesFilter(part, filterProp, filterValue)) continue;
                var partDict = new Dictionary<string, object>();
                foreach (var propName in propsToGet)
                {
                    try { partDict[propName] = ReadPartProperty(part, propName); }
                    catch (Exception exProp) { partDict[propName] = "<error: " + exProp.GetType().Name + ">"; }
                }
                partsList.Add(partDict);
            }
            results["success"] = true;
            results["count"] = partsList.Count;
            results["parts"] = partsList;
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }
        finally { if (db != null) CloseDb(db); }
''' % (int(limit), fp, fv, props_array)
    return _wrap("PartsQuery", body)


def count_script(filter_property, filter_value):
    fp, fv = _filter_literals(filter_property, filter_value)
    body = '''        object db = null;
        try
        {
            string filterProp = "%s";
            string filterValue = "%s";
            int count = 0;
            db = OpenDb();
            foreach (object part in PartsOf(db))
            {
                if (part == null) continue;
                if (PassesFilter(part, filterProp, filterValue)) count++;
            }
            results["success"] = true;
            results["count"] = count;
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }
        finally { if (db != null) CloseDb(db); }
''' % (fp, fv)
    return _wrap("PartsCount", body)


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


def product_groups_script():
    body = '''        try
        {
            string itemType = "Eplan.EplApi.MasterData.MDPartsDatabaseItem+Enums+";
            results["productGroups"] = new List<string>(Enum.GetNames(FindMdType(itemType + "ProductGroup")));
            results["productSubGroups"] = new List<string>(Enum.GetNames(FindMdType(itemType + "ProductSubGroup")));
            results["productTopGroups"] = new List<string>(Enum.GetNames(FindMdType(itemType + "ProductTopGroup")));
            results["success"] = true;
        }
        catch (Exception ex)
        {
            results["success"] = false;
            results["error"] = Flatten(ex);
        }
'''
    return _wrap("PartsGroups", body)
