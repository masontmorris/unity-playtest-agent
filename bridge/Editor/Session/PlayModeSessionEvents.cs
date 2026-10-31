// SPDX-License-Identifier: Apache-2.0
using System;

namespace Playtest.Bridge.Editor.Session
{
    /// <summary>One observed Editor play-mode transition, without the Unity enum that raised it.</summary>
    public enum SessionPlayModeChange
    {
        EnteredPlayMode,
        EnteredEditMode,
        ExitingPlayMode,
    }

    /// <summary>
    /// What the controller must do after a transition. The event rules live here, beside the machine
    /// they drive, so the callback and the tests share one implementation; the controller only persists,
    /// publishes and asks the Editor to exit.
    /// </summary>
    public struct SessionEventEffect
    {
        public bool Persist;
        public bool Publish;
        public bool RequestExit;
        public bool ExitFailed;
    }

    /// <summary>
    /// Applies an observed play-mode transition to the session machine. It is deliberately Unity-free:
    /// the caller supplies the observed instant, and the machine itself enforces order, idempotence and
    /// fail-closed behaviour. An event may arrive after a domain reload but before the first Editor
    /// update, so the caller must have restored the persisted machine first; nothing here guesses a
    /// state from flags or invents a timestamp.
    /// </summary>
    public static class PlayModeSessionEvents
    {
        public const string UnexpectedPlayModeExitReason = "unexpected_play_mode_exit";

        public static SessionEventEffect Apply(
            PlayModeSessionMachine machine, SessionPlayModeChange change, DateTime nowUtc)
        {
            if (machine == null)
            {
                throw new ArgumentNullException(nameof(machine));
            }

            var effect = new SessionEventEffect();
            switch (change)
            {
                case SessionPlayModeChange.EnteredPlayMode:
                    if (machine.Ticket != null && !machine.Ticket.IsTerminal)
                    {
                        // The machine keeps the first observation: a duplicate event is rejected and
                        // never moves an existing timestamp or publishes readiness.
                        machine.MarkPlayModeEntered(nowUtc);
                        effect.Persist = true;
                        effect.Publish = true;
                    }

                    break;

                case SessionPlayModeChange.EnteredEditMode:
                    if (machine.HostPhase == PlayModeSessionPhase.Ready)
                    {
                        // Not a selected restart: the bridge is gone and this run fails closed.
                        machine.Fail(UnexpectedPlayModeExitReason, nowUtc);
                        effect.Persist = true;
                        effect.Publish = true;
                        effect.RequestExit = true;
                        effect.ExitFailed = true;
                    }
                    else if (machine.Ticket != null && !machine.Ticket.IsTerminal)
                    {
                        // Valid only after the bridge release for this ticket; the machine rejects a
                        // premature or repeated event without starting another instance.
                        machine.MarkEditModeEntered(nowUtc);
                        effect.Persist = true;
                        effect.Publish = true;
                    }
                    else if (machine.HostPhase == PlayModeSessionPhase.Starting)
                    {
                        // An initial start that left Play Mode by itself is not a restart S1 selected.
                        machine.Fail(UnexpectedPlayModeExitReason, nowUtc);
                        effect.Persist = true;
                        effect.Publish = true;
                        effect.RequestExit = true;
                        effect.ExitFailed = true;
                    }
                    else
                    {
                        machine.ClearPlayInstance();
                        effect.Persist = true;
                    }

                    break;

                case SessionPlayModeChange.ExitingPlayMode:
                    effect.Persist = true;
                    break;

                default:
                    throw new ArgumentOutOfRangeException(nameof(change));
            }

            return effect;
        }
    }
}
