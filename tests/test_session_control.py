"""The host mailbox adapter: one ticket, verified release, bounded polling and a fail-closed terminal result.

No Unity, no network and no provider: the control directory, the clock, the lock and the bridge are fakes.
Every case checks what the runner measured, including the negative ones: a wrong ticket, an early ready, a
timeout, an unverified release, a concurrent request and a first observation that is not a new play session.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from playtest.contracts.loop import OperationStatus
from playtest.core.bridge_lock import BridgeEndpoint
from playtest.runner import session_control as control_module
from playtest.runner.session_control import SessionControl, SessionControlError, load_host_session

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
G0 = str(uuid.uuid4())
G1 = str(uuid.uuid4())
G2 = str(uuid.uuid4())
SCENE = "Assets/Scenes/Game.unity"
RUN = "run-restart-1"


class Clock:
    """A clock that moves exactly when the adapter sleeps, and records what it asked for.

    `after_sleep` lets a test publish the host's terminal records at the moment a real host would: after
    the runner has dispatched the request and is waiting, never before.
    """

    def __init__(self) -> None:
        self.now = NOW
        self.slept: list[float] = []
        self.after_sleep: Any = None

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.after_sleep is not None:
            action, self.after_sleep = self.after_sleep, None
            action()


NEW_SESSION = "new-bridge-session"  # this run's post-restart bridge session, not the released one


def endpoint(token: str, started: datetime) -> BridgeEndpoint:
    return BridgeEndpoint(path=Path("lock"), protocol="upa/1", pid=4242, port=1234, token=token,
                          started_utc=started)


class FakeClient:
    """A connected bridge: the adapter proves the new session through its endpoint, lock and session id."""

    def __init__(self, observation: Any, *, connected: BridgeEndpoint | None = None,
                 session: str | None = NEW_SESSION) -> None:
        self.observation = observation
        self.endpoint = connected if connected is not None else endpoint("new-token", NOW + timedelta(seconds=2))
        self.session = session
        self.closed = False

    async def observe_once(self) -> Any:
        return self.observation

    async def aclose(self) -> None:
        self.closed = True


class HungClient:
    """A bridge whose first observation never answers, so only the operation bound can end it."""

    def __init__(self, *, endpoint_value: BridgeEndpoint | None = None, session: str = NEW_SESSION) -> None:
        self.endpoint = endpoint_value or endpoint("new-token", NOW + timedelta(seconds=2))
        self.session = session
        self.closed = False

    async def observe_once(self) -> Any:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.closed = True


def observation(*, playing: bool = True, paused: bool = False, scene: str | None = "Game",
                session: str | None = None) -> Any:
    return SimpleNamespace(playing=playing, paused=paused, scene=scene, session=session)


def write_json(path: Path, body: dict[str, Any]) -> None:
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")


def control_directory(tmp_path: Path, *, project: Path, phase: str = "ready", generation: str | None = G0,
                      capabilities: list[str] | None = None, run_id: str = RUN) -> Path:
    directory = tmp_path / "control"
    directory.mkdir()
    write_json(directory / "config.json", {
        "version": 1, "run_id": run_id, "project_path": str(project),
        "catalog_path": str(project / "catalog.json"), "catalog_sha256": "a" * 64,
        "deadline_utc": (NOW + timedelta(hours=1)).isoformat(),
    })
    write_json(directory / "host-state.json", {
        "version": 1, "run_id": run_id, "phase": phase, "generation": generation,
        "scene_path": SCENE if phase == "ready" else None, "scene_handle": 7 if phase == "ready" else None,
        "ready_utc": (NOW + timedelta(seconds=4)).isoformat() if phase == "ready" else None,
        "error": None,
        "capabilities": capabilities if capabilities is not None else ["restart_play_mode"],
    })
    return directory


def ready_result(directory: Path, *, ticket: str = "t-1", run_id: str = RUN,
                 from_generation: str = G0, to_generation: str = G1) -> None:
    write_json(directory / "result.json", {
        "version": 1, "run_id": run_id, "ticket_id": ticket, "phase": "ready",
        "from_generation": from_generation, "to_generation": to_generation,
        "entered_edit_mode_utc": (NOW + timedelta(seconds=1)).isoformat(),
        "entered_play_mode_utc": (NOW + timedelta(seconds=2)).isoformat(),
        "ready_utc": (NOW + timedelta(seconds=3)).isoformat(),
        "scene_path": SCENE, "scene_handle": 8, "failure_reason": None,
    })


def publish_ready_state(directory: Path, *, generation: str = G1, run_id: str = RUN) -> None:
    write_json(directory / "host-state.json", {
        "version": 1, "run_id": run_id, "phase": "ready", "generation": generation, "scene_path": SCENE,
        "scene_handle": 8, "ready_utc": (NOW + timedelta(seconds=4)).isoformat(), "error": None,
        "capabilities": ["restart_play_mode"],
    })


def publish_then_answer(clock: Clock, directory: Path, *, ready: bool = True) -> None:
    """The host's transition, published during the runner's wait, as the real host does."""
    def publish() -> None:
        ready_result(directory)
        if ready:
            publish_ready_state(directory)
    clock.after_sleep = publish


def make(tmp_path: Path, *, clock: Clock | None = None, connect: Any = None,
         directory: Path | None = None, poll_interval_s: float | None = None,
         monotonic: Any = None, sleep: Any = None) -> tuple[SessionControl, Path, Clock]:
    project = tmp_path / "game"
    project.mkdir(exist_ok=True)
    directory = directory or control_directory(tmp_path, project=project)
    clock = clock or Clock()
    host = load_host_session(directory, project=project)
    connect = connect or (lambda path: _resolved(FakeClient(observation())))
    adapter = SessionControl(
        host, project=project, clock=clock, connect=connect,
        sleep=sleep if sleep is not None else clock.sleep,
        poll_interval_s=poll_interval_s if poll_interval_s is not None else control_module.DEFAULT_POLL_S,
        monotonic=monotonic)
    return adapter, directory, clock


class Manual:
    """A monotonic clock that only a test's fake sleep moves, so no case waits in real time."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value



