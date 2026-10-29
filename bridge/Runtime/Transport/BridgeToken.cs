// SPDX-License-Identifier: Apache-2.0
using System;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace Playtest.Bridge.Transport
{
    /// <summary>Per-session bearer token. Random at start, never logged, never committed.</summary>
    public static class BridgeToken
    {
        public const int DefaultByteLength = 32;

        /// <summary>Lowercase hex, 2 * <paramref name="byteLength"/> characters.</summary>
        public static string Create(int byteLength = DefaultByteLength)
        {
            if (byteLength < 16)
            {
                throw new ArgumentOutOfRangeException(nameof(byteLength), "use at least 128 bits of entropy");
            }

            var bytes = new byte[byteLength];
            using (RandomNumberGenerator rng = RandomNumberGenerator.Create())
            {
                rng.GetBytes(bytes);
            }

            var builder = new StringBuilder(byteLength * 2);
            for (int i = 0; i < bytes.Length; i++)
            {
                builder.Append(bytes[i].ToString("x2", CultureInfo.InvariantCulture));
            }

            return builder.ToString();
        }

        /// <summary>Compares without early exit on content. Length mismatch returns false.</summary>
        public static bool Matches(string expected, string actual)
        {
            if (expected == null || actual == null || expected.Length != actual.Length)
            {
                return false;
            }

            int difference = 0;
            for (int i = 0; i < expected.Length; i++)
            {
                difference |= expected[i] ^ actual[i];
            }

            return difference == 0;
        }
    }
}
