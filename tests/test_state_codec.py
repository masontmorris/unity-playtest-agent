"""The opt-in compact S1 state: lossless rules, the guards that keep them exact, and the adapter path."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.core.context import canonical
from playtest.runner import state_codec as C
from playtest.runner.probe import DisplacementProbe
from playtest.runner.s1 import SystemOneS1
from s1_fakes import Send, approval, reply
from test_runner_s1 import context as built_context

LIQUID = "liquid-d1-ultra"
INSTRUCTIONS = "Choose exactly one id from choices."


def _attempt(turn: int, choice: str, tail: str | None = " Before the action: a cast ahead was clear.") -> dict[str, Any]:
    expected = {"source": "calibration", "control": "w", "requested_hold_s": 0.4, "net_displacement": 2.0321,
                "sample_variance": 0.001862, "valid_trials": 2, "effective_in_all_repeats": True, "unit": "world units"}
    observed = {"net_displacement": 2.14123, "unit": "world units", "from_inspection_seq": turn,
                "to_inspection_seq": turn + 5, "window_game_s": 1.25, "applied_duration_s": 0.4}
    fact = DisplacementProbe._displacement_fact(SimpleNamespace(id=choice), expected, observed) + (tail or "")
    return {"turn": turn, "observation_seq": 10 * turn, "game_s": 1.5 * turn, "raw_choice": choice,
            "effective_choice": choice, "executed_action": choice, "outcome": "executed", "help_event": None,
            "trace_seq": 7 * turn, "choice_id": choice, "purpose": "choice", "terminal_status": "completed",
            "reject_reason": None, "expected_effect": expected, "observed_effect": observed, "effect_fact": fact}


def payload() -> dict[str, Any]:
    """A state shaped like the night sessions': history rows, a probe, choices with empty params."""
    return {
        "objective": "Try every control.",
        "as_of": {"seq": 40, "game_s": 60.25, "age_s": None, "sample_gap_s": 0.1},
        "observation": {"scene": "a", "public": {"probe": {"version": 1, "samples": [
            {"forward": 0.6, "lateral": -0.35, "hit": True, "delta_y": 0.0},
            {"forward": 0.6, "lateral": 0.0, "hit": False, "delta_y": None}]}}, "ui": None},
        "facts": ["Turn 3: wait was chosen; no input was sent."],
        "recent_history": {"epoch": 0, "boundary": None,
                           "turns": {"limit": 8, "total": 3, "omitted": 0, "items": [
                               {"turn": 1, "raw_choice": "c1", "effective_choice": "c1", "executed_action": "c1"},
                               {"turn": 2, "raw_choice": "wait", "effective_choice": "wait", "executed_action": None}]},
                           "action_attempts": {"limit": 8, "total": 2, "omitted": 0,
                                               "items": [_attempt(1, "c1"), _attempt(2, "c2", tail=None)]}},
        "choices": [{"id": "c1", "kind": "action", "label": "move ahead", "action": "c1", "params": {},
                     "preconditions": [], "source": "manifest"},
                    {"id": "wait", "kind": "wait", "label": "wait a moment", "params": {"duration_s": 0.5},
                     "preconditions": [], "source": "manifest"}],
        "instructions": INSTRUCTIONS,
        "pending_help": None,
    }


def criteria(p: dict[str, Any]) -> dict[str, str]:
    return {c["id"]: c["label"] for c in p["choices"] if "label" in c}


def roundtrip(p: dict[str, Any], rules=C.ALL_RULES, *, instructions=INSTRUCTIONS) -> tuple[str, list[str]]:
    original = copy.deepcopy(p)
    text, applied = C.encode(p, instructions=instructions, criteria=criteria(p), rules=rules)
    assert p == original, "encoding must not touch its input"
    assert canonical(C.decode(text, instructions=instructions, criteria=criteria(p))) == canonical(p)
    return text, applied


# --------------------------------------------------------------------------- rules


