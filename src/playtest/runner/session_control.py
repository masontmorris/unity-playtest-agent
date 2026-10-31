"""The Editor-owned host mailbox: one explicit Play Mode restart outside decision turns.

A live host writes `config.json` and `host-state.json` into a per-run control directory outside the
game project; the runner writes one `request.json` after a verified release and then waits, with
bounded local file checks only, for the host's one terminal `result.json`. No S1 call, context turn or
synthetic wait choice is made while it waits.

Fail closed: a request is written only after the input release is verified, one operation is outstanding
at a time, a result for another run/ticket/generation is a failure, and a ready result is success only
when the host state, a distinct new generation, a fresh bridge lock/token/session and a valid first
observation all agree. The operation's bound is a monotonic deadline: the smaller of the run's remaining
wall time, sixty seconds and the host's own absolute deadline. A timeout writes `cancel.json`; a late
transition can never publish success for a cancelled ticket and is never read again. A completed ticket
may be followed by a new explicit ticket: its request and result are archived first. The adapter never
retries an operation, and a ticket that ended in a timeout is never retried or replaced.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from playtest.contracts.loop import OperationStatus
from playtest.contracts.session_control import (
    HostPhase,
    HostState,
    RestartCancel,
    RestartPhase,
    RestartRequest,
    RestartResult,
    SessionControlConfig,
    is_generation,
)
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.bridge_lock import (
    BridgeEndpoint,
    BridgeLockError,
    read_project_lock,
)
from playtest.core.ports import RestartOutcome

CONTROL_DIRECTORY_VARIABLE = "UPA_SESSION_CONTROL_DIRECTORY"
RESTART_CAPABILITY = "restart_play_mode"
CONFIG_NAME = "config.json"
HOST_STATE_NAME = "host-state.json"
REQUEST_NAME = "request.json"
RESULT_NAME = "result.json"
CANCEL_NAME = "cancel.json"
ARCHIVE_DIRECTORY = "archive"
DEFAULT_POLL_S = 0.25
MAX_OPERATION_S = 60.0


class SessionControlError(Exception):
    """The control directory, its records or their identities are not usable for this run."""


@dataclass(frozen=True)
class HostSession:
    """One validated control directory: the run identity and the host's last published state."""

    directory: Path
    config: SessionControlConfig
    state: HostState

    @property
    def run_id(self) -> str:
        return self.config.run_id

    @property
    def generation(self) -> str | None:
        return self.state.generation

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.state.capabilities)

    @property
    def advertises_restart(self) -> bool:
        return self.state.supports(RESTART_CAPABILITY)


def control_directory(environ: Mapping[str, str] | None = None) -> Path | None:
    """The configured per-run control directory, or `None` when the runner is not host-owned."""
    raw = (environ if environ is not None else os.environ).get(CONTROL_DIRECTORY_VARIABLE, "").strip()
    return Path(raw) if raw else None


def load_host_session(
    directory: str | Path,
    *,
    project: str | Path | None = None,
    require_ready: bool = True,
) -> HostSession:
    """Read and validate the control records a live host wrote before it started the runner.

    A missing, malformed or mismatched directory is an error: the runner never invents a host, a run id
    or a generation. A ready state must already name its generation and scene, as the contract requires.
    """
    path = Path(directory).resolve()
    if not path.is_dir():
        raise SessionControlError(f"the session control directory is not a directory: {path}")
    config = _read_model(path / CONFIG_NAME, SessionControlConfig)
    if config is None:
        raise SessionControlError(f"the session control directory has no {CONFIG_NAME}: {path}")
    state = _read_model(path / HOST_STATE_NAME, HostState)
    if state is None:
        raise SessionControlError(f"the session control directory has no {HOST_STATE_NAME}: {path}")
    if state.run_id != config.run_id:
        raise SessionControlError(
            f"host-state run_id {state.run_id!r} differs from config run_id {config.run_id!r}")
    if project is not None:
        configured = Path(config.project_path).resolve()
        if configured != Path(project).resolve():
            raise SessionControlError(
                f"the control directory belongs to {configured}, not this project {Path(project).resolve()}")
    if require_ready and (state.phase is not HostPhase.ready or state.generation is None):
        raise SessionControlError(
            f"the host is {state.phase.value}, not ready at a known generation; the runner starts only after that")
    return HostSession(path, config, state)


