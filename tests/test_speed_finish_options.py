"""The S1 opt-ins added together: a pacing burst allowance, the conditional finish label, `policy/2`, `policy/3`.

Every default must pace and read exactly as before; each option changes one thing and nothing else. The
pacing tests run on a fake clock and sleep, and the command-line plan test stubs the run itself, so
nothing here waits, opens a bridge or calls a provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import FakeTime, choices
from runner_manifest import action, hold, manifest
from s1_fakes import ARM, Send, approval, reply

from playtest.bench import call_budget
from playtest.bench.record import digest
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.core.context import (
    INSTRUCTIONS,
    INSTRUCTIONS_FINISH_ONLY_WHEN_MET,
    INSTRUCTIONS_FINISH_VARIANT,
    INSTRUCTIONS_KEEP_PLAYING,
    INSTRUCTIONS_PENDING_VARIANT,
    INSTRUCTIONS_POLICY_VARIANT,
    INSTRUCTIONS_VARIANTS,
    KEEP_PLAYING,
    build_context,
    canonical,
)
from playtest.preflight import catalog as exporter
from playtest.runner import cli
from playtest.runner.choices import FINISH_LABELS, build_choices, core_choices
from playtest.runner.pacing import TokenBudget
from playtest.runner.s1 import (
    FOLLOW_UP_INSTRUCTIONS,
    FOLLOW_UP_KEEP_PLAYING,
    SystemOneS1,
    follow_up_for,
    prompts,
)

# --- pacing burst -------------------------------------------------------------------------


def budget(limit: float = 60_000, **kwargs: Any) -> tuple[TokenBudget, FakeTime]:
    fake = FakeTime()
    return TokenBudget(limit, clock=fake.clock, sleep=fake.sleep, **kwargs), fake


def ready(b: TokenBudget) -> float:
    return asyncio.run(b.ready())


def test_a_zero_burst_spaces_every_call_after_the_first_exactly_as_unset_does():
    """`burst=0.0` is the old behaviour: with `spread`, every call after the first waits its own rate."""
    results = []
    for kwargs in ({}, {"burst": 0.0}):
        b, fake = budget(spread=True, **kwargs)          # 1,000 tokens a second
        waits = [ready(b)]                               # nothing counted: no wait
        b.record(1_000)
        waits.append(ready(b))
        fake.now += 2.5                                  # the turn itself outlasts the spacing
        b.record(1_000)
        waits.append(ready(b))
        b.record(1_000)
        waits.append(ready(b))
        assert b.burst == 0.0
        results.append([round(wait, 6) for wait in waits])
    assert results[0] == results[1] == [0.0, 1.0, 1.0, 1.0]


def test_a_half_burst_lets_calls_go_while_window_and_expected_stay_under_half_the_limit():
    b, fake = budget(spread=True, burst=0.5)             # half of 60,000 is 30,000
    assert ready(b) == 0.0                               # an empty window never waits
    b.record(14_000)                                     # 14,000 + an expected 14,000 = 28,000
    assert ready(b) == 0.0
    b.record(2_000)                                      # 16,000 + an expected 2,000 = 18,000
    assert ready(b) == 0.0
    assert fake.slept == []


def test_spacing_returns_once_window_and_expected_pass_the_burst_share():
    b, fake = budget(spread=True, burst=0.5)
    b.record(14_000)
    b.record(2_000)                                      # 16,000 held; expected 2,000 is still under 30,000
    assert ready(b) == 0.0
    b.record(14_000)                                     # 30,000 held; expected 14,000 is over 30,000
    assert ready(b) == pytest.approx(14.0)               # 14,000 tokens at 1,000 a second
    assert fake.slept == [pytest.approx(14.0)]


@pytest.mark.parametrize("burst", [0.0, 0.25, 0.5, 1.0])
def test_the_window_rule_still_holds_at_any_burst(burst: float):
    b, fake = budget(spread=True, burst=burst)
    assert b.in_window == 0
    for _ in range(40):
        ready(b)                                         # a wait is a delay only; it never spends
        b.record(7_000)
        assert b.in_window <= b.limit
        fake.now += 0.1


def test_a_full_burst_is_a_short_session_that_never_waits_until_the_limit_is_near():
    b, fake = budget(spread=True, burst=1.0)
    for _ in range(4):
        assert ready(b) == 0.0                           # 8,000, 15,000, 22,000 and 29,000 held are all fine
        b.record(7_000)
    assert fake.slept == []


def test_a_burst_outside_zero_to_one_or_not_a_finite_number_is_refused():
    for bad in (-0.1, 1.5, float("nan"), float("inf"), True, False, "0.5", None):
        with pytest.raises(ValueError):
            TokenBudget(1_000, burst=bad)
    assert TokenBudget(1_000, burst=0).burst == 0.0
    assert TokenBudget(1_000, burst=1).burst == 1.0


def test_the_burst_property_reports_the_share_it_was_given():
    assert TokenBudget(1_000).burst == 0.0
    assert TokenBudget(1_000, spread=True, burst=0.5).burst == 0.5
    assert TokenBudget(1_000, burst=0.25).burst == 0.25


# --- the finish label ---------------------------------------------------------------------


def one_action() -> Any:
    return manifest(action("move", "w", hold_s=0.2, holds=[hold(0.2)]))


def finish_label(built_choices: list) -> str:
    return next(choice.label for choice in built_choices if choice.id == "finish")


def test_the_finish_label_is_finish_by_default_in_both_builders():
    assert FINISH_LABELS["plain"] == "Finish"
    assert finish_label(core_choices()) == "Finish"
    assert finish_label(build_choices(one_action()).choices) == "Finish"


def test_the_conditional_finish_label_reaches_the_finish_choice():
    label = FINISH_LABELS["conditional"]
    assert finish_label(core_choices(finish_label=label)) == label
    assert finish_label(build_choices(one_action(), finish_label=label).choices) == label
    assert label.startswith("Finish: end the whole session now.")
    assert "completion conditions are met; otherwise the session ends by itself." in label


def test_the_finish_label_changes_no_other_choice():
    plain = build_choices(one_action())
    conditional = build_choices(one_action(), finish_label=FINISH_LABELS["conditional"])

    def others(built) -> list[tuple[str, Any, str]]:
        return [(c.id, c.kind, c.label) for c in built.choices if c.id != "finish"]

    assert others(plain) == others(conditional)
    assert [c.id for c in plain.choices] == [c.id for c in conditional.choices]
    assert plain.not_offered == conditional.not_offered
    assert plain.leases == conditional.leases


def test_the_finish_choice_can_be_left_off_the_menu_alone():
    assert [c.id for c in core_choices(offer_finish=False)] == ["wait", "escalate", "delegate"]
    normal = build_choices(one_action())
    dropped = build_choices(one_action(), offer_finish=False)
    assert "finish" in [c.id for c in normal.choices]
    assert "finish" not in [c.id for c in dropped.choices]
    assert dropped.choices == [c for c in normal.choices if c.id != "finish"]
    assert dropped.expected == normal.expected and dropped.leases == normal.leases
    assert dropped.not_offered == normal.not_offered


def test_a_declaration_named_finish_collides_even_when_the_choice_is_not_offered():
    with pytest.raises(ValueError, match="collides with the core choice"):
        build_choices(manifest(action("finish", "w", hold_s=0.2, holds=[hold(0.2)])), offer_finish=False)


# --- the `policy/2` instruction text ------------------------------------------------------


FINISH_OLD = ("Finish when the objective's completion conditions are met or you judge no useful attempt "
              "remains.")
FINISH_NEW = ("Finish only when the objective's completion conditions are met; otherwise keep playing until "
              "the session ends by itself.")


def test_policy_two_swaps_only_the_finish_sentence():
    variant = INSTRUCTIONS_VARIANTS[INSTRUCTIONS_FINISH_VARIANT]
    assert variant == INSTRUCTIONS_FINISH_ONLY_WHEN_MET
    assert INSTRUCTIONS.count(FINISH_OLD) == 1
    assert variant.count(FINISH_NEW) == 1
    assert FINISH_OLD not in variant

    before = INSTRUCTIONS[:INSTRUCTIONS.index(FINISH_OLD)]
    after = INSTRUCTIONS[INSTRUCTIONS.index(FINISH_OLD) + len(FINISH_OLD):]
    assert before and after                               # a real sentence in the middle, not the whole text
    assert variant == before + FINISH_NEW + after


def test_policy_one_is_still_todays_text_object_for_object():
    assert INSTRUCTIONS_VARIANTS[INSTRUCTIONS_POLICY_VARIANT] is INSTRUCTIONS
    assert INSTRUCTIONS_VARIANTS["policy/1"] is INSTRUCTIONS
    assert INSTRUCTIONS_VARIANTS["policy/2"] is not INSTRUCTIONS


def test_the_new_finish_sentence_brings_in_no_game_word():
    from test_name_blind import BANNED, words

    assert not (words(FINISH_NEW) & BANNED)


# --- the command line ---------------------------------------------------------------------

REQUIRED = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]


def parsed(*extra: str):
    return cli._parser().parse_args([*REQUIRED, *extra])


def test_the_pacing_burst_defaults_to_zero_takes_a_share_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("UPA_RUN_PACING_BURST", raising=False)
    assert parsed().pacing_burst == 0.0
    assert parsed("--pacing-burst", "0.5").pacing_burst == 0.5
    monkeypatch.setenv("UPA_RUN_PACING_BURST", "0.25")
    assert parsed().pacing_burst == 0.25
    monkeypatch.setenv("UPA_RUN_PACING_BURST", "not-a-number")
    with pytest.raises(SystemExit):
        parsed()


def test_the_pacing_anchor_defaults_to_end_takes_send_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("UPA_RUN_PACING_ANCHOR", raising=False)
    assert parsed().pacing_anchor == "end"
    assert parsed("--pacing-anchor", "send").pacing_anchor == "send"
    assert parsed("--pacing-anchor", "end").pacing_anchor == "end"
    monkeypatch.setenv("UPA_RUN_PACING_ANCHOR", "send")
    assert parsed().pacing_anchor == "send"
    for bad in ("middle", "Send", "send "):
        with pytest.raises(SystemExit):
            parsed("--pacing-anchor", bad)


def test_the_finish_label_defaults_to_plain_takes_conditional_and_refuses_the_rest(monkeypatch):
    monkeypatch.delenv("UPA_RUN_FINISH_LABEL", raising=False)
    assert parsed().finish_label == "plain"
    assert parsed("--finish-label", "conditional").finish_label == "conditional"
    monkeypatch.setenv("UPA_RUN_FINISH_LABEL", "conditional")
    assert parsed().finish_label == "conditional"
    for bad in ("pretty", "Conditional", "plain "):
        with pytest.raises(SystemExit):
            parsed("--finish-label", bad)


def test_no_finish_is_off_by_default_takes_the_switch_and_reads_the_environment(monkeypatch):
    monkeypatch.delenv("UPA_RUN_NO_FINISH", raising=False)
    assert parsed().no_finish is False
    assert parsed("--no-finish").no_finish is True
    monkeypatch.setenv("UPA_RUN_NO_FINISH", "1")
    assert parsed().no_finish is True
    monkeypatch.setenv("UPA_RUN_NO_FINISH", "0")
    assert parsed().no_finish is False
    monkeypatch.setenv("UPA_RUN_NO_FINISH", "yes")
    with pytest.raises(SystemExit):
        parsed()


def test_the_cli_accepts_the_policy_two_instructions():
    assert parsed("--s1-instructions", "policy/2").s1_instructions == "policy/2"
    assert INSTRUCTIONS_VARIANTS["policy/2"] == INSTRUCTIONS_FINISH_ONLY_WHEN_MET
    with pytest.raises(SystemExit):
        parsed("--s1-instructions", "policy/99")


class PlanFiles:
    """Every input `main` reads before it writes the plan, but no bridge and no provider."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.project = root / "game"
        self.project.mkdir()
        self.manifest = root / "manifest.json"
        self.manifest.write_text(one_action().model_dump_json(), encoding="utf-8")
        self.catalog = root / "catalog.json"
        assert exporter.main(["--manifest", str(self.manifest), "--output", str(self.catalog)]) == 0
        self.catalog_sha = hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        self.entity = root / "entity.json"
        self.entity.write_text(json.dumps({"status": "identified", "entity_id": "entity"}), encoding="utf-8")
        self.calibration = root / "calibration.json"
        self.calibration.write_text(json.dumps({"entity_id": "entity"}), encoding="utf-8")
        self.approval = root / "approval.json"
        self.approval.write_text(json.dumps(approval(12)), encoding="utf-8")
        self.out = root / "out"
        self.summary = self.out / "run.json"

    def args(self, *extra: str) -> list[str]:
        return ["--project", str(self.project), "--entity", str(self.entity),
                "--calibration", str(self.calibration), "--output", str(self.summary),
                "--manifest", str(self.manifest), "--catalog", str(self.catalog),
                "--catalog-sha256", self.catalog_sha, "--approval", str(self.approval),
                "--max-calls", "6", "--wall-limit-s", "60", "--max-age-s", "2.0",
                "--objective", "Try the control once.", "--s1-arm", ARM, *extra]


