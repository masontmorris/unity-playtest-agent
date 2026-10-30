"""Runner-side directive scoring and S1-facing status facts."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import TERMINAL_STATUSES, ExecStatus, Observation
from playtest.contracts.investigation import Comparator, DirectiveWindow
from playtest.contracts.loop import ChoiceKind, TurnOutcome
from playtest.core.ports import FactsSource, TurnReport
from playtest.core.trace_sink import TraceSink
from playtest.roles import checks


def window_counts(reports: Sequence[TurnReport], first_turn: int, first_game_s: float | None = None) -> dict[str, Any]:
    """What S1's inputs did from `first_turn` on: sent, completed, rejected by reason, and the rest.

    Counts from the bridge's own terminal records, never a verdict. An input whose last record is
    not terminal, or that has no record at all, is counted as unrecorded rather than guessed.
    """
    sent = completed = unrecorded = waits = escalations = 0
    rejected: Counter[str] = Counter()
    unfinished: Counter[str] = Counter()
    for report in reports[first_turn:]:
        if report.selected is not None and report.selected.kind is ChoiceKind.wait:
            waits += 1
        if report.record.outcome is TurnOutcome.escalated:
            escalations += 1
        for item in report.executed:
            sent += 1
            records = item.execution.records
            terminal = records[-1] if records else None
            if terminal is None or terminal.status not in TERMINAL_STATUSES:
                unrecorded += 1
            elif terminal.status is ExecStatus.completed:
                completed += 1
            elif terminal.status is ExecStatus.rejected:
                if terminal.reject_reason is None:  # the contract forbids it; never guess a reason
                    unrecorded += 1
                else:
                    rejected[terminal.reject_reason.value] += 1
            else:
                unfinished[terminal.status.value] += 1
    window = {"first_turn": first_turn, "first_game_s": first_game_s,
              "turns": max(0, len(reports) - first_turn), "inputs_sent": sent,
              "inputs_completed": completed, "inputs_rejected": dict(rejected), "inputs_unfinished": dict(unfinished),
              "inputs_unrecorded": unrecorded, "waits": waits, "escalations": escalations}
    return DirectiveWindow.model_validate(window).model_dump(mode="json")


def _window_note(window: Mapping[str, Any]) -> str:
    rejected = window["inputs_rejected"]
    detail = f" ({', '.join(f'{reason} {count}' for reason, count in sorted(rejected.items()))})" if rejected else ""
    unfinished = sum(window["inputs_unfinished"].values()) + window["inputs_unrecorded"]
    return (f"window: {window['turns']} turns from turn {window['first_turn']}, {window['inputs_sent']} inputs sent, "
            f"{window['inputs_completed']} completed, {sum(rejected.values())} rejected{detail}"
            + (f", {unfinished} unfinished or unrecorded" if unfinished else "")
            + f", {window['waits']} waits")


def field_value(public: Mapping[str, Any], path: str) -> Any:
    node: Any = public
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


class DirectiveTracker(FactsSource):
    """Tracks frontier directives, one open at a time. It reports measurements and never selects actions.

    A directive's window starts on the first turn whose context states it. When the directive closes, the
    tracker records what S1's inputs did in that window, so a `not_met` whose test never ran says so
    (`error_class` `no_attempt` or `action_rejected`).
    """
    def __init__(self, sink: TraceSink | None = None) -> None:
        self.sink = sink
        self.active: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []

    def adopt(self, directive: Mapping[str, Any] | None, *, request_turn: int,
              based_on_seq: int, evidence_trace_seq: int | None = None) -> None:
        if directive is None:
            return
        if self.active is not None:
            self._close("invalidated", request_turn, based_on_seq, evidence_trace_seq,
                        "superseded by a later directive")
        item = dict(directive)
        item["status"] = "open"
        item["request_turn"] = request_turn
        item["based_on_seq"] = based_on_seq
        item["first_turn"] = None  # set when a context first states it
        self.active = item
        self.history.append(item)
        item["trace_seq"] = self._write("directive", request_turn, based_on_seq, item, "open", evidence_trace_seq)

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        from playtest.core.facts import RecordFacts
        lines = list(await RecordFacts().facts(reports, observation))
        item = self.active
        if item is not None:
            if item.get("first_turn") is None:
                item["first_turn"] = len(reports)  # this turn's context is the first to state it
                item["first_game_s"] = observation.game_s
                self._anchor_deadline(item, observation.game_s)
            item["window"] = window_counts(reports, item["first_turn"], item.get("first_game_s"))
        outcome = self._evaluate(item, observation) if item is not None else None
        if item is not None and outcome is not None:
            observation_trace = (self.sink.trace_seq_for_observation(observation.seq)
                                 if self.sink is not None and hasattr(self.sink, "trace_seq_for_observation") else None)
            reason = {
                "met": "the measured reader value satisfied the setpoint",
                "not_met": "the deadline passed and the measured reader value did not satisfy the setpoint",
                "expired": "the deadline passed without a scoreable reader value",
                "invalidated": "the scene changed or the measured game clock reset",
                "unknown": "the directive had no scoreable postcondition",
            }[outcome]
            self._close(outcome, len(reports), observation.seq, observation_trace, reason)
        # Every directive stays in S1's context with its outcome, not only on the turn it closed
        # (build bible law 4). The open one, if any, is the last line.
        lines.extend(self._fact_line(entry) for entry in self.history)
        return lines

    @staticmethod
    def _anchor_deadline(item: dict[str, Any], stated_game_s: float | None) -> None:
        """Count the frontier's deadline from the observation that first states the directive to S1.

        The frontier gives a relative deadline. `to_directive` turns it into game time from the escalation's
        observation, but the answer reaches S1 only after the frontier call and the next turn's pacing. On the
        #132 validation run, 11 of 14 deadlines had passed before S1 first saw its directive. The relative
        length is kept; only its start moves to when S1 received it. Without both game times it stays as given.
        """
        requested = item.get("requested_game_s")
        directive = item.get("directive") if isinstance(item.get("directive"), dict) else item
        deadline = directive.get("deadline_game_s")
        if None in (requested, deadline, stated_game_s):
            return
        anchored = stated_game_s + (deadline - requested)
        directive["deadline_game_s"] = anchored
        item["deadline_game_s"] = anchored

    def finish(self, *, turn: int, based_on_seq: int, evidence_trace_seq: int | None = None) -> None:
        if self.active is not None:
            self._close("unknown", turn, based_on_seq, evidence_trace_seq, "session ended before scoring")

    def _evaluate(self, item: Mapping[str, Any], observation: Observation) -> str | None:
        if observation.scene != item.get("scene"):
            return "invalidated"
        old_frame, old_game = item.get("last_frame"), item.get("last_game_s")
        if ((old_frame is not None and observation.frame < old_frame)
                or (old_game is not None and observation.game_s is not None and observation.game_s < old_game)):
            return "invalidated"
        item["last_frame"], item["last_game_s"] = observation.frame, observation.game_s
        expected = item.get("expected")
        if expected is not None:
            path = expected["field"].removeprefix("public.")
            value = field_value(observation.public, path)
            scored = checks.evaluate(value, kind=item["kind"], op=Comparator(expected["op"]),
                                     expected=expected.get("value"), baseline=item.get("baseline"),
                                     tolerance=item.get("tolerance"))
            if scored is True:
                return "met"
            deadline = item.get("deadline_game_s")
            if deadline is not None and observation.game_s is not None and observation.game_s >= deadline:
                return "not_met" if scored is False else "expired"
        elif item.get("deadline_game_s") is not None and observation.game_s is not None \
                and observation.game_s >= item["deadline_game_s"]:
            return "unknown"
        return None

    def _close(self, status: str, turn: int, seq: int, evidence: int | None, reason: str) -> None:
        assert self.active is not None
        item = self.active
        first = item.get("first_turn")
        # The window as of the last context built; a directive no context stated has an empty one.
        window = item.get("window") or window_counts((), first if first is not None else turn, item.get("first_game_s"))
        error_class = None
        if status == "not_met" and window["inputs_completed"] == 0:
            if window["inputs_sent"] == 0:
                error_class = "no_attempt"
            elif sum(window["inputs_rejected"].values()):
                error_class = "action_rejected"
        if window["inputs_completed"] == 0:
            reason = f"{reason}; no input completed in its window"
        item.update(status=status, outcome=status, reason=reason, window=window, error_class=error_class)
        if isinstance(item.get("directive"), dict):
            item["directive"].update(outcome=status, evidence_seq=[evidence] if evidence is not None else [],
                                     window=window, error_class=error_class)
        self._write("outcome", turn, seq, item, status, evidence)
        self.active = None

    def _write(self, event: str, turn: int, seq: int, item: Mapping[str, Any], outcome: str,
               evidence: int | None) -> int | None:
        if self.sink is None:
            return None
        refs = [value for value in (evidence, item.get("trace_seq")) if isinstance(value, int)]
        return self.sink.append("investigation", {
            "event": event, "request_turn": item.get("request_turn", turn),
            "based_on_seq": item.get("based_on_seq", seq), "outcome_seq": seq,
            "hypothesis": item.get("hypothesis"), "directive": item.get("directive", dict(item)),
            "outcome": outcome, "evidence_trace_seq": evidence, "reason": item.get("reason"),
        }, author="frontier", turn=None, refs=refs)

    @staticmethod
    def _fact_line(item: Mapping[str, Any]) -> str:
        d = item.get("directive", item)
        sp = d.get("expected")
        status = item.get("status", "open")
        if status != "open" and item.get("reason"):
            status = f"{status} ({item['reason']})"
        if status != "open" and isinstance(item.get("window"), Mapping):
            status = f"{status}; {_window_note(item['window'])}"
            if item.get("error_class"):
                status = f"{status}; error class {item['error_class']}"
        if not isinstance(sp, Mapping):
            return f"Frontier directive {d.get('directive_id')}: {d.get('text')}; status {status}."
        op = sp.get("op")
        val = "null" if sp.get("value") is None else repr(sp.get("value"))
        return (f"Frontier directive {d.get('directive_id')}: goal {d.get('text')}; setpoint "
                f"{sp.get('field')} {op} {val}; deadline game_s {d.get('deadline_game_s')}; "
                f"status {status}.")
