"""Live frontier integration (#33): one call per S1 escalation, adopted once, never repaired.

The fake game and the production `TurnLoop` are the real path. Only the S1 provider and the Muse
transport are scripted, so a test can hold the exact answer, HTTP status and clip. Nothing here
claims a paid trial: these are offline tests of the integration and its ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from fake_game import FakeGame
from playtest.bench import call_budget, frontier_live, live, replay
from playtest.bench.record import digest
from playtest.contracts.bridge import ExecStatus, RejectReason
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource, TurnOutcome, TurnRecord
from playtest.core.adherence import check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.context import canonical
from playtest.frontier import media, provider
from playtest.roles import frontier

TOKEN = "frontier-live-test-token"


def _offered() -> list[Choice]:
    label = "Request a 0.1-second hold of {}; calibrated net displacement {} world units (sample variance 0.000100, n=2)."
    controls = [
        Choice(id="c1", kind=ChoiceKind.action, label=label.format("a", "6.000"), action="shift",
               params={"delta": [1.0, 0.0, 0.0]}, source=ChoiceSource.manifest),
        Choice(id="c2", kind=ChoiceKind.action, label=label.format("b", "0.000"), action="increment",
               source=ChoiceSource.manifest),
    ]
    return controls + live.core_choices()


def _limit(calls: int = 100, *, per_call: float = 0.001, max_cost: float = 0.5,
           price: str = "2099-01-01T00:00:00Z", balance: float = 5.0) -> dict:
    return {"max_calls": calls, "max_cost_usd": max_cost, "per_call_max_usd": per_call,
            "verified_balance_usd": balance, "balance_checked_at_utc": "2026-01-01T00:00:00Z",
            "price_valid_until_utc": price}


def _approval(*, s1_calls: int = 100, frontier_calls: int = 100, overall: int = 200,
              price: str = "2099-01-01T00:00:00Z") -> dict:
    return {"approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z",
            "max_calls": overall, "max_cost_usd": 1.0,
            "arms": {"mammoth-1": _limit(s1_calls),
                     frontier_live.FRONTIER_ARM: _limit(frontier_calls, price=price)}}


def _s1_answer(criteria: dict, choice: str, **extra):
    others = len(criteria) - 1
    probabilities = {item: (0.9 if item == choice else 0.1 / others) for item in criteria}
    return 200, {"model": "mammoth-1", "answers": {"next_action": {
        "type": "choice", "choice": choice, "probabilities": probabilities}}}, 12.0, None


class S1:
    """A scripted S1 transport: one choice per call, defaulting to `finish`."""

    def __init__(self, choices: dict[int, str] | None = None, raw: dict[int, tuple] | None = None) -> None:
        self.choices, self.raw, self.bodies = choices or {}, raw or {}, []

    def __call__(self, arm: str, body: dict):
        index = len(self.bodies)
        self.bodies.append(body)
        if index in self.raw:
            return self.raw[index]
        criteria = body["questions"]["next_action"]["criteria"]
        return _s1_answer(criteria, self.choices.get(index, "finish"))


def _empty() -> dict:
    return {"add_choices": [], "candidate_finding": None, "directive": None, "hypotheses": [], "prune_choices": []}


def _directive(text: str = "Hold the current scene and report whether it changes.",
               field: str = "scene", value: str = "main", deadline: float | None = 10.0) -> dict:
    expected = None if field is None else {"field": field, "op": "==", "value": value, "tolerance": None}
    return {"text": text, "tests": None, "target": None, "expected": expected, "deadline_s": deadline,
            "objective_quote": "check each offered control once",
            "continue_if": ["the measured effect still supports the objective's check"],
            "replan_if": ["a completed check measures no effect on the objective"]}


def _meta_response(content: dict | str, *, finish: str = "stop", usage: dict | None = None,
                   model: str = provider.MODEL) -> tuple:
    text = content if isinstance(content, str) else json.dumps(content)
    return 200, {"model": model, "choices": [{"index": 0, "finish_reason": finish,
                                              "message": {"role": "assistant", "content": text}}],
                 "usage": usage}, 7.0, None


class Meta:
    """A scripted Muse transport; every request body is retained for assertions."""

    def __init__(self, answers: list[tuple] | None = None) -> None:
        self.answers = answers or [_meta_response(_empty())]
        self.bodies: list[dict] = []

    def __call__(self, body: dict):
        index = len(self.bodies)
        self.bodies.append(body)
        return self.answers[min(index, len(self.answers) - 1)]


def _packet(body: dict) -> dict:
    content = body["messages"][2]["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    return json.loads(text)


def _no_clip(observation, since, folder: Path) -> media.Clip:
    return media.Clip(None, None, "capture is disabled for this test", None)


def _fake_clip(observation, since, folder: Path) -> media.Clip:
    folder.mkdir(parents=True, exist_ok=False)
    data = b"\x00\x00\x00\x18ftypmp42frontier-test-clip"
    path = folder / "clip.mp4"
    path.write_bytes(data)
    descriptor = {
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "mime": "video/mp4",
        "source": "bridge_offscreen_composite", "consumer": "frontier", "frame_count": 2,
        "width": 640, "height": 360, "start_wall_utc": "2026-01-01T00:00:00+00:00",
        "end_wall_utc": "2026-01-01T00:00:00.200000+00:00", "nominal_duration_s": 0.2,
        "encoded_duration_s": 0.2, "timestamp_basis": "frame_file_mtime_utc", "frame_game_s": None,
        "caveat": "approximate writer times",
    }
    return media.Clip(descriptor, path, None, {"frames": []})


class ScriptedS1:
    """A minimal S1 stand-in for a `while_pending` continuation, with the loop's binding."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.episode = 0
        self.help_since_game_s = None
        self.last: dict = {}
        self.binding: dict = {}
        self.loop = None

    def bind(self, episode, loop, facts) -> None:
        self.episode, self.loop = episode, loop
        self.help_since_game_s, self.last, self.binding = None, {}, {}

    def decide(self, choice_set, observation):
        step = self.script[len(self.loop.turns)]
        self.binding = {"choices": choice_set, "observation": observation, "facts": [], "age_s": 0.0,
                        "objective": live.OBJECTIVE}
        return {"set_version": choice_set.set_version, **step}


