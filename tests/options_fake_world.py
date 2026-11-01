"""A tiny side-view world for the option tests.

One horizontal axis and one vertical axis, a run speed, a jump impulse and gravity. It exists so the
executor can be tested against physics rather than against assertions about physics, and it implements
exactly the client methods the executor uses: `observe_once`, `start_act`, `collect_act`, `stop`, `place`.

The world has three measured features the tests use:

* a **one-tile step**: a raised block. Walking into it stops the body; a standing jump against it cannot
  clear it (holding into the wall slides you down, because there is no air control); a hop with a run-up
  carries its takeoff momentum over the block and lands on top.
* a **pit**: no floor over a span, so the body falls. Falling past `dead_below` raises the declared
  `dead` reader, which the executor watches like any manifest reader.
* **stale and rejection injection**, so an option can be seen to end `rejected` with the bridge's own
  receipt.

No game, object, control or genre names: the controls, the actions and the catalog are the fixture's own.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from playtest.contracts.bridge import (
    ExecutionRecord,
    ExecStatus,
    Observation,
    RejectReason,
    StopResult,
)

PERIOD_S = 0.05          # the executor's observation period, so one observation is one physics step
WALL_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

LEFT, RIGHT, UP = "left", "right", "rise"


def catalog() -> list[dict[str, Any]]:
    """The declared actions this world accepts, in the manifest's shape."""
    def inputs(*controls: str) -> list[dict[str, Any]]:
        return [{"control_id": control, "device": "Keyboard", "control": control}
                for control in controls]

    return [
        {"name": "move_left", "inputs": inputs(LEFT)},
        {"name": "move_right", "inputs": inputs(RIGHT)},
        {"name": "rise_up", "inputs": inputs(UP)},
        {"name": "chord_right", "inputs": inputs(RIGHT, UP)},
        {"name": "chord_left", "inputs": inputs(LEFT, UP)},
    ]


def declarations() -> dict[str, tuple[str, ...]]:
    return {entry["name"]: tuple(item["control_id"] for item in entry["inputs"])
            for entry in catalog()}


@dataclass
class PlaceResult:
    placed: bool
    position_after_settle: list[float] | None
    moved_during_settle: float | None
    reason: str | None = None

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return asdict(self)


def _trial(control: str, hold_s: float, delta: Sequence[float]) -> dict[str, Any]:
    """One calibration trial in the recorded shape."""
    return {
        "requested_hold_s": hold_s,
        "valid": True,
        "control": control,
        "before": {"position": [0.0, 0.0, 0.0], "yaw_deg": 0.0, "world_forward": [0.0, 0.0, 1.0],
                   "world_right": [1.0, 0.0, 0.0], "basis_state": "present"},
        "after": {"position": [float(v) for v in delta], "yaw_deg": 0.0,
                  "world_forward": [0.0, 0.0, 1.0], "world_right": [1.0, 0.0, 0.0],
                  "basis_state": "present"},
        "effect_delta": [float(v) for v in delta],
        "execution": [
            {"type": "execution", "status": "running", "action_id": f"calibration-{control}-{hold_s}"},
            {"type": "execution", "status": "completed", "action_id": f"calibration-{control}-{hold_s}",
             "applied_duration_s": hold_s, "release_verified": True},
        ],
    }


def calibration(run_speed: float = 4.0, jump_impulse: float = 6.0,
                gravity: float = 12.0) -> dict[str, Any]:
    """A calibration record for this world, in the recorded shape, from the world's own physics."""
    holds = (0.1, 0.2, 0.4, 0.6, 0.8, 1.0)
    airtime = 2.0 * jump_impulse / gravity
    trials: list[dict[str, Any]] = []
    for control, sign in ((LEFT, -1.0), (RIGHT, 1.0)):
        for hold in (0.1, 0.2, 0.4, 1.0):
            trials.append(_trial(control, hold, [sign * run_speed * hold, 0.0, 0.0]))
    for hold in holds:
        rise = max(0.0, jump_impulse * hold - 0.5 * gravity * hold * hold) if hold <= airtime else 0.0
        trials.append(_trial(UP, hold, [0.0, rise, 0.0]))
    actions = [
        {"control": control, "control_name": control, "action": f"calibration_{control}",
         "device": "Keyboard", "control_type": "Button"}
        for control in (LEFT, RIGHT, UP)
    ]
    return {
        "schema_version": 1,
        "coordinate_frame": "Unity world space",
        "units": "Unity world units",
        "actions": actions,
        "action_summaries": actions,
        "trials": trials,
    }


