using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace Playtest.Bridge.Actions
{
    /// <summary>
    /// Which action catalog a bench-style host registers for the client module it was told to run.
    ///
    /// A manifest session (<see cref="SessionModule"/>) serves exactly the catalog the manifest exports
    /// (<c>playtest-export-catalog</c>), under each declaration's own name, so a supported chord, stick or
    /// mouse value, or ordered skill reaches the bridge as declared. That catalog is required and strict: an
    /// absent path or digest, a missing or altered file, an invalid catalog and an empty one all throw. There
    /// is no fallback; in particular a session never gets the keyboard bench catalog. Every other module keeps
    /// the keyboard bench catalog the host builds, and the session variables play no part in it.
    ///
    /// The Python runner reads the same file and refuses to start unless it is exactly the manifest's export.
    /// </summary>
    public static class BridgeSessionCatalog
    {
        /// <summary>The client module that is a manifest session; <c>playtest.runner.cli</c> names itself the same.</summary>
        public const string SessionModule = "playtest.runner.cli";

        /// <summary>The exported catalog file the session host serves.</summary>
        public const string PathVariable = "UPA_SESSION_CATALOG";

        /// <summary>The SHA-256 (64 hex digits) the exported file was pinned to.</summary>
        public const string Sha256Variable = "UPA_SESSION_CATALOG_SHA256";

        public static bool IsSessionModule(string module)
        {
            return string.Equals(module, SessionModule, StringComparison.Ordinal);
        }

        /// <summary>
        /// The catalog to register for <paramref name="module"/>. A session loads the exported file and fails
        /// closed; any other module gets <paramref name="keyboardBench"/>, which a session never calls.
        /// </summary>
        public static BridgeActionCatalog Select(
            string module,
            string path,
            string expectedSha256,
            Func<BridgeActionCatalog> keyboardBench)
        {
            if (keyboardBench == null)
            {
                throw new ArgumentNullException(nameof(keyboardBench));
            }

            return IsSessionModule(module) ? LoadExported(path, expectedSha256) : keyboardBench();
        }

        /// <summary>
        /// Loads an exported catalog through the strict <see cref="BridgeActionCatalog.Parse"/>, after checking
        /// the pinned digest of the file's bytes. Unlike <see cref="BridgeActionCatalog.Load"/>, nothing is
        /// optional: a missing file is not an empty catalog, and an empty catalog is an error.
        /// </summary>
        public static BridgeActionCatalog LoadExported(string path, string expectedSha256)
        {
            if (string.IsNullOrEmpty(path))
            {
                throw new InvalidOperationException(
                    "a manifest session needs " + PathVariable + ": the host serves only the catalog the manifest exports, never a default keyboard catalog");
            }

            if (!IsSha256(expectedSha256))
            {
                throw new InvalidOperationException(
                    "a manifest session needs " + Sha256Variable + " set to the exported file's SHA-256 as 64 hex digits");
            }

            if (!File.Exists(path))
            {
                throw new FileNotFoundException("the session catalog does not exist", path);
            }

            byte[] bytes = File.ReadAllBytes(path);
            string actual = Sha256Hex(bytes);
            if (!string.Equals(actual, expectedSha256, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException(
                    "the session catalog's SHA-256 is " + actual + ", not the pinned " + expectedSha256.ToLowerInvariant()
                    + ": it is not the file that was exported: " + path);
            }

            string text;
            try
            {
                text = new UTF8Encoding(false, true).GetString(bytes);
            }
            catch (DecoderFallbackException ex)
            {
                throw new InvalidDataException("the session catalog is not valid UTF-8: " + path, ex);
            }

            BridgeActionCatalog catalog = BridgeActionCatalog.Parse(text, path);
            if (catalog.Count == 0)
            {
                throw new InvalidDataException("the session catalog declares no action or skill: " + path);
            }

            return catalog;
        }

        /// <summary>Lower-case hex SHA-256 of <paramref name="bytes"/>.</summary>
        public static string Sha256Hex(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(bytes ?? new byte[0]);
                var hex = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash)
                {
                    hex.Append(b.ToString("x2"));
                }

                return hex.ToString();
            }
        }

        private static bool IsSha256(string value)
        {
            if (value == null || value.Length != 64)
            {
                return false;
            }

            foreach (char c in value)
            {
                bool hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
                if (!hex)
                {
                    return false;
                }
            }

            return true;
        }
    }
}
