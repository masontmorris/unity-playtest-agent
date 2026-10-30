from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from playtest.contracts.bridge import ExecutionRecord, Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, RunRecord
from playtest.contracts.loop import ChoiceSet, TurnOutcome, TurnRecord
from playtest.core.context import build_context, canonical
from playtest.core.decisions import parse_decision
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_replay import replay


def _record(path: Path) -> RunRecord:
    return RunRecord(run_id="synthetic-run", game_project="Synthetic", clock_mode=ClockMode.realtime,
                     models=[], objective="Check one step", budget=Budget(), core_version="0.0.1")


def make_trace(path: Path) -> None:
    run = _record(path)
    specs = [
        ({"choice_id": "act", "set_version": 0}, "completed", None),
        ({"choice_id": "wait", "set_version": 1}, None, None),
        ({"choice_id": "unknown", "set_version": 2}, None, None),
        ({"choice_id": "act", "set_version": 99}, None, None),
        ("not-an-object", None, None),
        (None, None, None),
        ({"choice_id": "act", "set_version": 6}, "rejected", None),
        ({"choice_id": "escalate", "set_version": 7, "while_pending": {"mode": "continue", "choice_id": "act"}}, "completed", "opened"),
        ({"choice_id": "escalate", "set_version": 8, "while_pending": {"mode": "wait"}}, None, "repeat"),
        ({"choice_id": "delegate", "set_version": 9}, None, "opened"),
        ({"choice_id": "finish", "set_version": 10}, None, None),
    ]
    with TraceWriter(path, run) as writer:
        opened_ref = None
        for turn, (answer, status, help_event) in enumerate(specs):
            observation = Observation(session="synthetic", seq=turn, game_s=float(turn),
                wall_utc=datetime(2026, 10, 26, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                scene="synthetic", playing=True, paused=False, time_scale=1.0, frame=turn, public={})
            choices = ChoiceSet(set_version=turn, built_from_seq=turn, choices=[
                {"id":"act","kind":"action","label":"Act","action":"synthetic_action","source":"manifest"},
                {"id":"wait","kind":"wait","label":"Wait","source":"core"},
                {"id":"finish","kind":"finish","label":"Finish","source":"core"},
                {"id":"escalate","kind":"escalate","label":"Escalate","role":"helper","source":"core"},
                {"id":"delegate","kind":"delegate","label":"Delegate","role":"frontier","source":"core"},
            ])
            pending = {"choice_id": "escalate", "kind": "escalate", "opened_turn": 7} if turn in (8, 9) else None
            payload = build_context(objective=run.objective, observation=observation, choices=choices,
                                    pending_help=pending)
            if turn == 9 and opened_ref is not None:
                writer.append("help", {"event":"adopted","choice_id":"escalate","kind":"escalate","opened_turn":7,
                    "source":"synthetic","result":{},"reason":None}, author="core", turn=turn, refs=[opened_ref])
                opened_ref = None
            o = writer.append("observation", observation.model_dump(mode="json"), author="bridge", turn=turn)
            c = writer.append("choice_set", choices.model_dump(mode="json"), author="core", turn=turn, refs=[o])
            ctx = writer.append("context", {"sha256": hashlib.sha256(canonical(payload).encode()).hexdigest(), "payload": payload},
                                author="core", turn=turn, refs=[o, c])
            question = "decision"
            provider_problem = "rate limited" if answer is None else None
            s1 = writer.append("s1_call", {"question":question,"provider":"synthetic","model":"synthetic",
                "resolved_model":"synthetic","request_sha256":"0"*64,"http_status":429 if answer is None else 200,
                "latency_ms":0,"problem":provider_problem,"response":answer,"usage":None}, author="s1", turn=turn, refs=[ctx])
            if turn == 7:
                s1b = writer.append("s1_call", {"question":"while_pending","provider":"synthetic","model":"synthetic",
                    "resolved_model":"synthetic","request_sha256":"1"*64,"http_status":200,"latency_ms":0,
                    "problem":None,"response":{"choice_id":"act"},"usage":None}, author="s1", turn=turn, refs=[ctx])
                call_refs = [s1, s1b]
            else:
                call_refs = [s1]
            parsed = parse_decision(answer, choices, ClockMode.realtime)
            d = writer.append("decision", {"answer":answer,"clock_mode":"realtime","valid":parsed.valid,
                "error":parsed.error,"policy_default":parsed.policy_default,
                "decision":parsed.decision.model_dump(mode="json",exclude_none=True) if parsed.decision else None},
                author="core", turn=turn, refs=[c, *call_refs])
            exec_ref = None
            executed = None
            outcome = TurnOutcome.rejected_invalid
            selected = choices.get(parsed.decision.choice_id) if parsed.decision else None
            if parsed.decision and selected:
                if selected.id == "act":
                    outcome = TurnOutcome.rejected_stale if status == "rejected" else TurnOutcome.executed
                    if turn == 7:
                        outcome = TurnOutcome.escalated
                    elif outcome is TurnOutcome.executed:
                        executed = "act"
                elif selected.id == "escalate":
                    outcome = TurnOutcome.escalated
                elif selected.id == "delegate":
                    outcome = TurnOutcome.delegated
                else: outcome = TurnOutcome.finished
            if turn in (0, 6, 7):
                choice_id = "act"
                purpose = "while_pending" if turn == 7 else "choice"
                action_id = f"a{turn}"
                request = {"action_id":action_id,"choice_id":choice_id,"action":"synthetic_action","params":{},
                    "based_on_seq":turn,"max_age_s":1.0,"depends_on":[],"lease_s":None}
                record = ExecutionRecord(session="synthetic",seq=turn,wall_utc=datetime(2026,10,26,tzinfo=timezone.utc),
                    action_id=action_id,status="rejected" if turn == 6 else "completed",reject_reason="stale_age" if turn == 6 else None,
                    release_verified=True if turn == 0 else None)
                exec_ref = writer.append("execution", {"purpose":purpose,"request":request,"records":[record.model_dump(mode="json")]},
                    author="bridge",turn=turn,refs=[d])
                if turn == 0:
                    writer.append("effect", {"choice_id":"act","action":"synthetic_action","expected":{},"observed":{}},
                                  author="core",turn=turn,refs=[exec_ref])
                if turn == 7:
                    executed = "act"
            if help_event:
                h = writer.append("help", {"event":help_event,"choice_id":selected.id,"kind":selected.kind.value,
                    "opened_turn":7 if help_event == "repeat" else turn,"source":"synthetic","result":None,"reason":None},
                    author="core",turn=turn,refs=[d] + ([opened_ref] if help_event == "repeat" and opened_ref is not None else []))
                if help_event == "opened":
                    opened_ref = h
            raw = answer.get("choice_id") if isinstance(answer,dict) and isinstance(answer.get("choice_id"),str) else None
            effective = selected.id if selected else None
            record = TurnRecord(turn=turn,set_version=turn,based_on_seq=turn,raw_choice=raw,effective_choice=effective,
                executed_action=executed,outcome=outcome,while_pending=parsed.decision.while_pending if parsed.decision else None,
                policy_default=parsed.policy_default)
            writer.append("turn",record.model_dump(mode="json"),author="core",turn=turn,refs=[o,c,ctx,d]+([exec_ref] if exec_ref is not None else []))
        if opened_ref is not None:
            writer.append("help", {"event":"cancelled","choice_id":"delegate","kind":"delegate","opened_turn":9,
                "source":"synthetic","result":None,"reason":"run finished"}, author="core", refs=[opened_ref])
        writer.append("stop", {"reason":"finish","result":None,"error":None}, author="core")
        writer.close("complete")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row["wall_utc"] = "2026-10-26T18:00:00Z"
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))


