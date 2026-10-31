"""The three S1 opt-ins added together: a pacing burst allowance, the conditional finish label, `policy/2`.

Every default must pace and read exactly as before; each option changes one thing and nothing else. The
pacing tests run on a fake clock and sleep, and the command-line plan test stubs the run itself, so
nothing here waits, opens a bridge or calls a provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import FakeTime
from runner_manifest import action, hold, manifest
from s1_fakes import ARM, approval

from playtest.core.context import (
    INSTRUCTIONS,
    INSTRUCTIONS_FINISH_ONLY_WHEN_MET,
    INSTRUCTIONS_FINISH_VARIANT,
    INSTRUCTIONS_POLICY_VARIANT,
    INSTRUCTIONS_VARIANTS,
)
from playtest.preflight import catalog as exporter
from playtest.runner import cli
from playtest.runner.choices import FINISH_LABELS, build_choices, core_choices
from playtest.runner.pacing import TokenBudget

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


def test_the_finish_label_defaults_to_plain_takes_conditional_and_refuses_the_rest(monkeypatch):
    monkeypatch.delenv("UPA_RUN_FINISH_LABEL", raising=False)
    assert parsed().finish_label == "plain"
    assert parsed("--finish-label", "conditional").finish_label == "conditional"
    monkeypatch.setenv("UPA_RUN_FINISH_LABEL", "conditional")
    assert parsed().finish_label == "conditional"
    for bad in ("pretty", "Conditional", "plain "):
        with pytest.raises(SystemExit):
            parsed("--finish-label", bad)


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
                 "UPA_RUN_PACING_BURST", "UPA_RUN_FINISH_LABEL", "UPA_RUN_S1_INSTRUCTIONS",
                 "UPA_RUN_TOKENS_PER_MINUTE"):
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
