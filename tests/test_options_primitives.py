"""Primitives: measured per-second vectors, directions, and the rise's duration and peak.

The synthetic cases are the fake side-view world's own calibration; the last test reads the real
DragonBlaze calibration record and prints what the classifier found.
"""

from __future__ import annotations

import json
from pathlib import Path

from options_fake_world import catalog, calibration
from playtest.runner.options.model import OptionSpec
from playtest.runner.options.primitives import (
    KIND_OTHER,
    Primitives,
    option_specs,
    primitives_from_calibration,
)

REPO = Path(__file__).resolve().parents[1]
RECORD = REPO / "runs" / "dragonblaze-upa" / "calibration-07" / "05" / "record.json"
MANIFEST = REPO / "runs" / "dragonblaze-upa" / "manifest-02-probed.json"


def world_primitives() -> Primitives:
    record = calibration()
    return primitives_from_calibration(
        record, ["move_left", "move_right", "rise_up", "chord_right", "chord_left"],
        catalog=catalog(),
    )


def test_measured_speed_direction_and_step():
    primitives = world_primitives()
    labels = [item.label for item in primitives.directions]
    assert labels == ["+x", "-x"]

    right = primitives.direction("+x")
    assert right is not None and right.action == "move_right" and right.control == "right"
    assert right.speed == 4.0
    assert right.step == 0.4                    # the shortest calibrated hold's displacement
    assert right.spread is not None and right.spread < 1e-9

    left = primitives.direction("-x")
    assert left is not None and left.speed == 4.0 and left.unit == (-1.0, 0.0, 0.0)


def test_rise_duration_and_peak_are_measured():
    primitives = world_primitives()
    assert primitives.rise is not None and primitives.rise.action == "rise_up"
    assert primitives.rise_duration_s == 1.0    # airtime: the hold whose rise returns to zero
    # Peak is the largest +y displacement a sampled hold measured, a lower bound of the true apex.
    assert abs(primitives.rise_peak - 1.44) < 1e-9
    assert primitives.run_up() == 4.0           # measured speed times measured rise time


def test_chords_come_from_the_catalog_and_enable_hop():
    primitives = world_primitives()
    assert primitives.chord_for("move_right") == "chord_right"
    assert primitives.chord_for("move_left") == "chord_left"
    assert primitives.can_hop

    # Without the catalog nothing says one declared action presses two controls at once.
    without = primitives_from_calibration(
        calibration(), ["move_left", "move_right", "rise_up"]
    )
    assert without.chords == ()
    assert not without.can_hop
    ids = [spec.option_id for spec in option_specs(without)]
    assert ids and not any(spec.startswith("hop") for spec in ids)


def test_option_specs_are_quantity_only_and_stable():
    primitives = world_primitives()
    specs = option_specs(primitives)
    ids = [spec.option_id for spec in specs]
    assert ids == [
        "traverse+x", "traverse-x",
        "hop+x", "hop-x",
        "retreat+x:4.0", "retreat-x:4.0",
        "wait:0.25", "wait:0.5", "wait:1",
    ]
    for spec in specs:
        assert isinstance(spec, OptionSpec)
        json.dumps(spec.params)                 # JSON-ready parameters
        assert not any(word in spec.intent.lower()
                       for word in ("try", "should", "better", "wall", "jump", "player"))

    hop = next(spec for spec in specs if spec.option_id == "hop+x")
    assert hop.params["chord"] == "chord_right"
    assert hop.params["run_up"] == 4.0
    assert hop.params["direction"] == [1.0, 0.0, 0.0]
    assert hop.params["flight_s"] == 1.0

    retreat = next(spec for spec in specs if spec.option_id == "traverse-x")
    assert retreat.template == "traverse" and retreat.params["action"] == "move_left"


def test_a_primitive_that_moves_nowhere_stays_a_plain_action():
    record = calibration()
    record["trials"].append({
        "requested_hold_s": 0.1,
        "valid": True,
        "control": "other",
        "before": {"position": [0.0, 0.0, 0.0], "yaw_deg": 0.0},
        "after": {"position": [0.0, 0.0, 0.0], "yaw_deg": 0.0},
        "effect_delta": [0.0, 0.0, 0.0],
        "execution": [{"type": "execution", "status": "completed", "applied_duration_s": 0.1}],
    })
    actions = catalog() + [
        {"name": "still", "inputs": [{"control_id": "other", "device": "Keyboard", "control": "other"}]}
    ]
    primitives = primitives_from_calibration(
        record, ["move_right", "rise_up", "still"], catalog=actions
    )
    kinds = {item.action: item.kind for item in primitives.classified}
    assert kinds["still"] == KIND_OTHER
    assert [item.label for item in primitives.directions] == ["+x"]


