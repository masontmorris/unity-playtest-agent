"""Manifest-declared reach regions: distance and membership from one measured pose.

A reach region is an operator's declaration about a place, not a measurement and not a verified target.
This module measures only the pose source the declaration names, in the frame and scene it names, and
derives two facts: the distance from that point to the declared entry volume, and whether the point is
inside it. A missing pose, a different scene or an unsupported source leaves both unknown with a reason;
it never becomes a distance of zero or a success.

The declaration, its provenance and the measured values are stated to S1 as fact lines and to the
frontier in the escalation packet. Nothing here selects, ranks, suppresses or replaces an action, opens
help or says the objective was completed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from playtest.contracts.bridge import Observation
from playtest.contracts.manifest import ReachBounds, ReachRegion
from playtest.core.ports import DirectiveView, FactsSource, TurnReport

# The only pose source this version supports: the bridge probe anchor's own world position. The name is
# the declaration's, not an inference about any other entity.
POSE_SOURCES: dict[str, tuple[str, ...]] = {"probe_anchor": ("probe", "anchor", "position")}
SUPPORTED_FRAMES = frozenset({"world"})
UNIT = "world units"

Pose = tuple[float, float, float]


def _finite_vector(value: Any) -> Pose | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            return None
        out.append(float(item))
    return out[0], out[1], out[2]


def pose_at(public: Mapping[str, Any] | None, source: str) -> tuple[Pose | None, str | None]:
    """The world position the declared pose source measures, or none and why it is unknown."""
    path = POSE_SOURCES.get(source)
    if path is None:
        return None, f"the pose source {source!r} is not supported"
    node: Any = public
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None, f"the measured pose {'.'.join(path)} is not in this observation"
        node = node[part]
    position = _finite_vector(node)
    if position is None:
        return None, f"the measured pose {'.'.join(path)} is not a finite world position"
    return position, None


def _corners(bounds: ReachBounds) -> tuple[Pose, Pose]:
    minimum, maximum = _finite_vector(bounds.min), _finite_vector(bounds.max)
    if minimum is None or maximum is None:
        raise ValueError("declared reach bounds are not finite 3-vectors")
    if any(low >= high for low, high in zip(minimum, maximum, strict=True)):
        raise ValueError("declared reach bounds are not ordered min < max on every axis")
    return minimum, maximum


def point_box_distance(point: Pose, minimum: Pose, maximum: Pose) -> float:
    """Euclidean distance from a point to an axis-aligned box; zero exactly when the point is inside."""
    total = 0.0
    for value, low, high in zip(point, minimum, maximum, strict=True):
        if value < low:
            total += (low - value) ** 2
        elif value > high:
            total += (value - high) ** 2
    return math.sqrt(total)


def inside_bounds(point: Pose, minimum: Pose, maximum: Pose) -> bool:
    return all(low <= value <= high for value, low, high in zip(point, minimum, maximum, strict=True))


def _declaration(region: ReachRegion) -> dict[str, Any]:
    return {
        "region_id": region.region_id,
        "label": region.label,
        "purpose": region.purpose,
        "frame": region.frame,
        "scene": region.scene,
        "bounds": {"min": [float(value) for value in region.bounds.min],
                   "max": [float(value) for value in region.bounds.max]},
        "pose": region.pose,
        "author": region.author,
        "source": region.source,
        "verification": region.verification,
    }


def read_region(region: ReachRegion, observation: Observation) -> dict[str, Any]:
    """One declaration's derived reading at this observation.

    Every key is always present. An unmeasured pose carries `measured_pose`, `distance` and `inside` of
    null and a `reason`; a measured one carries a finite `distance`, a bool `inside` and no reason.
    """
    row: dict[str, Any] = {
        **_declaration(region),
        "seq": observation.seq,
        "game_s": observation.game_s,
        "measured_pose": None,
        "distance": None,
        "inside": None,
        "reason": None,
    }
    if region.frame not in SUPPORTED_FRAMES:
        row["reason"] = f"the declared frame {region.frame!r} is not supported"
        return row
    if observation.scene != region.scene:
        row["reason"] = (f"the observation scene {observation.scene!r} is not the declared scene "
                         f"{region.scene!r}")
        return row
    pose, problem = pose_at(observation.public, region.pose)
    if pose is None:
        row["reason"] = problem
        return row
    minimum, maximum = _corners(region.bounds)
    row["measured_pose"] = list(pose)
    row["distance"] = point_box_distance(pose, minimum, maximum)
    row["inside"] = inside_bounds(pose, minimum, maximum)
    return row


def progress_rows(regions: Sequence[ReachRegion], observation: Observation) -> list[dict[str, Any]]:
    """Every declaration the manifest contains, read at this observation, in manifest order."""
    return [read_region(region, observation) for region in regions]


class ProgressProbe:
    """Reads one run's declared reach regions. With no declaration it reads nothing at all."""

    def __init__(self, regions: Sequence[ReachRegion] = ()) -> None:
        self.regions: tuple[ReachRegion, ...] = tuple(regions)

    def readings(self, observation: Observation) -> list[dict[str, Any]]:
        return progress_rows(self.regions, observation)

    def derived(self, observation: Observation) -> dict[str, Any]:
        """Only measured distances, by their setpoint field path. A derived field absent here is unknown."""
        return {f"progress.{row['region_id']}.distance": row["distance"]
                for row in self.readings(observation) if row["distance"] is not None}

    def fact_lines(self, observation: Observation) -> list[str]:
        return [fact_line(row) for row in self.readings(observation)]