def _resolved(value: Any) -> Any:
    async def answer() -> Any:
        return value
    return answer()


def dispatch(adapter: SessionControl, *, ticket: str = "t-1", previous: Any = None,
             deadline_s: float = 60.0, release_verified: bool = True,
             from_generation: str = G0) -> Any:
    return asyncio.run(adapter.restart(
        ticket_id=ticket, from_generation=from_generation,
        previous_endpoint=previous if previous is not None
        else SimpleNamespace(token="old-token", started_utc=NOW - timedelta(minutes=1)),
        deadline_s=deadline_s, release_verified=release_verified,
        previous_session="old-bridge-session"))


def new_lock(monkeypatch: pytest.MonkeyPatch, *, token: str = "new-token",
             started: datetime = NOW + timedelta(seconds=2)) -> None:
    monkeypatch.setattr(control_module, "read_project_lock", lambda project: endpoint(token, started))


# --- the happy path -------------------------------------------------------------------------


def test_a_ready_result_needs_the_new_generation_lock_and_first_observation(tmp_path, monkeypatch):
    adapter, directory, clock = make(tmp_path)
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.ready
    assert outcome.ticket_id == "t-1" and outcome.to_generation == G1
    assert outcome.checks == 2 and outcome.release_verified is True
    assert outcome.scene_path == SCENE
    assert isinstance(outcome.client, FakeClient) and not outcome.client.closed
    assert outcome.observation.scene == "Game"
    assert not (directory / "cancel.json").exists()
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    assert {key: request[key] for key in ("version", "run_id", "ticket_id", "operation", "from_generation")} == {
        "version": 1, "run_id": RUN, "ticket_id": "t-1", "operation": "restart_play_mode",
        "from_generation": G0}
    assert _utc(request["deadline_utc"]) == NOW + timedelta(seconds=60)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_the_operation_deadline_is_the_smaller_of_wall_time_and_sixty_seconds(tmp_path, monkeypatch):
    adapter, directory, clock = make(tmp_path)
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)

    dispatch(adapter, deadline_s=10_000.0)

    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    assert _utc(request["deadline_utc"]) == NOW + timedelta(seconds=60)


