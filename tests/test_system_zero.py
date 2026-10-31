"""System Zero (experimental S1 arm): the layout its option limits require, the fixed decoder, and the transport.

The layout asks S1 to choose exactly one offered choice; nothing here may rank, compare or pick. These tests
fail if a decoded choice comes from anywhere but S1's root answer and the selected group's answer, if a refusal
is repaired, or if the flat case drifts from the System One shape the other arms send.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from s1_fakes import Send, approval
from playtest.bench import call_budget, replay, system_zero
from playtest.bench.record import RECORDED_INSTRUCTIONS, context_for, digest, recorded_payload
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceSet
from playtest.core.context import INSTRUCTIONS, build_context, canonical
from playtest.runner import s1 as s1_module
from playtest.runner.s1 import FOLLOW_UP_INSTRUCTIONS, SystemOneS1

ARM = system_zero.ARM
QUESTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")   # the API's question-name rule


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)


def fpskit_like(actions: int = 14, skills: int = 4) -> list[Choice]:
    """The shape of the current FPSKit sessions: 14 actions, 4 skills, then the four core choices."""
    out = [Choice(id=f"a{i}", kind="action", label=f"Action {i}", action=f"a{i}", source="manifest")
           for i in range(1, actions + 1)]
    out += [Choice(id=f"s{i}", kind="skill", label=f"Skill {i}", skill=f"s{i}", source="manifest")
            for i in range(1, skills + 1)]
    return out + [Choice(id="w", kind="wait", label="Wait", source="core"),
                  Choice(id="h", kind="escalate", label="Escalate", role="frontier", source="core"),
                  Choice(id="d", kind="delegate", label="Delegate", role="helper", source="core"),
                  Choice(id="f", kind="finish", label="Finish", source="core")]


def items(choices: list[Choice]) -> list[tuple[str, str, str]]:
    return [(c.id, c.label, c.kind.value) for c in choices]


def context(choices: list[Choice], version: int = 4) -> dict[str, Any]:
    obs = Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 31, tzinfo=timezone.utc),
                      clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=9)
    return build_context(objective="Exercise the controls.", observation=obs,
                         choices=ChoiceSet(set_version=version, built_from_seq=7, choices=choices))


def answers(questions: dict[str, Any], picks: dict[str, str], favour_last: bool = True,
            refused: dict[str, str] | None = None) -> dict[str, Any]:
    """A reply answering every question: `picks` names each answer, while most probability goes elsewhere."""
    out: dict[str, Any] = {}
    for name, question in questions.items():
        if refused and name in refused:
            out[name] = {"error": {"code": refused[name], "retryable": False}}
            continue
        options = list(question["criteria"])
        choice = picks.get(name, options[0])
        top = options[-1] if favour_last and options[-1] != choice else choice
        probabilities = {key: (0.8 if key == top else 0.2 / (len(options) - 1)) for key in options}
        out[name] = {"type": "choice", "choice": choice, "probabilities": probabilities,
                     "decision": choice, "confidence": probabilities[choice], "calibrated": False}
    return {"model": "system-zero-x.y.z", "answers": out,
            "usage": {"input_tokens": 900, "cached_input_tokens": 0, "media_tokens": 0}}


# --- layout -------------------------------------------------------------------------------


def test_choices_that_fit_one_question_keep_the_system_one_shape():
    choices = fpskit_like(actions=3, skills=0)
    questions = system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=False)
    assert questions == {"next_action": {"type": "choice", "instructions": INSTRUCTIONS,
                                         "criteria": {c.id: c.label for c in choices}}}


def test_an_attachment_raises_the_limit_so_fpskits_22_choices_go_flat():
    choices = fpskit_like()
    assert len(choices) == 22
    flat = system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=True)
    assert list(flat) == ["next_action"] and len(flat["next_action"]["criteria"]) == 22


def test_too_many_choices_for_one_text_question_are_grouped_by_family_in_context_order():
    choices = fpskit_like()
    questions = system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=False)
    root = questions["next_action"]["criteria"]
    assert list(root) == ["next_action__action1", "next_action__action2", "next_action__skill1", "next_action__core1"]
    groups = {name: list(q["criteria"]) for name, q in questions.items() if name != "next_action"}
    assert groups["next_action__action1"] == [f"a{i}" for i in range(1, 8)]
    assert groups["next_action__action2"] == [f"a{i}" for i in range(8, 15)]
    assert groups["next_action__core1"] == ["w", "h", "d", "f"]
    # Every choice is offered exactly once, in the context's order; no option list breaks a served bound.
    flattened = [cid for name in root for cid in groups.get(name, [name])]
    assert flattened == [c.id for c in choices]
    for name, question in questions.items():
        assert QUESTION_NAME.match(name) and question["type"] == "choice"
        assert system_zero.MIN_OPTIONS <= len(question["criteria"]) <= system_zero.CHOICE_OPTIONS["text"]
    assert questions["next_action"]["instructions"] == INSTRUCTIONS + system_zero.ROOT_NOTE
    assert questions["next_action__skill1"]["instructions"] == INSTRUCTIONS + system_zero.GROUP_NOTE
    assert root["next_action__skill1"] == "one of: s1, s2, s3, s4"


def test_a_choice_alone_in_its_family_is_offered_at_the_root_as_itself():
    choices = fpskit_like(actions=11, skills=1)
    questions = system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=False)
    root = list(questions["next_action"]["criteria"])
    assert root == ["next_action__action1", "next_action__action2", "s1", "next_action__core1"]
    assert "s1" not in questions and [len(questions[g]["criteria"]) for g in root if g in questions] == [6, 5, 4]


def test_a_layout_that_cannot_fit_or_would_collide_is_refused():
    with pytest.raises(ValueError, match="do not fit two levels"):
        system_zero.layout("next_action", INSTRUCTIONS, items(fpskit_like(actions=120)), media=False)
    clash = fpskit_like()
    clash[0] = Choice(id="next_action__action1", kind="action", label="X", action="x", source="manifest")
    with pytest.raises(ValueError, match="collides"):
        system_zero.layout("next_action", INSTRUCTIONS, items(clash), media=False)


# --- decode -------------------------------------------------------------------------------


def grouped() -> tuple[dict[str, Any], set[str]]:
    choices = fpskit_like()
    return system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=False), {c.id for c in choices}


def test_the_root_and_the_selected_groups_answer_name_the_choice_whatever_the_probabilities():
    questions, offered = grouped()
    # Every other group names something too; only the selected group's answer is used.
    data = answers(questions, {"next_action": "next_action__action2", "next_action__action2": "a9",
                               "next_action__action1": "a1", "next_action__core1": "h"})
    choice, distribution, problem = system_zero.decode("next_action", questions, data, offered)
    assert choice == "a9" and problem is None
    # Most probability sits elsewhere (the last option of every question); it never selects.
    assert max(distribution, key=distribution.get) != "a9"
    assert set(distribution) == offered and abs(sum(distribution.values()) - 1.0) < 1e-9
    assert distribution["a9"] == pytest.approx(
        data["answers"]["next_action"]["probabilities"]["next_action__action2"]
        * data["answers"]["next_action__action2"]["probabilities"]["a9"])


def test_a_root_answer_naming_a_lone_choice_is_that_choice():
    choices = fpskit_like(actions=11, skills=1)
    questions = system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=False)
    choice, _, problem = system_zero.decode("next_action", questions, answers(questions, {"next_action": "s1"}),
                                            {c.id for c in choices})
    assert (choice, problem) == ("s1", None)


def test_a_refused_root_or_selected_group_leaves_no_choice_and_nothing_is_substituted():
    questions, offered = grouped()
    root_refused = answers(questions, {}, refused={"next_action": "injection_suspected"})
    assert system_zero.decode("next_action", questions, root_refused, offered) == (
        None, None, "next_action refused: injection_suspected")
    group_refused = answers(questions, {"next_action": "next_action__core1"},
                            refused={"next_action__core1": "question_not_grounded"})
    choice, distribution, problem = system_zero.decode("next_action", questions, group_refused, offered)
    assert choice is None and distribution is None and "next_action__core1 refused: question_not_grounded" in problem


def test_a_refused_unselected_group_keeps_the_choice_but_not_the_composed_distribution():
    questions, offered = grouped()
    data = answers(questions, {"next_action": "next_action__action1", "next_action__action1": "a3"},
                   refused={"next_action__skill1": "question_not_grounded"})
    choice, distribution, problem = system_zero.decode("next_action", questions, data, offered)
    assert choice == "a3" and distribution is None and problem == "next_action__skill1 refused: question_not_grounded"


def test_an_answer_outside_its_group_or_a_missing_reply_is_invalid():
    questions, offered = grouped()
    data = answers(questions, {"next_action": "next_action__action1"})
    data["answers"]["next_action__action1"]["choice"] = "a9"   # a9 is in the other action group
    choice, _, problem = system_zero.decode("next_action", questions, data, offered)
    assert choice is None and "next_action__action1: invalid or unoffered choice" in problem
    assert system_zero.decode("next_action", questions, None, offered) == (None, None, "invalid or unoffered choice")


def test_the_flat_case_decodes_like_the_other_arms():
    choices = fpskit_like(actions=3, skills=0)
    questions = system_zero.layout("next_action", INSTRUCTIONS, items(choices), media=False)
    data = answers(questions, {"next_action": "a2"})
    offered = {c.id for c in choices}
    got = system_zero.decode("next_action", questions, data, offered)
    assert got == replay.parse_answer(data, offered)
    data["answers"]["next_action"]["probabilities"]["a2"] = 0.5   # no longer sums to 1
    assert system_zero.decode("next_action", questions, data, offered) == ("a2", None, "next_action: invalid distribution")


# --- transport ----------------------------------------------------------------------------


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_transport_posts_once_with_the_key_and_returns_the_reply(monkeypatch):
    seen = []

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization"), json.loads(request.data), timeout))
        return _Response(json.dumps({"answers": {}}).encode())

    monkeypatch.setattr(system_zero.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("SYSTEM_ZERO_API_KEY", "test-key")
    status, data, latency, problem = system_zero.send(ARM, {"model": "system-zero", "state": "s", "questions": {}})
    assert (status, data, problem) == (200, {"answers": {}}, None) and latency >= 0
    assert seen == [(system_zero.URL, "Bearer test-key", {"model": "system-zero", "questions": {}, "state": "s"},
                     system_zero.TIMEOUT_S)]


def test_an_http_error_keeps_the_providers_code_and_is_never_retried(monkeypatch):
    calls = []
    envelope = {"error": {"message": "slow down", "type": "rate_limit_error", "param": None,
                          "code": "rate_limit_exceeded", "retryable": True}, "request_id": "req_1"}

    def urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(system_zero.URL, 429, "Too Many Requests", {},
                                     io.BytesIO(json.dumps(envelope).encode()))

    monkeypatch.setattr(system_zero.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("SYSTEM_ZERO_API_KEY", "test-key")
    status, data, _, problem = system_zero.send(ARM, {"model": "system-zero", "state": "s", "questions": {}})
    assert (status, data, problem) == (429, envelope, "HTTP error: rate_limit_exceeded (retryable)") and calls == [1]


def test_the_transport_refuses_without_a_key_or_for_another_arm(monkeypatch):
    with pytest.raises(ValueError, match="missing credential"):
        system_zero.send(ARM, {})
    with pytest.raises(ValueError, match="not a System Zero arm"):
        system_zero.send("mammoth-1", {})


def test_the_arm_is_registered_bills_rightnow_and_routes_to_its_own_transport(monkeypatch):
    assert replay.ARMS[ARM] == ("system-zero-2.0.0", "SYSTEM_ZERO_API_KEY", None)   # pinned (G0)
    assert replay.provider_of(ARM) == "rightnow" and replay.provider_of("mammoth-1") == "baseten"
    routed = []
    monkeypatch.setattr(system_zero, "send", lambda arm, body: routed.append(arm) or (200, {}, 1.0, None))
    assert replay.send_systemone(ARM, {}) == (200, {}, 1.0, None) and routed == [ARM]


# --- the S1 adapter -----------------------------------------------------------------------


def sz_adapter(tmp_path: Path, *replies: Any) -> tuple[SystemOneS1, Send, call_budget.Ledger]:
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=10, send=send, arm=ARM), send, ledger


def scripted(picks: dict[str, str]):
    def make(body: dict[str, Any]):
        return 200, answers(body["questions"], picks), 15.0, None
    return make


def test_the_adapter_lays_out_22_text_choices_and_returns_the_decoded_choice(tmp_path: Path):
    port, send, ledger = sz_adapter(tmp_path, scripted({"next_action": "next_action__skill1",
                                                        "next_action__skill1": "s3"}))
    ctx = context(fpskit_like())
    got = asyncio.run(port.decide(ctx))
    body = send.bodies[0]
    assert set(body) == {"model", "questions", "state"} and body["model"] == "system-zero-2.0.0" and body["state"] == ctx
    assert body["questions"] == system_zero.layout("next_action", INSTRUCTIONS, items(fpskit_like()), media=False)
    assert got.answer["choice_id"] == "s3" and got.answer["set_version"] == 4 and got.failure is None
    call = got.calls[0]
    assert (call.provider, call.model, call.resolved_model) == ("rightnow", "system-zero-2.0.0", "system-zero-x.y.z")
    assert call.request_sha256 == digest(canonical(body)) and call.usage["input_tokens"] == 900
    assert ledger.arm_rows()[0]["arm"] == ARM and len(send.bodies) == 1


def test_a_refused_choice_rejects_the_turn_without_a_substitute(tmp_path: Path):
    def refuse(body):
        return 200, answers(body["questions"], {}, refused={"next_action": "injection_suspected"}), 15.0, None

    port, send, _ = sz_adapter(tmp_path, refuse)
    got = asyncio.run(port.decide(context(fpskit_like())))
    assert got.answer is None and got.failure == "next_action refused: injection_suspected" and len(send.bodies) == 1


def test_an_escalation_follow_up_is_laid_out_over_the_pending_choices(tmp_path: Path):
    port, send, _ = sz_adapter(tmp_path,
                               scripted({"next_action": "next_action__core1", "next_action__core1": "h"}),
                               scripted({"while_pending": "while_pending__action1", "while_pending__action1": "a2"}))
    got = asyncio.run(port.decide(context(fpskit_like())))
    follow = send.bodies[1]["questions"]
    pending = [(c.id, c.label, c.kind.value) for c in fpskit_like() if c.kind.value in ("wait", "action", "skill")]
    assert follow == system_zero.layout("while_pending", FOLLOW_UP_INSTRUCTIONS, pending, media=False)
    assert got.answer["choice_id"] == "h" and got.answer["while_pending"] == {"mode": "continue", "choice_id": "a2"}


def test_a_verified_image_goes_as_an_attachment_and_lets_the_choices_go_flat(tmp_path: Path):
    assert s1_module.IMAGE_TRANSPORT[ARM] == "attachments"   # verified by G1
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, scripted({"next_action": "a5"}))
    port = SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, game_view=True)
    image = b"\xff\xd8\xff-jpeg-bytes"
    ctx = context(fpskit_like())
    ctx.update(game_view_status="sent", game_view_sha256=digest(image))
    got = asyncio.run(port.decide(ctx, image=image))
    body = send.bodies[0]
    assert list(body["questions"]) == ["next_action"] and len(body["questions"]["next_action"]["criteria"]) == 22
    assert body["attachments"][0]["name"] == "game_view" and body["attachments"][0]["type"] == "image"
    assert body["attachments"][0]["data"].startswith("data:image/jpeg;base64,") and got.answer["choice_id"] == "a5"


def test_without_its_verified_transport_system_zero_refuses_game_view(tmp_path: Path, monkeypatch):
    monkeypatch.delitem(s1_module.IMAGE_TRANSPORT, ARM)
    limits = approval()
    limits["arms"] = {ARM: limits["arms"]["mammoth-1"]}
    with pytest.raises(ValueError, match="no verified image transport"):
        SystemOneS1(call_budget.Ledger(tmp_path, limits, ARM), max_calls=1, arm=ARM, game_view=True)


def test_the_prompt_hash_covers_the_layout_notes(tmp_path: Path):
    port, _, _ = sz_adapter(tmp_path)
    other = SystemOneS1(call_budget.Ledger(tmp_path / "x", approval(), "mammoth-1"), max_calls=1, arm="mammoth-1")
    assert port.prompt_sha256 != other.prompt_sha256


def test_a_layout_that_cannot_fit_makes_no_call(tmp_path: Path):
    port, send, ledger = sz_adapter(tmp_path)
    got = asyncio.run(port.decide(context(fpskit_like(actions=120))))
    assert got.answer is None and "no call was made" in got.failure and send.bodies == [] and ledger.arm_rows() == []


# --- the benches --------------------------------------------------------------------------


def test_the_bench_body_and_parser_use_the_layout_for_system_zero_only(monkeypatch):
    ctx = context(fpskit_like())
    frame = {"payload_sha256": digest(canonical(ctx))}
    monkeypatch.setattr(replay, "recorded_payload", lambda recorded: ctx)
    body = replay.request_body(frame, "system-zero-2.0.0", ARM)
    assert body["questions"] == system_zero.layout("next_action", INSTRUCTIONS, items(fpskit_like()), media=False)
    assert list(replay.request_body(frame, "d1-ultra", "liquid-d1-ultra")["questions"]) == ["next_action"]
    data = answers(body["questions"], {"next_action": "next_action__action1", "next_action__action1": "a4"})
    offered = {c.id for c in fpskit_like()}
    assert replay.parse_reply(ARM, body, data, offered)[0] == "a4"
    assert replay.provider_answer(ARM, data) == data["answers"]


def test_a_frozen_frame_rebuilds_with_its_recorded_instructions_only_when_the_hash_matches_exactly():
    obs = Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 27, tzinfo=timezone.utc),
                      clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=9)
    frame = {"objective": "Exercise the controls.", "observation": obs.model_dump(mode="json"), "facts": [],
             "choice_set": ChoiceSet(set_version=1, built_from_seq=7, choices=fpskit_like()).model_dump(mode="json")}
    today = context_for(frame)
    frame["payload_sha256"] = digest(canonical(today))
    assert recorded_payload(frame) == today                       # today's build is tried first
    recorded = {**today, "instructions": RECORDED_INSTRUCTIONS}   # a Day 3 frame, recorded before #157
    frame["payload_sha256"] = digest(canonical(recorded))
    assert recorded_payload(frame) == recorded and recorded["instructions"] != INSTRUCTIONS
    frame["payload_sha256"] = digest(canonical({**recorded, "facts": ["changed"]}))
    assert recorded_payload(frame) is None                        # anything else still refuses