def test_unknown_measurements_stay_none():
    record = calibration()
    for trial in record["trials"]:
        if trial["control"] == "rise":
            trial["execution"] = [{"type": "execution", "status": "completed"}]
    primitives = primitives_from_calibration(
        record, ["move_right", "rise_up"], catalog=catalog()
    )
    assert primitives.rise is None              # no applied duration: no measured rate at all
    assert primitives.rise_duration_s is None and primitives.rise_peak is None
    assert not primitives.can_hop
    assert primitives.run_up() is None          # None, never zero


def test_a_direction_that_disagrees_across_a_basis_is_stated_not_labelled():
    record = calibration()

    # One trial cut short by an obstacle is a disagreeing sample, not a rotating basis.
    for trial in record["trials"]:
        if trial["control"] == "right" and trial["requested_hold_s"] == 0.1:
            trial["effect_delta"] = [0.0, 0.0, 0.0]
            trial["after"]["position"] = [0.0, 0.0, 0.0]
    primitives = primitives_from_calibration(record, ["move_right"], catalog=catalog())
    assert [item.label for item in primitives.directions] == ["+x"]

    # The same control measured at two entity yaws, moving along the entity's own right axis each time:
    # world-relative that is two different directions, in the basis it is one. It must be stated, not
    # offered as a world direction.
    record = calibration()
    for index, trial in enumerate(t for t in record["trials"] if t["control"] == "right"):
        hold = trial["requested_hold_s"]
        if index % 2:
            delta = [0.0, 0.0, 4.0 * hold]
            trial["before"]["yaw_deg"] = 90.0
            trial["before"]["world_right"] = [0.0, 0.0, 1.0]
            trial["before"]["world_forward"] = [1.0, 0.0, 0.0]
        else:
            delta = [4.0 * hold, 0.0, 0.0]
            trial["before"]["yaw_deg"] = 0.0
        trial["effect_delta"] = delta
        trial["after"]["position"] = delta
    primitives = primitives_from_calibration(record, ["move_right"], catalog=catalog())
    assert primitives.directions == ()
    assert any("basis" in note for note in primitives.unsupported)


def test_real_record_smoke(capsys):
    """Load the real DragonBlaze calibration record and print what the classifier measured."""
    if not RECORD.is_file() or not MANIFEST.is_file():
        import pytest
        pytest.skip("the recorded calibration and manifest are not present")

    record = json.loads(RECORD.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    offered = [action for action in manifest["actions"] if action.get("execution_supported")]
    primitives = primitives_from_calibration(record, offered, catalog=manifest["actions"])

    # The live runner uses exported, typed declarations rather than manifest dictionaries.
    from playtest.contracts.manifest import PreflightManifest
    from playtest.runner.choices import build_choices
    from playtest.contracts.loop import ChoiceKind
    built = build_choices(PreflightManifest.model_validate(manifest))
    live = primitives_from_calibration(
        record, [c.action for c in built.choices if c.kind is ChoiceKind.action and c.action],
        catalog=built.catalog.actions,
    )
    assert {d.label for d in live.directions} == {"+x", "-x"}
    assert live.can_hop

    with capsys.disabled():
        print("\n--- measured primitives, DragonBlaze calibration-07/05 ---")
        print(primitives.statement())
        print("--- offered options ---")
        for spec in option_specs(primitives):
            print(f"{spec.option_id}: {spec.intent}")

    labels = {item.label for item in primitives.directions}
    assert labels == {"+x", "-x"}
    assert primitives.rise is not None
    assert primitives.rise_duration_s is not None and primitives.rise_duration_s > 0
    assert primitives.rise_peak is not None and primitives.rise_peak > 0
    assert primitives.can_hop
    # The fastest action per direction is offered and the slower ones stay usable for a chord.
    assert primitives.chord_for("move_right") == "jump_right"
    assert primitives.chord_for("move_left") == "jump_left"
    hop_ids = [spec.option_id for spec in option_specs(primitives) if spec.template == "hop"]
    assert hop_ids == ["hop+x", "hop-x"]
