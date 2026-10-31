"""In-process fake game: a protocol peer for the bridge client.

The fake game speaks the same JSONL protocol the real bridge will speak,
so the Day 2 invariants run in CI without Unity. It is a peer, not a mock
of the client: the tests exercise the production `BridgeClient` against it.

One generic entity with `pos`, `vel` and `counter`; an input log records
every applied action and every release. Supported manifest actions are
`shift` (move `pos` by a delta), `increment` (bump the counter) and `hold`
(a held input that lasts until superseded, expired or stopped).

Every message emitted here is built through the Pydantic contracts; nothing
is hand-written around the models.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from playtest.contracts.bridge import (
    ActRequest,
    BridgeError as WireError,
    ClockRequest,
    ExecutionRecord,
    ExecStatus,
    Hello,
    InputBackend,
    InspectRequest,
    InspectResult,
    InspectionCap,
    ObserveRequest,
    Observation,
    RejectReason,
    Status,
    StopRequest,
    StopResult,
    LockState,
)
from playtest.contracts.common import PROTOCOL, ClockMode

MAX_LINE_SIZE = 8 * 1024 * 1024
_HISTORY = 128


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_MISSING = object()


def _resolve(node: object, path: str) -> object:
    current = node
    for part in path.split("."):
        if not isinstance(current, dict) or current.get(part, _MISSING) is _MISSING:
            return _MISSING
        current = current[part]
    return current


class FakeGame:
    """An asyncio TCP server implementing the six bridge verbs."""

    def __init__(
        self,
        *,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        rate_hz: float = 20.0,
        clock_mode: ClockMode = ClockMode.realtime,
        clock_control: bool = True,
        inspect_supported: bool = True,
        inspect_silent: bool = False,
        inspect_delay_s: float = 0.0,
        reject_acts: RejectReason | None = None,
        action_run_s: float | None = None,
        action_stall: bool = False,
        aliases: dict[str, str] | None = None,
    ) -> None:
        env_token = os.environ.get("UPA_BRIDGE_TOKEN")
        self._token = token if token is not None else (env_token or "")
        self._host = host
        self._port = port
        self._rate_hz = rate_hz
        self._clock_mode = clock_mode
        self._clock_control = clock_control
        self._inspect_supported = inspect_supported
        self._inspect_silent = inspect_silent
        self._inspect_delay_s = inspect_delay_s
        self._reject_acts = reject_acts
        if action_run_s is not None and (
            isinstance(action_run_s, bool) or action_run_s < 0
        ):
            raise ValueError("action_run_s must be a non-negative number or None")
        self._action_run_s = action_run_s
        self._action_stall = action_stall
        # Declared action names that run one of this fake's own behaviors (`shift`, `increment`, `hold`): a
        # manifest names its declarations, and this world implements each name as one of its three. The
        # input log records the behavior that ran.
        self._aliases = dict(aliases or {})
        self._server: asyncio.Server | None = None
        self._session: str | None = None
        self.input_log: list[dict] = []
        self.inspect_requests: list[InspectRequest] = []
        self._lock = asyncio.Lock()
        self._pos = [0.0, 0.0, 0.0]
        self._vel = [0.0, 0.0, 0.0]
        self._counter = 0
        self._frame = 0
        self._playing = True
        self._paused = False
        self._time_scale = 1.0
        self._base_game_s = 0.0
        self._base_wall = time.monotonic()
        self._held: dict[str, str] = {}
        self._expiry: dict[str, asyncio.Task] = {}
        self._obs_history: dict[int, tuple[float, dict]] = {}
        self._action_tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def reject_acts(self) -> RejectReason | None:
        return self._reject_acts

    @reject_acts.setter
    def reject_acts(self, value: RejectReason | None) -> None:
        self._reject_acts = value

    def applied(self) -> list[str]:
        """Action names applied so far, in order."""
        return [e["action"] for e in self.input_log if e["event"] == "applied"]

    def releases(self) -> list[tuple[str, str]]:
        """(label, cause) pairs released so far, in order."""
        return [(e["label"], e["cause"]) for e in self.input_log if e["event"] == "released"]

    async def start(self) -> int:
        if not self._token:
            raise RuntimeError("the bridge refuses to start without a token")
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port, limit=MAX_LINE_SIZE
        )
        sockets = self._server.sockets or []
        return sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Since Python 3.12, wait_closed waits for every client connection to close, so close ours first.
            for writer in list(self._writers):
                writer.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._expiry.values()) + list(self._action_tasks)
        self._expiry.clear()
        self._action_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- game clock ------------------------------------------------------

    def _game_s(self) -> float:
        if self._clock_mode is ClockMode.stepped or self._paused or not self._playing:
            return self._base_game_s
        return self._base_game_s + (time.monotonic() - self._base_wall) * self._time_scale

    def _freeze(self) -> None:
        self._base_game_s = self._game_s()
        self._base_wall = time.monotonic()

    def _public(self) -> dict:
        return {
            "entity": {"pos": list(self._pos), "vel": list(self._vel)},
            "counter": self._counter,
        }

    # --- message builders (all through the models) ------------------------

    def _envelope(self, seq: int, session: str) -> dict:
        return {
            "protocol": PROTOCOL,
            "session": session,
            "seq": seq,
            "game_s": self._game_s(),
            "wall_utc": _utcnow(),
        }

    def _status(self, seq: int, session: str) -> Status:
        return Status(
            **self._envelope(seq, session),
            type="status",
            bridge_version="0.0.1-fake",
            unity_version="6000.0.0f1",
            project="fake",
            input_backends=[InputBackend.input_system],
            clock_control=self._clock_control,
            capture=False,
            capture_self_test="unknown",
            lock=LockState(held=False),
        )

    def _observation(self, seq: int, session: str) -> Observation:
        self._frame += 1
        public = self._public()
        obs = Observation(
            **self._envelope(seq, session),
            type="observation",
            clock_mode=self._clock_mode,
            scene="main",
            playing=self._playing,
            paused=self._paused,
            time_scale=self._time_scale,
            frame=self._frame,
            public=public,
            ui=None,
            frame_ref=None,
            reader_errors=[],
        )
        self._obs_history[obs.seq] = (time.time(), copy.deepcopy(public))
        while len(self._obs_history) > _HISTORY:
            self._obs_history.pop(next(iter(self._obs_history)))
        return obs

    def _execution(
        self,
        seq: int,
        session: str,
        *,
        action_id: str,
        status: ExecStatus,
        reject_reason: RejectReason | None = None,
        validated_against_seq: int | None = None,
        held: bool = False,
        started_game_s: float | None = None,
        started_wall_s: float | None = None,
    ) -> ExecutionRecord:
        now_game = self._game_s()
        now_wall = time.time()
        return ExecutionRecord(
            **self._envelope(seq, session),
            type="execution",
            action_id=action_id,
            status=status,
            validated_against_seq=validated_against_seq,
            reject_reason=reject_reason,
            input_start_game_s=started_game_s,
            input_end_game_s=now_game if started_game_s is not None else None,
            input_start_wall_s=started_wall_s,
            input_end_wall_s=now_wall if started_wall_s is not None else None,
            applied_duration_s=0.0 if started_wall_s is not None else None,
            release_verified=True if status is ExecStatus.completed and not held else (
                False if status is ExecStatus.completed else None
            ),
        )

    def _running(
        self, seq: int, session: str, *, action_id: str, validated_against_seq: int | None
    ) -> ExecutionRecord:
        return ExecutionRecord(
            **self._envelope(seq, session),
            type="execution",
            action_id=action_id,
            status=ExecStatus.running,
            validated_against_seq=validated_against_seq,
        )

    def _stopped(self, seq: int, session: str, released: list[str]) -> StopResult:
        return StopResult(
            **self._envelope(seq, session),
            type="stopped",
            released=released,
            release_verified=True,
        )

    def _inspection(self, request: InspectRequest, seq: int, session: str) -> InspectResult:
        return InspectResult(
            **self._envelope(seq, session),
            type="inspection",
            stage=request.stage,
            in_reply_to=request.seq,
            data={"stage": request.stage, "records": ["alpha", "beta"]},
            caps={"records": InspectionCap(limit=64, observed=2, returned=2, truncated=False)},
            errors=[],
        )

    def _error(
        self, seq: int, session: str, code: str, message: str, in_reply_to: int | None
    ) -> WireError:
        return WireError(
            **self._envelope(seq, session),
            type="error",
            code=code,  # type: ignore[arg-type]
            message=message,
            in_reply_to=in_reply_to,
        )

    # --- connection handling ----------------------------------------------

    async def _send(self, writer: asyncio.StreamWriter, message: object) -> None:
        writer.write(message.model_dump_json().encode("utf-8") + b"\n")  # type: ignore[attr-defined]
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        seq = 0
        session: str | None = None
        bound_here = False
        stream: asyncio.Task | None = None
        pending: set[asyncio.Task] = set()

        def discard_pending(task: asyncio.Task) -> None:
            pending.discard(task)
            self._action_tasks.discard(task)

        def next_seq() -> int:
            nonlocal seq
            out, seq = seq, seq + 1
            return out

        async def fail(code: str, message: str, in_reply_to: int | None, session_hint: str) -> None:
            writer.write(self._error(next_seq(), session_hint, code, message, in_reply_to).model_dump_json().encode() + b"\n")
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass

        async def stream_observations(rate: float, session_now: str) -> None:
            try:
                while True:
                    async with self._lock:
                        obs = self._observation(next_seq(), session_now)
                    writer.write(obs.model_dump_json().encode() + b"\n")
                    await writer.drain()
                    await asyncio.sleep(1.0 / rate)
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                pass

        def stop_stream() -> None:
            nonlocal stream
            if stream is not None:
                stream.cancel()
                stream = None

        async def join_stream() -> None:
            current = stream
            stop_stream()
            if current is not None:
                try:
                    await current
                except asyncio.CancelledError:
                    pass

        async def finish_action(
            request: ActRequest, record: ExecutionRecord, delay: float
        ) -> None:
            """Emit the terminal record after a simulated running window."""
            try:
                await asyncio.sleep(delay)
                assert session is not None
                terminal = self._execution(
                    next_seq(),
                    session,
                    action_id=request.action_id,
                    status=record.status,
                    validated_against_seq=record.validated_against_seq,
                    held=request.action == "hold",
                    started_game_s=record.input_start_game_s,
                    started_wall_s=record.input_start_wall_s,
                )
                await self._send(writer, terminal)
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                pass

        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await fail("invalid", "line exceeds size limit", None, session or "unknown")
                    return
                if line == b"":
                    return
                try:
                    body = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await fail("invalid", "unreadable line", None, session or "unknown")
                    return
                if not isinstance(body, dict) or not isinstance(body.get("type"), str):
                    await fail("invalid", "message is not an object", None, session or "unknown")
                    return
                kind = body["type"]
                req_session = body.get("session")
                req_seq = body.get("seq") if isinstance(body.get("seq"), int) else None

                if session is None and kind != "hello":
                    await fail("protocol", "hello comes first", req_seq, req_session or "unknown")
                    return
                if kind != "hello" and req_session != session:
                    await fail("protocol", "session mismatch", req_seq, req_session or "unknown")
                    return

                if kind == "hello":
                    try:
                        hello = Hello.model_validate(body)
                    except ValidationError:
                        await fail("invalid", "bad hello", req_seq, req_session or "unknown")
                        return
                    if hello.token != self._token:
                        await fail("auth", "wrong token", hello.seq, hello.session)
                        return
                    async with self._lock:
                        if self._session is not None:
                            bound, held_session = False, self._session
                        else:
                            self._session = hello.session
                            bound, held_session = True, hello.session
                    if not bound:
                        await fail("busy", "a session is already bound", req_seq, req_session or "unknown")
                        return
                    session, bound_here = held_session, True
                    async with self._lock:
                        await self._send(writer, self._status(next_seq(), session))
                    continue

                assert session is not None
                if kind == "observe":
                    try:
                        request = ObserveRequest.model_validate(body)
                    except ValidationError:
                        await fail("invalid", "bad observe", req_seq, session)
                        return
                    if request.mode == "once":
                        async with self._lock:
                            await self._send(writer, self._observation(next_seq(), session))
                    elif request.mode == "subscribe":
                        stop_stream()
                        rate = min(request.rate_hz or self._rate_hz, 60.0)
                        stream = asyncio.create_task(stream_observations(rate, session), name="fake-stream")
                    else:
                        stop_stream()
                elif kind == "act":
                    try:
                        request = ActRequest.model_validate(body)
                    except ValidationError:
                        await fail("invalid", "bad act", req_seq, session)
                        return
                    if request.action in self._aliases:
                        request = request.model_copy(update={"action": self._aliases[request.action]})
                    async with self._lock:
                        record = self._apply(request, next_seq(), session)
                    if self._action_run_s is not None and record.status is ExecStatus.completed:
                        await self._send(
                            writer,
                            self._running(
                                next_seq(),
                                session,
                                action_id=request.action_id,
                                validated_against_seq=record.validated_against_seq,
                            ),
                        )
                        if not self._action_stall:
                            task = asyncio.create_task(
                                finish_action(request, record, self._action_run_s),
                                name="fake-action",
                            )
                            pending.add(task)
                            self._action_tasks.add(task)
                            task.add_done_callback(discard_pending)
                    else:
                        await self._send(writer, record)
                elif kind == "stop":
                    try:
                        request = StopRequest.model_validate(body)
                    except ValidationError:
                        await fail("invalid", "bad stop", req_seq, session)
                        return
                    stop_stream()
                    async with self._lock:
                        released = self._release_all("stop")
                        result = self._stopped(next_seq(), session, released)
                        if bound_here:
                            self._session = None
                            bound_here = False
                    await self._send(writer, result)
                    return
                elif kind == "clock":
                    try:
                        request = ClockRequest.model_validate(body)
                    except ValidationError:
                        await fail("invalid", "bad clock", req_seq, session)
                        return
                    if not self._clock_control:
                        await fail("invalid", "clock control is off", request.seq, session)
                        return
                    async with self._lock:
                        self._apply_clock(request)
                        await self._send(writer, self._status(next_seq(), session))
                elif kind == "inspect":
                    try:
                        request = InspectRequest.model_validate(body)
                    except ValidationError:
                        await fail("invalid", "bad inspect", req_seq, session)
                        return
                    self.inspect_requests.append(request)
                    if not self._inspect_supported:
                        await fail("internal", "no inspection source is attached", request.seq, session)
                        continue
                    if self._inspect_silent:
                        continue
                    if self._inspect_delay_s:
                        await asyncio.sleep(self._inspect_delay_s)
                    async with self._lock:
                        await self._send(writer, self._inspection(request, next_seq(), session))
                else:
                    await fail("invalid", f"unknown verb {kind!r}", req_seq, session)
                    return
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            for task in list(pending):
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await join_stream()
            async with self._lock:
                if self._held:
                    self._release_all("disconnect")
                if bound_here and self._session == session:
                    self._session = None
            try:
                writer.close()
            except OSError:
                pass

    # --- act ---------------------------------------------------------------

    def _check_stale(self, request: ActRequest) -> RejectReason | None:
        if self._reject_acts is not None:
            return self._reject_acts
        seen = self._obs_history.get(request.based_on_seq)
        if seen is None:
            return RejectReason.stale_age
        at_wall, snapshot = seen
        if time.time() - at_wall > request.max_age_s:
            return RejectReason.stale_age
        current = self._public()
        for path in request.depends_on:
            if _resolve(current, path) != _resolve(snapshot, path):
                return RejectReason.stale_fields
        return None

    def _apply(self, request: ActRequest, seq: int, session: str) -> ExecutionRecord:
        stale = self._check_stale(request)
        if stale is not None:
            known = request.based_on_seq in self._obs_history
            return self._execution(
                seq, session,
                action_id=request.action_id,
                status=ExecStatus.rejected,
                reject_reason=stale,
                validated_against_seq=request.based_on_seq if known else None,
            )
        started_game_s = self._game_s()
        started_wall_s = time.time()
        if request.action == "shift":
            delta = request.params.get("delta")
            if (
                not isinstance(delta, list) or len(delta) != 3
                or not all(isinstance(v, (int, float)) for v in delta)
            ):
                return self._execution(
                    seq, session,
                    action_id=request.action_id,
                    status=ExecStatus.rejected,
                    reject_reason=RejectReason.invalid_params,
                    validated_against_seq=request.based_on_seq,
                )
            self._pos = [p + float(d) for p, d in zip(self._pos, delta)]
        elif request.action == "increment":
            self._counter += 1
        elif request.action == "hold":
            tag = request.params.get("tag", "held")
            if not isinstance(tag, str) or not tag:
                return self._execution(
                    seq, session,
                    action_id=request.action_id,
                    status=ExecStatus.rejected,
                    reject_reason=RejectReason.invalid_params,
                    validated_against_seq=request.based_on_seq,
                )
            self._release_all("superseded")
            self._held[tag] = request.action_id
            if request.lease_s is not None:
                old = self._expiry.pop(tag, None)
                if old is not None:
                    old.cancel()
                self._expiry[tag] = asyncio.create_task(self._expire(tag, request.action_id, request.lease_s))
        else:
            return self._execution(
                seq, session,
                action_id=request.action_id,
                status=ExecStatus.rejected,
                reject_reason=RejectReason.unknown_action,
                validated_against_seq=request.based_on_seq,
            )
        self.input_log.append({
            "event": "applied",
            "action": request.action,
            "choice_id": request.choice_id,
            "action_id": request.action_id,
            "params": copy.deepcopy(request.params),
        })
        return self._execution(
            seq, session,
            action_id=request.action_id,
            status=ExecStatus.completed,
            validated_against_seq=request.based_on_seq,
            held=request.action == "hold",
            started_game_s=started_game_s,
            started_wall_s=started_wall_s,
        )

    async def _expire(self, tag: str, action_id: str, lease_s: float) -> None:
        try:
            await asyncio.sleep(lease_s)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._held.get(tag) == action_id:
                del self._held[tag]
                self._expiry.pop(tag, None)
                self.input_log.append({"event": "released", "label": tag, "cause": "expired"})

    def _release_all(self, cause: str) -> list[str]:
        released = sorted(self._held)
        for tag in released:
            task = self._expiry.pop(tag, None)
            if task is not None:
                task.cancel()
            self.input_log.append({"event": "released", "label": tag, "cause": cause})
        self._held.clear()
        return released

    def _apply_clock(self, request: ClockRequest) -> None:
        if request.op == "pause":
            self._freeze()
            self._paused = True
        elif request.op == "resume":
            self._freeze()
            self._paused = False
        elif request.op == "step":
            assert request.step_game_s is not None
            self._base_game_s = self._game_s() + request.step_game_s
            self._base_wall = time.monotonic()
        elif request.op == "set_scale":
            assert request.time_scale is not None
            self._freeze()
            self._time_scale = request.time_scale
