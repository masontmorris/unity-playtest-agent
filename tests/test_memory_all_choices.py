"""Outcome memory records every executed choice, not only the options (docs/design/options-layer.md, 5 and 7).

An option files its own measured outcome where it runs. A plain action or skill is executed by the turn loop
instead, and its measured effect is recorded in the same shape, so a place's history covers what actually ran:
an action blocked eleven times at one spot must not keep reading `untried`. Quantities only; unknown stays
unmeasured and is never filed under a guessed cell or a guessed direction.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from types import SimpleNamespace

from catalog_game import CatalogGame
from loop_fakes import TOKEN, FakeS1, Sleeps
from runner_manifest import action, hold, manifest

from playtest.contracts.bridge import ExecStatus, ExecutionRecord
from playtest.contracts.loop import Choice
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import Execution
from playtest.core.ports import EffectReport, Executed
from playtest.core.trace_sink import MemorySink
from playtest.preflight.catalog import export
from playtest.runner.choices import build_choices
from playtest.runner.options.memory import OutcomeMemory
from playtest.runner.options.model import OptionOutcome
from playtest.runner.options.primitives import Direction, Primitives
from playtest.runner.probe import DisplacementProbe, make_snapshot
from playtest.runner.session import choice_outcome, remember_executed, run_session

STEP = 0.47  # one measured short move step: the executor's success margin at one step
SCENE = "scene-a"
OBJECTIVE = "Try each control once, then finish."


# --- fakes and shapes ---------------------------------------------------------------------


def direction(for_action: str = "press_left") -> Direction:
    """One calibrated horizontal direction, produced by one offered action."""
    return Direction(label="+x", action=for_action, control="c", unit=(1.0, 0.0, 0.0), speed=20.0,
                     spread=None, step=STEP, trials=3)


def primitives(*actions: str) -> Primitives:
    return Primitives(directions=tuple(direction(name) for name in actions))


def plain(choice_id: str, kind: str = "action") -> Choice:
    """A manifest action (`kind="action"`) or skill (`kind="skill"`) choice, as `build_choices` makes one."""
    if kind == "skill":
        return Choice(id=choice_id, kind="skill", skill=choice_id, label=choice_id, source="manifest")
    return Choice(id=choice_id, kind="action", action=choice_id, label=choice_id, source="manifest")


def execution(rejected: bool = False) -> Execution:
    record = ExecutionRecord(
        session="s", seq=3, game_s=1.0, wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc), action_id="a",
        status=ExecStatus.rejected if rejected else ExecStatus.completed,
        reject_reason="stale_age" if rejected else None, applied_duration_s=None if rejected else 0.2,
        release_verified=True)
    return Execution(request={}, records=(record,))


def observed(start: list[float] | None = None, end: list[float] | None = None,
             applied: float | None = 0.2) -> dict:
    """The effect probe's observed shape: positions, their signed difference, and the applied hold."""
    if start is None or end is None:
        return {"net_displacement": None, "displacement": None, "from_position": None, "to_position": None,
                "unit": "world units", "from_inspection_seq": 1, "to_inspection_seq": 2,
                "window_game_s": 0.2, "applied_duration_s": applied}
    moved = [b - a for a, b in zip(start, end)]
    return {"net_displacement": math.dist(moved, (0.0,) * len(moved)), "displacement": moved,
            "from_position": list(start), "to_position": list(end), "unit": "world units",
            "from_inspection_seq": 1, "to_inspection_seq": 2, "window_game_s": 0.2,
            "applied_duration_s": applied}


CALIBRATED = {"source": "calibration", "control": "c", "requested_hold_s": 0.2, "net_displacement": 6.04,
              "sample_variance": 0.0001, "valid_trials": 2, "effective_in_all_repeats": True,
              "unit": "world units"}


def ran(choice: Choice, effect: dict | None, *, rejected: bool = False) -> Executed:
    """One executed choice, with the effect the turn loop measured for it (or none)."""
    report = None if effect is None else EffectReport(expected=CALIBRATED, observed=effect)
    return Executed(purpose="choice", choice=choice, execution=execution(rejected), effect=report)


def recorded(choice: Choice, effect: dict | None, *, calibrated: Primitives | None = None, **kwargs):
    return choice_outcome(choice, ran(choice, effect, **kwargs),
                          primitives=primitives(choice.id) if calibrated is None else calibrated,
                          scene=SCENE)


# --- what an executed action or skill records ---------------------------------------------