def test_a_shorter_remaining_wall_time_bounds_the_operation(tmp_path, monkeypatch):
    adapter, directory, clock = make(tmp_path)
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)

    dispatch(adapter, deadline_s=7.5)

    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    assert _utc(request["deadline_utc"]) == NOW + timedelta(seconds=7.5)


# --- fail closed ----------------------------------------------------------------------------


def test_a_result_for_another_ticket_is_refused_before_any_request(tmp_path):
    """A terminal record this run cannot attribute is refused before a ticket is published."""
    adapter, directory, clock = make(tmp_path)
    ready_result(directory, ticket="some-other-ticket")

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed
    assert "which this run did not dispatch" in outcome.reason
    assert outcome.checks == 0
    assert not (directory / "request.json").exists()   # nothing was dispatched for it to answer
    assert not (directory / "cancel.json").exists()    # and no operation was in flight to cancel
    assert not clock.slept


def test_a_result_for_another_run_is_refused_before_any_request(tmp_path):
    adapter, directory, _ = make(tmp_path)
    ready_result(directory, run_id="another-run")

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed and "names a different run" in outcome.reason
    assert not (directory / "request.json").exists()
    assert not (directory / "cancel.json").exists()


def test_a_failed_result_fails_closed_without_cancelling(tmp_path):
    adapter, directory, _ = make(tmp_path)
    write_json(directory / "result.json", {
        "version": 1, "run_id": RUN, "ticket_id": "t-1", "phase": "failed", "from_generation": G0,
        "to_generation": None, "entered_edit_mode_utc": None, "entered_play_mode_utc": None,
        "ready_utc": None, "scene_path": None, "scene_handle": None,
        "failure_reason": "the scene did not load",
    })

    outcome = dispatch(adapter)

    assert (outcome.status, outcome.reason) == (OperationStatus.failed, "the scene did not load")
    assert outcome.client is None and outcome.observation is None
    assert not (directory / "cancel.json").exists()


def test_a_cancelled_result_is_reported_as_cancelled(tmp_path):
    adapter, directory, _ = make(tmp_path)
    write_json(directory / "result.json", {
        "version": 1, "run_id": RUN, "ticket_id": "t-1", "phase": "cancelled", "from_generation": G0,
        "to_generation": None, "entered_edit_mode_utc": None, "entered_play_mode_utc": None,
        "ready_utc": None, "scene_path": None, "scene_handle": None, "failure_reason": "supervisor stop",
    })

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.cancelled and outcome.reason == "supervisor stop"


def test_an_unverified_release_never_publishes_a_request(tmp_path):
    adapter, directory, clock = make(tmp_path)

    outcome = dispatch(adapter, release_verified=False)

    assert outcome.status is OperationStatus.failed and "release was not verified" in outcome.reason
    assert not (directory / "request.json").exists()
    assert not (directory / "cancel.json").exists()
    assert clock.slept == []


def test_a_concurrent_request_already_published_is_refused(tmp_path, monkeypatch):
    adapter, directory, _ = make(tmp_path)
    existing = {"version": 1, "run_id": RUN, "ticket_id": "earlier", "operation": "restart_play_mode",
                "from_generation": G0, "deadline_utc": (NOW + timedelta(seconds=5)).isoformat()}
    write_json(directory / "request.json", existing)
    new_lock(monkeypatch)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed and "concurrent tickets are refused" in outcome.reason
    assert json.loads((directory / "request.json").read_text()) == existing


def test_a_stale_generation_never_dispatches(tmp_path):
    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project, generation=str(uuid.uuid4()))
    adapter, directory, _ = make(tmp_path, directory=directory)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed and "not the one S1 restarted" in outcome.reason
    assert not (directory / "request.json").exists()


def test_a_ready_result_whose_lock_is_still_the_old_endpoint_times_out(tmp_path, monkeypatch):
    adapter, directory, clock = make(tmp_path)
    publish_then_answer(clock, directory)
    monkeypatch.setattr(control_module, "read_project_lock",
                        lambda project: endpoint("old-token", NOW + timedelta(seconds=2)))

    outcome = dispatch(adapter, deadline_s=0.5)

    assert outcome.status is OperationStatus.timeout
    assert "no ready result" in outcome.reason
    assert outcome.checks >= 2 and clock.slept
    assert (directory / "cancel.json").is_file()


