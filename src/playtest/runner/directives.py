"""Runner-side directive scoring and S1-facing status facts."""
from __future__ import annotations

import copy
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import TERMINAL_STATUSES, ExecStatus, Observation
from playtest.contracts.investigation import Comparator, Directive, DirectiveWindow
from playtest.contracts.loop import ChoiceKind, TurnOutcome
from playtest.core.ports import DirectiveView, FactsSource, TurnReport
from playtest.core.trace_sink import TraceSink
from playtest.roles import checks
from playtest.roles.checks import field_value

_REASONS = {
    "met": "the measured reader value satisfied the setpoint",
    "not_met": "the deadline passed and the measured reader value did not satisfy the setpoint",
    "expired": "the deadline passed without a scoreable reader value",
    "invalidated": "the session or scene changed or the measured game clock or frame reset",
    "unknown": "the directive had no scoreable postcondition",
}
# `unchanged` is a hold: the first change misses it, and only lasting to the deadline meets it.
_HOLD_REASONS = {
    "met": "the value stayed unchanged through the deadline",
    "not_met": "the value changed before the deadline",
}


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


def _op(item: Mapping[str, Any]) -> Comparator | None:
    expected = item.get("expected")
    return Comparator(expected["op"]) if isinstance(expected, Mapping) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seconds(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _game_age(now: float | None, then: float | None) -> float | None:
    """Game seconds from `then` to `now`; unknown when either clock is unknown or `now` is earlier. Never wall time."""
    if now is None or then is None:
        return None
    elapsed = now - then
    return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None


def _shown(value: Any) -> str:
    """A measured value as text for a fact line; an unmeasured one says so instead of reading as zero."""
    return "unknown" if value is None else str(value)


def _expiry_note(directive: Mapping[str, Any], now_game_s: float | None) -> str:
    """Game seconds left on an open directive: one subtraction from this observation's clock, never a verdict.

    A missing deadline or a missing clock states `unknown`; nothing here rounds, clamps, extends or acts.
    """
    deadline = _seconds(directive.get("deadline_game_s"))
    if deadline is None:
        return "time left unknown (no deadline)"
    now = _seconds(now_game_s)
    if now is None:
        return "time left unknown (no game clock)"
    return f"{deadline - now} game s left until deadline game_s {deadline}"


def _keep_note(item: Mapping[str, Any]) -> str | None:
    """What an adopted keep request measured, or which parts are unknown. Never what would have happened."""
    keep = item.get("keep")
    if not isinstance(keep, Mapping):
        return None
    requested = keep.get("requested") if isinstance(keep.get("requested"), Mapping) else {}
    parts = [f"a keep request at turn {_shown(requested.get('turn'))} (seq {_shown(requested.get('seq'))}, "
             f"game_s {_shown(requested.get('game_s'))}) had "
             f"expires_in_game_s {_shown(keep.get('expires_in_game_s'))} and was adopted at turn "
             f"{_shown(keep.get('adopted_turn'))}"]
    if item.get("status", "open") != "open":
        deadline_closed = keep.get("closed_by_deadline")
        parts.append(f"the directive closed at game_s {_shown(keep.get('closed_at_game_s'))}, "
                     f"by its deadline: {'unknown' if deadline_closed is None else 'yes' if deadline_closed else 'no'}")
    return "; ".join(parts)


def _clean_keep(keep: Mapping[str, Any]) -> dict[str, Any]:
    """The measured fields of a keep request, with anything unmeasured left `null`. Copied, never shared."""
    requested = keep.get("requested") if isinstance(keep.get("requested"), Mapping) else {}
    at, deadline = _seconds(requested.get("game_s")), _seconds(keep.get("deadline_game_s"))
    return {"directive_id": str(keep.get("directive_id") or ""),
            "requested": {"turn": _count(requested.get("turn")), "seq": _count(requested.get("seq")),
                          "game_s": at},
            "deadline_game_s": deadline,
            "expires_in_game_s": _seconds(keep.get("expires_in_game_s")),
            "request_before_deadline": at < deadline if at is not None and deadline is not None else None}


class DirectiveTracker(FactsSource):
    """Tracks frontier directives, one open at a time. It reports measurements and never selects actions.

    A directive's window starts on the first turn whose context states it. When the directive closes, the
    tracker records what S1's inputs did in that window, so a `not_met` whose test never ran says so
    (`error_class` `no_attempt` or `action_rejected`).

    Every operator but `changed` and `unchanged` is met on the first reading that satisfies it. `changed` is
    met on the first reading that differs from its baseline, and `unchanged` is a hold: every scored reading
    from the first statement to the deadline must equal the baseline. Both baselines are the value at the
    observation that first states the directive to S1.

    `directive_view` states the same directives as typed records for S1's context, after `facts` has evaluated
    the observation it is asked about. An outcome scores a setpoint over a reader and nothing else: the tracker
    never reads a directive's text, so no outcome here says a goal was reached.
    """
    def __init__(self, sink: TraceSink | None = None, *,
                 derived: Callable[[Observation], Mapping[str, Any]] | None = None) -> None:
        self.sink = sink
        self.active: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self._evaluated: tuple[str, int] | None = None  # the observation `facts` last evaluated
        # Derived setpoint readings at an observation, by field path (declared progress distances). None
        # when the manifest declared none; an absent path is unmeasured, so it scores unknown.
        self._derived = derived

    def _reading(self, field: str, observation: Observation) -> Any:
        """One setpoint field's value at this observation: a declared derived reading, or the public one."""
        if field.startswith("progress."):
            return None if self._derived is None else self._derived(observation).get(field)
        return field_value(observation.public, field.removeprefix("public."))

    def adopt(self, directive: Mapping[str, Any] | None, *, request_turn: int,
              based_on_seq: int, evidence_trace_seq: int | None = None) -> None:
        if directive is None:
            return
        self._evaluated = None  # what `facts` last evaluated no longer includes this directive
        if self.active is not None:
            superseded = self.active
            self._close("invalidated", request_turn, based_on_seq, evidence_trace_seq,
                        "superseded by a later directive")
            # Set after the close so the outcome entry already written is what it always was.
            superseded["superseded_by"] = directive.get("directive_id") or (
                directive.get("directive") or {}).get("directive_id")
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
                item["first_seq"] = observation.seq  # a bridge observation seq, never a trace entry
                item["first_session"] = observation.session
                self._anchor_deadline(item, observation.game_s)
                self._anchor_baseline(item, observation)
            item["window"] = window_counts(reports, item["first_turn"], item.get("first_game_s"))
        outcome = self._evaluate(item, observation) if item is not None else None
        if item is not None and outcome is not None:
            observation_trace = (self.sink.trace_seq_for_observation(observation.seq, observation.session)
                                 if self.sink is not None and hasattr(self.sink, "trace_seq_for_observation") else None)
            self._close(outcome, len(reports), observation.seq, observation_trace, self._reason(item, outcome),
                        closed_game_s=observation.game_s)
        # Every directive stays in S1's context with its outcome, not only on the turn it closed
        # (build bible law 4). The open one, if any, is the last line.
        lines.extend(self._fact_line(entry, observation.game_s) for entry in self.history)
        self._evaluated = (observation.session, observation.seq)
        return lines

    def rebind(self, *, client: Any = None, observation: Observation | None = None,
               generation: str | None = None) -> None:
        """Invalidate the open directive at a play-session boundary, keeping it as attributed history.

        A directive is advice about one scene and one session. After a restart its setpoint, deadline and
        baseline describe a play instance that no longer exists, so it is closed as `invalidated` at once —
        not left current until some later facts call — and its earlier measurements stay in the history
        with that outcome. No new directive is created, no action or help is selected, and `directive_view`
        states nothing until the next facts call evaluates the new generation's observation.
        """
        self._evaluated = None
        if self.active is None:
            return
        if observation is None:
            raise ValueError("invalidating a directive at a play boundary needs the first new observation")
        item = self.active
        if generation is not None:
            item["invalidated_generation"] = generation
        evidence = (self.sink.trace_seq_for_observation(observation.seq, observation.session)
                    if self.sink is not None and hasattr(self.sink, "trace_seq_for_observation") else None)
        self._close("invalidated", int(item.get("request_turn") or 0), observation.seq, evidence,
                    "the play session restarted; the directive was bound to the earlier session")

    def note_keep(self, keep: Mapping[str, Any], *, adopted_turn: int) -> None:
        """Record that an adopted answer asked for no replacement while this directive was open.

        The facts come from the request the answer replied to and the adoption turn: the directive id, that
        request's bridge turn, seq and game time, its deadline and the exact game seconds left. Nothing is
        inferred about what would have happened; whether the directive then closed at its deadline is filled in
        only if the tracker measures that close, and stays `null` otherwise (no clock, no deadline, or still
        open). A directive that has already closed keeps the keep beside its outcome, so a late answer is not
        dropped.
        """
        directive_id = keep.get("directive_id")
        item = self.active if (self.active or {}).get("directive", {}).get("directive_id") == directive_id else None
        if item is None:
            item = next((entry for entry in reversed(self.history)
                         if (entry.get("directive") or {}).get("directive_id") == directive_id), None)
        if item is None:
            return
        item["keep"] = {**_clean_keep(keep), "adopted_turn": _count(adopted_turn),
                        "closed_at_game_s": None, "closed_by_deadline": None}
        if item.get("status", "open") != "open":
            self._keep_close(item)
        # The keep is a measurement of its own: record it beside the directive at the turn it was adopted.
        self._write("keep", _count(adopted_turn) or 0, _count((item["keep"]["requested"] or {}).get("seq")) or 0,
                    item, item.get("status", "open"), None)
        self._evaluated = None  # the view states what this adoption added, once facts evaluate an observation

    @staticmethod
    def _keep_close(item: dict[str, Any]) -> None:
        """State a keep's measured close, or leave it unknown. Never a cause, a recommendation or a timing guess."""
        keep = item.get("keep")
        if not isinstance(keep, dict) or item.get("status", "open") == "open":
            return
        keep["closed_at_game_s"] = _seconds(item.get("closed_at_game_s"))
        keep["closed_by_deadline"] = (bool(item.get("closed_by_deadline"))
                                      if item.get("closed_at_game_s") is not None else None)
        requested = (keep.get("requested") or {}).get("game_s")
        deadline = keep.get("deadline_game_s")
        keep["request_before_deadline"] = (requested < deadline
                                           if requested is not None and deadline is not None else None)

    def directive_view(self, observation: Observation) -> DirectiveView | None:
        """The tracked directives as of `observation`, or `None` when none exist or `facts` has not evaluated it.

        Call this after `facts` for the same observation: only then has the open directive's deadline and baseline
        been taken from the observation that first states it, and has the directive been scored against it. Every
        directive is rebuilt from the tracker's record into a new `Directive`, so a later update cannot change a view
        already handed out. The open one is `current`; the rest are `closed`, oldest first, each with the outcome
        it ended with.

        `provenance` is measured per directive id, with `null` for anything unmeasured and no wall-time stand-in:
        - `requested`: the turn the help was asked on, the bridge observation `seq` the answer was composed
          against, and that observation's game time. A bridge `seq`, never a trace sequence number.
        - `first_stated`: the same three for the first context that stated the directive to S1.
        - `age_game_s` (the open directive only): game seconds from each of the above to this observation.
          It is a measurement. No age is compared with a limit here, and none hides or ranks a directive.
        - `expires_in_game_s` (the open directive only): `deadline_game_s - this observation's game_s`, one
          subtraction with no rounding, threshold or clamp. It is null when the record has no deadline or the
          clock is unknown; the tracker never extends a deadline or acts on the number.
        - `scored_on`: `reader_setpoint` when the directive has a setpoint, else `null`. The outcome says only
          whether a measured reader value satisfied that setpoint.
        - `baseline` (`changed` and `unchanged` setpoints): the reader value they compare with and where it was taken.
        - `superseded_by`: the directive that replaced it, when one did.
        - `keep`: present only when an adopted answer asked for no replacement while this directive was open:
          that request's stamps and exact time left, the adoption turn, and, once measured, the game time it
          closed and whether its deadline is what closed it. Unmeasured fields are `null`.
        """
        if not self.history or self._evaluated != (observation.session, observation.seq):
            return None
        current: Directive | None = None
        closed: list[Directive] = []
        provenance: dict[str, dict[str, Any]] = {}
        for item in self.history:
            directive = Directive.model_validate(item["directive"]).model_copy(deep=True)
            is_current = item is self.active
            provenance[directive.directive_id] = self._provenance(item, directive, observation, current=is_current)
            if is_current:
                current = directive
            else:
                closed.append(directive)
        return DirectiveView(current=current, closed=tuple(closed), provenance=copy.deepcopy(provenance))

    @staticmethod
    def _provenance(item: Mapping[str, Any], directive: Directive, observation: Observation,
                    *, current: bool) -> dict[str, Any]:
        def stamp(turn: Any, seq: Any, game_s: Any) -> dict[str, Any]:
            return {"turn": _count(turn), "seq": _count(seq), "game_s": _seconds(game_s)}

        requested = stamp(item.get("request_turn"), item.get("based_on_seq"), item.get("requested_game_s"))
        stated = stamp(item.get("first_turn"), item.get("first_seq"), item.get("first_game_s"))
        origin: dict[str, Any] = {"requested": requested, "first_stated": stated}
        if current:
            origin["age_game_s"] = {"since_requested": _game_age(observation.game_s, requested["game_s"]),
                                    "since_first_stated": _game_age(observation.game_s, stated["game_s"])}
            deadline = directive.deadline_game_s
            origin["expires_in_game_s"] = (deadline - observation.game_s
                                           if deadline is not None and observation.game_s is not None else None)
        if isinstance(item.get("keep"), Mapping):
            origin["keep"] = copy.deepcopy(dict(item["keep"]))
        setpoint = directive.expected
        origin["scored_on"] = "reader_setpoint" if setpoint is not None else None
        if setpoint is not None and setpoint.op in checks.CHANGE:
            source = item.get("baseline_source")
            origin["baseline"] = {"value": item.get("baseline") if source is not None else None, "source": source}
        if item.get("superseded_by") is not None:
            origin["superseded_by"] = item["superseded_by"]
        return origin

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

    def _anchor_baseline(self, item: dict[str, Any], observation: Observation) -> None:
        """Take a changed/unchanged baseline from the observation that first states the directive to S1.

        The frontier is told to compare with the value when S1 receives the directive. Like the deadline, that
        is this observation and not the escalation's, which is older by the frontier call and the next turn's
        pacing; a value that moved in between would otherwise count as a change S1 never caused. The escalation's
        value stays as the fallback for a field this observation cannot read. `baseline_source` records which one
        scoring uses: "first_statement", "escalation", or null when neither could be read (then nothing scores).
        """
        expected = item.get("expected")
        if _op(item) not in checks.CHANGE:
            return
        stated = self._reading(expected["field"], observation)
        if stated is not None:
            item["baseline"], item["baseline_source"] = stated, "first_statement"
        else:
            item["baseline_source"] = "escalation" if item.get("baseline") is not None else None

    @staticmethod
    def _reason(item: Mapping[str, Any], outcome: str) -> str:
        reasons = {**_REASONS, **_HOLD_REASONS} if _op(item) is Comparator.unchanged else _REASONS
        reason = reasons[outcome]
        if item.get("baseline_source") == "escalation" and outcome in ("met", "not_met", "expired"):
            reason += "; baseline from the escalation, as the field could not be read when S1 first saw the directive"
        return reason

    def finish(self, *, turn: int, based_on_seq: int, evidence_trace_seq: int | None = None) -> None:
        if self.active is not None:
            self._close("unknown", turn, based_on_seq, evidence_trace_seq, "session ended before scoring")

    def _evaluate(self, item: Mapping[str, Any], observation: Observation) -> str | None:
        if (observation.scene != item.get("scene")
                or observation.session != item.get("first_session", observation.session)):
            return "invalidated"
        old_frame, old_game = item.get("last_frame"), item.get("last_game_s")
        if ((old_frame is not None and observation.frame < old_frame)
                or (old_game is not None and observation.game_s is not None and observation.game_s < old_game)):
            return "invalidated"
        item["last_frame"], item["last_game_s"] = observation.frame, observation.game_s
        deadline = item.get("deadline_game_s")
        due = deadline is not None and observation.game_s is not None and observation.game_s >= deadline
        expected = item.get("expected")
        if expected is None:
            outcome = "unknown" if due else None
        else:
            op = Comparator(expected["op"])
            value = self._reading(expected["field"], observation)
            scored = checks.evaluate(value, kind=item["kind"], op=op, expected=expected.get("value"),
                                     baseline=item.get("baseline"), tolerance=item.get("tolerance"))
            if op is Comparator.unchanged:
                outcome = self._hold(item, scored, due)
            elif scored is True:
                outcome = "met"
            elif due:
                outcome = "not_met" if scored is False else "expired"
            else:
                outcome = None
        # Whether the deadline is what ended it: a measurement for a keep beside the outcome, never a cause.
        if outcome is not None:
            item["closed_by_deadline"] = bool(due and outcome in ("expired", "not_met", "unknown"))
        return outcome

    @staticmethod
    def _hold(item: dict[str, Any], scored: bool | None, due: bool) -> str | None:
        """Score one reading of an `unchanged` setpoint. The first scored change misses it, and only lasting to
        the deadline meets it. A reading that cannot be scored is skipped, never counted as held; if none could
        be scored by the deadline nothing measured the hold, so it expires."""
        if scored is False:
            return "not_met"
        if scored is True:
            item["held_readings"] = item.get("held_readings", 0) + 1
        if not due:
            return None
        return "met" if item.get("held_readings") else "expired"

    def _close(self, status: str, turn: int, seq: int, evidence: int | None, reason: str, *,
               closed_game_s: float | None = None) -> None:
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
        item.update(status=status, outcome=status, reason=reason, window=window, error_class=error_class,
                    closed_at_game_s=_seconds(closed_game_s) if status != "invalidated" else None)
        self._keep_close(item)  # measured here only: what the keep asked for and how the directive ended
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
            "keep": copy.deepcopy(item.get("keep")),
            "outcome": outcome, "evidence_trace_seq": evidence, "reason": item.get("reason"),
        }, author="frontier", turn=None, refs=refs)

    @staticmethod
    def _fact_line(item: Mapping[str, Any], now_game_s: float | None = None) -> str:
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
            head = f"Frontier directive {d.get('directive_id')}: {d.get('text')}"
        elif sp.get("op") == Comparator.unchanged:  # a hold, stated as one: its deadline is when it is met
            head = (f"Frontier directive {d.get('directive_id')}: goal {d.get('text')}; setpoint "
                    f"{sp.get('field')} unchanged until game_s {d.get('deadline_game_s')}")
        else:
            val = "null" if sp.get("value") is None else repr(sp.get("value"))
            head = (f"Frontier directive {d.get('directive_id')}: goal {d.get('text')}; setpoint "
                    f"{sp.get('field')} {sp.get('op')} {val}; deadline game_s {d.get('deadline_game_s')}")
        # Only an open directive has time left to state, and it is one subtraction from this observation's clock.
        parts = [head]
        if item.get("status", "open") == "open":
            parts.append(_expiry_note(d, now_game_s))
        parts.append(f"status {status}")
        keep = _keep_note(item)
        if keep:
            parts.append(keep)
        return "; ".join(parts) + "."
