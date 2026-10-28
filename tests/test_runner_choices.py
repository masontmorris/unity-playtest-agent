"""Manifest to choices: every executable action is offered as declared, and nothing is ranked or dropped quietly."""

from __future__ import annotations

import pytest

from playtest.contracts.loop import ChoiceKind, ChoiceSet, ChoiceSource
from playtest.contracts.manifest import PreflightManifest
from playtest.runner.choices import build_choices

from runner_manifest import action, fixture, hold, manifest


def offered_ids(built) -> list[str]:
    return [c.id for c in built.choices]


def test_an_executable_action_becomes_a_choice_with_its_hold_and_calibrated_effect():
    built = build_choices(manifest(
        action("move", "w", hold_s=0.2, intent="walk forward", holds=[hold(0.2), hold(0.4, mean=12.0)]),
        controls={"w": "preflight_key_w"}))
    move = built.choices[0]
    assert (move.id, move.kind, move.source) == ("move", ChoiceKind.action, ChoiceSource.manifest)
    assert move.action == "preflight_key_w" and move.params == {"hold_s": 0.2}
    assert move.label == ("walk forward: request a 0.2-second hold of w; calibrated net displacement 6.040 "
                          "world units (sample variance 0.000100, n=2).")
    assert built.leases == {"move": 0.2}
    assert built.expected["move"] == {
        "source": "calibration", "control": "w", "requested_hold_s": 0.2, "net_displacement": 6.04,
        "sample_variance": 0.0001, "valid_trials": 2, "effective_in_all_repeats": True, "unit": "world units"}
    assert built.not_offered == []


def test_the_core_choices_follow_the_actions_and_the_set_is_valid():
    built = build_choices(manifest(action("move", holds=[hold(0.2)])))
    assert offered_ids(built) == ["move", "wait", "escalate", "delegate", "finish"]
    core = built.choices[1:]
    assert [c.source for c in core] == [ChoiceSource.core] * 4
    assert [c.kind for c in core] == [ChoiceKind.wait, ChoiceKind.escalate, ChoiceKind.delegate, ChoiceKind.finish]
    assert [c.role for c in core] == [None, "frontier", "helper", None]
    ChoiceSet(set_version=0, built_from_seq=0, choices=built.choices)


def test_the_wait_choice_names_the_game_seconds_it_waits():
    built = build_choices(manifest(action("move", holds=[hold(0.2)])))
    wait = next(c for c in built.choices if c.kind is ChoiceKind.wait)
    assert (wait.id, wait.label, wait.params, wait.source) == (
        "wait", "Wait 0.5 s with no input", {"duration_game_s": 0.5}, ChoiceSource.core)
    assert wait.action is None and wait.skill is None            # it still sends no input
    others = [c for c in built.choices if c.source is ChoiceSource.core and c.kind is not ChoiceKind.wait]
    assert all(c.params == {} for c in others)                   # no other core choice carries a duration


def test_a_tap_hold_is_the_requested_hold_and_the_lease():
    built = build_choices(manifest(action("tap", "w", hold_s=None, tap_hold_s=0.1, holds=[hold(0.1)])))
    assert built.choices[0].params == {"hold_s": 0.1} and built.leases == {"tap": 0.1}
    assert "request a 0.1-second hold of w" in built.choices[0].label


def test_unmeasured_calibration_reads_unknown_never_zero():
    built = build_choices(manifest(
        action("none", "w", hold_s=0.7, holds=[hold(0.2)]),                    # no calibration at 0.7 s
        action("empty", "a", hold_s=0.2, holds=[hold(0.2, trials=0)]),          # measured, but no valid trial
        action("single", "s", hold_s=0.2, holds=[hold(0.2, trials=1, mean=3.0)]),   # one trial: variance unknown
        action("flat", "d", hold_s=0.2, holds=[hold(0.2, mean=0.0, effective=False)]),
        controls={"w": "preflight_key_w", "a": "preflight_key_a", "s": "preflight_key_s", "d": "preflight_key_d"}))
    labels = {c.id: c.label for c in built.choices[:4]}
    assert labels["none"].endswith("; no calibration at this hold.")
    assert labels["empty"].endswith("; calibrated effect unknown (no valid trial at this hold).")
    assert "calibrated net displacement 3.000 world units (sample variance unknown, n=1)." in labels["single"]
    assert "calibrated net displacement 0.000 world units (sample variance 0.000100, n=2); not effective in every repeat." in labels["flat"]
    assert built.expected["none"] is None and built.expected["empty"] is None
    assert built.expected["single"]["sample_variance"] is None and built.expected["single"]["valid_trials"] == 1
    assert built.expected["flat"]["net_displacement"] == 0.0 and built.expected["flat"]["effective_in_all_repeats"] is False


def test_non_executable_actions_and_skills_are_not_offered_with_the_reason():
    value = PreflightManifest.model_validate(fixture("valid-chord-skill.json"))
    built = build_choices(value)
    assert offered_ids(built) == ["wait", "escalate", "delegate", "finish"]
    assert built.not_offered == [
        {"kind": "action", "name": "chord",
         "reason": "execution_supported is false: it declares 2 controls and the bridge executes one"},
        {"kind": "skill", "name": "sequence",
         "reason": "execution_supported is false: the current bridge cannot execute skills"},
    ]
    axis = build_choices(manifest(action("steer", "w", axis=True, holds=[hold(0.2)])))
    assert axis.not_offered[0]["reason"].startswith("execution_supported is false: it drives an axis")


def test_an_action_the_bridge_has_no_name_for_is_not_offered_and_says_why():
    built = build_choices(manifest(
        action("move", "w", holds=[hold(0.2)]), action("look", "a", holds=[hold(0.2)]),
        controls={"w": "preflight_key_w", "a": None}))
    assert offered_ids(built)[:2] == ["move", "wait"]
    assert built.not_offered == [
        {"kind": "action", "name": "look",
         "reason": "the manifest's offered controls name no bridge action for control 'a'"}]


def test_actions_keep_the_manifests_order_and_none_is_dropped_or_reordered():
    names = ["zeta", "alpha", "mid", "beta"]
    controls = {"w": "preflight_key_w", "a": "preflight_key_a"}
    built = build_choices(manifest(*[action(n, "w" if i % 2 else "a", holds=[hold(0.2)]) for i, n in enumerate(names)],
                                   controls=controls))
    assert offered_ids(built)[:4] == names and built.not_offered == []
    assert [c.action for c in built.choices[:4]] == ["preflight_key_a", "preflight_key_w"] * 2


def test_preconditions_are_passed_through_as_text_and_never_evaluated():
    built = build_choices(manifest(action("move", holds=[hold(0.2)], preconditions=[
        {"field": "speed", "op": ">", "value": 0.5}, {"field": "speed", "op": "changed", "value": None}])))
    assert built.choices[0].preconditions == ["speed > 0.5", "speed changed"]


def test_an_action_named_like_a_core_choice_is_an_error_not_a_silent_rename():
    with pytest.raises(ValueError, match="collides with the core choice"):
        build_choices(manifest(action("wait", holds=[hold(0.2)])))