def _episode(tmp_path: Path, model: S1 | ScriptedS1, meta: Meta, *, approval: dict | None = None,
             media_source=_no_clip, max_turns: int = 8, offered: list[Choice] | None = None,
             objective: str | None = None) -> dict:
    out: dict = {}
    approval = approval or _approval()
    choices_to_offer = list(offered or _offered())

    async def main() -> None:
        game = FakeGame(token=TOKEN)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        s1 = model if isinstance(model, ScriptedS1) else live.LiveS1(
            tmp_path / "run", approval, send=model, objective=objective or live.OBJECTIVE)
        adapter = frontier_live.LiveFrontier(tmp_path / "run", approval, media_source=media_source, send=meta)
        adapter.use_manifest(frontier_live.manifest_for(choices_to_offer))
        s1.directive_source = adapter.directives
        section = {"episode": 1, "ids": [], "choices": [], "turns": [], "summary": None, "adherence": None}
        count = {"n": 0}

        async def snapshot():
            count["n"] += 1
            return {"inspection_seq": count["n"], "game_time_s": float(count["n"]),
                    "position": [6.0 * game.applied().count("shift"), 0.0, 0.0]}

        try:
            out["loop"] = await live.run_episode(
                live.Capturing(client), episode=1, offered=choices_to_offer, snapshot=snapshot, s1=s1,
                max_turns=max_turns, section=section, save=lambda: None, clock=lambda: 0.0, frontier=adapter)
        except live.ProviderStop as exc:
            out["stop"] = exc
        finally:
            out["frontier"], out["s1"], out["section"] = adapter, s1, section
            out["model"] = model
            out["applied"] = list(game.applied())
            await client.aclose()
            await game.stop()

    asyncio.run(main())
    return out


# --- one call, one adoption ----------------------------------------------------------------


def test_an_escalation_makes_one_call_and_the_next_s1_turn_sees_the_directive(tmp_path: Path) -> None:
    meta = Meta([_meta_response({**_empty(), "directive": _directive("Try the scene once more.")})])
    out = _episode(tmp_path, S1({0: "escalate", 1: "finish"}), meta)
    rows = out["frontier"].rows

    assert "stop" not in out
    assert len(meta.bodies) == 1 and len(rows) == 1
    row = rows[0]
    assert row["role"] == "frontier" and row["arm"] == frontier_live.FRONTIER_ARM
    assert row["status"] == "finished" and row["http_status"] == 200
    assert row["parse_valid"] is True and row["schema_valid"] is True and row["review"]["clean"] is True
    assert row["directive_result"] == "adopted" and row["directive"]["text"] == "Try the scene once more."
    assert row["based_on_seq"] == row["packet"]["as_of"]["seq"] and row["packet"]["escalation"]["turn"] == 0
    assert row["request_sha256"] == digest(canonical(meta.bodies[0]))
    assert row["schema_sha256"] == digest(canonical(frontier.schema(frontier_live.manifest_for(_offered()))))
    assert row["system_prompt_sha256"] == digest(frontier.SYSTEM)
    assert row["requested_model"] == frontier_live.FRONTIER_ARM and row["resolved_model"] == provider.MODEL
    assert row["media"]["attached"] is False and row["media"]["clip"] is None
    assert row["media"]["unavailable_reason"] == "capture is disabled for this test"
    assert row["received_unapplied"] == []
    packet = _packet(meta.bodies[0])
    assert packet["facts"] and all(set(fact) == {"text", "seqs"} for fact in packet["facts"])
    assert any(fact["seqs"] == [1] for fact in packet["facts"])  # the position fact's explicit inspection seq
    assert packet["recent"][0]["turn"]["outcome"] == "escalated"
    assert "reason" not in json.dumps(packet["escalation"])
    assert "distribution" not in json.dumps(packet["recent"])

    assert len(out["loop"].adoptions) == 1 and out["loop"].pending_helper is None
    assert out["applied"] == []  # the frontier sends no input; S1's escalate turn waited
    assert out["frontier"].directive is not None and out["frontier"].directive_history == []
    state = out["model"].bodies[1]["state"]
    assert state["directive"]["text"] == "Try the scene once more."
    assert state["directive_history"] == []
    assert out["section"]["adherence"] == {"rate": 1.0, "violations": []}
    assert check_adherence(out["loop"].turns) == []
    assert [t["outcome"] for t in out["section"]["turns"]] == ["escalated", "finished"]