@dataclass
class FakeWorld:
    """A side-view body with a step, a pit and input injection."""

    x: float = 0.0
    y: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    grounded: bool = True
    run_speed: float = 4.0
    jump_impulse: float = 6.0
    gravity: float = 12.0
    step_x: float | None = 3.0
    step_height: float = 1.0
    step_width: float = 3.0
    pit_from: float | None = None
    pit_to: float | None = None
    dead_below: float = -0.5
    coyote_s: float = 0.0          # a jump still allowed this long after walking off an edge
    left_ground_at: float | None = None
    dead: bool = False
    publish_pose: bool = True
    reject_acts: int = 0
    scene: str = "fixture:scene"

    # --- live state
    now: float = 0.0
    seq: int = 0
    frame: int = 0
    held: set[str] = field(default_factory=set)
    previous_held: set[str] = field(default_factory=set)
    lease_until: float | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    records: dict[str, list[ExecutionRecord]] = field(default_factory=dict)
    terminal: dict[str, list[ExecutionRecord]] = field(default_factory=dict)
    started_at: dict[str, float] = field(default_factory=dict)
    _declared: dict[str, tuple[str, ...]] = field(default_factory=declarations)

    # --- time -------------------------------------------------------------------------------

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        """The world advances on observation, so a pacing sleep is a no-op."""

    # --- physics ----------------------------------------------------------------------------

    def _floor_at(self, x: float) -> float | None:
        if self.pit_from is not None and self.pit_to is not None and self.pit_from <= x < self.pit_to:
            return None
        if (self.step_x is not None
                and self.step_x <= x <= self.step_x + self.step_width):
            return self.step_height
        return 0.0

    def _step(self) -> None:
        held = self.held
        previous_x, previous_y = self.x, self.y
        if self.grounded:
            if RIGHT in held and LEFT not in held:
                self.vx = self.run_speed
            elif LEFT in held and RIGHT not in held:
                self.vx = -self.run_speed
            else:
                self.vx = 0.0
        # The rise is a press, not a hold: only a newly applied input leaves the ground, so holding
        # it through the flight does not bounce the body on landing. Within `coyote_s` of walking off an
        # edge a press still leaves the ground, as many platformers allow.
        in_coyote = (not self.grounded and self.coyote_s > 0 and self.left_ground_at is not None
                     and self.now - self.left_ground_at <= self.coyote_s and self.vy <= 0)
        if (self.grounded or in_coyote) and UP in held and UP not in self.previous_held:
            self.vy = self.jump_impulse
            self.grounded = False
            self.left_ground_at = None
        # Airborne there is no air control: the body keeps the momentum it took off with.
        self.vy -= self.gravity * PERIOD_S
        self.x += self.vx * PERIOD_S
        self.y += self.vy * PERIOD_S

        if (self.step_x is not None and self.step_height > 0
                and previous_y < self.step_height - 1e-9
                and self.y < self.step_height - 1e-9
                and self.step_x <= self.x <= self.step_x + self.step_width):
            # Entered the block from the side: the face stops it, and it keeps falling.
            half = self.step_x + self.step_width / 2.0
            self.x = self.step_x - 1e-6 if self.x < half else self.step_x + self.step_width + 1e-6
            self.vx = 0.0

        floor = self._floor_at(self.x)
        was_grounded = self.grounded
        if floor is not None and self.y <= floor:
            self.y = floor
            self.vy = 0.0
            self.grounded = True
            self.left_ground_at = None
        else:
            if was_grounded and self.vy <= 0:  # walked off an edge, not jumped
                self.left_ground_at = self.now
            self.grounded = False

        if self.y < self.dead_below:
            self.dead = True
        self.previous_held = set(self.held)

    # --- the client the executor uses ---------------------------------------------------------

    async def observe_once(self) -> Observation:
        self.now += PERIOD_S
        if self.lease_until is not None and self.now >= self.lease_until:
            self.held.clear()
            self.lease_until = None
        self._step()
        self.seq += 1
        self.frame += 1
        public: dict[str, Any] = {"dead": self.dead}
        if self.publish_pose:
            public["controlled"] = {
                "object_id": "fixture:body",
                "path": "Body",
                "position": [self.x, self.y, 0.0],
                "velocity": [self.vx, self.vy, 0.0],
                "source": "rigidbody2d",
            }
        return Observation(
            protocol="upa/1",
            session="fixture",
            seq=self.seq,
            game_s=self.now,
            wall_utc=WALL_EPOCH + timedelta(seconds=self.now),
            clock_mode="realtime",
            scene=self.scene,
            playing=not self.dead,
            paused=False,
            time_scale=1.0,
            frame=self.frame,
            public=public,
        )

    async def start_act(self, **request: Any) -> list[ExecutionRecord]:
        self.requests.append(dict(request))
        action = request["action"]
        action_id = request["action_id"]
        lease = request.get("lease_s")
        record = ExecutionRecord(
            protocol="upa/1",
            session="fixture",
            seq=self.seq,
            game_s=self.now,
            wall_utc=WALL_EPOCH + timedelta(seconds=self.now),
            action_id=action_id,
            status=ExecStatus.running,
            validated_against_seq=request.get("based_on_seq"),
            release_verified=None,
            applied_duration_s=None,
        )
        if self.reject_acts > 0:
            self.reject_acts -= 1
            rejected = record.model_copy(update={
                "status": ExecStatus.rejected,
                "reject_reason": RejectReason.stale_age,
            })
            self.records[action_id] = [rejected]
            self.terminal[action_id] = [rejected]
            return [rejected]
        if action not in self._declared:
            rejected = record.model_copy(update={
                "status": ExecStatus.rejected,
                "reject_reason": RejectReason.unknown_action,
            })
            self.records[action_id] = [rejected]
            self.terminal[action_id] = [rejected]
            return [rejected]
        self.held = set(self._declared[action])
        self.lease_until = self.now + float(lease) if lease else None
        self.started_at[action_id] = self.now
        self.records[action_id] = [record]
        return [record]

    async def collect_act(self, action_id: str) -> list[ExecutionRecord]:
        held = self.records.get(action_id, [])
        running = held[-1]
        started = self.started_at.get(action_id, self.now)
        done = running.model_copy(update={
            "status": ExecStatus.completed,
            "release_verified": True,
            "applied_duration_s": max(0.0, self.now - started),
        })
        self.records[action_id] = [*held, done]
        self.terminal[action_id] = [done]
        return [done]

    def action_records(self, action_id: str) -> list[ExecutionRecord]:
        return list(self.records.get(action_id, []))

    async def stop(self, reason: str = "finish") -> StopResult:
        released = sorted(self.held)
        self.held.clear()
        self.lease_until = None
        self.stops.append(reason)
        return StopResult(
            protocol="upa/1",
            session="fixture",
            seq=self.seq,
            game_s=self.now,
            wall_utc=WALL_EPOCH + timedelta(seconds=self.now),
            released=released,
            release_verified=True,
        )

    async def place(self, position: Sequence[float], settle_s: float) -> PlaceResult:
        before = (self.x, self.y)
        self.x, self.y = float(position[0]), float(position[1])
        self.vx = self.vy = 0.0
        floor = self._floor_at(self.x)
        self.grounded = floor is not None and self.y <= floor
        self.held.clear()
        self.now += settle_s
        moved = math.hypot(self.x - before[0], self.y - before[1])
        return PlaceResult(placed=True, position_after_settle=[self.x, self.y, 0.0],
                           moved_during_settle=moved, reason=None)