@pytest.fixture
def plan_run(tmp_path: Path, monkeypatch):
    """Run `main` to the plan file with the session itself stubbed out: no bridge, no provider."""
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-plan-key-0001")
    for name in ("UPA_SESSION_CONTROL_DIRECTORY", "UPA_SESSION_CATALOG", "UPA_SESSION_CATALOG_SHA256",
                 "UPA_RUN_PACING_BURST", "UPA_RUN_PACING_ANCHOR", "UPA_RUN_FINISH_LABEL", "UPA_RUN_NO_FINISH",
                 "UPA_RUN_S1_INSTRUCTIONS", "UPA_RUN_TOKENS_PER_MINUTE"):
        monkeypatch.delenv(name, raising=False)

    async def stub(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "complete", "turns": 1, "ending": "finish"}

    monkeypatch.setattr(cli, "_run", stub)
    counter = {"n": 0}

    def run(*extra: str) -> dict[str, Any]:
        counter["n"] += 1
        files = PlanFiles(tmp_path / f"case{counter['n']}")
        assert cli.main(files.args(*extra)) == 0
        return json.loads((files.out / "plan.json").read_text(encoding="utf-8"))

    return run


def test_the_plan_gains_pacing_burst_and_finish_label_only_when_non_default(plan_run):
    default = plan_run()
    assert "pacing_burst" not in default and "finish_label" not in default
    assert "s1_instructions" not in default
    chosen = plan_run("--pacing-burst", "0.5", "--finish-label", "conditional")
    assert chosen["pacing_burst"] == 0.5
    assert chosen["finish_label"] == "conditional"