def test_action_and_displacement_evidence_carries_both_inspection_sequences(tmp_path: Path) -> None:
    meta = Meta([_meta_response({**_empty(), "directive": _directive("After the hold.")})])
    out = _episode(tmp_path, S1({0: "c1", 1: "escalate", 2: "finish"}), meta)
    packet = _packet(meta.bodies[0])
    displacement = [fact for fact in packet["facts"] if "net displacement" in fact["text"]]
    assert len(displacement) == 1 and displacement[0]["seqs"] == [1, 2]
    assert any(fact["seqs"] == [2] for fact in packet["facts"])  # the escalation turn's position
    assert out["applied"] == ["shift"] and len(out["frontier"].rows) == 1


def test_an_invalid_s1_answer_note_is_not_cited_game_evidence(tmp_path: Path) -> None:
    bad = (200, {"model": "mammoth-1", "answers": {"next_action": {"type": "choice", "choice": "zz"}}}, 9.0, None)
    meta = Meta([_meta_response({**_empty(), "directive": _directive("After the invalid answer.")})])
    out = _episode(tmp_path, S1({1: "escalate", 2: "finish"}, raw={0: bad}), meta)
    packet = _packet(meta.bodies[0])
    assert packet["facts"] and all("did not name an offered choice" not in fact["text"] for fact in packet["facts"])
    assert out["section"]["turns"][0]["outcome"] == "rejected_invalid"
    assert "did not name an offered choice" in out["section"]["turns"][1]["facts"][0]
    assert len(out["frontier"].rows) == 1


@pytest.mark.parametrize("choice", ["wait", "finish", "delegate"])
def test_no_escalation_or_a_delegate_makes_zero_frontier_calls(tmp_path: Path, choice: str) -> None:
    meta = Meta([_meta_response(_empty())])
    out = _episode(tmp_path, S1({0: choice, 1: "finish"}), meta, max_turns=4)
    assert meta.bodies == [] and out["frontier"].rows == []
    assert "stop" not in out


def test_many_action_facts_never_trigger_a_frontier_call(tmp_path: Path) -> None:
    choices = {turn: ("c1" if turn % 2 == 0 else "c2") for turn in range(7)}
    choices[7] = "finish"
    meta = Meta([_meta_response(_empty())])
    out = _episode(tmp_path, S1(choices), meta, max_turns=9)
    assert meta.bodies == [] and out["frontier"].rows == []
    assert len(out["section"]["turns"]) == 8
    assert len(out["section"]["turns"][-1]["facts"]) >= 7
    assert out["section"]["adherence"] == {"rate": 1.0, "violations": []}


def test_the_packet_carries_the_objective_s1_was_actually_given(tmp_path: Path) -> None:
    meta = Meta([_meta_response({**_empty(), "directive": _directive("Asked once.")})])
    out = _episode(tmp_path, S1({0: "escalate", 1: "finish"}), meta, objective=frontier_live.HELP_OBJECTIVE)
    assert out["model"].bodies[0]["state"]["objective"] == frontier_live.HELP_OBJECTIVE
    assert _packet(meta.bodies[0])["objective"] == frontier_live.HELP_OBJECTIVE
    assert out["frontier"].rows[0]["packet"]["objective"] == frontier_live.HELP_OBJECTIVE
    assert frontier_live.HELP_OBJECTIVE.startswith(live.OBJECTIVE) and frontier_live.HELP_OBJECTIVE != live.OBJECTIVE


