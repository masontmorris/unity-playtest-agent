// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Concurrent;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Playtest.Bridge.Transport;
using UnityEditor;
using WireCap = Playtest.Bridge.Protocol.InspectionCap;

namespace Playtest.Bridge.Editor.Preflight
{
    /// <summary>Marshals authenticated transport requests onto the Editor main thread.</summary>
    public sealed class EditorInspectionSource : IBridgeInspectionSource, IDisposable
    {
        private const int ComponentLimit = 4096;
        private const int FieldLimit = 30000;
        private const int BindingLimit = 10000;
        private const int TransformLimit = SceneTransformCollector.ObjectLimit;
        private const int MaximumSnapshotBytes = 6 * 1024 * 1024;
        private readonly ConcurrentQueue<Pending> _pending = new ConcurrentQueue<Pending>();
        private readonly object _gate = new object();
        private bool _disposed;

        public EditorInspectionSource()
        {
            EditorApplication.update += Drain;
        }

        public BridgeInspectionSnapshot Inspect(string stage)
        {
            var request = new Pending(stage);
            lock (_gate)
            {
                if (_disposed) throw new ObjectDisposedException(nameof(EditorInspectionSource));
                _pending.Enqueue(request);
            }

            try
            {
                if (!request.Completion.Task.Wait(TimeSpan.FromSeconds(90)))
                {
                    Interlocked.Exchange(ref request.Cancelled, 1);
                    throw new BridgeInspectionUnavailableException("the Editor did not complete inspection within 90 seconds");
                }
                return request.Completion.Task.GetAwaiter().GetResult();
            }
            catch (AggregateException ex) when (ex.InnerException != null)
            {
                throw ex.InnerException;
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                if (_disposed) return;
                _disposed = true;
                EditorApplication.update -= Drain;
                while (_pending.TryDequeue(out Pending request))
                    request.Completion.TrySetException(new BridgeInspectionUnavailableException("the Editor inspection source stopped"));
            }
        }

        private void Drain()
        {
            while (_pending.TryDequeue(out Pending request))
            {
                if (Volatile.Read(ref request.Cancelled) != 0) continue;
                try { request.Completion.TrySetResult(Collect(request.Stage)); }
                catch (Exception ex) { request.Completion.TrySetException(ex); }
            }
        }

        private static BridgeInspectionSnapshot Collect(string stage)
        {
            var snapshot = new BridgeInspectionSnapshot();
            if (stage == "inventory")
            {
                if (EditorApplication.isPlayingOrWillChangePlaymode)
                    throw new BridgeInspectionUnavailableException("the requested stage requires Edit mode");
                Merge(snapshot, ProjectSurveyCollector.Collect());
            }
            else if (stage == "discovery")
            {
                if (!EditorApplication.isPlaying)
                    throw new BridgeInspectionUnavailableException("the requested stage requires Play mode");
                SceneDiscoveryResult scene = SceneDiscoveryCollector.Collect(ComponentLimit, FieldLimit, BindingLimit);
                foreach (var pair in scene.Data) snapshot.Data.Add(pair.Key, pair.Value);
                foreach (var pair in scene.Caps)
                    snapshot.Caps.Add(pair.Key, new WireCap
                    {
                        Limit = pair.Value.Limit, Observed = pair.Value.Observed,
                        Returned = pair.Value.Returned, Truncated = pair.Value.Truncated,
                    });
                snapshot.Errors.AddRange(scene.Errors);
                Merge(snapshot, InputAssetCollector.Collect());
                Merge(snapshot, SourceCollector.Collect());
                Merge(snapshot, IlDiscoveryCollector.Collect());
            }
            else if (stage == "transforms")
            {
                if (!EditorApplication.isPlaying)
                    throw new BridgeInspectionUnavailableException("the requested stage requires Play mode");
                Merge(snapshot, SceneTransformCollector.Collect(TransformLimit));
            }
            else if (stage == "fields")
            {
                if (!EditorApplication.isPlaying)
                    throw new BridgeInspectionUnavailableException("the requested stage requires Play mode");
                Merge(snapshot, SceneFieldsCollector.Collect(ComponentLimit, FieldLimit, BindingLimit));
            }
            else throw new BridgeInspectionUnavailableException("unknown inspection stage");

            // Leave room for the response envelope, and fail explicitly instead of writing a
            // line the client cannot receive or silently dropping a part of the snapshot.
            if (Encoding.UTF8.GetByteCount(JsonConvert.SerializeObject(snapshot)) > MaximumSnapshotBytes)
                throw new BridgeInspectionUnavailableException("the inspection snapshot exceeds the transport size limit");
            return snapshot;
        }

        private static void Merge(BridgeInspectionSnapshot target, InspectionDocument source)
        {
            foreach (var pair in source.Data) target.Data.Add(pair.Key, pair.Value);
            foreach (var pair in source.Caps)
                target.Caps.Add(pair.Key, new WireCap
                {
                    Limit = pair.Value.Limit, Observed = pair.Value.Observed,
                    Returned = pair.Value.Returned, Truncated = pair.Value.Truncated,
                });
            target.Errors.AddRange(source.Errors);
        }

        private sealed class Pending
        {
            public readonly string Stage;
            public readonly TaskCompletionSource<BridgeInspectionSnapshot> Completion =
                new TaskCompletionSource<BridgeInspectionSnapshot>();
            public int Cancelled;

            public Pending(string stage) { Stage = stage; }
        }
    }
}
