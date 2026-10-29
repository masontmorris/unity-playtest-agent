using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.Json.Nodes;
using Playtest.Bridge.Editor.Preflight;

namespace Playtest.IlScan
{
    /// <summary>
    /// Builds discovery's IL sections from compiled assemblies that are loaded for inspection only and
    /// never executed.
    /// </summary>
    /// <remarks>
    /// This is the scanning half of the Editor's former IL discovery collector, moved out of the bridge
    /// package. The Editor still selects the assemblies that contain authored scripts and reports the
    /// files it loaded. This tool joins their direct IL stores to the fields they declare. Limits, ids,
    /// ordering and the uncertainty rules are unchanged: once any input is incomplete, every later count
    /// is unknown rather than a total.
    /// </remarks>
    public static class IlDiscovery
    {
        public const int TypeLimit = 20000;
        public const int FieldLimit = 50000;
        public const int MethodLimit = 20000;
        public const int WriteLimit = 100000;
        public const int IssueLimit = 10000;
        public const string Scope = "direct_stores_in_loaded_assemblies_containing_authored_scripts";

        private const BindingFlags Fields = BindingFlags.DeclaredOnly | BindingFlags.Instance
            | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        /// <param name="inputs">The bridge's selection and the loaded assembly files.</param>
        /// <param name="coreAssemblyName">
        /// The assembly that defines <c>System.Object</c>, or null to use the first of <c>mscorlib</c>,
        /// <c>System.Runtime</c> and <c>netstandard</c> found among the references.
        /// </param>
        public static InspectionDocument Collect(IlScanInputs inputs, string coreAssemblyName = null)
        {
            if (inputs == null) throw new ArgumentNullException(nameof(inputs));
            var document = new InspectionDocument();
            bool incomplete = !inputs.Complete;
            var types = new List<Type>();
            MetadataLoadContext context = null;
            try
            {
                try
                {
                    var paths = new List<string>();
                    foreach (IlScanAssembly item in inputs.Assemblies) paths.Add(item.Path);
                    paths.AddRange(inputs.References);
                    context = new MetadataLoadContext(new ReferenceResolver(paths), coreAssemblyName);
                    if (context.CoreAssembly == null)
                    {
                        incomplete = true;
                        document.Errors.Add("il_references: no core assembly was found");
                    }
                }
                catch (Exception ex)
                {
                    incomplete = true;
                    document.Errors.Add("il_references: " + ex.GetType().Name);
                }

                if (context != null)
                {
                    foreach (IlScanAssembly item in inputs.Assemblies)
                    {
                        Assembly assembly;
                        try
                        {
                            assembly = context.LoadFromAssemblyPath(item.Path);
                        }
                        catch (Exception ex)
                        {
                            incomplete = true;
                            document.Errors.Add("il_assembly:" + item.Name + ": " + ex.GetType().Name);
                            continue;
                        }

                        if (!string.Equals(assembly.GetName().Name, item.Name, StringComparison.Ordinal))
                        {
                            incomplete = true;
                            document.Errors.Add("il_assembly:" + item.Name + ": the file holds a different assembly");
                            continue;
                        }

                        try { types.AddRange(assembly.GetTypes()); }
                        catch (ReflectionTypeLoadException ex)
                        {
                            incomplete = true;
                            types.AddRange(ex.Types.Where(t => t != null));
                            document.Errors.Add("il_assembly:" + item.Name + ": some types could not be loaded");
                        }
                        catch (Exception ex)
                        {
                            incomplete = true;
                            document.Errors.Add("il_assembly:" + item.Name + ": " + ex.GetType().Name);
                        }
                    }
                }

                using (MetadataFieldResolver resolver = context == null ? null : new MetadataFieldResolver(context))
                {
                    Build(document, types, (IFieldTokenResolver)resolver ?? new UnavailableResolver(), incomplete);
                }
            }
            finally
            {
                context?.Dispose();
            }

            return document;
        }

