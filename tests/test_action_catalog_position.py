"""Mouse position: an absolute pointer placement, held like a button, used before a button to click at a place."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from playtest.contracts.action_catalog import (ActionDefinition, InputBinding, control_type, declaration_requirements,
                                               receipt_problems)


def test_position_is_a_placement_that_needs_both_coordinates():
    assert control_type("Mouse", "position") == "Position"
    InputBinding(device="Mouse", control="position", x=0.25, y=-1)
    with pytest.raises(ValidationError, match="absolute position"):
        InputBinding(device="Mouse", control="position", x=0.25, y=None)
    with pytest.raises(ValidationError):
        InputBinding(device="Mouse", control="position", x=1.5, y=0)


def test_a_click_at_a_place_is_a_chord_with_measured_receipts():
    click = ActionDefinition(backend="input_system", default_lease_s=0.05, inputs=[
        {"device": "Mouse", "control": "position", "x": 0.5, "y": -0.5},
        {"device": "Mouse", "control": "leftButton"}])
    receipt = lambda control, x, y: SimpleNamespace(  # noqa: E731
        step_index=0, device="Mouse", control=control, x=x, y=y, applied_duration_s=0.05, applied_delta_x=None,
        applied_delta_y=None, input_start_wall_s=10.0, input_end_wall_s=10.05, release_verified=True)
    receipts = [receipt("position", 0.5, -0.5), receipt("leftButton", None, None)]
    assert receipt_problems(declaration_requirements(click), receipts, declared=click,
                            applied_total_s=0.05, terminal_start_s=10.0, terminal_end_s=10.05) == []
    moved = [receipt("position", 0.5, 0.5), receipt("leftButton", None, None)]
    assert any("y is 0.5" in problem for problem in receipt_problems(
        declaration_requirements(click), moved, declared=click, applied_total_s=0.05,
        terminal_start_s=10.0, terminal_end_s=10.05))


def test_a_manifest_click_at_a_place_passes_review_and_is_not_a_look_axis():
    from runner_manifest import action, button, manifest, vector

    from playtest.preflight import manifest as manifest_checks
    from playtest.roles import preflight

    click = [vector("Mouse", "position", 0.5, -0.5), button("Mouse", "leftButton")]
    assert manifest_checks.validate(manifest(action("select_centre", click, hold_s=0.08))) == []
    unheld = manifest_checks.validate(manifest(action("select_centre", click, hold_s=None)))
    assert any("an input with x and y needs a hold time" in problem for problem in unheld)
    assert "Position" not in manifest_checks.LOOK_CONTROL_TYPES
    assert "Position" not in preflight.AXIS_CONTROLS