def _read_model(path: Path, model: type[Any]) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SessionControlError(f"cannot read {path.name}: {exc}") from exc
    try:
        return model.model_validate_json(text)
    except ValidationError as exc:
        raise SessionControlError(f"{path.name} is not a valid record: {exc.errors()[0]['msg']}") from exc


def _write_atomic(path: Path, model: Any) -> None:
    """Publish one record by writing a same-directory temp file and renaming it over the target."""
    payload = model.model_dump_json() + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class _Fatal(Exception):
    """A terminal protocol or evidence failure; the operation fails closed at once."""


class _Transient(Exception):
    """The new bridge is not reachable or not fresh yet; retry within the operation deadline only."""


class _Expired(Exception):
    """The operation's monotonic bound passed; nothing later may be accepted as its success."""


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    return max(0.0, deadline - monotonic())


class SessionControl:
    """One run's mailbox adapter. `restart()` is the loop's `RestartPort`."""

    def __init__(
        self,
        session: HostSession,
        *,
        project: str | Path,
        poll_interval_s: float = DEFAULT_POLL_S,
        max_operation_s: float = MAX_OPERATION_S,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        connect: Callable[[Path], Awaitable[BridgeClient]] | None = None,
    ) -> None:
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be a finite positive number")
        if not math.isfinite(max_operation_s) or max_operation_s <= 0:
            raise ValueError("max_operation_s must be a finite positive number")
        self._session = session
        self._directory = session.directory
        self._project = Path(project).resolve()
        self._poll = poll_interval_s
        self._max_operation_s = max_operation_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep
        self._connect = connect or BridgeClient.connect_from_project
        self._dispatched: dict[str, float] = {}  # ticket -> the monotonic instant it was dispatched
        self._terminal: dict[str, RestartResult] = {}  # one terminal record per completed ticket
        self._abandoned: set[str] = set()  # ended locally (timeout/failure) with no terminal record
        self._cancelled: set[str] = set()
        self._inflight: str | None = None
        self._archived = 0
        self._archive: dict[str, Path] = {}  # ticket -> the one folder its superseded records were copied to

    @property
    def run_id(self) -> str:
        return self._session.run_id

    @property
    def generation(self) -> str | None:
        return self._session.generation

    @property
    def advertises_restart(self) -> bool:
        return self._session.advertises_restart

    def state(self) -> HostState:
        state = _read_model(self._directory / HOST_STATE_NAME, HostState)
        if state is None:
            raise SessionControlError(f"{HOST_STATE_NAME} disappeared from {self._directory}")
        return state

    async def restart(
        self,
        *,
        ticket_id: str,
        from_generation: str,
        previous_endpoint: Any | None,
        deadline_s: float,
        release_verified: bool | None,
        previous_session: str | None = None,
    ) -> RestartOutcome:
        """Dispatch one ticket and await its terminal result; never retry or accept a late one.

        One operation is outstanding at a time. A later explicit selection may dispatch a new ticket only
        after the previous one ended with a terminal record; that record is archived before the request
        file is superseded. A ticket that ended in a local timeout is never replaced: there is no retry.
        """
        started = self._monotonic()
        if release_verified is not True:
            return self._failed(ticket_id, from_generation, "input release was not verified", started,
                                release_verified=release_verified)
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            return self._failed(ticket_id, from_generation, "the restart ticket id is empty", started,
                                release_verified=True)
        if not is_generation(from_generation):
            return self._failed(ticket_id, from_generation, "the current play generation is not canonical",
                                started, release_verified=True)
        if self._inflight is not None:
            return self._failed(
                ticket_id, from_generation,
                f"restart ticket {self._inflight} is still in flight: concurrent tickets are refused",
                started, release_verified=True)
        if ticket_id in self._dispatched:
            return self._failed(ticket_id, from_generation,
                                f"restart ticket {ticket_id!r} was already dispatched: it is never replayed",
                                started, release_verified=True)
        if self._dispatched:
            last = next(reversed(self._dispatched))
            if last not in self._terminal:
                return self._failed(
                    ticket_id, from_generation,
                    f"restart ticket {last!r} has no terminal result: a second ticket is refused",
                    started, release_verified=True)
        try:
            config, _ = self._recheck(from_generation)
        except (SessionControlError, ValidationError) as exc:
            return self._failed(ticket_id, from_generation, str(exc), started, release_verified=True)
        limit_s = self._limit(deadline_s, self._clock(), config)
        if limit_s <= 0:
            return RestartOutcome(
                status=OperationStatus.timeout, ticket_id=ticket_id, from_generation=from_generation,
                reason="no operation time remains before the run or host deadline",
                checks=0, waited_s=0.0, release_verified=True)
        try:
            superseded = self._outstanding_request(config.run_id, ticket_id)
        except SessionControlError as exc:
            return self._failed(ticket_id, from_generation, str(exc), started, release_verified=True)
        if superseded is not None:
            return self._failed(
                ticket_id, from_generation,
                f"restart ticket {superseded!r} has no terminal result: a second ticket is refused",
                started, release_verified=True)
        deadline = started + limit_s
        request = RestartRequest(
            version=1, run_id=config.run_id, ticket_id=ticket_id, operation="restart_play_mode",
            from_generation=from_generation,
            deadline_utc=self._clock() + timedelta(seconds=limit_s))
        self._inflight = ticket_id
        self._dispatched[ticket_id] = started
        try:
            _write_atomic(self._directory / REQUEST_NAME, request)
        except OSError as exc:
            self._inflight = None
            self._dispatched.pop(ticket_id, None)
            return self._failed(ticket_id, from_generation,
                                f"the restart request could not be published: {exc}",
                                started, release_verified=True)
        checks = 0
        try:
            while True:
                checks += 1
                try:
                    outcome = await self._attempt(
                        ticket_id, from_generation, previous_endpoint, previous_session, deadline, checks,
                        started)
                except _Expired:
                    raise
                except _Transient:
                    outcome = None
                if outcome is not None:
                    return outcome
                if _remaining(deadline, self._monotonic) <= 0:
                    raise _Expired
                await self._sleep(min(self._poll, _remaining(deadline, self._monotonic)))
        except _Expired:
            self._cancel(ticket_id)
            self._abandoned.add(ticket_id)
            return RestartOutcome(
                status=OperationStatus.timeout, ticket_id=ticket_id, from_generation=from_generation,
                reason=f"no ready result for ticket {ticket_id} before the operation deadline",
                checks=checks, waited_s=self._waited(started), release_verified=True)
        except _Fatal as exc:
            self._cancel(ticket_id)
            self._abandoned.add(ticket_id)
            return self._failed(ticket_id, from_generation, str(exc), started, checks=checks,
                                release_verified=True)
        except (SessionControlError, ValidationError) as exc:
            self._cancel(ticket_id)
            self._abandoned.add(ticket_id)
            return self._failed(ticket_id, from_generation, str(exc), started, checks=checks,
                                release_verified=True)
        except asyncio.CancelledError:
            self._cancel(ticket_id)
            raise
        except Exception as exc:  # noqa: BLE001 - a failed operation is reported, never retried
            self._cancel(ticket_id)
            self._abandoned.add(ticket_id)
            return self._failed(ticket_id, from_generation,
                                f"host restart failed: {type(exc).__name__}: {exc}", started,
                                checks=checks, release_verified=True)
        finally:
            if self._inflight == ticket_id:
                self._inflight = None

    # --- one check --------------------------------------------------------------------------

    async def _attempt(
        self,
        ticket_id: str,
        from_generation: str,
        previous_endpoint: Any | None,
        previous_session: str | None,
        deadline: float,
        checks: int,
        started: float,
    ) -> RestartOutcome | None:
        if _remaining(deadline, self._monotonic) <= 0:
            raise _Expired
        result = _read_model(self._directory / RESULT_NAME, RestartResult)
        if result is None:
            return None
        if result.ticket_id != ticket_id:
            if result.ticket_id in self._terminal:
                return None  # a known earlier ticket's record is not evidence about this one
            raise _Fatal(f"result ticket {result.ticket_id!r} is not the dispatched ticket {ticket_id!r}")
        if result.run_id != self._session.run_id:
            raise _Fatal(f"result run_id {result.run_id!r} is not this run")
        if result.from_generation != from_generation:
            raise _Fatal("result from_generation is not the generation this operation restarted")
        if result.phase in (RestartPhase.failed, RestartPhase.cancelled):
            self._terminal[ticket_id] = result
            return RestartOutcome(
                status=(OperationStatus.failed if result.phase is RestartPhase.failed
                        else OperationStatus.cancelled),
                ticket_id=ticket_id, from_generation=from_generation, to_generation=result.to_generation,
                reason=result.failure_reason, checks=checks, waited_s=self._waited(started),
                release_verified=True)
        if result.phase is not RestartPhase.ready:
            return None
        if not (result.entered_edit_mode_utc <= result.entered_play_mode_utc <= result.ready_utc):
            raise _Fatal("the restart events are not in edit-mode, play-mode, ready order")
        if _remaining(deadline, self._monotonic) <= 0:
            raise _Expired  # a ready record that arrived after the bound is late, never accepted
        state = self.state()
        if state.run_id != self._session.run_id:
            raise _Fatal("the host state belongs to another run")
        if state.phase is not HostPhase.ready or state.generation != result.to_generation:
            raise _Fatal("the host is not ready at the result's new generation")
        if state.scene_path != result.scene_path:
            raise _Fatal("the host's ready scene differs from the result's scene")
        if state.scene_handle != result.scene_handle:
            raise _Fatal("the host's ready scene handle differs from the result's")
        if state.ready_utc is None or state.ready_utc < result.ready_utc:
            raise _Fatal("the host's ready timestamp is older than the result's")
        client, observation = await self._fresh_client(
            result, previous_endpoint, previous_session, deadline)
        self._terminal[ticket_id] = result
        return RestartOutcome(
            status=OperationStatus.ready, ticket_id=ticket_id, from_generation=from_generation,
            to_generation=result.to_generation, checks=checks, waited_s=self._waited(started),
            release_verified=True, client=client, observation=observation, scene_path=result.scene_path)

    async def _fresh_client(
        self, result: RestartResult, previous_endpoint: Any | None, previous_session: str | None,
        deadline: float,
    ) -> tuple[BridgeClient, Any]:
        """A new endpoint, session and first observation for the new generation.

        The lock is the transport authority: the client must have connected through exactly the lock read
        here, and that lock, the client session and the first observation must all be distinct from the
        pre-restart ones. An old lock or image never counts, and a handshake that completes after the bound
        is closed unadopted.
        """
        if previous_endpoint is None:
            raise _Fatal("the previous bridge endpoint is unknown, so a new one cannot be proven")
        try:
            endpoint = read_project_lock(self._project)
        except BridgeLockError as exc:
            raise _Transient(str(exc)) from exc
        assert result.entered_play_mode_utc is not None
        previous_token = getattr(previous_endpoint, "token", None)
        previous_started = getattr(previous_endpoint, "started_utc", None)
        if endpoint.token == previous_token or endpoint.started_utc == previous_started:
            raise _Transient("the bridge lock is still the pre-restart endpoint")
        if endpoint.started_utc < result.entered_play_mode_utc:
            raise _Transient("the bridge lock predates the new play instance")
        if _remaining(deadline, self._monotonic) <= 0:
            raise _Expired
        try:
            client = await asyncio.wait_for(
                self._connect(self._project), timeout=_remaining(deadline, self._monotonic))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise _Expired from exc
        except (OSError, BridgeError) as exc:
            raise _Transient(f"the new bridge is not ready: {exc}") from exc
        try:
            connected = getattr(client, "endpoint", None)
            if connected is None:
                raise _Fatal("the new client did not connect through the project lock")
            if not _same_endpoint(connected, endpoint):
                raise _Fatal("the new client is connected to a different endpoint than the lock read here")
            session_id = getattr(client, "session", None)
            if previous_session is not None and session_id == previous_session:
                raise _Fatal("the new client reuses the pre-restart bridge session")
            if _remaining(deadline, self._monotonic) <= 0:
                raise _Expired
            try:
                observation = await asyncio.wait_for(
                    client.observe_once(), timeout=_remaining(deadline, self._monotonic))
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise _Expired from exc
            except (OSError, BridgeError) as exc:
                raise _Transient(f"the new bridge is not ready: {exc}") from exc
            problem = _observation_problem(observation, result.scene_path, session_id=session_id)
            if problem is not None:
                raise _Fatal(problem)
            if _remaining(deadline, self._monotonic) <= 0:
                raise _Expired  # the handshake completed late; its client is closed, never adopted
            return client, observation
        except BaseException:
            await _close_quietly(client)
            raise

    # --- helpers ----------------------------------------------------------------------------

    def _recheck(self, from_generation: str) -> tuple[SessionControlConfig, HostState]:
        """Re-read the pinned directory; a changed run, project, catalog pin or generation is refused."""
        config = _read_model(self._directory / CONFIG_NAME, SessionControlConfig)
        if config is None:
            raise SessionControlError("config.json disappeared from the control directory")
        pinned = self._session.config
        if config.run_id != pinned.run_id:
            raise SessionControlError("config.json names a different run")
        if Path(config.project_path).resolve() != self._project:
            raise SessionControlError("config.json names a different project")
        if Path(config.catalog_path).resolve() != Path(pinned.catalog_path).resolve():
            raise SessionControlError("config.json names a different pinned catalog")
        if config.catalog_sha256 != pinned.catalog_sha256:
            raise SessionControlError("config.json pins a different catalog digest")
        state = self.state()
        if state.run_id != config.run_id:
            raise SessionControlError("host-state.json names a different run than config.json")
        if not state.supports(RESTART_CAPABILITY):
            raise SessionControlError("the host no longer advertises restart_play_mode")
        if state.phase is not HostPhase.ready:
            raise SessionControlError(f"the host is {state.phase.value}, not ready at dispatch time")
        if state.generation != from_generation:
            raise SessionControlError("the host's current generation is not the one S1 restarted")
        return config, state

    def _outstanding_request(self, run_id: str, ticket_id: str) -> str | None:
        """Archive a completed ticket's records, then clear them so a new ticket may be dispatched.

        Only this adapter's own completed tickets are cleared. A request for a ticket this adapter
        dispatched but never saw terminate is reported back (the caller refuses the new ticket: there is
        no retry). A request or result for any other ticket is refused here, before a request is
        published, so a stale record can never be mistaken for the new ticket's outcome. A result that
        already names the ticket about to be dispatched is left for the first poll to judge.
        """
        request = _read_model(self._directory / REQUEST_NAME, RestartRequest)
        result = _read_model(self._directory / RESULT_NAME, RestartResult)
        if request is not None:
            if request.run_id != run_id:
                raise SessionControlError("request.json names a different run")
            if request.ticket_id in self._terminal:
                self._preserve(request.ticket_id)
                _unlink_quietly(self._directory / REQUEST_NAME)
            elif request.ticket_id in self._dispatched:
                return request.ticket_id
            else:
                raise SessionControlError(
                    f"restart ticket {request.ticket_id!r} is already published: concurrent tickets are refused")
        if result is not None:
            if result.run_id != run_id:
                raise SessionControlError("result.json names a different run")
            if result.ticket_id in self._terminal:
                self._preserve(result.ticket_id)
                _unlink_quietly(self._directory / RESULT_NAME)
            elif result.ticket_id != ticket_id:
                raise SessionControlError(
                    f"result.json names ticket {result.ticket_id!r}, which this run did not dispatch")
        return None

    def _preserve(self, ticket_id: str) -> Path:
        """Copy a superseded ticket's local records into the control directory's archive, before clearing.

        One ticket gets one folder: its request, result and cancel marker are archived together, so the
        evidence stays attributable after the live files are replaced by the next ticket's.
        """
        folder = self._archive.get(ticket_id)
        if folder is None:
            while True:
                candidate = self._directory / ARCHIVE_DIRECTORY / f"{self._archived:04d}-{ticket_id}"
                self._archived += 1
                try:
                    candidate.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    continue
                break
            folder = self._archive[ticket_id] = candidate
        for name in (REQUEST_NAME, RESULT_NAME, CANCEL_NAME):
            source = self._directory / name
            if source.is_file():
                shutil.copy2(source, folder / name)
        return folder

    def _limit(self, deadline_s: float, now_utc: datetime, config: SessionControlConfig) -> float:
        """The operation bound: the run's remaining wall time, sixty seconds and the host's own deadline."""
        if isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float)) \
                or not math.isfinite(float(deadline_s)):
            return 0.0
        limit = min(max(0.0, float(deadline_s)), self._max_operation_s)
        host_left = (config.deadline_utc - now_utc).total_seconds()
        if not math.isfinite(host_left):
            return 0.0
        return min(limit, max(0.0, host_left))

    def _waited(self, started: float) -> float:
        """Elapsed operation seconds from the monotonic clock, so a wall-clock rollback cannot extend it."""
        return max(0.0, self._monotonic() - started)

    def _failed(
        self, ticket_id: str, from_generation: str | None, reason: str, started: float,
        *, checks: int = 0, release_verified: bool | None = None,
    ) -> RestartOutcome:
        return RestartOutcome(
            status=OperationStatus.failed, ticket_id=ticket_id, from_generation=from_generation,
            reason=reason, checks=checks, waited_s=self._waited(started), release_verified=release_verified)

    def _cancel(self, ticket_id: str) -> None:
        """Publish the fail-closed cancel marker; a host that transitions later cannot publish success here."""
        if ticket_id in self._cancelled:
            return
        try:
            _write_atomic(self._directory / CANCEL_NAME, RestartCancel(
                version=1, run_id=self._session.run_id, ticket_id=ticket_id, operation="cancel_restart"))
        except OSError:
            return
        self._cancelled.add(ticket_id)