        /// <summary>
        /// Joins the direct stores in <paramref name="loaded"/> to the fields those types declare, whichever
        /// way the types were loaded. The parity tests run it on live and inspection-only types alike.
        /// </summary>
        public static void Build(InspectionDocument document, IEnumerable<Type> loaded, IFieldTokenResolver resolver, bool incomplete)
        {
            if (document == null) throw new ArgumentNullException(nameof(document));
            if (loaded == null) throw new ArgumentNullException(nameof(loaded));

            List<Type> types = loaded.Distinct().OrderBy(ReflectionIds.TypeId, StringComparer.Ordinal).ToList();
            List<Type> selectedTypes = document.Take("il_types", types, TypeLimit);
            if (incomplete) MarkUncertain(document.Caps["il_types"]);
            incomplete |= selectedTypes.Count != types.Count;

            var fields = new SortedDictionary<string, FieldInfo>(StringComparer.Ordinal);
            foreach (Type type in selectedTypes)
            {
                try
                {
                    foreach (FieldInfo field in type.GetFields(Fields)) fields[ReflectionIds.FieldId(field)] = field;
                }
                catch (Exception ex)
                {
                    incomplete = true;
                    document.Errors.Add("il_type:" + ReflectionIds.TypeId(type) + ": " + ex.GetType().Name);
                }
            }
            List<string> selectedFields = document.Take("il_fields", fields.Keys.ToList(), FieldLimit);
            if (incomplete) MarkUncertain(document.Caps["il_fields"]);
            incomplete |= selectedFields.Count != fields.Count;

            IlWriteScanResult scan = IlWriteScanner.Scan(selectedTypes, MethodLimit, resolver);
            document.Caps["il_methods"] = new InspectionCap
            {
                Limit = MethodLimit,
                Observed = incomplete ? (int?)null : scan.TotalMethods,
                Returned = scan.ScannedMethods,
                Truncated = scan.LimitReached ? true : incomplete ? (bool?)null : false,
            };
            incomplete |= !scan.IsComplete;
            var writes = scan.Writes.ToList();
            var selectedWrites = document.Take("il_writes", writes, WriteLimit);
            if (incomplete) MarkUncertain(document.Caps["il_writes"]);
            incomplete |= selectedWrites.Count != writes.Count;
            var issues = scan.Issues.ToList();
            var selectedIssues = document.Take("il_issues", issues, IssueLimit);
            if (incomplete) MarkUncertain(document.Caps["il_issues"]);
            incomplete |= selectedIssues.Count != issues.Count;

            var methodIds = new Dictionary<MethodBase, string>();
            var grouped = new Dictionary<string, JsonArray>(StringComparer.Ordinal);
            foreach (IlWrite write in selectedWrites)
            {
                string id = ReflectionIds.FieldId(write.Field);
                if (!grouped.TryGetValue(id, out JsonArray list)) grouped[id] = list = new JsonArray();
                list.Add(new JsonObject
                {
                    ["method"] = MethodId(write.Method, methodIds, document, ref incomplete), ["il_offset"] = write.IlOffset,
                    ["constructor"] = write.Method.IsConstructor,
                });
            }
            var writeSets = new JsonArray();
            foreach (string id in selectedFields)
            {
                writeSets.Add(new JsonObject
                {
                    ["field_id"] = id,
                    ["direct_writers"] = grouped.TryGetValue(id, out JsonArray list) ? list : new JsonArray(),
                });
            }
            document.Data["il_write_sets"] = writeSets;
            var listed = new HashSet<string>(selectedFields, StringComparer.Ordinal);
            var unlisted = new JsonArray();
            foreach (KeyValuePair<string, JsonArray> pair in grouped.Where(pair => !listed.Contains(pair.Key))
                .OrderBy(pair => pair.Key, StringComparer.Ordinal))
            {
                unlisted.Add(new JsonObject { ["field_id"] = pair.Key, ["direct_writers"] = pair.Value });
            }
            document.Data["il_unlisted_writes"] = unlisted;
            var issueRows = new JsonArray();
            foreach (IlWriteScanIssue issue in selectedIssues)
            {
                issueRows.Add(new JsonObject
                {
                    ["kind"] = issue.Kind.ToString(),
                    ["method"] = issue.Method == null ? null : MethodId(issue.Method, methodIds, document, ref incomplete),
                    ["field_id"] = issue.Field == null ? null : ReflectionIds.FieldId(issue.Field),
                    ["il_offset"] = issue.IlOffset < 0 ? (int?)null : issue.IlOffset,
                    ["reason"] = issue.Reason,
                });
            }
            document.Data["il_issues"] = issueRows;
            document.Data["il_scope"] = Scope;
            document.Data["il_complete"] = !incomplete;
        }

        public static string MethodId(MethodBase method)
        {
            return ReflectionIds.TypeId(method.DeclaringType) + ":" + method.Name + "("
                + string.Join(",", method.GetParameters().Select(p => p.ParameterType.FullName ?? p.ParameterType.Name)) + ")";
        }

        private static string MethodId(
            MethodBase method, Dictionary<MethodBase, string> ids, InspectionDocument document, ref bool incomplete)
        {
            if (ids.TryGetValue(method, out string id))
            {
                if (id == null) incomplete = true;
                return id;
            }

            try
            {
                id = MethodId(method);
            }
            catch (Exception ex)
            {
                // A parameter type that cannot be resolved leaves the id unknown, never approximated.
                incomplete = true;
                document.Errors.Add("il_method:" + ReflectionIds.TypeId(method.DeclaringType) + ":" + method.Name + ": "
                    + ex.GetType().Name);
                id = null;
            }

            ids.Add(method, id);
            return id;
        }

        private static void MarkUncertain(InspectionCap cap)
        {
            cap.Observed = null;
            if (cap.Truncated != true) cap.Truncated = null;
        }

        private sealed class UnavailableResolver : IFieldTokenResolver
        {
            public FieldInfo ResolveField(Module module, int token, Type[] typeArguments, Type[] methodArguments)
                => throw new InvalidOperationException("no assemblies could be loaded");
        }
    }
}
