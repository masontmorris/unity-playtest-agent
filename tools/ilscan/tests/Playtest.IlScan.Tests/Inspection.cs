using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using Playtest.IlScan.Fixtures;
using Playtest.IlScan.Fixtures.Dependency;

namespace Playtest.IlScan.Tests
{
    /// <summary>The fixture assembly on disk and the files that resolve its references.</summary>
    internal static class Fixture
    {
        public static readonly Assembly Live = typeof(ScanTargets).Assembly;

        public static string AssemblyName => Live.GetName().Name;

        public static string AssemblyPath => Live.Location;

        public static string DependencyPath => typeof(ExternalState).Assembly.Location;

        /// <summary>The running framework's assemblies, the fixture and, unless left out, its dependency.</summary>
        public static List<string> References(bool withDependency = true)
        {
            var paths = Directory.GetFiles(RuntimeEnvironment.GetRuntimeDirectory(), "*.dll").OrderBy(p => p, StringComparer.Ordinal).ToList();
            paths.Add(AssemblyPath);
            if (withDependency) paths.Add(DependencyPath);
            return paths;
        }

        public static IlScanInputs Inputs(bool complete = true, bool withDependency = true)
        {
            return new IlScanInputs(new[] { new IlScanAssembly(AssemblyName, AssemblyPath) }, References(withDependency), complete);
        }
    }

    /// <summary>The fixture loaded for inspection only, with the resolver the tool uses.</summary>
    internal sealed class InspectionScope : IDisposable
    {
        public InspectionScope(bool withDependency = true)
        {
            Context = new MetadataLoadContext(new PathAssemblyResolver(Fixture.References(withDependency)));
            Resolver = new MetadataFieldResolver(Context);
            Fixtures = Context.LoadFromAssemblyPath(Fixture.AssemblyPath);
        }

        public MetadataLoadContext Context { get; }

        public MetadataFieldResolver Resolver { get; }

        public Assembly Fixtures { get; }

        /// <summary>The inspection-only counterpart of a live type, including a constructed generic type.</summary>
        public Type Type(Type live)
        {
            if (live.IsConstructedGenericType)
                return Type(live.GetGenericTypeDefinition()).MakeGenericType(live.GetGenericArguments().Select(Type).ToArray());
            Assembly assembly = Context.LoadFromAssemblyName(live.Assembly.GetName());
            return assembly.GetType(live.FullName, throwOnError: true);
        }

        public void Dispose()
        {
            Resolver.Dispose();
            Context.Dispose();
        }
    }

    /// <summary>The Editor's former resolution: live reflection resolves the token itself.</summary>
    internal sealed class RuntimeFieldResolver : IFieldTokenResolver
    {
        public FieldInfo ResolveField(Module module, int token, Type[] typeArguments, Type[] methodArguments)
            => module.ResolveField(token, typeArguments, methodArguments);
    }
}