def test_every_rule_applies_to_a_night_shaped_state_and_restores_it_exactly():
    text, applied = roundtrip(payload())
    assert applied == [rule.value for rule in C.ALL_RULES]
    head, body = text.split("\n", 1)
    assert head.startswith(f"{C.VERSION} rules=") and "\n" not in body
    state = json.loads(body)
    assert "instructions" not in state and all("label" not in c for c in state["choices"])
    attempts = state["recent_history"]["action_attempts"]["items"]
    assert attempts["columns"][:4] == ["turn", "observation_seq", "game_s", "raw_choice"]
    row = dict(zip(attempts["columns"], attempts["rows"][0]))
    assert row["effective_choice"] == row["executed_action"] == row["choice_id"] == "="
    assert row["effect_fact"] == "@ Before the action: a cast ahead was clear."   # the measured tail is kept
    assert dict(zip(attempts["columns"], attempts["rows"][1]))["effect_fact"] == "@"
    assert state["choices"][0] == {"id": "c1", "kind": "action", "action": "c1", "source": "manifest"}
    assert state["observation"]["public"]["probe"]["samples"]["rows"][1] == [0.6, 0.0, False, None]


def test_the_default_rules_keep_labels_and_the_format_alone_changes_only_whitespace():
    text, applied = roundtrip(payload(), C.DEFAULT_RULES)
    assert C.Rule.labels.value not in applied
    assert [c["label"] for c in json.loads(text.split("\n", 1)[1])["choices"]] == ["move ahead", "wait a moment"]
    plain, none = roundtrip(payload(), ())
    assert none == [] and plain.split("\n", 1)[1] == json.dumps(payload(), ensure_ascii=False, separators=(",", ":"))


def test_the_text_is_much_smaller_than_the_indented_rendering_the_server_bills():
    text, _ = roundtrip(payload())
    assert len(text) < 0.7 * len(json.dumps(payload(), ensure_ascii=False, indent=2))


@pytest.mark.parametrize("mutate, rule", [
    (lambda p: p["recent_history"]["turns"]["items"][1].pop("executed_action"), None),  # differing keys: no table
    (lambda p: p["recent_history"]["turns"]["items"][0].update(effective_choice="="), C.Rule.same),
    (lambda p: p["recent_history"]["action_attempts"]["items"][1].update(effect_fact="@literal"), C.Rule.effect),
    (lambda p: p["choices"][1].pop("preconditions"), C.Rule.empty),
    (lambda p: p["choices"][1].pop("label"), C.Rule.labels),
    (lambda p: p.update(instructions="other text"), C.Rule.instructions),
])
def test_a_rule_that_could_not_be_undone_exactly_is_not_applied(mutate, rule):
    p = payload()
    mutate(p)
    _, applied = roundtrip(p)
    if rule is not None:
        assert rule.value not in applied


def test_a_table_is_written_only_where_every_row_has_the_same_keys():
    p = payload()
    p["recent_history"]["turns"]["items"][1].pop("executed_action")
    text, _ = roundtrip(p)
    state = json.loads(text.split("\n", 1)[1])
    assert isinstance(state["recent_history"]["turns"]["items"], list)
    assert set(state["recent_history"]["action_attempts"]["items"]) == {"columns", "rows"}


def test_an_effect_fact_the_formatter_does_not_reproduce_is_kept_whole():
    p = payload()
    p["recent_history"]["action_attempts"]["items"][0]["effect_fact"] = "c1: measured by hand."
    text, _ = roundtrip(p)
    rows = json.loads(text.split("\n", 1)[1])["recent_history"]["action_attempts"]["items"]
    assert dict(zip(rows["columns"], rows["rows"][0]))["effect_fact"] == "c1: measured by hand."


def test_decode_refuses_text_it_cannot_restore():
    text, _ = C.encode(payload(), instructions=INSTRUCTIONS, criteria=criteria(payload()))
    with pytest.raises(C.CodecError):
        C.decode("{}", instructions=INSTRUCTIONS, criteria={})
    with pytest.raises(C.CodecError):
        C.decode(text, instructions=None, criteria=criteria(payload()))      # instructions-ref without the text
    with pytest.raises(C.CodecError):
        C.decode(text.replace("rules=", "rules=bogus,"), instructions=INSTRUCTIONS, criteria=criteria(payload()))


def test_a_non_finite_state_is_refused():
    p = payload()
    p["as_of"]["game_s"] = float("nan")
    with pytest.raises(C.CodecError):
        C.encode(p, instructions=INSTRUCTIONS, criteria=criteria(p))


# --------------------------------------------------------------------------- adapter


