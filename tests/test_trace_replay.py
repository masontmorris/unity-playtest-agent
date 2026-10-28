from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, RunRecord
from playtest.contracts.loop import ChoiceSet, TurnRecord, TurnOutcome
from playtest.contracts.trace import TraceAuthor
from playtest.core.context import build_context, canonical
from playtest.core.decisions import parse_decision
from playtest.core.trace import TraceWriter, read_trace
from playtest.core.trace_replay import replay


def make_trace(path):
    run = RunRecord(run_id="synthetic-run", game_project="Synthetic", clock_mode=ClockMode.realtime,
                    models=[], objective="Check one step", budget=Budget(), core_version="0.0.1")
    obs = Observation(session="s-test", seq=4, game_s=1.0, wall_utc=datetime(2026, 10, 26, tzinfo=timezone.utc),
                      clock_mode=ClockMode.realtime, scene="scene", playing=True, paused=False,
                      time_scale=1.0, frame=10, public={})
    choices = ChoiceSet(set_version=0, built_from_seq=4, choices=[
        {"id": "wait", "kind": "wait", "label": "Wait", "source": "core"},
        {"id": "finish", "kind": "finish", "label": "Finish", "source": "core"},
    ])
    payload = build_context(objective=run.objective, observation=obs, choices=choices)
    answer = {"choice_id": "finish", "set_version": 0}
    parsed = parse_decision(answer, choices, ClockMode.realtime)
    with TraceWriter(path, run) as w:
        o = w.append("observation", obs.model_dump(mode="json"), author="bridge", turn=0)
        c = w.append("choice_set", choices.model_dump(mode="json"), author="core", turn=0, refs=[o])
        x = w.append("context", {"sha256": hashlib.sha256(canonical(payload).encode()).hexdigest(), "payload": payload},
                     author="core", turn=0, refs=[o, c])
        d = w.append("decision", {"answer": answer, "clock_mode": "realtime", "valid": parsed.valid,
                                   "error": parsed.error, "policy_default": parsed.policy_default,
                                   "decision": parsed.decision.model_dump(mode="json", exclude_none=True)},
                     author="core", turn=0, refs=[c])
        w.append("turn", TurnRecord(turn=0, set_version=0, based_on_seq=4, raw_choice="finish",
                                     effective_choice="finish", executed_action=None, outcome=TurnOutcome.finished).model_dump(mode="json"),
                 author="core", turn=0, refs=[o, c, x, d])


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
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    assert any("context hash" in p.message for p in replay(read_trace(bad)))
