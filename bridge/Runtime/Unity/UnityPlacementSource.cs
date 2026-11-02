// SPDX-License-Identifier: Apache-2.0
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Playtest.Bridge.Transport;
using UnityEngine;
#if UNITY_EDITOR
using UnityEditor;
#endif

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// One placement in progress: set the configured entity's pose, zero its velocity, wait
    /// <c>settle_s</c> of game time, then measure where it ended up. Every member runs on the Unity
    /// main thread and touches nothing but that one entity.
    ///
    /// The wait is game time, so a paused clock never advances it. A paused clock and a project
    /// that is not playing are rejected before the pose is touched. A wall bound ends the wait if
    /// the game clock stalls or crawls after the pose was set; the result then says that no
    /// measurement was taken rather than reporting a settle that never happened.
    /// </summary>
    public sealed class BridgePlacementOperation
    {
        private readonly GameObject _entity;
        private readonly Vector3 _target;
        private readonly double _settleS;
        private readonly double _wallBoundS;
        private readonly bool _playing;
        private readonly bool _paused;
        private bool _started;
        private double _startGameS;
        private double _deadlineWallS;
        private BridgePlacementResult _result;

        public BridgePlacementOperation(
            GameObject entity,
            Vector3 target,
            double settleS,
            double wallBoundS,
            bool playing,
            bool paused)
        {
            _entity = entity;
            _target = target;
            _settleS = settleS;
            _wallBoundS = wallBoundS;
            _playing = playing;
            _paused = paused;
        }

        /// <summary>The entity this placement acts on, or null when none was resolved.</summary>
        public GameObject Entity
        {
            get { return _entity; }
        }

        /// <summary>True once <see cref="Step"/> has produced the measured or rejected result.</summary>
        public bool IsComplete
        {
            get { return _result != null; }
        }

        /// <summary>How long the game clock must advance before the measurement is taken.</summary>
        public double SettleS
        {
            get { return _settleS; }
        }

        /// <summary>
        /// The wall seconds a settle is allowed before it is given up on. A clock at time scale
        /// <paramref name="timeScale"/> needs that much wall time per game second; the floor keeps a
        /// slowed clock bounded and the constant is slack for frames the main thread could not run.
        /// </summary>
        public static double WallBoundSeconds(double settleS, double timeScale)
        {
            const double SlowestScale = 0.05;
            const double SlackSeconds = 10;
            double scale = timeScale > SlowestScale ? timeScale : SlowestScale;
            return settleS / scale + SlackSeconds;
        }

        /// <summary>
        /// Advances the placement by one main-thread frame. Returns the result once the settle wait
        /// ended, and null while it is still waiting. The pose is set exactly once, on the first call.
        /// </summary>
        public BridgePlacementResult Step(double gameS, double wallS)
        {
            if (_result != null)
            {
                return _result;
            }

            if (!_started)
            {
                _started = true;
                if (!_playing)
                {
                    _result = BridgePlacementResult.Unplaced("the project is not playing");
                    return _result;
                }

                if (_paused)
                {
                    _result = BridgePlacementResult.Unplaced(
                        "the game clock is paused, so settle_s of game time cannot be waited");
                    return _result;
                }

                if (_entity == null)
                {
                    _result = BridgePlacementResult.Unplaced("the controlled entity is gone");
                    return _result;
                }

                Apply(_entity, _target);
                _startGameS = gameS;
                _deadlineWallS = wallS + _wallBoundS;
                return null;
            }

            if (gameS - _startGameS < _settleS)
            {
                if (wallS < _deadlineWallS)
                {
                    return null;
                }

                _result = BridgePlacementResult.Unplaced(
                    "the game clock did not advance settle_s within the wall bound, so nothing was measured");
                return _result;
            }

            GameObject entity = _entity;
            if (entity == null)
            {
                _result = BridgePlacementResult.Unplaced("the controlled entity was destroyed while settling");
                return _result;
            }

            Vector3 settled = entity.transform.position;
            _result = new BridgePlacementResult
            {
                Placed = true,
                PositionAfterSettle = new List<double> { settled.x, settled.y, settled.z },
                MovedDuringSettle = Vector3.Distance(_target, settled),
            };
            return _result;
        }

        /// <summary>
        /// Sets the pose on whichever body this entity has, or on its transform when it has none:
        /// the body's own position and a zeroed linear velocity, then the transform in step with it.
        /// Generic over 2D and 3D physics; no other object is touched.
        /// </summary>
        private static void Apply(GameObject entity, Vector3 target)
        {
            Rigidbody2D body2d = entity.GetComponent<Rigidbody2D>();
            if (body2d != null)
            {
                body2d.position = new Vector2(target.x, target.y);
                body2d.linearVelocity = Vector2.zero;
                entity.transform.position = target;
                return;
            }

            Rigidbody body = entity.GetComponent<Rigidbody>();
            if (body != null)
            {
                body.position = target;
                body.linearVelocity = Vector3.zero;
                entity.transform.position = target;
                return;
            }

            entity.transform.position = target;
        }
    }

    /// <summary>
    /// Marshals a placement request from the transport thread onto the Unity main thread, waits for
    /// its settle at frame rate and returns what was measured. The entity is the one the session
    /// configured as controlled, resolved on the main thread through
    /// <see cref="UnityControlledEntity"/>; no request can name another object.
    /// </summary>
    public sealed class UnityPlacementSource : IBridgePlacementSource, IDisposable
    {
        /// <summary>
        /// Backstop for a reply that never arrives because the main thread stopped ticking. It is
        /// far longer than any settle this source waits for on its own wall bound, so reaching it
        /// means the Editor is not running frames.
        /// </summary>
        private const double StalledThreadBackstopSeconds = 300;

        private readonly ConcurrentQueue<Pending> _pending = new ConcurrentQueue<Pending>();
        private readonly object _gate = new object();
        private Pending _active;
        private bool _disposed;

        public UnityPlacementSource()
        {
#if UNITY_EDITOR
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
#else
            Application.onBeforeRender -= Tick;
            Application.onBeforeRender += Tick;
#endif
        }

        /// <summary>Called from a transport thread. Never touches a Unity API itself.</summary>
        public BridgePlacementResult Place(BridgePlacementRequest request)
        {
            if (request == null)
            {
                throw new BridgePlacementUnavailableException("missing placement request");
            }

            var pending = new Pending(request);
            lock (_gate)
            {
                if (_disposed)
                {
                    throw new BridgePlacementUnavailableException("the placement source stopped");
                }

                _pending.Enqueue(pending);
            }

            double backstop = request.SettleS + StalledThreadBackstopSeconds;
            try
            {
                if (!pending.Completion.Task.Wait(TimeSpan.FromSeconds(backstop)))
                {
                    Interlocked.Exchange(ref pending.Cancelled, 1);
                    throw new BridgePlacementUnavailableException(
                        "the Editor did not complete the placement within " + backstop.ToString("0.###") + " seconds");
                }

                return pending.Completion.Task.GetAwaiter().GetResult();
            }
            catch (AggregateException ex) when (ex.InnerException != null)
            {
                if (ex.InnerException is BridgePlacementUnavailableException unavailable)
                {
                    throw unavailable;
                }

                throw new BridgePlacementUnavailableException(
                    ex.InnerException.GetType().Name + ": " + ex.InnerException.Message);
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                if (_disposed)
                {
                    return;
                }

                _disposed = true;
#if UNITY_EDITOR
                EditorApplication.update -= Tick;
#else
                Application.onBeforeRender -= Tick;
#endif
                Pending active = _active;
                _active = null;
                if (active != null)
                {
                    active.Completion.TrySetException(
                        new BridgePlacementUnavailableException("the placement source stopped"));
                }

                while (_pending.TryDequeue(out Pending request))
                {
                    request.Completion.TrySetException(
                        new BridgePlacementUnavailableException("the placement source stopped"));
                }
            }
        }

        private void Tick()
        {
            try
            {
                Pending active = _active;
                if (active == null)
                {
                    if (!_pending.TryDequeue(out active))
                    {
                        return;
                    }

                    _active = active;
                    if (Volatile.Read(ref active.Cancelled) != 0)
                    {
                        _active = null;
                        return;
                    }

                    active.Operation = Begin(active);
                }

                if (active.Operation == null)
                {
                    // The request was rejected before a pose was touched: nothing is waiting.
                    _active = null;
                    return;
                }

                BridgePlacementResult result = active.Operation.Step(
                    Time.timeAsDouble, Time.realtimeSinceStartupAsDouble);
                if (result != null)
                {
                    _active = null;
                    active.Completion.TrySetResult(result);
                }
            }
            catch (Exception ex)
            {
                Pending active = _active;
                _active = null;
                if (active != null)
                {
                    active.Completion.TrySetException(new BridgePlacementUnavailableException(
                        ex.GetType().Name + ": " + ex.Message));
                }
            }
        }

        /// <summary>
        /// Resolves the configured entity and builds the operation, or completes the request with
        /// the reason it cannot run. Runs on the main thread because resolution and the pause check
        /// read Unity state.
        /// </summary>
        private BridgePlacementOperation Begin(Pending pending)
        {
            bool playing = Application.isPlaying;
            bool paused = ReadPaused(playing);
            GameObject entity;
            string reason;
            if (!playing)
            {
                pending.Completion.TrySetResult(BridgePlacementResult.Unplaced("the project is not playing"));
                return null;
            }

            if (!UnityControlledEntity.TryResolve(out entity, out reason))
            {
                pending.Completion.TrySetResult(BridgePlacementResult.Unplaced(reason));
                return null;
            }

            List<double> position = pending.Request.Position;
            if (position == null || position.Count != 3)
            {
                pending.Completion.TrySetResult(
                    BridgePlacementResult.Unplaced("the requested position is not three numbers"));
                return null;
            }

            var target = new Vector3((float)position[0], (float)position[1], (float)position[2]);
            double wallBound = BridgePlacementOperation.WallBoundSeconds(
                pending.Request.SettleS, paused ? 1 : Time.timeScale);
            return new BridgePlacementOperation(
                entity, target, pending.Request.SettleS, wallBound, playing, paused);
        }

        private static bool ReadPaused(bool playing)
        {
#if UNITY_EDITOR
            try
            {
                if (EditorApplication.isPaused)
                {
                    return true;
                }
            }
            catch (Exception)
            {
                // Fall through to the time-scale rule instead of inventing a pause.
            }
#endif

            return playing && Time.timeScale == 0f;
        }

        private sealed class Pending
        {
            public readonly BridgePlacementRequest Request;

            public readonly TaskCompletionSource<BridgePlacementResult> Completion =
                new TaskCompletionSource<BridgePlacementResult>();

            public int Cancelled;

            public BridgePlacementOperation Operation;

            public Pending(BridgePlacementRequest request)
            {
                Request = request;
            }
        }
    }
}