class ProgressFacts:
    """State each declared region's reading as a fact, then forward everything else to the inner source.

    With no reach declaration it appends nothing, so a manifest without one states exactly the facts it
    did before. The readings are measurements and declarations, never a recommendation or an outcome.
    """

    def __init__(self, inner: FactsSource, probe: ProgressProbe) -> None:
        self._inner, self._probe = inner, probe

    async def facts(self, reports: Sequence[TurnReport], observation: Observation) -> list[str]:
        lines = list(await self._inner.facts(reports, observation))
        if self._probe.regions:
            lines.extend(self._probe.fact_lines(observation))
        return lines

    def directive_view(self, observation: Observation) -> DirectiveView | None:
        view = getattr(self._inner, "directive_view", None)
        return view(observation) if view is not None else None

    def finish(self, **values: Any) -> None:
        finish = getattr(self._inner, "finish", None)
        if finish is not None:
            finish(**values)

    def rebind(self, **values: Any) -> None:
        """Forward a play-session boundary to the inner source, exactly as `finish` is forwarded.

        The wrapper adds fact lines and owns nothing else. A source that invalidates a directive at a
        restart must still receive that boundary when declarations exist, or the wrapper would silently
        drop an existing seam.
        """
        rebind = getattr(self._inner, "rebind", None)
        if rebind is not None:
            rebind(**values)


def _vector_text(vector: Sequence[float]) -> str:
    return "[" + ", ".join(f"{value:.3f}" for value in vector) + "]"


def fact_line(row: Mapping[str, Any]) -> str:
    """One S1-facing fact: the declaration, its provenance, and what this observation measured."""
    bounds = row["bounds"]
    declaration = (
        f"Declared reach region {row['region_id']} ({row['label']}; {row['purpose']}): entry volume "
        f"{_vector_text(bounds['min'])} to {_vector_text(bounds['max'])} in the {row['frame']} frame, "
        f"scene {row['scene']!r}, declared by {row['author']} from {row['source']}, "
        f"verification {row['verification']}."
    )
    if row["measured_pose"] is None:
        body = (f"At seq {row['seq']} (game_s {row['game_s']}), the distance and membership are unknown: "
                f"{row['reason']}.")
    else:
        body = (
            f"At seq {row['seq']} (game_s {row['game_s']}), the measured pose source {row['pose']!r} gave "
            f"{_vector_text(row['measured_pose'])}: distance {row['distance']:.3f} {UNIT}, "
            f"inside {row['inside']}."
        )
    return (f"{declaration} {body} This is a declaration, not a verified target; neither distance nor "
            f"membership shows the objective was completed or any particular object was used.")