def test_synthetic_trace_round_trip_and_replay(tmp_path):
    path = tmp_path / "replay.jsonl"
    make_trace(path)
    assert replay(read_trace(path)) == []


def test_committed_replay_fixture():
    path = Path(__file__).resolve().parents[1] / "fixtures" / "traces" / "replay-basic.jsonl"
    assert replay(read_trace(path)) == []


def test_replay_reports_context_mutation(tmp_path):
    path = tmp_path / "replay.jsonl"
    make_trace(path)
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    rows[3]["body"]["sha256"] = "bad"
    assert any("context hash" in p.message for p in replay(_models(rows)))


def _models(rows, *, bypass=False):
    from playtest.contracts.trace import TraceAuthor, TraceEntry, TraceKind
    result = []
    for row in rows:
        if bypass and any(ref >= row["trace_seq"] for ref in row["refs"]):
            result.append(TraceEntry.model_construct(**{**row, "kind":TraceKind(row["kind"]),
                "author":TraceAuthor(row["author"])}))
        else:
            result.append(TraceEntry.model_validate(row))
    return result


def _rows(tmp_path):
    path = tmp_path / "trace.jsonl"
    make_trace(path)
    return [json.loads(line) for line in path.read_text().splitlines()]


def _problem(rows, phrase, *, bypass=False):
    assert any(phrase in problem.message for problem in replay(_models(rows, bypass=bypass)))


def test_replay_detects_execution_choice_mismatch(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "execution" and x["turn"] == 0)["body"]["request"]["choice_id"] = "wait"
    _problem(rows, "choice execution differs")