def test_a_first_observation_that_is_not_a_new_play_session_is_fatal(tmp_path, monkeypatch):
    client = FakeClient(observation(playing=False))
    adapter, directory, clock = make(tmp_path, connect=lambda project: _resolved(client))
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed
    assert "does not report a running play session" in outcome.reason
    assert client.closed
    assert (directory / "cancel.json").is_file()


def test_a_ready_result_before_the_host_is_ready_at_the_new_generation_is_fatal(tmp_path, monkeypatch):
    adapter, directory, clock = make(tmp_path)
    clock.after_sleep = lambda: (ready_result(directory), publish_ready_state(directory, generation=G0))
    new_lock(monkeypatch)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed
    assert "not ready at the result's new generation" in outcome.reason


def test_a_second_distinct_ticket_after_a_completed_one_is_archived_and_allowed(tmp_path, monkeypatch):
    """Two S1 selections are two verified cycles: distinct tickets, one at a time, no budget refill."""
    adapter, directory, clock = make(tmp_path)
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)
    first = dispatch(adapter)
    assert first.status is OperationStatus.ready and first.to_generation == G1

    # The host is now ready at the generation the first ticket produced; the next selection names it.
    publish_ready_state(directory, generation=G1)
    second_client = FakeClient(observation(), connected=endpoint("second-token", NOW + timedelta(seconds=6)))
    monkeypatch.setattr(control_module, "read_project_lock",
                        lambda project: endpoint("second-token", NOW + timedelta(seconds=6)))
    adapter._connect = lambda project: _resolved(second_client)  # noqa: SLF001 - the fake host's next bridge
    clock.after_sleep = lambda: (ready_result(directory, ticket="t-2", from_generation=G1,
                                              to_generation=G2),
                                 publish_ready_state(directory, generation=G2))
    second = dispatch(adapter, ticket="t-2", from_generation=G1,
                      previous=SimpleNamespace(token="new-token", started_utc=NOW + timedelta(seconds=2)))

    assert second.status is OperationStatus.ready and second.to_generation == G2
    assert json.loads((directory / "request.json").read_text())["ticket_id"] == "t-2"
    archived = sorted((directory / "archive").iterdir())
    assert [folder.name.split("-", 1)[1] for folder in archived] == ["t-1"]
    assert (archived[0] / "request.json").is_file() and (archived[0] / "result.json").is_file()
    assert json.loads((archived[0] / "request.json").read_text())["ticket_id"] == "t-1"


def test_a_replayed_ticket_is_refused_and_never_dispatched_again(tmp_path, monkeypatch):
    adapter, directory, clock = make(tmp_path)
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)
    assert dispatch(adapter).status is OperationStatus.ready

    publish_ready_state(directory, generation=G1)
    before = (directory / "request.json").read_bytes()
    waited_before = list(clock.slept)
    again = dispatch(adapter, ticket="t-1", from_generation=G1)

    assert again.status is OperationStatus.failed and "already dispatched" in again.reason
    assert (directory / "request.json").read_bytes() == before
    assert clock.slept == waited_before


def test_a_timeout_publishes_cancel_and_a_later_ready_cannot_publish_success(tmp_path, monkeypatch):
    adapter, directory, _ = make(tmp_path)

    first = dispatch(adapter, deadline_s=0.5)
    assert first.status is OperationStatus.timeout and (directory / "cancel.json").is_file()

    ready_result(directory)     # the host transitions after the runner stopped waiting
    new_lock(monkeypatch)
    again = dispatch(adapter, deadline_s=60.0)

    assert again.status is OperationStatus.failed and "already dispatched" in again.reason


