"""Runner-side directive scoring and S1-facing status facts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.investigation import Comparator
from playtest.core.ports import FactsSource, TurnReport
from playtest.core.trace_sink import TraceSink
from playtest.roles import checks


def field_value(public: Mapping[str, Any], path: str) -> Any:
    node: Any = public
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


class DirectiveTracker(FactsSource):
    """Tracks one frontier directive. It reports measurements and never selects actions."""
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
        self.active = item
        self.history.append(item)
        item["trace_seq"] = self._write("directive", request_turn, based_on_seq, item, "open", evidence_trace_seq)

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        from playtest.core.facts import RecordFacts
        lines = list(await RecordFacts().facts(reports, observation))
        item = self.active
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
        item["status"] = status
        item["outcome"] = status
        item["reason"] = reason
        if isinstance(item.get("directive"), dict):
            item["directive"]["outcome"] = status
            item["directive"]["evidence_seq"] = [evidence] if evidence is not None else []
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
        if not isinstance(sp, Mapping):
            return f"Frontier directive {d.get('directive_id')}: {d.get('text')}; status {status}."
        op = sp.get("op")
        val = "null" if sp.get("value") is None else repr(sp.get("value"))
        return (f"Frontier directive {d.get('directive_id')}: goal {d.get('text')}; setpoint "
                f"{sp.get('field')} {op} {val}; deadline game_s {d.get('deadline_game_s')}; "
                f"status {status}.")