def test_replay_detects_recorded_executed_action_differing_from_choice(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "turn" and x["turn"] == 0)["body"]["executed_action"] = "wait"
    _problem(rows, "turn record executed_action differs")


def test_replay_detects_while_pending_execution_choice_mismatch(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "execution" and x["turn"] == 7)["body"]["request"]["choice_id"] = "wait"
    _problem(rows, "while_pending execution differs")


def test_replay_detects_turn_pending_mismatch(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "turn" and x["turn"] == 7)["body"]["while_pending"] = {"mode": "wait"}
    _problem(rows, "while_pending or policy_default differs")


def test_replay_detects_missing_pending_help(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "context" and x["turn"] == 0)["body"]["payload"].pop("pending_help")
    _problem(rows, "missing pending_help")


def test_replay_detects_context_choices_mismatch(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "context" and x["turn"] == 0)["body"]["payload"]["choices"] = []
    _problem(rows, "context choices differ")


def test_replay_detects_sequence_gap(tmp_path):
    rows = _rows(tmp_path)
    rows[5]["trace_seq"] += 1
    _problem(rows, "trace_seq is")


def test_replay_detects_forward_reference_bypassing_writer(tmp_path):
    rows = _rows(tmp_path)
    rows[2]["refs"] = [999]
    _problem(rows, "ref", bypass=True)


def test_replay_detects_two_open_helpers(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "help" and x["body"]["event"] == "repeat")["body"]["event"] = "opened"
    _problem(rows, "opened while another was open")


def test_replay_detects_help_resolved_twice(tmp_path):
    rows = _rows(tmp_path)
    adopted = [x for x in rows if x["kind"] == "help" and x["body"]["event"] == "adopted"][0]
    duplicate = next(x for x in rows if x["kind"] == "help" and x["body"]["event"] == "opened" and x["turn"] == 9)
    duplicate["body"]["event"] = "adopted"
    duplicate["refs"] = adopted["refs"]
    _problem(rows, "resolved more than once")


def test_replay_detects_non_increasing_set_version(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "choice_set" and x["turn"] == 1)["body"]["set_version"] = 0
    _problem(rows, "set_version did not increase")


def test_replay_reports_truncated_final_turn(tmp_path):
    rows = _rows(tmp_path)
    last_turn = max(x["turn"] for x in rows if x["turn"] is not None)
    rows = rows[:max(i for i, x in enumerate(rows) if x["kind"] == "turn" and x["turn"] == last_turn)]
    _problem(rows, "turn entries stop before a turn record")


def test_replay_detects_turn_without_s1_call(tmp_path):
    rows = _rows(tmp_path)
    rows = [x for x in rows if not (x["kind"] == "s1_call" and x["turn"] == 0)]
    _problem(rows, "one or two s1_call")


def test_replay_detects_repaired_answer(tmp_path):
    rows = _rows(tmp_path)
    decision = next(x for x in rows if x["kind"] == "decision" and x["turn"] == 2)["body"]
    decision.update(answer={"choice_id":"missing","set_version":2}, valid=True,
                    decision={"choice_id":"act","set_version":2,"while_pending":None,"reason":None,"distribution":None}, error=None)
    _problem(rows, "decision valid differs")


def test_replay_detects_wrong_context_hash(tmp_path):
    rows = _rows(tmp_path)
    next(x for x in rows if x["kind"] == "context")["body"]["sha256"] = "bad"
    _problem(rows, "context hash")


def test_epoch_aware_adoption_requires_observation_then_publication_then_choices(tmp_path):
    rows = _rows(tmp_path)  # historical adoption-before-observation traces remain readable
    context = next(row for row in rows if row["turn"] == 9 and row["kind"] == "context")
    context["body"]["payload"]["recent_history"] = {"epoch": 0}
    context["body"]["sha256"] = hashlib.sha256(canonical(context["body"]["payload"]).encode()).hexdigest()
    _problem(rows, "help adoption must follow the epoch check")
    adopted = next(row for row in rows if row["turn"] == 9 and row["kind"] == "help")
    observed = next(row for row in rows if row["turn"] == 9 and row["kind"] == "observation")
    rows.remove(adopted)
    rows.insert(rows.index(observed) + 1, adopted)

    def renumber():
        mapping = {row["trace_seq"]: index for index, row in enumerate(rows)}
        for index, row in enumerate(rows):
            row["trace_seq"] = index
            row["refs"] = [mapping[ref] for ref in row["refs"]]

    renumber()
    assert replay(_models(rows)) == []
    offered = next(row for row in rows if row["turn"] == 9 and row["kind"] == "choice_set")
    rows.remove(adopted)
    rows.insert(rows.index(offered) + 1, adopted)
    renumber()
    _problem(rows, "precede choice assembly")
