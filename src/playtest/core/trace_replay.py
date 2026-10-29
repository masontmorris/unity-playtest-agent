"""Re-derive decisions and adherence from append-only evidence; replay never repairs entries."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from pydantic import ValidationError

from playtest.contracts.bridge import ExecutionRecord, Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceKind, ChoiceSet, Decision, TurnOutcome, TurnRecord
from playtest.contracts.trace import TraceEntry, TraceExecutionRequest, TraceKind
from .adherence import check_adherence
from .context import canonical
from .decisions import parse_decision


@dataclass(frozen=True)
class Problem:
    message: str
    trace_seq: int | None = None
    turn: int | None = None

    def __str__(self) -> str:
        where = f"seq {self.trace_seq}" if self.trace_seq is not None else "trace"
        if self.turn is not None:
            where += f", turn {self.turn}"
        return f"{where}: {self.message}"


def replay(entries: Iterable[TraceEntry]) -> list[Problem]:
    rows = list(entries)
    problems: list[Problem] = []
    def issue(message: str, row: TraceEntry | None = None, turn: int | None = None) -> None:
        problems.append(Problem(message, row.trace_seq if row else None, turn))

    if not rows or rows[0].kind is not TraceKind.run:
        issue("entry 0 must be run")
    elif rows[0].body.get("run_id") != rows[0].run_id:
        issue("run entry body run_id differs from entry run_id", rows[0])
    ids = {r.run_id for r in rows}
    if len(ids) > 1:
        issue("trace contains more than one run_id")
    ends = [i for i, r in enumerate(rows) if r.kind is TraceKind.end]
    if len(ends) > 1 or (ends and ends[0] != len(rows) - 1):
        issue("end must occur at most once and be last")
    for i, row in enumerate(rows):
        if row.trace_seq != i:
            issue(f"trace_seq is {row.trace_seq}, expected {i}", row)
        if any(ref >= row.trace_seq for ref in row.refs):
            issue("ref does not point backward", row)

    groups: dict[int, list[TraceEntry]] = defaultdict(list)
    for row in rows:
        if row.turn is not None:
            groups[row.turn].append(row)
    turn_records: list[TurnRecord] = []
    if sorted(groups) != list(range(len(groups))):
        issue("turn numbers are not contiguous from 0")
    seen_turns: set[int] = set()
    last_turn: int | None = None
    for row in rows:
        if row.turn is None:
            continue
        if last_turn is not None and row.turn < last_turn:
            issue("turn entries are out of trace order", row, row.turn)
        if last_turn is not None and row.turn > last_turn:
            seen_turns.add(last_turn)
        if row.turn in seen_turns:
            issue("turn entries are interleaved", row, row.turn)
        last_turn = row.turn
    last_version: int | None = None
    help_open = False
    adopted_or_cancelled: set[int] = set()
    for row in rows:
        if row.kind is TraceKind.help:
            event = row.body["event"]
            if event == "opened":
                if help_open:
                    issue("a help request was opened while another was open", row)
                else:
                    help_open = True
            elif event == "repeat":
                if not help_open:
                    issue("help repeat occurred without an open request", row)
            else:
                target = next((x for x in rows if x.trace_seq in row.refs and x.kind is TraceKind.help and x.body["event"] == "opened"), None)
                if target is None:
                    issue("help resolution does not ref an opened request", row)
                else:
                    if target.trace_seq in adopted_or_cancelled:
                        issue("help request resolved more than once", row)
                    adopted_or_cancelled.add(target.trace_seq)
                    if event == "adopted" and not help_open:
                        issue("help adopted when no request was open", row)
                    if event == "cancelled" and not help_open:
                        issue("help cancelled when no request was open", row)
                    help_open = False

    raised: set[str] = set()
    observed_at: dict[int | None, int] = {}
    for row in rows:
        if row.kind is TraceKind.observation:
            observed_at.setdefault(row.turn, row.trace_seq)
        elif row.kind is TraceKind.suspicion:
            suspicion_id = row.body["suspicion_id"]
            if suspicion_id in raised:
                issue(f"suspicion {suspicion_id} was raised more than once", row)
            raised.add(suspicion_id)
            if row.turn not in observed_at:
                issue("a suspicion precedes the observation of its turn", row, row.turn)

    for turn_no in sorted(groups):
        group = groups[turn_no]
        kinds = [x.kind for x in group]
        adopted = [x for x in group if x.kind is TraceKind.help and x.body.get("event") == "adopted"]
        if adopted and group[0] is not adopted[0]:
            issue("adopted help must open the turn", adopted[0], turn_no)
        rank = {TraceKind.observation: 0, TraceKind.choice_set: 1, TraceKind.context: 2,
                TraceKind.s1_call: 3, TraceKind.decision: 4, TraceKind.execution: 5,
                TraceKind.effect: 6, TraceKind.turn: 7}
        ordered = [rank[x.kind] for x in group if x.kind in rank]
        if ordered != sorted(ordered):
            issue("turn entries are out of documented order", group[-1], turn_no)
        for required in (TraceKind.observation, TraceKind.choice_set, TraceKind.context, TraceKind.decision, TraceKind.turn):
            if kinds.count(required) != 1:
                issue(f"turn must contain exactly one {required.value} entry", group[-1], turn_no)
        if TraceKind.turn not in kinds:
            issue("turn entries stop before a turn record", group[-1], turn_no)
            if turn_no != max(groups):
                issue("truncated turn is not final", group[-1], turn_no)
            continue
        record_row = next(x for x in group if x.kind is TraceKind.turn)
        obsrow = next((x for x in group if x.kind is TraceKind.observation), None)
        setrow = next((x for x in group if x.kind is TraceKind.choice_set), None)
        ctxrow = next((x for x in group if x.kind is TraceKind.context), None)
        decrow = next((x for x in group if x.kind is TraceKind.decision), None)
        if not all((obsrow, setrow, ctxrow, decrow)):
            issue("turn is missing observation, choice_set, context, or decision", record_row, turn_no)
            continue
        calls = [x for x in group if x.kind is TraceKind.s1_call]
        if not 1 <= len(calls) <= 2:
            issue("turn must have one or two s1_call entries", record_row, turn_no)
        if len([x for x in group if x.kind is TraceKind.execution]) > 2:
            issue("turn has more than two executions", record_row, turn_no)
        def refs(row: TraceEntry, expected: set[int], label: str) -> None:
            if not expected.issubset(set(row.refs)):
                issue(f"{label} is missing required refs", row, turn_no)
        if obsrow and setrow and ctxrow:
            refs(setrow, {obsrow.trace_seq}, "choice_set")
            refs(ctxrow, {obsrow.trace_seq, setrow.trace_seq}, "context")
            for call in calls:
                refs(call, {ctxrow.trace_seq}, "s1_call")
        if decrow and setrow:
            refs(decrow, {setrow.trace_seq, *(x.trace_seq for x in calls)}, "decision")
        if decrow:
            for x in group:
                if x.kind is TraceKind.execution:
                    refs(x, {decrow.trace_seq}, "execution")
                if x.kind is TraceKind.help and x.body.get("event") in ("opened", "repeat"):
                    refs(x, {decrow.trace_seq}, "help")
        for effect in (x for x in group if x.kind is TraceKind.effect):
            executions = [x for x in group if x.kind is TraceKind.execution and x.trace_seq in effect.refs]
            if not executions:
                issue("effect does not ref an execution", effect, turn_no)
            elif any(effect.body.get("choice_id") != x.body.get("request", {}).get("choice_id") or
                     effect.body.get("action") != x.body.get("request", {}).get("action") for x in executions):
                issue("effect differs from execution request", effect, turn_no)
        refs(record_row, {x.trace_seq for x in group if x.kind in (TraceKind.observation, TraceKind.choice_set, TraceKind.context, TraceKind.decision, TraceKind.execution)}, "turn")
        try:
            obs = Observation.model_validate(obsrow.body)
            choices = ChoiceSet.model_validate(setrow.body)
            record = TurnRecord.model_validate(record_row.body)
            turn_records.append(record)
        except ValidationError as exc:
            issue(f"invalid typed turn evidence: {exc.errors()[0]['msg']}", record_row, turn_no)
            continue
        if choices.built_from_seq != obs.seq:
            issue("choice set built_from_seq differs from observation seq", setrow, turn_no)
        if last_version is not None and choices.set_version <= last_version:
            issue("set_version did not increase", setrow, turn_no)
        last_version = choices.set_version
        payload = ctxrow.body.get("payload", {})
        try:
            context_hash = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            context_hash = None
        if ctxrow.body.get("sha256") != context_hash:
            issue("context hash differs from canonical payload", ctxrow, turn_no)
        if not isinstance(payload, dict):
            issue("context payload is not an object", ctxrow, turn_no)
            payload = {}
        as_of = payload.get("as_of")
        if not isinstance(as_of, dict) or as_of.get("seq") != obs.seq:
            issue("context as_of.seq differs from observation", ctxrow, turn_no)
        if payload.get("choice_set_version") != choices.set_version:
            issue("context choice_set_version differs from set", ctxrow, turn_no)
        expected_choices = [c.model_dump(mode="json", exclude_none=True) for c in choices.choices]
        if payload.get("choices") != expected_choices:
            issue("context choices differ from choice set", ctxrow, turn_no)
        if "pending_help" not in payload:
            issue("context payload is missing pending_help", ctxrow, turn_no)
        decision_body = decrow.body
        try:
            clock_mode = ClockMode(decision_body.get("clock_mode"))
            parsed = parse_decision(decision_body.get("answer"), choices, clock_mode)
        except (ValueError, TypeError) as exc:
            issue(f"invalid decision clock_mode: {exc}", decrow, turn_no)
            continue
        expected_decision = parsed.decision
        for field, expected in (("valid", parsed.valid), ("error", parsed.error), ("policy_default", parsed.policy_default),
                                ("decision", expected_decision.model_dump(mode="json", exclude_none=True) if expected_decision else None)):
            got = decision_body.get(field)
            if field == "decision" and got is not None and expected is not None:
                # Decision wire representation may include unset nulls; normalize through the contract.
                try:
                    got = Decision.model_validate(got).model_dump(mode="json", exclude_none=True)
                except ValidationError:
                    pass
            if got != expected:
                issue(f"decision {field} differs from parse_decision", decrow, turn_no)
        answer = decision_body.get("answer")
        raw = answer.get("choice_id") if isinstance(answer, dict) and isinstance(answer.get("choice_id"), str) else None
        selected = choices.get(expected_decision.choice_id) if expected_decision else None
        exec_rows = [x for x in group if x.kind is TraceKind.execution]
        action_exec: str | None = None
        terminal: ExecutionRecord | None = None
        for exrow in exec_rows:
            try:
                req = TraceExecutionRequest.model_validate(exrow.body["request"])
                records = [ExecutionRecord.model_validate(x) for x in exrow.body["records"]]
                if not records:
                    issue("execution has no records: the act raised before the bridge answered", exrow, turn_no)
                if req.based_on_seq != obs.seq:
                    issue("execution based_on_seq differs from observation", exrow, turn_no)
                if any(x.action_id != req.action_id for x in records):
                    issue("execution record action_id differs from request", exrow, turn_no)
                if records:
                    terminal = records[-1]
                    if terminal.status.value != "rejected":
                        action_exec = req.choice_id
            except (ValidationError, KeyError, TypeError, ValueError) as exc:
                issue(f"invalid execution evidence: {exc}", exrow, turn_no)
        if selected and selected.kind in (ChoiceKind.action, ChoiceKind.skill):
            outcome = TurnOutcome.rejected_stale if terminal and terminal.status.value == "rejected" else TurnOutcome.executed
        elif selected and selected.kind is ChoiceKind.escalate:
            outcome = TurnOutcome.escalated
        elif selected and selected.kind is ChoiceKind.delegate:
            outcome = TurnOutcome.delegated
        elif selected and selected.kind in (ChoiceKind.wait, ChoiceKind.finish):
            outcome = TurnOutcome.finished
        else:
            outcome = TurnOutcome.rejected_invalid
        expected_effective = selected.id if selected else None
        decision = expected_decision
        pending = decision.while_pending if decision else None
        actual_pending = record.while_pending
        if pending is not None:
            if actual_pending != pending or record.policy_default != bool(decision_body.get("policy_default")):
                issue("turn record while_pending or policy_default differs from decision", record_row, turn_no)
        elif selected and selected.kind in (ChoiceKind.escalate, ChoiceKind.delegate) and decision and decision.while_pending is None:
            if actual_pending is None or actual_pending.model_dump(mode="json") != {"mode": "wait"} or not record.policy_default:
                issue("help turn missing policy-default wait", record_row, turn_no)
            if exec_rows:
                issue("default-wait help turn has an executed action", exec_rows[0], turn_no)
        elif actual_pending is not None or record.policy_default:
            issue("turn has unexpected while_pending or policy_default", record_row, turn_no)
        for exrow in exec_rows:
            req = exrow.body.get("request", {})
            if exrow.body.get("purpose") == "while_pending":
                if pending is None or pending.mode != "continue" or req.get("choice_id") != pending.choice_id:
                    issue("while_pending execution differs from S1 choice", exrow, turn_no)
            elif exrow.body.get("purpose") == "choice" and req.get("choice_id") != expected_effective:
                issue("choice execution differs from effective choice", exrow, turn_no)
        expected_exec = action_exec
        for field, expected in (("set_version", choices.set_version), ("based_on_seq", obs.seq), ("raw_choice", raw),
                                ("effective_choice", expected_effective), ("executed_action", expected_exec), ("outcome", outcome)):
            got = getattr(record, field)
            if hasattr(got, "value"):
                got = got.value
            if hasattr(expected, "value"):
                expected = expected.value
            if got != expected:
                issue(f"turn record {field} differs from entries", record_row, turn_no)
    for violation in check_adherence(turn_records):
        issue(f"adherence: {violation.problem}", turn=violation.turn)
    return problems
