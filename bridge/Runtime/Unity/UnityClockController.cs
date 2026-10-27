using System;
using System.Collections.Concurrent;
using System.Threading;
using Playtest.Bridge.Protocol;
using UnityEngine;
#if UNITY_EDITOR
using UnityEditor;
#endif

namespace Playtest.Bridge.Clock
{
    /// <summary>
    /// Unity clock control. Requests arrive on the transport thread and are applied on the Unity
    /// main thread. Pause/resume/set_scale are immediate on the next tick; step advances the game
    /// clock for the requested game-time interval and then returns to paused.
    /// </summary>
    public sealed class UnityClockController : IBridgeClock, IDisposable
    {
        private const string Realtime = "realtime";
        private const string Stepped = "stepped";

        private readonly ConcurrentQueue<ClockRequestMessage> _requests =
            new ConcurrentQueue<ClockRequestMessage>();
        private readonly object _gate = new object();
        private volatile string _clockMode = Realtime;
        private volatile bool _paused;
        private double _timeScale = 1;
        private bool _started;
        private bool _disposed;
        private bool _stepActive;
        private volatile bool _resetRequested;
        private volatile bool _isPlaying;
        private readonly int _mainThreadId;
        private double _stepStartGameS;
        private double _stepGoalS;

        public UnityClockController()
        {
            _mainThreadId = Thread.CurrentThread.ManagedThreadId;
            _isPlaying = UnityEngine.Application.isPlaying;
        }

        public string ClockMode
        {
            get { return _clockMode; }
        }

        public bool Paused
        {
            get { return _paused; }
        }

        public double TimeScale
        {
            get
            {
                lock (_gate)
                {
                    return _timeScale;
                }
            }
        }

        public void Start()
        {
            if (_started)
            {
                return;
            }

            _started = true;
#if UNITY_EDITOR
            EditorApplication.update -= Tick;
            EditorApplication.update += Tick;
#else
            Application.onBeforeRender -= Tick;
            Application.onBeforeRender += Tick;
#endif
        }

        public void Stop()
        {
            if (!_started)
            {
                return;
            }

            _started = false;
#if UNITY_EDITOR
            EditorApplication.update -= Tick;
#else
            Application.onBeforeRender -= Tick;
#endif
            Reset();
        }

        public bool TryApply(ClockRequestMessage request, out string error)
        {
            error = null;
            if (request == null)
            {
                error = "missing clock request";
                return false;
            }

            if (!_isPlaying)
            {
                error = "the project is not playing";
                return false;
            }

            _requests.Enqueue(request);
            return true;
        }

        public void Reset()
        {
            if (Thread.CurrentThread.ManagedThreadId != _mainThreadId)
            {
                _resetRequested = true;
                return;
            }

            ApplyReset();
        }

        private void ApplyReset()
        {
            lock (_gate)
            {
                _stepActive = false;
                _paused = false;
                _clockMode = Realtime;
                _timeScale = 1;
                Time.timeScale = 1;
            }
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            Stop();
        }

        private void Tick()
        {
#if UNITY_EDITOR
            // In the Editor the bridge runs in play mode; edit-mode ticks are harmless.
#endif
            if (_resetRequested)
            {
                _resetRequested = false;
                ApplyReset();
            }

            _isPlaying = Application.isPlaying;
            while (_requests.TryDequeue(out ClockRequestMessage request))
            {
                Apply(request);
            }

            if (_stepActive)
            {
                double elapsed = Time.timeAsDouble - _stepStartGameS;
                if (elapsed >= _stepGoalS)
                {
                    lock (_gate)
                    {
                        _stepActive = false;
                        _paused = true;
                        _clockMode = Stepped;
                        _timeScale = 0;
                        Time.timeScale = 0;
                    }
                }
            }
        }

        private void Apply(ClockRequestMessage request)
        {
            switch (request.Op)
            {
                case BridgeClockOps.Pause:
                    lock (_gate)
                    {
                        _stepActive = false;
                        _paused = true;
                        _clockMode = Stepped;
                        _timeScale = 0;
                        Time.timeScale = 0;
                    }
                    break;

                case BridgeClockOps.Resume:
                    lock (_gate)
                    {
                        _stepActive = false;
                        _paused = false;
                        _clockMode = Realtime;
                        _timeScale = 1;
                        Time.timeScale = 1;
                    }
                    break;

                case BridgeClockOps.SetScale:
                    double scale = request.TimeScale ?? 1;
                    lock (_gate)
                    {
                        _stepActive = false;
                        _timeScale = scale;
                        _paused = scale == 0;
                        _clockMode = scale == 0 ? Stepped : Realtime;
                        Time.timeScale = (float)scale;
                    }
                    break;

                case BridgeClockOps.Step:
                    double step = request.StepGameS ?? 0;
                    if (step <= 0)
                    {
                        break;
                    }

                    lock (_gate)
                    {
                        _stepActive = true;
                        _stepStartGameS = Time.timeAsDouble;
                        _stepGoalS = step;
                        _paused = false;
                        _clockMode = Stepped;
                        _timeScale = 1;
                        Time.timeScale = 1;
                    }
                    break;
            }
        }
    }
}