def test_the_plan_gains_no_finish_only_when_set_and_leaves_the_finish_choice_out(plan_run, monkeypatch):
    default = plan_run()
    assert "no_finish" not in default and "finish" in default["offered"]
    chosen = plan_run("--no-finish")
    assert chosen["no_finish"] is True and "finish" not in chosen["offered"]
    # The switch changes no other offered choice, and no other plan field.
    assert [c for c in chosen["offered"] if c != "finish"] == [c for c in default["offered"] if c != "finish"]
    assert {k: v for k, v in chosen.items() if k not in ("offered", "no_finish")} == \
           {k: v for k, v in default.items() if k != "offered"}
    monkeypatch.setenv("UPA_RUN_NO_FINISH", "1")        # the wrapper's own route reaches the same plan
    from_env = plan_run()
    assert from_env["no_finish"] is True and "finish" not in from_env["offered"]


def test_the_plan_gains_the_pacing_anchor_only_with_send(plan_run):
    default = plan_run()
    assert "pacing_anchor" not in default
    assert plan_run("--pacing-anchor", "end").get("pacing_anchor") is None
    assert plan_run("--pacing-anchor", "send")["pacing_anchor"] == "send"


def test_a_pacing_anchor_that_is_neither_end_nor_send_is_refused_before_the_plan_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-plan-key-0003")
    monkeypatch.delenv("UPA_SESSION_CONTROL_DIRECTORY", raising=False)
    files = PlanFiles(tmp_path / "case")
    for bad in ("middle", "Send", "send "):
        with pytest.raises(SystemExit) as raised:
            cli.main(files.args("--pacing-anchor", bad))
        assert raised.value.code == 2
    monkeypatch.setenv("UPA_RUN_PACING_ANCHOR", "middle")
    with pytest.raises(SystemExit) as raised:
        cli.main(files.args())
    assert raised.value.code == 2
    assert not (files.out / "plan.json").exists()