def test_an_objective_that_invites_help_never_makes_code_ask_for_it(tmp_path: Path) -> None:
    # `increment` moves nothing in the fake game, so S1 is shown "0.0 world units" for the checks it picks.
    # The objective may invite a request for help; only S1 choosing `escalate` can make a frontier call.
    meta = Meta([_meta_response(_empty())])
    out = _episode(tmp_path, S1({0: "c2", 1: "c2", 2: "c2", 3: "finish"}), meta,
                   objective=frontier_live.HELP_OBJECTIVE, max_turns=6)
    shown = [fact for turn in out["section"]["turns"] for fact in turn["facts"]]
    assert any("net displacement" in fact and ": 0.0 world units" in fact for fact in shown)
    assert meta.bodies == [] and out["frontier"].rows == []
    assert "stop" not in out and out["loop"].pending_helper is None
    assert out["section"]["adherence"] == {"rate": 1.0, "violations": []}


def test_a_binding_without_the_objective_makes_no_call(tmp_path: Path) -> None:
    class NoObjective(ScriptedS1):
        def decide(self, choice_set, observation):
            decision = super().decide(choice_set, observation)
            del self.binding["objective"]
            return decision

    meta = Meta([_meta_response(_empty())])
    out = _episode(tmp_path, NoObjective([{"choice_id": "escalate"}]), meta, max_turns=2)
    assert meta.bodies == [] and out["frontier"].rows == []
    assert isinstance(out["stop"], live.ProviderStop) and "objective" in str(out["stop"])


def test_a_directive_without_a_checkable_outcome_is_adopted_unscored(tmp_path: Path) -> None:
    meta = Meta([_meta_response({**_empty(), "directive": _directive("Look around once.", field=None)})])
    out = _episode(tmp_path, S1({0: "escalate", 1: "finish"}), meta)
    assert out["frontier"].directive is not None and out["frontier"].directive.expected is None
    assert out["frontier"].directive.outcome.value == "unknown"
    assert out["frontier"].rows[0]["directive_result"] == "adopted"
    assert out["model"].bodies[1]["state"]["directive"]["text"] == "Look around once."


def test_a_second_escalation_is_a_second_call_and_supersedes_unknown(tmp_path: Path) -> None:
    meta = Meta([_meta_response({**_empty(), "directive": _directive("First test.")}),
                 _meta_response({**_empty(), "directive": _directive("Second test.")})])
    out = _episode(tmp_path, S1({0: "escalate", 1: "escalate", 2: "finish"}), meta)
    rows = out["frontier"].rows
    assert len(meta.bodies) == 2 and len(rows) == 2
    assert [row["directive_result"] for row in rows] == ["adopted", "adopted"]
    assert out["frontier"].directive.text == "Second test."
    assert len(out["frontier"].directive_history) == 1
    assert out["frontier"].directive_history[0].outcome.value == "unknown"
    assert out["model"].bodies[2]["state"]["directive"]["text"] == "Second test."
    assert out["model"].bodies[2]["state"]["directive_history"][0]["outcome"] == "unknown"
    assert len(out["loop"].adoptions) == 2


# --- malformed and rejected answers -------------------------------------------------------


def _refused() -> tuple:
    return 200, {"model": provider.MODEL, "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": "", "refusal": "I cannot help"}}]}, 5.0, None


MALFORMED = [
    ("refused", _refused, "rejected"),
    ("truncated", lambda: _meta_response(_empty(), finish="length"), "not 'stop'"),
    ("not_json", lambda: _meta_response("not json"), "not JSON"),
    ("missing_fields", lambda: _meta_response({}), "add_choices"),
    ("http_500", lambda: (500, None, 3.0, "HTTP error"), "500"),
    ("transport", lambda: (None, None, 3.0, "transport error"), "transport failure"),
]


@pytest.mark.parametrize("data, needle", [(make(), needle) for _, make, needle in MALFORMED],
                         ids=[name for name, _, _ in MALFORMED])
def test_a_bad_frontier_answer_stops_without_retry(tmp_path: Path, data, needle) -> None:
    meta = Meta([data])
    out = _episode(tmp_path, S1({0: "escalate", 1: "finish"}), meta)
    rows = out["frontier"].rows
    assert isinstance(out["stop"], live.ProviderStop) and needle in str(out["stop"])
    assert len(meta.bodies) == 1 and len(rows) == 1
    assert rows[0]["status"] == "finished" and rows[0]["parse_valid"] is False
    assert rows[0]["review"] is None and rows[0]["errors"]
    assert out["frontier"].loop.adoptions == []
    assert len(out["section"]["turns"]) == 1


