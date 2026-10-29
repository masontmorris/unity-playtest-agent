// SPDX-License-Identifier: Apache-2.0
using System;

namespace Playtest.Bridge.Lock
{
    /// <summary>
    /// Thrown by <see cref="BridgeLockFile.Acquire"/> when a live process already
    /// owns the project lock. The caller gets the owner, not a timeout.
    /// </summary>
    public sealed class BridgeLockBusyException : Exception
    {
        public BridgeLockBusyException(BridgeLockInfo owner)
            : base(BuildMessage(owner, null))
        {
            Owner = owner;
        }

        public BridgeLockBusyException(BridgeLockInfo owner, string path)
            : base(BuildMessage(owner, path))
        {
            Owner = owner;
        }

        public BridgeLockInfo Owner { get; }

        private static string BuildMessage(BridgeLockInfo owner, string path)
        {
            string where = string.IsNullOrEmpty(path) ? "the project lock" : "'" + path + "'";
            if (owner == null)
            {
                return "Bridge is busy: " + where + " is held by another process.";
            }

            return "Bridge is busy: " + where + " is held by pid "
                + owner.Pid + " on port " + owner.Port + ".";
        }
    }
}
