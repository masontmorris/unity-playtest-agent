"""Manifest to choices: every supported declaration is offered exactly as declared, and nothing is reduced, ranked or dropped quietly.

The builder reads what can run from `playtest.preflight.catalog.export`, so these tests build real manifests
whose declarations carry exact release-verified evidence (`runner_manifest` derives it from each declaration)
and check the choices against the exporter's own output.
"""

from __future__ import annotations

import pytest

from playtest.contracts.action_catalog import ActionCatalog
from playtest.contracts.loop import ChoiceKind, ChoiceSet, ChoiceSource
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight.catalog import export, serialize
from playtest.runner.choices import build_choices

from runner_manifest import action, button, fixture, hold, key, manifest, skill, vector


def offered_ids(built) -> list[str]:
    return [c.id for c in built.choices]


def by_id(built):
    return {c.id: c for c in built.choices}


def test_an_executable_action_becomes_a_choice_with_its_hold_and_calibrated_effect():
    built = build_choices(manifest(
        action("move", "w", hold_s=0.2, intent="walk forward", holds=[hold(0.2), hold(0.4, mean=12.0)])))
    move = built.choices[0]
    assert (move.id, move.kind, move.source) == ("move", ChoiceKind.action, ChoiceSource.manifest)
    assert move.action == "move" and move.skill is None and move.params == {}
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
    assert built.choices[0].params == {} and built.leases == {"tap": 0.1}
    assert "request a 0.1-second hold of w" in built.choices[0].label


def test_unmeasured_calibration_reads_unknown_never_zero():
    built = build_choices(manifest(
        action("none", "w", hold_s=0.7, holds=[hold(0.2)]),                    # no calibration at 0.7 s
        action("empty", "a", hold_s=0.2, holds=[hold(0.2, trials=0)]),          # measured, but no valid trial
        action("single", "s", hold_s=0.2, holds=[hold(0.2, trials=1, mean=3.0)]),   # one trial: variance unknown
        action("flat", "d", hold_s=0.2, holds=[hold(0.2, mean=0.0, effective=False)])))
    labels = {c.id: c.label for c in built.choices[:4]}
    assert labels["none"].endswith("; no calibration at this hold.")
    assert labels["empty"].endswith("; calibrated effect unknown (no valid trial at this hold).")
    assert "calibrated net displacement 3.000 world units (sample variance unknown, n=1)." in labels["single"]
    assert "calibrated net displacement 0.000 world units (sample variance 0.000100, n=2); not effective in every repeat." in labels["flat"]
    assert built.expected["none"] is None and built.expected["empty"] is None
    assert built.expected["single"]["sample_variance"] is None and built.expected["single"]["valid_trials"] == 1
    assert built.expected["flat"]["net_displacement"] == 0.0 and built.expected["flat"]["effective_in_all_repeats"] is False


def test_unsupported_declarations_are_not_offered_with_the_reason_the_manifest_gives():
    value = PreflightManifest.model_validate(fixture("valid-chord-skill.json"))
    built = build_choices(value)
    assert offered_ids(built) == ["wait", "escalate", "delegate", "finish"]
    assert built.not_offered == [
        {"kind": "action", "name": "chord",
         "reason": "execution_supported is false: no exact release-verified execution evidence"},
        {"kind": "skill", "name": "sequence",
         "reason": "execution_supported is false: no exact release-verified execution evidence"},
    ]
    assert built.catalog.actions == {}                       # nothing unproven is in the catalog the host serves
    unresolved = build_choices(manifest(
        action("tapless", "w", hold_s=None, tap_hold_s=None, holds=[hold(0.2)]),
        skills=(skill("noholds", [(["w"], 0.2, 0.0)], 1.0, supported=False),)))
    assert unresolved.not_offered[0] == {
        "kind": "action", "name": "tapless",
        "reason": "execution_supported is false: it has no resolved positive hold, so there is no exact declaration to prove"}
    assert unresolved.not_offered[1]["reason"] == "execution_supported is false: no exact release-verified execution evidence"