def test_an_invalid_directive_is_not_converted_and_keeps_the_current_one(tmp_path: Path) -> None:
    bad = {**_empty(), "directive": _directive("Impossible field.", field="public.unknown")}
    meta = Meta([_meta_response({**_empty(), "directive": _directive("Keep this test.")}), _meta_response(bad)])
    out = _episode(tmp_path, S1({0: "escalate", 1: "escalate", 2: "finish"}), meta)
    rows = out["frontier"].rows
    assert len(rows) == 2 and "stop" not in out
    assert rows[1]["parse_valid"] is True and rows[1]["schema_valid"] is False
    assert rows[1]["directive_result"] == "rejected" and rows[1]["directive"] is None
    assert any(problem["item"] == "directive" for problem in rows[1]["review"]["problems"])
    assert out["frontier"].directive.text == "Keep this test."
    assert out["frontier"].directive_history == []
    assert out["model"].bodies[2]["state"]["directive"]["text"] == "Keep this test."

    schema = frontier.schema(frontier_live.manifest_for(_offered()))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_schema_valid_is_not_review_clean(tmp_path: Path) -> None:
    hypothesis = {"id": "h99", "statement": "an interpretation", "status": "open",
                  "supporting_seq": [], "contradicting_seq": [],
                  "confirm_if": "it happens again", "refute_if": "it never happens", "what_one_would_expect": "unknown"}
    answer = {**_empty(), "hypotheses": [hypothesis]}
    meta = Meta([_meta_response(answer)])
    out = _episode(tmp_path, S1({0: "escalate", 1: "finish"}), meta)
    row = out["frontier"].rows[0]
    assert row["parse_valid"] is True and row["schema_valid"] is True
    assert row["review"]["clean"] is False
    assert {problem["item"] for problem in row["review"]["problems"]} == {"hypotheses[0]"}
    assert row["received_unapplied"][0]["item"] == "hypotheses"
    schema = frontier.schema(frontier_live.manifest_for(_offered()))
    jsonschema.validate(answer, schema)


def test_a_null_directive_keeps_the_current_one_and_other_items_stay_in_the_row(tmp_path: Path) -> None:
    finding = {"actual": "the scene stayed", "conditions": "after the escalation",
               "evidence_seq": [], "expected": "the scene changes", "expected_source": "interpretation",
               "repro_check": None, "severity": "unknown", "severity_reason": "not measured",
               "steps": ["repeat the same action"], "title": "scene did not change", "uncertainty": "one sample"}
    second = _meta_response({**_empty(), "candidate_finding": finding})
    meta = Meta([_meta_response({**_empty(), "directive": _directive("Keep this test.")}), second])
    out = _episode(tmp_path, S1({0: "escalate", 1: "escalate", 2: "finish"}), meta)
    rows = out["frontier"].rows
    assert rows[1]["directive_result"] == "kept_current" and rows[1]["directive"] is None
    assert out["frontier"].directive.text == "Keep this test."
    assert out["frontier"].directive_history == []
    assert {item["item"] for item in rows[1]["received_unapplied"]} == {"candidate_finding"}
    assert out["loop"].adoptions[1]["answer"]["candidate_finding"]["title"] == "scene did not change"


# --- pending help and the exact S1 context ------------------------------------------------


def test_a_while_pending_continuation_executes_once_and_is_not_replaced(tmp_path: Path) -> None:
    script = [{"choice_id": "escalate", "while_pending": {"mode": "continue", "choice_id": "c1"}},
              {"choice_id": "finish"}]
    meta = Meta([_meta_response({**_empty(), "directive": _directive("After the continuation.")})])
    out = _episode(tmp_path, ScriptedS1(script), meta)
    turns = out["section"]["turns"]
    assert turns[0]["outcome"] == "escalated" and turns[0]["executed_action"] == "c1"
    assert out["applied"] == ["shift"]
    assert len(meta.bodies) == 1 and len(out["loop"].adoptions) == 1
    recent = _packet(meta.bodies[0])["recent"][0]
    assert recent["executed_choice"]["id"] == "c1"
    assert recent["executed_choice"]["action"] == "shift"
    assert recent["turn"]["while_pending"] == {"mode": "continue", "choice_id": "c1"}
    assert "input_start_game_s" in recent["execution"]
    assert "input_end_game_s" in recent["execution"]
    assert recent["execution"]["wall_utc"] is not None
    assert out["section"]["summary"]["controls_completed"] == 1
    assert turns[0]["observed_displacement"] == 6.0
    assert out["loop"].pending_helper is None
    assert out["section"]["adherence"] == {"rate": 1.0, "violations": []}


