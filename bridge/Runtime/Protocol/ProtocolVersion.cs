// SPDX-License-Identifier: Apache-2.0
using System;
using System.Text.RegularExpressions;

namespace Playtest.Bridge.Protocol
{
    /// <summary>
    /// Wire protocol version. The Python contracts in <c>src/playtest/contracts/common.py</c>
    /// define <c>PROTOCOL</c>; this constant must match it.
    /// </summary>
    public static class ProtocolVersion
    {
        public const string Current = "upa/1";

        private static readonly Regex Pattern = new Regex(@"^upa/\d+$", RegexOptions.CultureInvariant);

        public static bool IsWellFormed(string protocol)
        {
            return !string.IsNullOrEmpty(protocol) && Pattern.IsMatch(protocol);
        }

        public static bool IsSupported(string protocol)
        {
            return string.Equals(protocol, Current, StringComparison.Ordinal);
        }
    }
}