def test_support_follows_the_manifest_not_the_shape_of_the_declaration():
    """A chord, an axis or a value is neither refused nor accepted for its shape: only the manifest's own support decides."""
    unproven = build_choices(manifest(
        action("chord", ["w", "a"], supported=False),
        action("steer", vector("Gamepad", "leftStick", 0.0, 1.0), axis=True, supported=False)))
    assert offered_ids(unproven) == ["wait", "escalate", "delegate", "finish"]
    assert all(row["reason"] == "execution_supported is false: no exact release-verified execution evidence"
               for row in unproven.not_offered)
    proven = build_choices(manifest(
        action("chord", ["w", "a"]), action("steer", vector("Gamepad", "leftStick", 0.0, 1.0), axis=True)))
    assert offered_ids(proven)[:2] == ["chord", "steer"] and proven.not_offered == []


# --- regressions: what the prior integration did to supported declarations ------------------------------------


def test_a_proven_chord_is_offered_whole_under_its_own_name_never_as_its_first_key():
    built = build_choices(manifest(action("strafe", ["w", "a"], hold_s=0.2, intent="move sideways")))
    strafe = by_id(built)["strafe"]
    assert (strafe.kind, strafe.action, strafe.skill, strafe.params) == (ChoiceKind.action, "strafe", None, {})
    assert strafe.label.startswith("move sideways: request a 0.2-second hold of w + a")
    declared = built.catalog.actions["strafe"]
    assert [(i.device, i.control) for i in declared.inputs] == [("Keyboard", "w"), ("Keyboard", "a")]
    assert declared.default_lease_s == 0.2 and built.leases == {"strafe": 0.2}
    assert not any(c.action in ("preflight_key_w", "preflight_key_a") for c in built.choices)


def test_a_three_input_chord_across_devices_keeps_every_input_and_value_in_order():
    built = build_choices(manifest(action(
        "fire", [key("w"), vector("Gamepad", "rightTrigger", 1.0, 0.0), button("Mouse", "leftButton")], hold_s=0.3)))
    fire = by_id(built)["fire"]
    assert fire.action == "fire"
    assert fire.label.startswith("fire: request a 0.3-second hold of w + Gamepad rightTrigger (x=1, y=0) + Mouse leftButton")
    assert [(i.device, i.control, i.x, i.y) for i in built.catalog.actions["fire"].inputs] == [
        ("Keyboard", "w", None, None), ("Gamepad", "rightTrigger", 1.0, 0.0), ("Mouse", "leftButton", None, None)]


@pytest.mark.parametrize("spec, hold_s, text", [
    (vector("Mouse", "delta", 0.5, -0.25), 0.1,
     "Mouse delta (x=0.5, y=-0.25; one relative impulse of x*64, y*64 pixels) within a 0.1-second lease"),
    (vector("Gamepad", "leftStick", 0.3, 1.0), 0.5, "a 0.5-second hold of Gamepad leftStick (x=0.3, y=1)"),
    (vector("Gamepad", "leftTrigger", 0.5, 0.0), 0.3, "a 0.3-second hold of Gamepad leftTrigger (x=0.5, y=0)"),
    (vector("Gamepad", "dpad", -1.0, 1.0), 0.2, "a 0.2-second hold of Gamepad dpad (x=-1, y=1)"),
    (button("Gamepad", "buttonSouth"), 0.1, "a 0.1-second hold of Gamepad buttonSouth"),
    (button("Mouse", "rightButton"), 0.1, "a 0.1-second hold of Mouse rightButton"),
], ids=["mouse-delta", "stick", "trigger", "dpad", "gamepad-button", "mouse-button"])
def test_a_proven_mouse_or_gamepad_declaration_is_offered_with_its_exact_values(spec, hold_s, text):
    built = build_choices(manifest(action("act", spec, hold_s=hold_s, intent="do it")))
    act = by_id(built)["act"]
    assert (act.kind, act.action, act.params) == (ChoiceKind.action, "act", {})
    assert act.label.startswith(f"do it: request {text}")
    declared = built.catalog.actions["act"].inputs
    assert [(i.device, i.control, i.x, i.y) for i in declared] == [(spec["device"], spec["control"], spec["x"], spec["y"])]
    assert built.leases == {"act": hold_s}


