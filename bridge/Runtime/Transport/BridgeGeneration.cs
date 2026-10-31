// SPDX-License-Identifier: Apache-2.0
using System;
using System.IO;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// A capture generation names exactly one play instance. The mailbox and the frame directory
    /// both carry it, so its shape is closed: a canonical lower-case, hyphenated UUID
    /// (<c>Guid.ToString("D")</c>, the same string <c>str(uuid.uuid4())</c> writes). A value that
    /// cannot name a directory component is rejected before it reaches the file system, so a
    /// generation can never address a parent directory or another session's frames.
    /// </summary>
    public static class BridgeGeneration
    {
        /// <summary><c>&lt;project&gt;/Temp/playtest-bridge-frames</c>.</summary>
        public const string FramesDirectoryName = "playtest-bridge-frames";

        /// <summary>A new generation for one new play instance. Minted once; never reused.</summary>
        public static string New()
        {
            return Guid.NewGuid().ToString("D");
        }

        /// <summary>True for the canonical 36-character lower-case hyphenated form only.</summary>
        public static bool IsCanonical(string value)
        {
            if (value == null || value.Length != 36)
            {
                return false;
            }

            Guid parsed;
            if (!Guid.TryParseExact(value, "D", out parsed))
            {
                return false;
            }

            // TryParseExact accepts upper case; the wire form the Python side writes is lower case.
            return string.Equals(parsed.ToString("D"), value, StringComparison.Ordinal);
        }

        /// <summary>Throws <see cref="ArgumentException"/> unless <paramref name="value"/> is canonical.</summary>
        public static string Validate(string value, string parameterName)
        {
            if (!IsCanonical(value))
            {
                throw new ArgumentException(
                    "a generation must be a canonical lower-case hyphenated UUID, not '"
                    + (value ?? "<null>") + "'",
                    parameterName);
            }

            return value;
        }

        /// <summary>
        /// The generation's own frame directory below <paramref name="framesRoot"/>. The generation is
        /// validated first, so the result is always a direct child of the root.
        /// </summary>
        public static string Directory(string framesRoot, string generation)
        {
            if (string.IsNullOrEmpty(framesRoot))
            {
                throw new ArgumentException("A frames root is required.", nameof(framesRoot));
            }

            Validate(generation, nameof(generation));
            return Path.Combine(Path.GetFullPath(framesRoot), generation);
        }
    }
}