def test_a_zero_deadline_never_publishes_a_request(tmp_path):
    """With no operation time left the adapter times out before dispatch: no ticket, no cancel."""
    adapter, directory, clock = make(tmp_path)

    outcome = dispatch(adapter, deadline_s=0.0)

    assert outcome.status is OperationStatus.timeout
    assert "no operation time remains" in outcome.reason
    assert outcome.checks == 0
    assert not (directory / "request.json").exists()
    assert not (directory / "cancel.json").exists()      # nothing was in flight to cancel
    assert clock.slept == []


def test_a_ready_record_written_after_the_bound_is_late_and_never_succeeds(tmp_path, monkeypatch):
    """The operation ends on its own bound even when the host publishes a ready record as it expires."""
    manual, slept = Manual(), []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        manual.value += seconds
        if len(slept) == 2:                                  # the host transitions as the bound passes
            ready_result(directory)
            publish_ready_state(directory)

    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project)
    adapter, directory, _ = make(tmp_path, directory=directory, monotonic=manual, sleep=sleep,
                                 poll_interval_s=0.25)
    new_lock(monkeypatch)

    outcome = dispatch(adapter, deadline_s=0.5)

    assert outcome.status is OperationStatus.timeout and "before the operation deadline" in outcome.reason
    assert outcome.waited_s == 0.5
    assert (directory / "result.json").is_file() and (directory / "cancel.json").is_file()
    assert not (directory / "archive").exists()


def test_a_wall_clock_rollback_cannot_extend_the_operation(tmp_path, monkeypatch):
    """The bound is monotonic: moving the wall clock backwards gives the operation no extra time."""
    manual, clock, slept = Manual(), Clock(), []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        manual.value += seconds
        if len(slept) == 1:
            clock.now -= timedelta(hours=1)                  # a clock correction during the wait
        assert len(slept) <= 50, "the operation bound followed the rolled-back wall clock"

    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project)
    adapter, directory, _ = make(tmp_path, clock=clock, directory=directory, monotonic=manual,
                                 sleep=sleep, poll_interval_s=0.25)
    new_lock(monkeypatch)

    outcome = dispatch(adapter, deadline_s=1.0)

    assert outcome.status is OperationStatus.timeout
    assert outcome.waited_s == 1.0 and sum(slept) == 1.0    # the rollback bought no extra quarter
    assert (directory / "cancel.json").is_file()


def test_a_hung_connect_ends_at_the_operation_bound(tmp_path, monkeypatch):
    """A connection that never answers is cut at the remaining bound, not left waiting."""
    manual, published = Manual(), []

    async def sleep(seconds: float) -> None:
        manual.value += seconds
        if not published:
            published.append(True)
            ready_result(directory)
            publish_ready_state(directory)

    async def hanging_connect(_project: Path) -> Any:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project)
    adapter, directory, _ = make(tmp_path, connect=hanging_connect, directory=directory,
                                 monotonic=manual, sleep=sleep, poll_interval_s=0.05)
    new_lock(monkeypatch)

    outcome = dispatch(adapter, deadline_s=0.2)

    assert outcome.status is OperationStatus.timeout and "before the operation deadline" in outcome.reason
    assert (directory / "cancel.json").is_file()


def test_a_hung_first_observation_is_cut_and_its_client_closed(tmp_path, monkeypatch):
    """A handshake that never observes fails closed at the bound and the unadopted client is closed."""
    manual, published = Manual(), []
    client = HungClient()

    async def sleep(seconds: float) -> None:
        manual.value += seconds
        if not published:
            published.append(True)
            ready_result(directory)
            publish_ready_state(directory)

    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project)
    adapter, directory, _ = make(tmp_path, connect=lambda project: _resolved(client), directory=directory,
                                 monotonic=manual, sleep=sleep, poll_interval_s=0.05)
    new_lock(monkeypatch)

    outcome = dispatch(adapter, deadline_s=0.2)

    assert outcome.status is OperationStatus.timeout
    assert outcome.client is None and client.closed
    assert (directory / "cancel.json").is_file()