def test_an_action_that_moved_nothing_records_blocked():
    """A measured zero displacement against a calibrated expectation is a blocked try, never untried.

    The ending is the option rule's: nothing got past the executor's success margin. Where the direction was
    measured the record also carries it, so the label keeps the same `best` an option's label carries.
    """
    choice = plain("press_left")
    outcome = recorded(choice, observed([1.0, 2.0, 0.0], [1.0, 2.0, 0.0]))
    assert outcome.terminated_by == "blocked"
    assert (outcome.start_position, outcome.end_position) == ([1.0, 2.0, 0.0], [1.0, 2.0, 0.0])
    assert outcome.displacement == [0.0, 0.0, 0.0] and outcome.duration_s == 0.2
    assert outcome.template == "action" and outcome.scene == SCENE
    assert outcome.params["direction"] == [1.0, 0.0, 0.0]
    memory = OutcomeMemory(cell=STEP)
    memory.record(outcome)
    assert memory.suffix(SCENE, [1.0, 2.0, 0.0], "press_left") == (
        "here: 1 tried \u2014 blocked 1, best +0.0")


def test_an_uncalibrated_direction_reads_the_same_counts_without_a_best():
    """A choice calibration gave no direction for still counts its blocked tries, and claims no best."""
    choice = plain("press_left")
    prims = primitives("some_other_action")  # nothing measured points this choice's way
    outcome = choice_outcome(choice, ran(choice, observed([1.0, 2.0, 0.0], [1.0, 2.0, 0.0])),
                             primitives=prims, scene=SCENE)
    assert outcome.terminated_by == "blocked" and outcome.params == {}
    memory = OutcomeMemory(cell=STEP)
    memory.record(outcome)
    assert memory.suffix(SCENE, [1.0, 2.0, 0.0], "press_left") == "here: 1 tried \u2014 blocked 1"


def test_a_blocked_action_accumulates_tries_at_the_one_cell():
    """Eleven tries at one spot read as eleven tries at that spot, and only there."""
    choice = plain("press_left")
    memory = OutcomeMemory(cell=STEP)
    for _ in range(11):
        memory.record(recorded(choice, observed([1.0, 2.0, 0.0], [1.0, 2.0, 0.0])))
    assert memory.suffix(SCENE, [1.05, 2.05, 0.0], "press_left") == (
        "here: 11 tried \u2014 blocked 11, best +0.0")
    assert memory.suffix(SCENE, [1.0, 2.0 + 5 * STEP, 0.0], "press_left") == "here: untried"
    assert memory.suffix("scene-b", [1.0, 2.0, 0.0], "press_left") == "here: untried"


def test_an_action_measured_past_its_margin_records_succeeded():
    """Moving at least one measured step along the calibrated direction is a success, not a guess."""
    choice = plain("press_left")
    outcome = recorded(choice, observed([0.0, 0.0, 0.0], [3.0, 0.0, 0.0]))
    assert outcome.terminated_by == "succeeded"
    memory = OutcomeMemory(cell=STEP)
    memory.record(outcome)
    assert memory.suffix(SCENE, [0.0, 0.0, 0.0], "press_left") == (
        "here: 1 tried \u2014 succeeded 1, best +3.0")


def test_a_rejected_input_records_rejected():
    """An input the bridge refused never ran, and the record says so rather than calling it blocked."""
    assert recorded(plain("press_left"), observed(), rejected=True).terminated_by == "rejected"


def test_an_unmeasured_effect_records_unmeasured_and_is_never_placed():
    """No measured effect means an unplaced unmeasured try: no guessed cell, so the place stays untried."""
    choice = plain("press_left")
    for effect in (observed(), None):
        outcome = recorded(choice, effect)
        assert outcome.terminated_by == "unmeasured"
        assert (outcome.start_position, outcome.end_position, outcome.displacement) == (None, None, None)
        memory = OutcomeMemory(cell=STEP)
        memory.record(outcome)
        assert memory.suffix(SCENE, [1.0, 2.0, 0.0], "press_left") == "here: untried"
        assert memory.unplaced[(SCENE, "press_left")] == {"unmeasured": 1}


def test_a_skill_without_a_calibrated_direction_reads_from_its_magnitude_and_claims_no_best():
    """A skill has no measured direction: its distance says succeeded or blocked, and nothing says which way."""
    choice = plain("combo", "skill")
    prims = primitives("press_left")  # the skill is not one of the calibrated directions
    far = choice_outcome(choice, ran(choice, observed([0.0, 0.0, 0.0], [0.9, 0.0, 0.0])),
                         primitives=prims, scene=SCENE)
    near = choice_outcome(choice, ran(choice, observed([0.0, 0.0, 0.0], [0.1, 0.0, 0.0])),
                          primitives=prims, scene=SCENE)
    assert (far.template, near.template) == ("skill", "skill")
    assert (far.terminated_by, near.terminated_by) == ("succeeded", "blocked")
    assert "direction" not in far.params and "direction" not in near.params
    memory = OutcomeMemory(cell=STEP)
    memory.record(far)
    memory.record(near)
    assert memory.suffix(SCENE, [0.0, 0.0, 0.0], "combo") == (
        "here: 2 tried \u2014 succeeded 1, blocked 1")


