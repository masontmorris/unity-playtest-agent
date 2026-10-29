// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Generic;

namespace Playtest.Bridge.Transport
{
    /// <summary>
    /// The observations the bridge actually sent, keyed by their outgoing seq. An action binds
    /// to one of these; the bridge can therefore check its age and its declared fields against
    /// the exact sample the core saw. Bounded so a long stream cannot grow without limit.
    /// </summary>
    public sealed class BridgeObservationHistory
    {
        private readonly object _gate = new object();
        private readonly Dictionary<long, BridgeObservationSnapshot> _bySeq =
            new Dictionary<long, BridgeObservationSnapshot>();
        private readonly Queue<long> _order = new Queue<long>();
        private readonly int _capacity;
        private BridgeObservationSnapshot _latest;

        public BridgeObservationHistory(int capacity = 2048)
        {
            if (capacity <= 0)
            {
                throw new ArgumentOutOfRangeException(nameof(capacity));
            }

            _capacity = capacity;
        }

        public int Count
        {
            get
            {
                lock (_gate)
                {
                    return _bySeq.Count;
                }
            }
        }

        public void Add(long seq, BridgeObservationSnapshot snapshot)
        {
            if (seq < 0 || snapshot == null)
            {
                return;
            }

            lock (_gate)
            {
                if (!_bySeq.ContainsKey(seq))
                {
                    _order.Enqueue(seq);
                }

                _bySeq[seq] = snapshot;
                if (_latest == null || snapshot.CapturedWallUtc >= _latest.CapturedWallUtc)
                {
                    _latest = snapshot;
                }

                while (_bySeq.Count > _capacity && _order.Count > 0)
                {
                    long oldest = _order.Dequeue();
                    _bySeq.Remove(oldest);
                }
            }
        }

        public bool TryGet(long seq, out BridgeObservationSnapshot snapshot)
        {
            lock (_gate)
            {
                return _bySeq.TryGetValue(seq, out snapshot);
            }
        }

        public bool TryGetLatest(out BridgeObservationSnapshot snapshot)
        {
            lock (_gate)
            {
                snapshot = _latest;
                return snapshot != null;
            }
        }

        public void Clear()
        {
            lock (_gate)
            {
                _bySeq.Clear();
                _order.Clear();
                _latest = null;
            }
        }
    }
}