def test_the_plan_records_the_policy_two_variant_only_when_named(plan_run):
    plan = plan_run("--s1-instructions", "policy/2")
    assert plan["s1_instructions"] == "policy/2"


def test_a_burst_outside_zero_to_one_is_refused_before_the_plan_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-plan-key-0002")
    monkeypatch.delenv("UPA_SESSION_CONTROL_DIRECTORY", raising=False)
    files = PlanFiles(tmp_path / "case")
    for bad in ("1.5", "-0.1", "nan"):
        with pytest.raises(SystemExit) as raised:
            cli.main(files.args("--pacing-burst", bad))
        assert raised.value.code == 2
    assert not (files.out / "plan.json").exists()


# --- the `policy/3` pending-help variant ---------------------------------------------------


PENDING_SENTENCE = "On an escalate or delegate turn, also say what to do while help is pending."


def test_policy_three_inserts_one_sentence_after_the_pending_one():
    """`policy/3` is `policy/2` plus `KEEP_PLAYING`, one space after the pending-help sentence."""
    variant = INSTRUCTIONS_VARIANTS[INSTRUCTIONS_PENDING_VARIANT]
    assert INSTRUCTIONS_PENDING_VARIANT == "policy/3"
    assert variant == INSTRUCTIONS_KEEP_PLAYING and variant != INSTRUCTIONS_FINISH_ONLY_WHEN_MET
    assert INSTRUCTIONS_FINISH_ONLY_WHEN_MET.count(PENDING_SENTENCE) == 1
    assert variant.count(f"{PENDING_SENTENCE} {KEEP_PLAYING}") == 1
    # Taking exactly the added sentence out gives `policy/2` back, so no other character moved.
    assert variant.replace(f"{PENDING_SENTENCE} {KEEP_PLAYING}",
                           PENDING_SENTENCE) == INSTRUCTIONS_FINISH_ONLY_WHEN_MET
    assert variant.count(KEEP_PLAYING) == 1
    assert KEEP_PLAYING.startswith("While help is pending, keep playing:")
    assert "Choose wait only when continuing would spoil what you asked about." in variant