def _same_endpoint(left: Any, right: BridgeEndpoint) -> bool:
    """Whether the connected client names exactly the lock endpoint that was read and checked."""
    return (getattr(left, "token", None) == right.token
            and getattr(left, "pid", None) == right.pid
            and getattr(left, "port", None) == right.port
            and getattr(left, "protocol", None) == right.protocol
            and getattr(left, "started_utc", None) == right.started_utc)


async def _close_quietly(client: Any) -> None:
    close = getattr(client, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except BaseException:  # noqa: BLE001 - cleanup of a client that will never be adopted
        pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SessionControlError(f"cannot clear {path.name}: {exc}") from exc


def _observation_problem(observation: Any, scene_path: str | None, *, session_id: str | None = None) -> str | None:
    """Why the first post-restart observation is not valid evidence of a new play session, or None."""
    if observation.playing is not True:
        return "the first post-restart observation does not report a running play session"
    if observation.paused is not False:
        return "the first post-restart observation reports a paused play session"
    scene = observation.scene
    if not isinstance(scene, str) or not scene.strip():
        return "the first post-restart observation names no scene"
    if scene_path is not None and Path(scene_path).stem.casefold() != scene.casefold():
        return (f"the first post-restart observation is scene {scene!r}, "
                f"not the ready result's {Path(scene_path).stem!r}")
    measured = getattr(observation, "session", None)
    if measured is not None and session_id is not None and measured != session_id:
        return "the first post-restart observation belongs to another bridge session"
    return None
