"""Facts the loop can state from its own records of the last turn.

Each line states something measured: what S1's answer led to, what the bridge answered, the applied
duration and release verification it reported, and an effect probe's expected-versus-observed line.
A value the bridge or probe did not report reads `unknown`. A line carries counts and margins but never
a recommended action, and nothing here decides what S1 may choose (S1 ownership rule 3).
"""

from __future__ import annotations

from collections.abc import Sequence

from playtest.contracts.bridge import ExecStatus, Observation
from playtest.contracts.loop import TurnOutcome

from .ports import Executed, TurnReport

_HELP_OUTCOMES = (TurnOutcome.escalated, TurnOutcome.delegated)


class RecordFacts:
    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        if not reports:
            return []
        last = reports[-1]
        lines = [self._outcome(last)]
        for sent in last.executed:
            if not sent.execution.rejected:
                lines.append(self._execution(sent))
            if sent.effect is not None and sent.effect.fact:
                lines.append(sent.effect.fact)
        return lines

    @staticmethod
    def _outcome(report: TurnReport) -> str:
        record = report.record
        prefix = f"Turn {record.turn}:"
        if record.outcome is TurnOutcome.rejected_invalid:
            if report.failure is not None:
                return f"{prefix} the provider gave no usable answer ({report.failure}); no input was sent."
            return f"{prefix} the answer was rejected ({report.problem}); no input was sent."
        chosen = record.effective_choice
        rejection = next((s for s in report.executed if s.execution.rejected), None)
        if rejection is not None and record.outcome is TurnOutcome.rejected_stale:
            reason = rejection.execution.terminal.reject_reason
            return (f"{prefix} the bridge rejected {rejection.choice.id} "
                    f"({reason.value if reason is not None else 'unknown'}); no input was sent.")
        if record.outcome is TurnOutcome.executed:
            return f"{prefix} {chosen} was executed."
        if record.outcome in _HELP_OUTCOMES:
            return f"{prefix} {chosen} was chosen; {RecordFacts._pending(report, rejection)}"
        return f"{prefix} {chosen} was chosen; no input was sent."

    @staticmethod
    def _pending(report: TurnReport, rejection: Executed | None) -> str:
        record = report.record
        opened = ("a help request was opened" if report.help_event == "opened"
                  else "a help request was already open, so no new request was made")
        pending = record.while_pending
        if pending is None:
            return f"{opened}."
        how = "wait" if pending.mode == "wait" else f"continue with {pending.choice_id}"
        origin = " (the answer gave none, so wait was recorded)" if record.policy_default else ""
        text = f"{opened}; while help is pending: {how}{origin}"
        if rejection is not None:
            reason = rejection.execution.terminal.reject_reason
            text += (f"; the bridge rejected {rejection.choice.id} "
                     f"({reason.value if reason is not None else 'unknown'}), so no input was sent")
        return text + "."

    @staticmethod
    def _execution(sent: Executed) -> str:
        terminal = sent.execution.terminal
        duration = terminal.applied_duration_s
        text = f"{duration:.3f} s" if duration is not None else "unknown"
        verified = {True: "true", False: "false", None: "unknown"}[terminal.release_verified]
        status = terminal.status.value if terminal.status is not ExecStatus.rejected else "rejected"
        return f"{sent.choice.id} ended {status}; applied duration {text}; release verified {verified}."