def test_policy_one_and_two_keep_their_text_object_for_object():
    assert INSTRUCTIONS_VARIANTS["policy/1"] is INSTRUCTIONS
    assert INSTRUCTIONS_VARIANTS["policy/2"] is INSTRUCTIONS_FINISH_ONLY_WHEN_MET
    assert KEEP_PLAYING not in INSTRUCTIONS and KEEP_PLAYING not in INSTRUCTIONS_FINISH_ONLY_WHEN_MET
    assert sorted(INSTRUCTIONS_VARIANTS) == ["direct/1", "policy/1", "policy/2", "policy/3"]


def test_the_keep_playing_sentence_brings_in_no_game_word():
    from test_name_blind import BANNED, words

    assert not (words(KEEP_PLAYING) & BANNED)
    assert "keep playing" in KEEP_PLAYING


def test_the_follow_up_wording_follows_the_primary_text():
    assert follow_up_for(INSTRUCTIONS_KEEP_PLAYING) is FOLLOW_UP_KEEP_PLAYING
    assert follow_up_for(INSTRUCTIONS) is FOLLOW_UP_INSTRUCTIONS
    assert follow_up_for(INSTRUCTIONS_FINISH_ONLY_WHEN_MET) is FOLLOW_UP_INSTRUCTIONS
    assert FOLLOW_UP_KEEP_PLAYING != FOLLOW_UP_INSTRUCTIONS
    assert prompts(INSTRUCTIONS_KEEP_PLAYING) == {"next_action": INSTRUCTIONS_KEEP_PLAYING,
                                                  "while_pending": FOLLOW_UP_KEEP_PLAYING}
    assert prompts(INSTRUCTIONS_KEEP_PLAYING)["while_pending"] is FOLLOW_UP_KEEP_PLAYING
    assert prompts()["while_pending"] is FOLLOW_UP_INSTRUCTIONS


def observation() -> Observation:
    return Observation(session="s", seq=7, game_s=3.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                       clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0,
                       frame=9)


def payload(instructions: str = INSTRUCTIONS) -> dict[str, Any]:
    return build_context(objective="Exercise the controls.", observation=observation(),
                         choices=ChoiceSet(set_version=4, built_from_seq=7, choices=choices()),
                         instructions=instructions)


