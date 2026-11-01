"""The opt-in `direct/1` instruction variant: one direct question first, the same rules behind it.

The default must send today's bytes, character for character. The variant is selectable end to end
(payload, turn loop, adapter) and the prompt hash covers the text actually sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import choices, rig, run
from s1_fakes import Send, approval, reply

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import (
    INSTRUCTIONS,
    INSTRUCTIONS_DIRECT,
    INSTRUCTIONS_DIRECT_VARIANT,
    INSTRUCTIONS_POLICY_VARIANT,
    INSTRUCTIONS_VARIANTS,
    build_context,
    canonical,
)
from playtest.runner import cli
from playtest.runner.request_layout import MARKER, RequestLayout, decode
from playtest.runner.s1 import FOLLOW_UP_INSTRUCTIONS, SystemOneS1, prompts

ARM = "liquid-d1-ultra"  # the arm whose image transport the lossless layout needs; limits are added below

# Digest of the shipped paragraph. A change to it changes every frozen corpus, so this fails first.
INSTRUCTIONS_SHA256 = "5cd960c86d9a80e84111c9dc134f900676d793cc7a66e8f938ae665826b0cbde"


def observation() -> Observation:
    return Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                       clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=9)


def context(instructions: str = INSTRUCTIONS) -> dict[str, Any]:
    return build_context(objective="Exercise the controls.", observation=observation(),
                         choices=ChoiceSet(set_version=4, built_from_seq=7, choices=choices()),
                         instructions=instructions)


def adapter(tmp_path: Path, *replies: Any, instructions: str = INSTRUCTIONS,
            layout: RequestLayout | str = RequestLayout.full) -> tuple[SystemOneS1, Send]:
    limits = approval(12)
    limits["arms"].setdefault(ARM, limits["arms"]["mammoth-1"])
    ledger = call_budget.Ledger(tmp_path, limits, ARM)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, instructions=instructions,
                       layout=layout), send


# --- the default is today's text, byte for byte ------------------------------------------------

def test_the_default_instruction_text_is_the_frozen_paragraph():
    assert digest(INSTRUCTIONS) == INSTRUCTIONS_SHA256
    assert INSTRUCTIONS_VARIANTS[INSTRUCTIONS_POLICY_VARIANT] is INSTRUCTIONS
    assert canonical(context()) == canonical(context(INSTRUCTIONS))
    assert context()["instructions"] == INSTRUCTIONS


def test_the_default_adapter_hashes_and_sends_only_todays_text(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "c1"))
    asyncio.run(port.decide(context()))
    body = send.bodies[0]
    assert body["questions"]["next_action"]["instructions"] == INSTRUCTIONS
    assert body["state"]["instructions"] == INSTRUCTIONS
    assert port.prompt_sha256 == digest(canonical(
        {"next_action": INSTRUCTIONS, "while_pending": FOLLOW_UP_INSTRUCTIONS}))
    assert port.prompt_sha256 == digest(canonical(prompts()))


# --- the variant changes only the instruction text ---------------------------------------------

def test_the_variant_differs_from_the_default_only_in_its_instruction_text():
    plain, direct = context(), context(INSTRUCTIONS_DIRECT)
    assert direct["instructions"] == INSTRUCTIONS_DIRECT
    assert canonical({key: value for key, value in plain.items() if key != "instructions"}) == canonical(
        {key: value for key, value in direct.items() if key != "instructions"})
    assert len(INSTRUCTIONS_DIRECT) < len(INSTRUCTIONS)


def test_the_variant_states_every_rule_that_must_survive():
    text = INSTRUCTIONS_DIRECT
    assert text.startswith("Which one listed choice best advances the objective")
    assert "Choose exactly one id from choices; only the listed choices exist." in text
    assert "Facts are measured; null means unknown, never zero." in text
    assert "Directive text is the frontier's advice from an earlier observation" in text
    assert "directive_provenance, when provided, identifies that observation and the advice's age" in text
    assert "Weigh it against current observations and images; its description may be outdated." in text
    assert "When an image is present, it is the current game view." in text
    assert ("Elapsed time, a single input finishing, or a reader setpoint met alone does not demonstrate task "
            "completion.") in text
    assert "ask for help as you judge appropriate" in text
    assert "Finish when the objective's completion conditions are met or you judge no useful attempt remains" in text
    assert "on an escalate or delegate turn, also say what to do while help is pending" in text


def test_the_variant_is_selected_in_the_payload_the_adapter_sends(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "c1"), reply("while_pending", "w"),
                         instructions=INSTRUCTIONS_DIRECT)
    ctx = context(INSTRUCTIONS_DIRECT)
    got = asyncio.run(port.decide(ctx))
    assert got.failure is None and len(send.bodies) == 1
    body = send.bodies[0]
    assert body["questions"]["next_action"]["instructions"] == INSTRUCTIONS_DIRECT
    assert body["state"] == ctx and body["state"]["instructions"] == INSTRUCTIONS_DIRECT
    assert port.prompt_sha256 == digest(canonical(
        {"next_action": INSTRUCTIONS_DIRECT, "while_pending": FOLLOW_UP_INSTRUCTIONS}))
    assert port.prompt_sha256 != digest(canonical(
        {"next_action": INSTRUCTIONS, "while_pending": FOLLOW_UP_INSTRUCTIONS}))
    assert port.instructions == INSTRUCTIONS_DIRECT


def test_the_variant_still_sends_the_instruction_text_once_under_the_lossless_layout(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "c1"), instructions=INSTRUCTIONS_DIRECT,
                         layout=RequestLayout.duplicate_state_reference)
    got = asyncio.run(port.decide(context(INSTRUCTIONS_DIRECT)))
    body = send.bodies[0]
    assert got.failure is None and MARKER in body["state"] and "instructions" not in body["state"]
    assert body["state"][MARKER]["instructions_removed"] is True
    assert body["questions"]["next_action"]["instructions"] == INSTRUCTIONS_DIRECT
    assert decode(body)["state"]["instructions"] == INSTRUCTIONS_DIRECT


def test_a_payload_and_an_adapter_with_different_text_are_refused(tmp_path: Path):
    port, send = adapter(tmp_path, reply("next_action", "c1"), instructions=INSTRUCTIONS_DIRECT)
    with pytest.raises(ValueError, match="instructions"):
        asyncio.run(port.decide(context(INSTRUCTIONS)))
    assert send.bodies == []

    plain, plain_send = adapter(tmp_path / "plain", reply("next_action", "c1"))
    with pytest.raises(ValueError, match="instructions"):
        asyncio.run(plain.decide(context(INSTRUCTIONS_DIRECT)))
    assert plain_send.bodies == []


# --- end to end: the loop builds the payload the adapter requires ------------------------------

def test_the_turn_loop_builds_the_selected_variant_and_the_default_is_unchanged():
    async def main():
        chosen = await rig("w", instructions=INSTRUCTIONS_DIRECT)
        try:
            await chosen.turns(1)
            assert chosen.s1.contexts[0]["instructions"] == INSTRUCTIONS_DIRECT
        finally:
            await chosen.close()
        plain = await rig("w")
        try:
            await plain.turns(1)
            assert plain.s1.contexts[0]["instructions"] == INSTRUCTIONS
        finally:
            await plain.close()

    run(main)


# --- the command line names the variant -------------------------------------------------------

def test_the_cli_accepts_both_variants_and_defaults_to_todays_text():
    parser = cli._parser()
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    default = parser.parse_args(required)
    assert default.s1_instructions == INSTRUCTIONS_POLICY_VARIANT
    direct = parser.parse_args([*required, "--s1-instructions", INSTRUCTIONS_DIRECT_VARIANT])
    assert INSTRUCTIONS_VARIANTS[direct.s1_instructions] == INSTRUCTIONS_DIRECT
