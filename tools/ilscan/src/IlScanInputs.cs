using System;
using System.Collections.Generic;
using System.Text.Json;

namespace Playtest.IlScan
{
    /// <summary>One assembly to scan: the name the Editor selected and the file it loaded.</summary>
    public sealed class IlScanAssembly
    {
        public IlScanAssembly(string name, string path)
        {
            Name = name;
            Path = path;
        }

        public string Name { get; }

        public string Path { get; }
    }

    /// <summary>
    /// The bridge discovery snapshot's <c>il_inputs</c>: which assemblies contain authored scripts, every
    /// loaded assembly file that can resolve their references, and whether that selection was complete.
    /// </summary>
    public sealed class IlScanInputs
    {
        public IlScanInputs(IReadOnlyList<IlScanAssembly> assemblies, IReadOnlyList<string> references, bool complete)
        {
            Assemblies = assemblies ?? throw new ArgumentNullException(nameof(assemblies));
            References = references ?? throw new ArgumentNullException(nameof(references));
            Complete = complete;
        }

        public IReadOnlyList<IlScanAssembly> Assemblies { get; }

        public IReadOnlyList<string> References { get; }

        public bool Complete { get; }

        /// <summary>Parses the inputs strictly; anything else is a <see cref="FormatException"/>.</summary>
        public static IlScanInputs Parse(string json)
        {
            using (JsonDocument document = JsonDocument.Parse(json))
            {
                JsonElement root = document.RootElement;
                RequireObject(root, "the inputs", "assemblies", "references", "complete");

                var assemblies = new List<IlScanAssembly>();
                var names = new HashSet<string>(StringComparer.Ordinal);
                JsonElement assemblyList = root.GetProperty("assemblies");
                if (assemblyList.ValueKind != JsonValueKind.Array) throw new FormatException("assemblies must be a list");
                foreach (JsonElement item in assemblyList.EnumerateArray())
                {
                    RequireObject(item, "an assembly", "name", "path");
                    string name = RequireText(item.GetProperty("name"), "an assembly name");
                    string path = RequireText(item.GetProperty("path"), "an assembly path");
                    if (!names.Add(name)) throw new FormatException("assembly '" + name + "' is listed twice");
                    assemblies.Add(new IlScanAssembly(name, path));
                }

                var references = new List<string>();
                JsonElement referenceList = root.GetProperty("references");
                if (referenceList.ValueKind != JsonValueKind.Array) throw new FormatException("references must be a list");
                foreach (JsonElement item in referenceList.EnumerateArray()) references.Add(RequireText(item, "a reference path"));

                JsonElement complete = root.GetProperty("complete");
                if (complete.ValueKind != JsonValueKind.True && complete.ValueKind != JsonValueKind.False)
                    throw new FormatException("complete must be true or false");
                return new IlScanInputs(assemblies, references, complete.GetBoolean());
            }
        }

        private static void RequireObject(JsonElement element, string what, params string[] keys)
        {
            if (element.ValueKind != JsonValueKind.Object) throw new FormatException(what + " must be an object");
            var expected = new HashSet<string>(keys, StringComparer.Ordinal);
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (JsonProperty property in element.EnumerateObject())
            {
                if (!expected.Contains(property.Name) || !seen.Add(property.Name))
                    throw new FormatException(what + " has an unexpected or repeated key '" + property.Name + "'");
            }

            if (seen.Count != expected.Count) throw new FormatException(what + " must have exactly: " + string.Join(", ", keys));
        }

        private static string RequireText(JsonElement element, string what)
        {
            if (element.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(element.GetString()))
                throw new FormatException(what + " must be a non-empty string");
            return element.GetString();
        }
    }
}