def test_a_proven_skill_is_offered_as_a_skill_with_no_lease_and_no_borrowed_effect():
    built = build_choices(manifest(
        action("move", "w", holds=[hold(0.2)]),
        skills=(skill("dash", [(["w"], 0.2, 0.1), (["a", key("space")], 0.2, 0.0)], 1.0, purpose="dash and strafe"),)))
    dash = by_id(built)["dash"]
    assert (dash.kind, dash.skill, dash.action, dash.params, dash.preconditions) == (
        ChoiceKind.skill, "dash", None, {}, [])
    assert dash.label == ("dash and strafe: request this ordered sequence of 2 steps, at most 1 s in total: "
                          "step 1: hold w for 0.2 s, then wait 0.1 s; step 2: hold a + space for 0.2 s; "
                          "no calibrated effect is recorded for this sequence.")
    assert "dash" not in built.leases                        # its steps and max_s are its bound, not a host lease
    assert built.expected["dash"] is None                    # the calibrated `w` effect is not the sequence's effect
    declared = built.catalog.actions["dash"]
    assert declared.is_skill and declared.max_s == 1.0 and [s.wait_s for s in declared.steps] == [0.1, 0.0]
    assert "dash" not in built.leases and built.not_offered == []


def test_a_skill_with_a_mouse_impulse_step_states_its_value_and_its_lease():
    built = build_choices(manifest(skills=(skill(
        "sweep", [([vector("Mouse", "delta", 0.5, -0.25), button("Mouse", "leftButton")], 0.05, 0.1)], 0.5),)))
    assert by_id(built)["sweep"].label.startswith(
        "sweep: request this ordered sequence of 1 step, at most 0.5 s in total: step 1: apply "
        "Mouse delta (x=0.5, y=-0.25; one relative impulse of x*64, y*64 pixels) + Mouse leftButton "
        "within a 0.05-second lease, then wait 0.1 s;")


def test_supported_actions_then_skills_are_offered_in_the_manifests_order_and_none_is_dropped():
    actions = [action(n, "w" if i % 2 else "a", holds=[hold(0.2)]) for i, n in enumerate(["zeta", "alpha", "mid", "beta"])]
    skills = (skill("second", [(["w"], 0.1, 0.0)], 0.5), skill("first", [(["a"], 0.1, 0.0)], 0.5))
    built = build_choices(manifest(*actions, skills=skills))
    assert offered_ids(built) == ["zeta", "alpha", "mid", "beta", "second", "first", "wait", "escalate", "delegate", "finish"]
    assert [c.kind for c in built.choices[:6]] == [ChoiceKind.action] * 4 + [ChoiceKind.skill] * 2
    assert [c.action for c in built.choices[:4]] == ["zeta", "alpha", "mid", "beta"]
    assert [c.skill for c in built.choices[4:6]] == ["second", "first"] and built.not_offered == []


def test_the_full_manifest_cap_is_offered_without_ranking_or_suppression():
    actions = [action(f"act_{i:02d}", ["w", "a"] if i % 2 else "w", hold_s=0.1 + i / 100) for i in range(14)]
    skills = tuple(skill(f"skill_{i}", [(["w"], 0.1, 0.0), (["a"], 0.1, 0.1)], 1.0) for i in range(6))
    built = build_choices(manifest(*actions, skills=skills))
    assert offered_ids(built)[:20] == [a["name"] for a in actions] + [s["name"] for s in skills]
    assert set(built.catalog.actions) == set(offered_ids(built)[:20])