def test_an_unmeasured_margin_records_unmeasured_rather_than_zero():
    """With no measured step and no measured speed there is no margin, so nothing is called blocked."""
    choice = plain("combo", "skill")
    outcome = choice_outcome(choice, ran(choice, observed([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])),
                             primitives=Primitives(), scene=SCENE)
    assert outcome.terminated_by == "unmeasured"


# --- beside the options -------------------------------------------------------------------


def test_cell_line_lists_a_plain_action_beside_an_option_and_option_recording_is_unchanged():
    """The place fact covers every choice that ran there; an option's own record and rendering do not change."""
    option = OptionOutcome(option_id="traverse+x", template="traverse", params={"direction": [1.0, 0.0, 0.0]},
                           start_position=[1.0, 2.0, 0.0], end_position=[1.0, 2.0, 0.0],
                           displacement=[0.0, 0.0, 0.0], terminated_by="blocked", duration_s=0.5, scene=SCENE)
    memory = OutcomeMemory(cell=STEP)
    memory.record(option)
    memory.record(recorded(plain("press_left"), observed([1.0, 2.0, 0.0], [1.0, 2.0, 0.0])))
    assert memory.suffix(SCENE, [1.0, 2.0, 0.0], "traverse+x") == (
        "here: 1 tried \u2014 blocked 1, best +0.0")
    assert memory.cell_line(SCENE, [1.0, 2.0, 0.0]) == (
        "This place (cell (2, 4, 0)): press_left 1 tried, blocked 1; traverse+x 1 tried, blocked 1.")


def test_an_option_choice_is_never_recorded_here():
    """Options file their own outcome where they run, so the hook leaves them alone."""
    option = Choice(id="traverse+x", kind="option", label="traverse", option_id="traverse+x",
                    template="traverse", params={"direction": [1.0, 0.0, 0.0]}, source="manifest")
    memory = OutcomeMemory(cell=STEP)
    report = SimpleNamespace(observation=SimpleNamespace(scene=SCENE),
                             executed=(ran(option, observed([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])),))
    remember_executed(memory, primitives("press_left"), report)
    assert memory.suffix(SCENE, [0.0, 0.0, 0.0], "traverse+x") == "here: untried"


# --- the session hook ---------------------------------------------------------------------


def two_actions():
    """`move` and `look`, each a proven single-key declaration held for its manifest lease."""
    return manifest(
        action("move", "w", hold_s=0.2, holds=[hold(0.2)]),
        action("look", "a", hold_s=0.25, holds=[hold(0.25, mean=0.0, effective=False)]))


def test_a_session_records_what_the_actions_it_executed_did():
    """The driver files each executed action's measured effect, in the cell it started from."""

    async def main():
        value = two_actions()
        game = CatalogGame(token=TOKEN, catalog=export(value), effects={"move": (6.0, 0.0, 0.0)})
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        try:
            built = build_choices(value)
            prims = Primitives(directions=(
                Direction(label="+x", action="move", control="w", unit=(1.0, 0.0, 0.0), speed=30.0,
                          spread=None, step=STEP, trials=2),))
            memory = OutcomeMemory(cell=STEP)
            sink = MemorySink()
            probe = DisplacementProbe(make_snapshot(client, "entity"), built.expected)
            result = await run_session(
                client, built, s1=FakeS1("move", "look", "finish"), objective=OBJECTIVE, probe=probe,
                sink=sink, sleep=Sleeps(), max_age_s=2.0, wall_limit_s=60.0,
                option_primitives=prims, option_memory=memory)
            assert (result.status, result.ending) == ("complete", "finish")
            scenes = {entry["turn"]: entry["body"]["scene"] for entry in sink.of_kind("observation")}
            effects = {entry["body"]["choice_id"]: entry for entry in sink.of_kind("effect")}
            move, look = effects["move"], effects["look"]
            moved = move["body"]["observed"]["from_position"]
            stopped = look["body"]["observed"]["from_position"]
            assert memory.suffix(scenes[move["turn"]], moved, "move") == (
                "here: 1 tried \u2014 succeeded 1, best +6.0")
            assert memory.suffix(scenes[look["turn"]], stopped, "look") == (
                "here: 1 tried \u2014 blocked 1")
            assert "move 1 tried" in (memory.cell_line(scenes[move["turn"]], moved) or "")
            assert "look 1 tried" in (memory.cell_line(scenes[look["turn"]], stopped) or "")
            assert memory.unplaced == {}  # both measured effects were placed, none went astray
        finally:
            await client.aclose()
            await game.stop()

    asyncio.run(main())
