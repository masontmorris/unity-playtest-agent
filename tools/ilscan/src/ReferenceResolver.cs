using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;

namespace Playtest.IlScan
{
    /// <summary>
    /// Resolves an assembly reference by simple name to the first listed file that defines an assembly
    /// with that name. The list is the set of files the Editor loaded, where one name is one file, so
    /// versions are not compared.
    /// </summary>
    internal sealed class ReferenceResolver : MetadataAssemblyResolver
    {
        private readonly Dictionary<string, string> _paths = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public ReferenceResolver(IEnumerable<string> paths)
        {
            foreach (string path in paths)
            {
                string name = AssemblyNameOf(path);
                if (name != null && !_paths.ContainsKey(name)) _paths.Add(name, path);
            }
        }

        public override Assembly Resolve(MetadataLoadContext context, AssemblyName assemblyName)
        {
            return assemblyName.Name != null && _paths.TryGetValue(assemblyName.Name, out string path)
                ? context.LoadFromAssemblyPath(path)
                : null;
        }

        private static string AssemblyNameOf(string path)
        {
            try
            {
                using (var pe = new PEReader(File.OpenRead(path)))
                {
                    if (!pe.HasMetadata) return null;
                    MetadataReader reader = pe.GetMetadataReader();
                    return reader.IsAssembly ? reader.GetString(reader.GetAssemblyDefinition().Name) : null;
                }
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is BadImageFormatException)
            {
                // An unreadable file resolves nothing. A reference that needed it stays unresolved and the
                // scan records that where it matters.
                return null;
            }
        }
    }
}
