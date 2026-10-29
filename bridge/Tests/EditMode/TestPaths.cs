// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.CompilerServices;
using Playtest.Bridge.Protocol;

namespace Playtest.Bridge.Tests
{
    /// <summary>
    /// Locates the shared contract fixtures (<c>fixtures/contracts</c>) from either the Unity
    /// EditMode runner or the Unity-free dotnet harness. The fixtures are the same files the Python
    /// suite validates, so resolution has to work in both hosts without duplicating them.
    /// </summary>
    internal static class TestPaths
    {
        private const string ContractsFolder = "fixtures/contracts";

        private static readonly Lazy<string> LazyContracts = new Lazy<string>(ResolveContracts);

        /// <summary>Absolute path to the contracts fixture directory.</summary>
        public static string Contracts
        {
            get { return LazyContracts.Value; }
        }

        private static string ResolveContracts()
        {
            var tried = new List<string>();

            string fromEnvironment = FromEnvironment("UPA_FIXTURES", null, tried);
            if (fromEnvironment != null)
            {
                return fromEnvironment;
            }

#if UNITY_EDITOR
            string fromPackage = FromUnityPackage(tried);
            if (fromPackage != null)
            {
                return fromPackage;
            }
#endif

            string fromAncestors = FromAncestors(AppContext.BaseDirectory, tried);
            if (fromAncestors != null)
            {
                return fromAncestors;
            }

            string fromRepoRoot = FromEnvironment("UPA_REPO_ROOT", ContractsFolder, tried);
            if (fromRepoRoot != null)
            {
                return fromRepoRoot;
            }

            // Build output can live outside the repository (dotnet test --artifacts-path). The
            // compiler bakes this file's absolute path into the assembly, so it still points at
            // the checkout. Kept last: the environment and ancestor probes stay authoritative.
            string fromSource = FromAncestors(SourceDirectory(), tried);
            if (fromSource != null)
            {
                return fromSource;
            }

            throw new DirectoryNotFoundException(
                "Could not locate the shared contract fixtures ('" + ContractsFolder + "'). Tried:"
                + Environment.NewLine
                + string.Join(Environment.NewLine, tried)
                + Environment.NewLine
                + "Set UPA_FIXTURES to the contracts directory, or UPA_REPO_ROOT to the repository root.");
        }

        private static string FromEnvironment(string variable, string relative, List<string> tried)
        {
            string value = Environment.GetEnvironmentVariable(variable);
            if (string.IsNullOrEmpty(value))
            {
                tried.Add(variable + " (not set)");
                return null;
            }

            string candidate = relative == null ? value : Path.Combine(value, relative);
            candidate = Path.GetFullPath(candidate);
            if (Directory.Exists(candidate))
            {
                tried.Add(variable + " -> " + candidate);
                return candidate;
            }

            tried.Add(variable + " -> " + candidate + " (missing)");
            return null;
        }

#if UNITY_EDITOR
        private static string FromUnityPackage(List<string> tried)
        {
            UnityEditor.PackageManager.PackageInfo package =
                UnityEditor.PackageManager.PackageInfo.FindForAssembly(typeof(HelloMessage).Assembly);
            if (package == null)
            {
                tried.Add("Unity package lookup (assembly not owned by a package)");
                return null;
            }

            // This test assembly belongs to the bridge package, which sits at <repo>/bridge, so the
            // fixtures are one directory above the package root.
            string candidate = Path.GetFullPath(
                Path.Combine(package.resolvedPath, "..", ContractsFolder));
            if (Directory.Exists(candidate))
            {
                tried.Add("Unity package -> " + candidate);
                return candidate;
            }

            tried.Add("Unity package -> " + candidate + " (missing)");
            return null;
        }
#endif

        private static string FromAncestors(string start, List<string> tried)
        {
            if (string.IsNullOrEmpty(start))
            {
                tried.Add("ancestor walk from an empty path");
                return null;
            }

            var directory = new DirectoryInfo(Path.GetFullPath(start));
            while (directory != null)
            {
                string candidate = Path.Combine(directory.FullName, ContractsFolder);
                if (Directory.Exists(candidate))
                {
                    tried.Add("ancestor walk from " + start + " -> " + candidate);
                    return candidate;
                }

                directory = directory.Parent;
            }

            tried.Add("ancestor walk from " + start + " (no '" + ContractsFolder + "' in any parent)");
            return null;
        }

        private static string SourceDirectory([CallerFilePath] string path = "")
        {
            return string.IsNullOrEmpty(path) ? null : Path.GetDirectoryName(path);
        }
    }
}
