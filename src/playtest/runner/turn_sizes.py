"""Deterministic single-input declarations from measured view turns; no symmetry or selection."""

from playtest.contracts.action_catalog import ActionDefinition, InputBinding, is_pointer_impulse
from playtest.contracts.calibration import ActionCalibration
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource




def measured_turns(manifest, calibration=None, *, include_yaw=False):
    if calibration is not None and calibration.get("entity_id") != manifest.entity_id:
        raise ValueError("turn calibration names a different entity")
    rows = (calibration.get("action_summaries", []) if calibration is not None else
            [c.model_dump(mode="json") for a in [*manifest.actions, *manifest.skills] for c in a.calibrations])
    seen = {}
    for raw in rows:
        row = ActionCalibration.model_validate(raw)
        key = (row.control, row.declared_x, row.declared_y)
        if key in seen:
            if seen[key] != row:
                raise ValueError("conflicting turn calibration summaries")
            continue
        seen[key] = row
        if row.device is None or row.control_name is None:
            continue
        binding = InputBinding(device=row.device, control=row.control_name, x=row.declared_x, y=row.declared_y)
        impulse = is_pointer_impulse(binding.device, binding.control)
        largest = 0.0
        for hold in sorted(row.holds, key=lambda h: h.requested_hold_s):
            view = hold.mean_view_turn
            if view is None or abs(view.yaw_deg) < 5 or view.spread_deg > 5:
                continue
            # A longer hold that measured no larger a turn than a shorter one has wrapped past a full circle:
            # its yaw is the net heading change, not the rotation made, so it is not offered as a turn of that size.
            if abs(view.yaw_deg) <= largest:
                continue
            largest = abs(view.yaw_deg)
            sign = "pos" if view.yaw_deg > 0 else "neg"
            ident = f"turn.{row.control}.{sign}{hold.requested_hold_s:g}"
            # Distinct measured values must never alias the same executable choice.
            definition = ActionDefinition(backend="input_system", inputs=[binding], default_lease_s=hold.requested_hold_s)
            direction = "right" if view.yaw_deg > 0 else "left"
            values = f"x={binding.x:g}, y={binding.y:g}" if binding.x is not None else ""
            timing = f"one relative impulse within {hold.requested_hold_s:g} s" if impulse else f"for {hold.requested_hold_s:g} s"
            label = (f"turn view {direction} {abs(view.yaw_deg):.1f}° (calibrated: {binding.device} {binding.control} "
                     f"{values} {timing}, spread {view.spread_deg:.1f}°)")
            choice = Choice(id=ident, kind=ChoiceKind.action, label=label, action=ident.replace(".", "_"), params={}, source=ChoiceSource.manifest)
            yield (choice, definition, view.yaw_deg) if include_yaw else (choice, definition)
            if impulse:
                break