def _liquid(tmp_path: Path, *replies: Any, **options: Any) -> tuple[SystemOneS1, Send, call_budget.Ledger]:
    limits = approval()
    limits["arms"] = {LIQUID: limits["arms"].pop("mammoth-1")}
    ledger = call_budget.Ledger(tmp_path, limits, LIQUID)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=10, send=send, arm=LIQUID, **options), send, ledger


def test_off_by_default_the_body_is_unchanged(tmp_path: Path):
    plain, plain_send, _ = _liquid(tmp_path / "a", reply("next_action", "c1"))
    off, off_send, _ = _liquid(tmp_path / "b", reply("next_action", "c1"), compact_state="off")
    asyncio.run(plain.decide(built_context()))
    asyncio.run(off.decide(built_context()))
    assert canonical(plain_send.bodies[0]) == canonical(off_send.bodies[0])
    assert isinstance(plain_send.bodies[0]["state"], dict) and plain.compact_state is None


def test_the_adapter_sends_a_compact_state_that_restores_the_traced_context(tmp_path: Path):
    port, send, ledger = _liquid(tmp_path, reply("next_action", "c1", favoured="c2"), compact_state="all")
    ctx = built_context()
    before = copy.deepcopy(ctx)
    got = asyncio.run(port.decide(ctx))
    body = send.bodies[0]
    asked = body["questions"]["next_action"]
    assert ctx == before                                                # the traced context is not touched
    assert isinstance(body["state"], str) and body["state"].startswith(C.VERSION)
    assert canonical(C.decode(body["state"], instructions=asked["instructions"], criteria=asked["criteria"])) \
        == canonical(ctx)
    assert got.calls[0].request_sha256 == digest(canonical(body))      # the hash covers the bytes sent
    row = ledger.arm_rows()[0]
    assert row["state_codec"] == C.VERSION and "labels-ref" in row["state_rules"]
    assert got.answer["choice_id"] == "c1"                              # the favoured id never selects
    assert port.compact_state == f"{C.VERSION}:" + ",".join(rule.value for rule in C.ALL_RULES)


def test_a_follow_up_call_restores_from_its_own_question(tmp_path: Path):
    port, send, ledger = _liquid(tmp_path, reply("next_action", "h"), reply("while_pending", "c2"),
                                 compact_state="default")
    ctx = built_context()
    asyncio.run(port.decide(ctx))
    assert len(send.bodies) == 2
    for body in send.bodies:
        name, asked = next(iter(body["questions"].items()))
        assert canonical(C.decode(body["state"], instructions=asked["instructions"],
                                  criteria=asked["criteria"])) == canonical(ctx)
    # The follow-up asks with other instructions, so the state keeps its own copy there.
    rules = {row["question"]: row["state_rules"] for row in ledger.arm_rows()}
    assert "instructions-ref" in rules["next_action"] and "instructions-ref" not in rules["while_pending"]


def test_batched_and_compact_send_one_body_that_restores_exactly(tmp_path: Path):
    def both(body):
        return 200, {"answers": {"next_action": {"type": "choice", "choice": "h"},
                                 "while_pending": {"type": "choice", "choice": "c2"}}}, 10, None
    port, send, _ = _liquid(tmp_path, both, compact_state="all", pending_question="batched")
    ctx = built_context()
    got = asyncio.run(port.decide(ctx))
    assert len(send.bodies) == 1 and got.answer["while_pending"] == {"mode": "continue", "choice_id": "c2"}
    asked = send.bodies[0]["questions"]["next_action"]
    assert canonical(C.decode(send.bodies[0]["state"], instructions=asked["instructions"],
                              criteria=asked["criteria"])) == canonical(ctx)


@pytest.mark.parametrize("arm, options, match", [
    ("mammoth-1", {}, "no plain `state`"),
    ("meta-muse-spark-s1", {}, "no plain `state`"),
    (LIQUID, {"layout": "dup-state-ref/1"}, "full layout"),
    (LIQUID, {"compact_state": "bogus"}, "unknown compact-state"),
])
def test_unsupported_combinations_are_refused(tmp_path: Path, arm, options, match):
    limits = approval()
    limits["arms"] = {arm: limits["arms"].pop("mammoth-1")}
    ledger = call_budget.Ledger(tmp_path, limits, arm)
    with pytest.raises(ValueError, match=match):
        SystemOneS1(ledger, max_calls=1, arm=arm, **{"compact_state": "default", **options})