def test_the_choices_come_from_the_exporters_catalog_and_agree_with_it_name_for_name():
    value = manifest(
        action("move", "w", holds=[hold(0.2)]), action("strafe", ["w", "a"]),
        action("look", vector("Mouse", "delta", 0.5, -0.25), hold_s=0.1),
        action("steer", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.5),
        action("unproven", "s", supported=False),
        skills=(skill("dash", [(["w"], 0.2, 0.1), (["a"], 0.2, 0.0)], 1.0), skill("never", [(["d"], 0.2, 0.0)], 1.0, supported=False)))
    built = build_choices(value)
    assert built.catalog == export(value)                               # the exporter's own output, not a copy of it
    manifest_choices = [c for c in built.choices if c.source is ChoiceSource.manifest]
    assert [c.id for c in manifest_choices] == list(built.catalog.actions) == ["move", "strafe", "look", "steer", "dash"]
    for choice in manifest_choices:
        assert (choice.action or choice.skill) == choice.id             # the request names the declaration itself
        assert (choice.kind is ChoiceKind.skill) == built.catalog.actions[choice.id].is_skill
    assert {row["name"] for row in built.not_offered} == {"unproven", "never"}
    # What the host registers is the serialized export: the same names and the same values.
    served = ActionCatalog.model_validate(serialize(built.catalog), strict=True)
    assert served == built.catalog


def test_host_leases_are_the_exported_holds_and_never_a_skills_bound():
    built = build_choices(manifest(
        action("move", "w", hold_s=0.2), action("tap", "a", hold_s=None, tap_hold_s=0.1, holds=[hold(0.1)]),
        action("look", vector("Mouse", "delta", 0.5, 0.0), hold_s=0.05),
        skills=(skill("dash", [(["w"], 0.2, 0.1)], 1.0),)))
    assert built.leases == {name: built.catalog.actions[name].default_lease_s for name in ("move", "tap", "look")}
    assert built.leases == {"move": 0.2, "tap": 0.1, "look": 0.05}      # a skill has no entry: max_s bounds it


# --- the expected effect is a measurement of that declaration or nothing --------------------------------------


def test_a_compound_declaration_has_no_expected_effect_and_the_label_says_so():
    """Both keys were calibrated alone (and `w` shows a displacement at 0.2 s); that is not the chord's effect."""
    built = build_choices(manifest(action("strafe", ["w", "a"], hold_s=0.2, intent="move sideways",
                                          holds=[hold(0.2, mean=6.04)])))
    strafe = by_id(built)["strafe"]
    assert built.expected["strafe"] is None
    assert strafe.label == ("move sideways: request a 0.2-second hold of w + a; "
                            "no calibrated effect is recorded for this combination of inputs.")
    assert "6.04" not in strafe.label and "calibrated net displacement" not in strafe.label


def test_a_single_non_key_input_has_an_expected_effect_only_at_the_value_calibration_measured():
    at_value = build_choices(manifest(action(
        "steer", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.5, holds=[hold(0.5, mean=2.0)])))
    assert at_value.expected["steer"] == {
        "source": "calibration", "control": "gamepad_leftstick", "requested_hold_s": 0.5, "net_displacement": 2.0,
        "sample_variance": 0.0001, "valid_trials": 2, "effective_in_all_repeats": True, "unit": "world units"}
    assert by_id(at_value)["steer"].label.endswith("calibrated net displacement 2.000 world units (sample variance 0.000100, n=2).")

    other_value = build_choices(manifest(action(
        "steer", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.5, holds=[hold(0.5, mean=2.0)], calibrated=(1.0, 0.0))))
    assert other_value.expected["steer"] is None
    assert by_id(other_value)["steer"].label.endswith(
        "no calibrated effect is recorded at this declared value (calibration measured x=1, y=0).")

    unrecorded = build_choices(manifest(action(
        "steer", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.5, holds=[hold(0.5, mean=2.0)], calibrated=(None, None))))
    assert unrecorded.expected["steer"] is None
    assert by_id(unrecorded)["steer"].label.endswith("(calibration measured no declared value).")

    untested_hold = build_choices(manifest(action(
        "steer", vector("Gamepad", "leftStick", 0.3, 1.0), hold_s=0.7, holds=[hold(0.5, mean=2.0)])))
    assert untested_hold.expected["steer"] is None and by_id(untested_hold)["steer"].label.endswith("; no calibration at this hold.")