def s1_adapter(tmp_path: Path, *replies: Any, instructions: str = INSTRUCTIONS) -> tuple[SystemOneS1, Send]:
    ledger = call_budget.Ledger(tmp_path, approval(12), ARM)
    send = Send(tmp_path, *replies)
    return SystemOneS1(ledger, max_calls=10, send=send, arm=ARM, instructions=instructions), send


def decide(port: SystemOneS1, context: dict[str, Any]):
    return asyncio.run(port.decide(context))


def test_the_keep_playing_adapter_hashes_a_different_prompt(tmp_path: Path):
    plain, _ = s1_adapter(tmp_path / "plain", reply("next_action", "c1"))
    variant, _ = s1_adapter(tmp_path / "variant", reply("next_action", "c1"),
                            instructions=INSTRUCTIONS_KEEP_PLAYING)
    assert plain.prompt_sha256 == digest(canonical(prompts()))
    assert variant.prompt_sha256 == digest(canonical(prompts(INSTRUCTIONS_KEEP_PLAYING)))
    assert variant.prompt_sha256 != plain.prompt_sha256
    assert variant.instructions == INSTRUCTIONS_KEEP_PLAYING


@pytest.mark.parametrize("instructions, expected", [
    (INSTRUCTIONS, FOLLOW_UP_INSTRUCTIONS),
    (INSTRUCTIONS_KEEP_PLAYING, FOLLOW_UP_KEEP_PLAYING),
])
def test_a_help_turn_asks_the_follow_up_that_matches_the_primary_text(tmp_path: Path, instructions, expected):
    port, send = s1_adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "c2"),
                            instructions=instructions)
    context = payload(instructions)
    got = decide(port, context)
    assert len(send.bodies) == 2
    follow = send.bodies[1]
    assert set(follow) == {"model", "questions", "state"} and follow["state"] == context
    assert list(follow["questions"]) == ["while_pending"]
    question = follow["questions"]["while_pending"]
    assert question["type"] == "choice" and question["instructions"] == expected
    # Criteria are the wait choice and the executable choices; help and finish are not continuations.
    assert list(question["criteria"]) == ["c1", "c2", "c3", "w"]
    assert got.answer["choice_id"] == "h" and got.answer["while_pending"] == {"mode": "continue", "choice_id": "c2"}
    assert all(c.problem is None for c in got.calls)


def test_the_keep_playing_wording_still_maps_a_wait_to_wait(tmp_path: Path):
    port, _ = s1_adapter(tmp_path, reply("next_action", "h"), reply("while_pending", "w"),
                         instructions=INSTRUCTIONS_KEEP_PLAYING)
    got = decide(port, payload(INSTRUCTIONS_KEEP_PLAYING))
    assert got.answer["choice_id"] == "h" and got.answer["while_pending"] == {"mode": "wait"}


def test_a_failed_keep_playing_follow_up_leaves_the_primary_standing(tmp_path: Path):
    unoffered = (200, {"answers": {"while_pending": {"type": "choice", "choice": "zz"}}}, 9.0, None)
    for index, bad in enumerate(((500, None, 5.0, "HTTP error"), (None, None, 5.0, "transport error"), unoffered,
                                 (200, {"answers": {}}, 5.0, None))):
        port, _ = s1_adapter(tmp_path / f"bad{index}", reply("next_action", "h"), bad,
                             instructions=INSTRUCTIONS_KEEP_PLAYING)
        got = decide(port, payload(INSTRUCTIONS_KEEP_PLAYING))
        assert got.answer["choice_id"] == "h" and "while_pending" not in got.answer and got.failure is None
        assert len(got.calls) == 2 and got.calls[1].question == "while_pending" and got.calls[1].problem


def test_the_cli_accepts_the_policy_three_instructions():
    assert parsed("--s1-instructions", "policy/3").s1_instructions == "policy/3"
    assert INSTRUCTIONS_VARIANTS["policy/3"] == INSTRUCTIONS_KEEP_PLAYING
    for bad in ("policy/99", "policy/4", "policy/3 "):
        with pytest.raises(SystemExit):
            parsed("--s1-instructions", bad)


def test_the_plan_records_the_policy_three_variant_when_named(plan_run):
    assert plan_run("--s1-instructions", "policy/3")["s1_instructions"] == "policy/3"
