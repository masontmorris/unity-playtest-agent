using System;
using System.Collections.Generic;
using Playtest.Bridge.Actions;
using UnityEngine;
#if UNITY_EDITOR
using UnityEditor;
#endif

namespace Playtest.Bridge.Unity
{
    /// <summary>
    /// Input System backend for keyboard, mouse and gamepad actions. The device work is in
    /// <see cref="BridgeInputExecutor"/> (applying, verifying and releasing, with the main-thread
    /// rules) over <see cref="InputSystemSurface"/> (the Input System itself). This class wires them
    /// to the Unity update loop and keeps the original single-key calls for callers that apply and
    /// release without a lease. The manifest action names a device control, never a game control.
    /// </summary>
    public sealed class InputSystemInputBackend : IBridgeInputBackend
    {
        private readonly InputSystemSurface _surface;
        private readonly BridgeInputExecutor _executor;
        private BridgeInputRun _legacyRun = new BridgeInputRun("direct");
        private bool _pumping;

        public InputSystemInputBackend()
        {
            // Constructed on the Unity main thread: the executor treats this thread as the one
            // that runs device work.
            _surface = new InputSystemSurface();
            _executor = new BridgeInputExecutor(_surface);
            _surface.UpdateObserved = _executor.OnGameInputUpdate;
#if UNITY_EDITOR
            EditorApplication.update += _executor.Pump;
#else
            Application.onBeforeRender += _executor.Pump;
#endif
            _pumping = true;
        }

        public string Name
        {
            get { return _executor.Name; }
        }

        public bool IsAvailable
        {
            get { return _executor.IsAvailable; }
        }

        public IReadOnlyList<string> ReleasedInputs
        {
            get { return _executor.ReleasedInputs; }
        }

        public bool TryValidate(BridgeActionDefinition action, out string error)
        {
            return _executor.TryValidate(action, out error);
        }

        public bool TryApplyInputs(
            BridgeInputRun run,
            int stepIndex,
            IReadOnlyList<BridgeInputBinding> inputs,
            out BridgeInputAck ack,
            out string error)
        {
            return _executor.TryApplyInputs(run, stepIndex, inputs, out ack, out error);
        }

        public bool TryReleaseInputs(BridgeInputRun run, out bool releaseVerified, out string error)
        {
            return _executor.TryReleaseInputs(run, out releaseVerified, out error);
        }

        public bool TryVerifyRelease(out string error)
        {
            return _executor.TryVerifyRelease(out error);
        }

        /// <summary>
        /// Applies an action's inputs without a lease and returns once a device read confirmed
        /// them. Skills are executed by the lease, which times and releases every step.
        /// </summary>
        public bool TryApply(BridgeActionDefinition action, out string error)
        {
            if (!TryValidate(action, out error))
            {
                return false;
            }

            if (action.IsSkill)
            {
                error = "a skill is executed by the lease, not applied in one call";
                return false;
            }

            if (_legacyRun.Outstanding == 0)
            {
                _legacyRun = new BridgeInputRun(action.Name);
            }

            return _executor.TryApplyInputs(_legacyRun, 0, action.InputSets()[0], out _, out error);
        }

        /// <summary>Releases what <see cref="TryApply"/> applied and verifies it from device reads.</summary>
        public bool TryRelease(out bool releaseVerified, out string error)
        {
            return _executor.TryReleaseInputs(_legacyRun, out releaseVerified, out error);
        }

        public void Dispose()
        {
            if (_pumping)
            {
                _pumping = false;
#if UNITY_EDITOR
                EditorApplication.update -= _executor.Pump;
#else
                Application.onBeforeRender -= _executor.Pump;
#endif
            }

            _executor.Dispose();
            _surface.Dispose();
        }
    }
}