def test_negative_turn_facts_cite_the_turn_and_the_exact_execution_record() -> None:
    facts = live.Facts(lambda: {})
    choice = Choice(id="c1", kind=ChoiceKind.action, label="hold a", action="shift", source=ChoiceSource.manifest)
    record = TurnRecord(turn=0, set_version=0, based_on_seq=4, raw_choice="c1", effective_choice="c1",
                        executed_action=None, outcome=TurnOutcome.rejected_stale, latency_ms=1.0)
    execution = type("Execution", (), {
        "seq": 42, "status": ExecStatus.rejected, "reject_reason": RejectReason.stale_age,
        "validated_against_seq": 4, "applied_duration_s": None, "release_verified": None})()
    facts.note_turn(choice, record, execution)
    assert facts.evidence() == [{
        "text": "c1 (shift) was rejected by the bridge (stale_age); no input was sent.",
        "seqs": [4, 42],
    }]
    facts.note_turn(None, TurnRecord(turn=1, set_version=0, based_on_seq=5, raw_choice=None, effective_choice=None,
                                     executed_action=None, outcome=TurnOutcome.rejected_invalid),
                    None)
    assert len(facts.evidence()) == 1  # S1's answer note is not game evidence
    assert len(facts.context()) == 2


# --- ledger and approval ------------------------------------------------------------------


def test_the_shared_ledger_counts_all_rows_overall_and_only_its_arm_per_arm(tmp_path: Path) -> None:
    approval = _approval(s1_calls=2, frontier_calls=2, overall=3)
    s1_ledger = call_budget.Ledger(tmp_path / "run", approval, live.ARM)
    frontier_ledger = call_budget.Ledger(tmp_path / "run", approval, frontier_live.FRONTIER_ARM)
    s1_ledger.reserve(episode=1, turn=0, based_on_seq=1, request_sha256="a" * 64,
                      requested_model="s1").finish(http_status=200)
    frontier_ledger.reserve(episode=1, turn=0, based_on_seq=1, request_sha256="b" * 64,
                            requested_model="f").finish(http_status=200)
    s1_ledger.reserve(episode=1, turn=1, based_on_seq=2, request_sha256="c" * 64,
                      requested_model="s1").finish(http_status=200)
    assert len(s1_ledger.arm_rows()) == 2 and len(frontier_ledger.arm_rows()) == 1
    with pytest.raises(call_budget.BudgetStop, match="envelope"):
        frontier_ledger.reserve(episode=1, turn=1, based_on_seq=2, request_sha256="d" * 64, requested_model="f")
    assert s1_ledger.arm_rows()[0]["cost_bound_exceeded"] is None


def test_one_full_arm_does_not_block_another_arms_own_allowance(tmp_path: Path) -> None:
    approval = _approval(s1_calls=3, frontier_calls=1, overall=10)
    s1_ledger = call_budget.Ledger(tmp_path / "run", approval, live.ARM)
    frontier_ledger = call_budget.Ledger(tmp_path / "run", approval, frontier_live.FRONTIER_ARM)
    frontier_ledger.reserve(episode=1, turn=0, based_on_seq=1, request_sha256="b" * 64,
                            requested_model="f").finish(http_status=200)
    for turn in range(3):
        s1_ledger.reserve(episode=1, turn=turn, based_on_seq=turn + 1, request_sha256=str(turn) * 64,
                          requested_model="s1").finish(http_status=200)
    with pytest.raises(call_budget.BudgetStop, match="envelope"):
        s1_ledger.reserve(episode=1, turn=3, based_on_seq=4, request_sha256="e" * 64, requested_model="s1")
    assert len(frontier_ledger.arm_rows()) == 1 and len(s1_ledger.arm_rows()) == 3


def test_an_expired_price_or_a_stale_in_flight_row_sends_nothing(tmp_path: Path) -> None:
    expired = _approval(price="2000-01-01T00:00:00Z")
    with pytest.raises(call_budget.BudgetStop, match="pricing validity expired"):
        call_budget.Ledger(tmp_path / "expired", expired, frontier_live.FRONTIER_ARM).reserve(
            episode=1, turn=0, based_on_seq=1, request_sha256="a" * 64, requested_model="f")

    run = tmp_path / "stale"
    calls = run / "calls"
    calls.mkdir(parents=True)
    (calls / "old.json").write_text(json.dumps({"slot": "old", "arm": live.ARM, "status": "in_flight",
                                                "reserved_usd": "0.001"}), encoding="utf-8")
    with pytest.raises(call_budget.BudgetStop, match="in flight"):
        call_budget.Ledger(run, _approval(), frontier_live.FRONTIER_ARM).reserve(
            episode=1, turn=0, based_on_seq=1, request_sha256="a" * 64, requested_model="f")
    assert [path.name for path in calls.glob("*.json")] == ["old.json"]