def test_a_skill_never_inherits_a_step_controls_calibration():
    built = build_choices(manifest(
        action("move", "w", holds=[hold(0.2, mean=6.04)]),
        skills=(skill("walk", [(["w"], 0.2, 0.0)], 1.0),)))        # the same single key, held for the same time
    assert built.expected["move"]["net_displacement"] == 6.04 and built.expected["walk"] is None
    assert "calibrated net displacement" not in by_id(built)["walk"].label


def test_preconditions_are_passed_through_as_text_and_never_evaluated():
    built = build_choices(manifest(action("move", holds=[hold(0.2)], preconditions=[
        {"field": "speed", "op": ">", "value": 0.5}, {"field": "speed", "op": "changed", "value": None}])))
    assert built.choices[0].preconditions == ["speed > 0.5", "speed changed"]


def test_the_legacy_per_key_bridge_names_in_the_offered_controls_are_not_consulted():
    """The host serves the declarations, so a control's `preflight_key_*` name decides nothing, present or absent."""
    for controls in ({"w": "preflight_key_w"}, {"w": None}, {"w": "something_else"}):
        built = build_choices(manifest(action("move", "w", holds=[hold(0.2)]), controls=controls))
        assert offered_ids(built)[0] == "move" and built.choices[0].action == "move" and built.not_offered == []


def test_a_name_that_collides_with_a_core_choice_is_an_error_not_a_silent_rename():
    with pytest.raises(ValueError, match="collides with the core choice"):
        build_choices(manifest(action("wait", holds=[hold(0.2)])))
    with pytest.raises(ValueError, match="collides with the core choice"):
        build_choices(manifest(skills=(skill("finish", [(["w"], 0.1, 0.0)], 0.5),)))
    unsupported = build_choices(manifest(action("wait", supported=False)))     # never offered, so no collision
    assert unsupported.not_offered[0]["name"] == "wait"


def test_a_supported_declaration_missing_from_the_export_is_an_error_not_a_dropped_choice(monkeypatch):
    from playtest.runner import choices

    value = manifest(action("move", "w", holds=[hold(0.2)]))
    monkeypatch.setattr(choices, "export", lambda _manifest: ActionCatalog(actions={}))
    with pytest.raises(ValueError, match="execution_supported but the exported catalog has no declaration"):
        build_choices(value)
    wrong_kind = export(manifest(skills=(skill("dash", [(["w"], 0.2, 0.0)], 1.0),)))
    monkeypatch.setattr(choices, "export", lambda _manifest: ActionCatalog(actions={"move": wrong_kind.actions["dash"]}))
    with pytest.raises(ValueError, match="is exported as a skill"):
        build_choices(value)


def test_a_label_states_a_declared_number_exactly_and_never_rounds_it():
    built = build_choices(manifest(
        action("fine", vector("Gamepad", "leftStick", 0.123456789, -0.987654321), hold_s=0.123456789),
        skills=(skill("slow", [(["w"], 0.123456789, 0.0000123)], 0.5),)))
    fine, slow = by_id(built)["fine"], by_id(built)["slow"]
    assert "a 0.123456789-second hold of Gamepad leftStick (x=0.123456789, y=-0.987654321)" in fine.label
    assert "hold w for 0.123456789 s, then wait 1.23e-05 s" in slow.label
    assert built.catalog.actions["fine"].inputs[0].x == 0.123456789 and built.leases == {"fine": 0.123456789}