def test_an_external_cancellation_with_a_request_in_flight_publishes_cancel(tmp_path, monkeypatch):
    """Cancelling the awaiting task publishes the cancel marker before the cancellation propagates."""
    adapter, directory, _ = make(tmp_path, sleep=asyncio.sleep, poll_interval_s=0.02)
    new_lock(monkeypatch)

    async def scenario() -> None:
        task = asyncio.create_task(adapter.restart(
            ticket_id="t-1", from_generation=G0,
            previous_endpoint=SimpleNamespace(token="old-token", started_utc=NOW - timedelta(minutes=1)),
            deadline_s=30.0, release_verified=True, previous_session="old-bridge-session"))
        for _ in range(1_000):
            if (directory / "request.json").exists():
                break
            await asyncio.sleep(0.002)
        assert (directory / "request.json").exists()          # the ticket was published and is in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert (directory / "cancel.json").is_file()


def test_a_malformed_result_after_dispatch_fails_closed_and_cancels(tmp_path, monkeypatch):
    """An unreadable terminal record is a fatal protocol failure, not a wait that goes on."""
    manual, slept = Manual(), []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        manual.value += seconds
        if len(slept) == 1:
            (directory / "result.json").write_text("{not json", encoding="utf-8")

    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project)
    adapter, directory, _ = make(tmp_path, directory=directory, monotonic=manual, sleep=sleep,
                                 poll_interval_s=0.05)
    new_lock(monkeypatch)

    outcome = dispatch(adapter, deadline_s=1.0)

    assert outcome.status is OperationStatus.failed
    assert "result.json is not a valid record" in outcome.reason
    assert outcome.checks >= 2 and (directory / "cancel.json").is_file()


def test_a_changed_catalog_pin_is_refused_before_dispatch(tmp_path):
    """The pinned catalog digest is re-read at dispatch; a changed pin is not the run's host."""
    adapter, directory, _ = make(tmp_path)
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    config["catalog_sha256"] = "b" * 64
    write_json(directory / "config.json", config)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed
    assert "pins a different catalog digest" in outcome.reason
    assert not (directory / "request.json").exists()


def test_a_client_connected_to_a_different_endpoint_than_the_lock_is_refused(tmp_path, monkeypatch):
    """The lock read here is the transport authority: a client on a newer lock is never adopted."""
    racer = FakeClient(observation(), connected=endpoint("newer-token", NOW + timedelta(seconds=3)))
    adapter, directory, clock = make(tmp_path, connect=lambda project: _resolved(racer))
    monkeypatch.setattr(control_module, "read_project_lock",
                        lambda project: endpoint("lock-token", NOW + timedelta(seconds=2)))
    publish_then_answer(clock, directory)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed
    assert "different endpoint than the lock read here" in outcome.reason
    assert racer.closed
    assert (directory / "cancel.json").is_file()


def test_an_unexpected_connect_failure_fails_closed_and_cancels(tmp_path, monkeypatch):
    """An exception the adapter does not model is reported as a failed operation, never retried."""
    async def exploding_connect(_project: Path) -> Any:
        raise RuntimeError("transport exploded")

    adapter, directory, clock = make(tmp_path, connect=exploding_connect)
    new_lock(monkeypatch)
    publish_then_answer(clock, directory)

    outcome = dispatch(adapter)

    assert outcome.status is OperationStatus.failed
    assert "host restart failed: RuntimeError" in outcome.reason and "transport exploded" in outcome.reason
    assert (directory / "cancel.json").is_file()


# --- reading the control directory ----------------------------------------------------------


def test_load_host_session_refuses_a_different_project(tmp_path):
    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project)

    with pytest.raises(SessionControlError, match="belongs to"):
        load_host_session(directory, project=tmp_path / "another")


def test_load_host_session_refuses_a_host_that_is_not_ready(tmp_path):
    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project, phase="starting", generation=None)

    with pytest.raises(SessionControlError, match="not ready"):
        load_host_session(directory, project=project)


def test_load_host_session_refuses_a_missing_directory(tmp_path):
    with pytest.raises(SessionControlError, match="not a directory"):
        load_host_session(tmp_path / "missing", project=tmp_path)


def test_host_state_capabilities_gate_the_restart_choice(tmp_path):
    project = tmp_path / "game"
    project.mkdir()
    directory = control_directory(tmp_path, project=project, capabilities=[])
    host = load_host_session(directory, project=project)

    assert host.advertises_restart is False and host.capabilities == ()