def test_a_live_sessions_arms_may_overlap_but_each_keeps_one_call_in_flight(tmp_path: Path) -> None:
    # In a live session S1 keeps choosing while a frontier request is pending (the Day 5 gate run halted on this).
    approval = _approval(overall=10)
    s1 = call_budget.Ledger(tmp_path / "run", approval, live.ARM, concurrent_arms=True)
    frontier = call_budget.Ledger(tmp_path / "run", approval, frontier_live.FRONTIER_ARM, concurrent_arms=True)
    pending = frontier.reserve(episode=0, turn=3, based_on_seq=4, request_sha256="a" * 64, requested_model="f")
    s1.reserve(episode=0, turn=4, based_on_seq=5, request_sha256="b" * 64, requested_model="s1").finish(http_status=200)
    with pytest.raises(call_budget.BudgetStop, match="in flight"):  # still one frontier request at a time
        frontier.reserve(episode=0, turn=5, based_on_seq=6, request_sha256="c" * 64, requested_model="f")
    s1_pending = s1.reserve(episode=0, turn=5, based_on_seq=6, request_sha256="d" * 64, requested_model="s1")
    with pytest.raises(call_budget.BudgetStop, match="in flight"):  # and one S1 call at a time
        s1.reserve(episode=0, turn=6, based_on_seq=7, request_sha256="e" * 64, requested_model="s1")
    pending.finish(http_status=200)
    s1_pending.finish(http_status=200)
    frontier.reserve(episode=0, turn=6, based_on_seq=7, request_sha256="f" * 64, requested_model="f").finish(
        http_status=200)
    assert len(frontier.arm_rows()) == 2 and len(s1.arm_rows()) == 2


def test_an_overlapping_arms_in_flight_reservation_still_counts_against_the_envelope(tmp_path: Path) -> None:
    approval = _approval(overall=2)
    s1 = call_budget.Ledger(tmp_path / "run", approval, live.ARM, concurrent_arms=True)
    frontier = call_budget.Ledger(tmp_path / "run", approval, frontier_live.FRONTIER_ARM, concurrent_arms=True)
    frontier.reserve(episode=0, turn=1, based_on_seq=2, request_sha256="a" * 64, requested_model="f")
    s1.reserve(episode=0, turn=2, based_on_seq=3, request_sha256="b" * 64, requested_model="s1").finish(http_status=200)
    with pytest.raises(call_budget.BudgetStop, match="envelope"):
        s1.reserve(episode=0, turn=3, based_on_seq=4, request_sha256="c" * 64, requested_model="s1")


def test_validate_approval_maps_the_frontier_arm_to_meta_not_baseten() -> None:
    approval = {
        "approved_by": "Test User", "approved_at_utc": "2026-01-01T00:00:00Z",
        "max_calls": 10, "max_cost_usd": 1.0,
        "arms": {"mammoth-1": _limit(per_call=0.001, max_cost=0.006, balance=0.01),
                 frontier_live.FRONTIER_ARM: _limit(per_call=0.001, max_cost=0.006, balance=5.0)},
    }
    replay.validate_approval(approval, [live.ARM, frontier_live.FRONTIER_ARM])
    # A Baseten mapping would total 0.012 against the 0.01 Baseten balance and fail.
    assert approval["arms"][frontier_live.FRONTIER_ARM]["verified_balance_usd"] == 5.0


def test_main_requires_the_meta_key_ffmpeg_and_an_envelope_for_the_frontier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BASETEN_API_KEY", "test-baseten")
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("UPA_LIVE_FRONTIER", raising=False)
    base = ["--project", str(tmp_path / "game"), "--entity", str(tmp_path / "e.json"),
            "--calibration", str(tmp_path / "c.json"), "--output", str(tmp_path / "out" / "trace.json"),
            "--frontier"]
    with pytest.raises(SystemExit) as no_meta:
        live.main(base)
    assert no_meta.value.code == 2

    monkeypatch.setenv("META_API_KEY", "test-meta")
    monkeypatch.setattr(live.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as no_ffmpeg:
        live.main(base)
    assert no_ffmpeg.value.code == 2

    monkeypatch.setattr(live.shutil, "which", lambda name: "ffmpeg")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(_approval(s1_calls=2, frontier_calls=2, overall=3)), encoding="utf-8")
    args = base + ["--approval", str(approval), "--episodes", "1", "--max-turns", "2"]
    assert live.main(args) == 1  # 2 S1 + 2 frontier calls cannot fit an overall cap of 3
    assert not (tmp_path / "out").exists()

    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.setenv("UPA_LIVE_FRONTIER", "1")
    with pytest.raises(SystemExit) as via_env:
        live.main([item for item in args if item != "--frontier"])
    assert via_env.value.code == 2


@pytest.mark.parametrize("frontier_on", [True, False])
def test_main_invites_help_in_the_objective_only_when_the_frontier_is_on(
        tmp_path: Path, monkeypatch, frontier_on: bool) -> None:
    seen: dict = {}

    async def fake_run(project, selected, calibration, trace, output, s1, *args, **kwargs):
        seen.update(s1=s1, trace=trace, plan=kwargs["plan"])

    monkeypatch.setattr(live, "_run", fake_run)
    monkeypatch.setenv("BASETEN_API_KEY", "test-baseten")
    monkeypatch.setenv("META_API_KEY", "test-meta")
    monkeypatch.delenv("UPA_LIVE_FRONTIER", raising=False)
    monkeypatch.setattr(live.shutil, "which", lambda name: "ffmpeg")
    for name in ("e.json", "c.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(_approval()), encoding="utf-8")
    args = ["--project", str(tmp_path / "game"), "--entity", str(tmp_path / "e.json"),
            "--calibration", str(tmp_path / "c.json"), "--output", str(tmp_path / "out" / "trace.json"),
            "--approval", str(approval), "--episodes", "1", "--max-turns", "2"]
    live.main(args + (["--frontier"] if frontier_on else []))

    expected = frontier_live.HELP_OBJECTIVE if frontier_on else live.OBJECTIVE
    assert seen["s1"].objective == expected
    assert seen["trace"]["objective_sha256"] == digest(expected)
    recorded = seen["plan"].get("frontier", {}).get("objective_sha256")
    assert recorded == (digest(expected) if frontier_on else None)


def test_acceptance_needs_a_complete_run_and_a_nonempty_attached_clip() -> None:
    passing = {"role": "frontier", "http_status": 200, "parse_valid": True, "schema_valid": True,
               "media": {"attached": True, "clip": {"bytes": 12}}}
    assert frontier_live.acceptance([], run_status="complete")["acceptance"] == "inconclusive"
    assert frontier_live.acceptance([passing], run_status="complete")["acceptance"] == "passed"
    assert frontier_live.acceptance([passing], run_status="stopped")["acceptance"] == "inconclusive"
    no_clip = {**passing, "media": {"attached": False, "clip": None}}
    assert frontier_live.acceptance([no_clip], run_status="complete")["acceptance"] == "failed"
    assert frontier_live.acceptance([{**passing, "schema_valid": False}], run_status="complete")["acceptance"] == "failed"


def test_the_manifest_is_a_conservative_calibrated_stand_in(tmp_path: Path) -> None:
    offered = _offered() + [Choice(id="c3", kind=ChoiceKind.action, label="duplicate",
                                    action="shift", source=ChoiceSource.manifest)]
    manifest = frontier_live.manifest_for(offered)
    assert manifest.fields == {"scene": "string"}
    assert list(manifest.actions) == ["increment", "shift"]
    assert all(spec == frontier.ActionSpec() for spec in manifest.actions.values())
    assert manifest.skills == []
    adapter = frontier_live.LiveFrontier(tmp_path / "run", _approval(), media_source=_no_clip)
    block = adapter.use_manifest(manifest)
    assert block["schema_sha256"] == digest(canonical(frontier.schema(manifest)))
    assert block["system_prompt_sha256"] == digest(frontier.SYSTEM)
    assert block["manifest_message_sha256"] == digest(provider.manifest_message(manifest))
    assert block["manifest_sha256"] == digest(canonical({
        "fields": [{"path": "scene", "kind": "string"}],
        "actions": ["increment", "shift"], "skills": []}))
    assert block["manifest_provenance"] == frontier_live.MANIFEST_PROVENANCE
    assert block["manifest_source"] == "calibrated offered choices"


def test_a_clip_is_attached_hashed_and_kept_out_of_the_saved_request(tmp_path: Path) -> None:
    meta = Meta([_meta_response({**_empty(), "directive": _directive("With a clip.")})])
    out = _episode(tmp_path, S1({0: "escalate", 1: "finish"}), meta, media_source=_fake_clip)
    row = out["frontier"].rows[0]
    assert row["media"]["attached"] is True and row["media"]["clip"]["bytes"] > 0
    assert row["packet"]["clip"]["frame_count"] == 2
    content = meta.bodies[0]["messages"][2]["content"]
    assert isinstance(content, list) and content[1]["type"] == "video_url"
    assert "base64" in content[1]["video_url"]["url"]
    saved = canonical(row)
    assert "base64" not in saved
    assert str(tmp_path).replace("\\", "/") not in saved
    assert row["media"]["folder"].startswith("clips/")
    assert row["media"]["manifest"].endswith("/manifest.json")
