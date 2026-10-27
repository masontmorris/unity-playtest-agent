"""asyncio JSONL client for the bridge wire protocol.

One TCP connection over loopback. The client speaks first (`hello`), then
`observe` / `act` / `stop` / `clock` / `inspect`. A background reader demultiplexes
replies: observations go to the stream or once queues, execution records
accumulate per `action_id`, and errors complete the matching request.

Fail closed: the client never reconnects and never replays. A dropped
connection fails every pending request and ends the run. On shutdown the
client sends `stop` and reports the release it verified.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from playtest.contracts.bridge import (
    TERMINAL_STATUSES,
    ActRequest,
    BridgeError as WireError,
    ClockRequest,
    ExecStatus,
    ExecutionRecord,
    Hello,
    InspectRequest,
    InspectResult,
    Observation,
    ObserveRequest,
    Status,
    StopRequest,
    StopResult,
)
from playtest.contracts.common import PROTOCOL

from .bridge_lock import BridgeEndpoint, read_bridge_lock, read_project_lock

MAX_LINE_SIZE = 8 * 1024 * 1024
MAX_INSPECTION_SAMPLES = 1024


def _finite_positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


class BridgeError(Exception):
    """Typed client failure. `code` mirrors the wire error codes, plus
    `timeout` and `closed` for client-side transport failures."""

    def __init__(self, code: str, message: str, in_reply_to: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.in_reply_to = in_reply_to


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BridgeClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        timeout_s: float = 10.0,
        core_version: str = "0.0.1",
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._timeout_s = timeout_s
        self._core_version = core_version
        self._session = uuid.uuid4().hex
        self._seq = 0
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._clean_close = False
        self._by_seq: dict[int, asyncio.Future] = {}
        self._status_future: asyncio.Future | None = None
        self._stop_future: asyncio.Future | None = None
        self._exec: dict[str, dict] = {}
        self._stream: asyncio.Queue[Observation] = asyncio.Queue()
        self._once: asyncio.Queue[Observation] = asyncio.Queue()
        self._once_pending = 0
        self._subscribed = False
        self._latest: Observation | None = None
        self._stop_result: StopResult | None = None
        self._status: Status | None = None
        self._endpoint: BridgeEndpoint | None = None

    @property
    def session(self) -> str:
        return self._session

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def latest(self) -> Observation | None:
        return self._latest

    @property
    def status(self) -> Status | None:
        return self._status

    @property
    def endpoint(self) -> BridgeEndpoint | None:
        """The lock-file endpoint when connected through one; otherwise None."""
        return self._endpoint

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    async def connect(self) -> Status:
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port, limit=MAX_LINE_SIZE)
        except OSError as exc:
            raise BridgeError("closed", f"connect failed: {exc}") from exc
        self._reader = reader
        self._writer = writer
        self._reader_task = asyncio.create_task(self._read_loop(), name="bridge-reader")
        hello = Hello(
            session=self._session,
            seq=self._next_seq(),
            game_s=None,
            wall_utc=_utcnow(),
            token=self._token,
            core_version=self._core_version,
        )
        status = await self._request_status(hello)
        self._status = status
        return status

    @classmethod
    async def connect_from_project(
        cls,
        project: str | Path,
        *,
        host: str = "127.0.0.1",
        timeout_s: float = 10.0,
        core_version: str = "0.0.1",
    ) -> "BridgeClient":
        """Connect to the bridge advertised by `<project>/Temp/playtest-bridge.lock`."""
        return await cls._connect_from_endpoint(
            read_project_lock(project),
            host=host,
            timeout_s=timeout_s,
            core_version=core_version,
        )

    @classmethod
    async def connect_from_lock(
        cls,
        lock_path: str | Path,
        *,
        host: str = "127.0.0.1",
        timeout_s: float = 10.0,
        core_version: str = "0.0.1",
    ) -> "BridgeClient":
        """Connect to the bridge advertised by an explicit lock file."""
        return await cls._connect_from_endpoint(
            read_bridge_lock(lock_path),
            host=host,
            timeout_s=timeout_s,
            core_version=core_version,
        )

    @classmethod
    async def _connect_from_endpoint(
        cls,
        endpoint: BridgeEndpoint,
        *,
        host: str,
        timeout_s: float,
        core_version: str,
    ) -> "BridgeClient":
        client = cls(
            host,
            endpoint.port,
            endpoint.token,
            timeout_s=timeout_s,
            core_version=core_version,
        )
        client._endpoint = endpoint
        try:
            await client.connect()
        except BaseException:
            await client.aclose()
            raise
        return client

    async def __aenter__(self) -> "BridgeClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # --- sending ----------------------------------------------------------

    async def _send(self, message: object) -> None:
        assert self._writer is not None
        raw = message.model_dump_json().encode("utf-8") + b"\n"  # type: ignore[attr-defined]
        self._writer.write(raw)
        try:
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError) as exc:
            self._connection_lost(f"send failed: {exc}")
            raise BridgeError("closed", f"send failed: {exc}") from exc

    def _register(self, seq: int, future: asyncio.Future) -> None:
        self._by_seq[seq] = future

    def _forget(self, seq: int, future: asyncio.Future) -> None:
        if self._by_seq.get(seq) is future:
            del self._by_seq[seq]

    async def _await(self, seq: int, future: asyncio.Future) -> object:
        try:
            return await asyncio.wait_for(asyncio.shield(future), self._timeout_s)
        except asyncio.TimeoutError as exc:
            self._forget(seq, future)
            if not future.done():
                future.cancel()
            raise BridgeError("timeout", f"no reply to request seq {seq}") from exc

    # --- receiving --------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                self._connection_lost(f"line exceeds {MAX_LINE_SIZE} bytes")
                return
            except (ConnectionResetError, asyncio.IncompleteReadError) as exc:
                self._connection_lost(f"connection reset: {exc}")
                return
            if line == b"":
                self._connection_lost("bridge closed the connection")
                return
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                self._connection_lost(f"invalid utf-8: {exc}")
                return
            try:
                body = json.loads(text)
            except json.JSONDecodeError as exc:
                self._connection_lost(f"invalid json: {exc}")
                return
            if (
                not isinstance(body, dict)
                or body.get("protocol") != PROTOCOL
                or body.get("session") != self._session
                or not isinstance(body.get("type"), str)
            ):
                self._connection_lost("message failed envelope checks")
                return
            self._dispatch(body)

    def _dispatch(self, body: dict) -> None:
        kind = body["type"]
        try:
            if kind == "status":
                message = Status.model_validate(body)
            elif kind == "observation":
                message = Observation.model_validate(body)
            elif kind == "execution":
                message = ExecutionRecord.model_validate(body)
            elif kind == "stopped":
                message = StopResult.model_validate(body)
            elif kind == "inspection":
                message = InspectResult.model_validate(body)
            elif kind == "error":
                message = WireError.model_validate(body)
            else:
                raise ValueError(f"unknown message type {kind!r}")
        except (ValidationError, ValueError) as exc:
            self._connection_lost(f"invalid message: {exc}")
            return
        if isinstance(message, Status):
            self._on_status(message)
        elif isinstance(message, Observation):
            self._on_observation(message)
        elif isinstance(message, ExecutionRecord):
            self._on_execution(message)
        elif isinstance(message, StopResult):
            self._on_stopped(message)
        elif isinstance(message, InspectResult):
            self._on_inspection(message)
        else:
            self._on_error(message)

    def _fail(self, future: asyncio.Future | None, exc: BaseException) -> None:
        if future is not None and not future.done():
            future.set_exception(exc)

    def _sweep(self) -> None:
        # Cancelled inspection tombstones must survive unrelated replies until
        # their own late reply arrives or the connection closes.
        done = [seq for seq, future in self._by_seq.items() if future.done() and not future.cancelled()]
        for seq in done:
            del self._by_seq[seq]

    def _on_status(self, message: Status) -> None:
        future, self._status_future = self._status_future, None
        if future is not None and not future.done():
            future.set_result(message)
        self._sweep()

    def _on_observation(self, message: Observation) -> None:
        self._latest = message
        if self._once_pending > 0:
            self._once_pending -= 1
            self._once.put_nowait(message)
        elif self._subscribed:
            self._stream.put_nowait(message)
        # Otherwise the stream is off: in-flight observations are dropped.

    def _on_execution(self, message: ExecutionRecord) -> None:
        entry = self._exec.get(message.action_id)
        if entry is None:
            return
        entry["records"].append(message)
        first = entry.get("first")
        if first is not None and not first.done() and (
            message.status is ExecStatus.running or message.status in TERMINAL_STATUSES
        ):
            first.set_result(message)
        if message.status in TERMINAL_STATUSES:
            done = entry["done"]
            if not done.done():
                done.set_result(entry["records"])
            self._sweep()

    def _on_stopped(self, message: StopResult) -> None:
        future, self._stop_future = self._stop_future, None
        if future is not None and not future.done():
            future.set_result(message)
        self._sweep()

    def _on_inspection(self, message: InspectResult) -> None:
        future = self._by_seq.pop(message.in_reply_to, None)
        if future is None:
            self._connection_lost(f"unmatched inspection reply for seq {message.in_reply_to}")
            return
        if not future.done():
            future.set_result(message)
        self._sweep()

    def _on_error(self, message: WireError) -> None:
        future = self._by_seq.pop(message.in_reply_to, None) if message.in_reply_to is not None else None
        if future is None:
            self._connection_lost(f"unmatched error {message.code}: {message.message}")
            return
        self._forget(message.in_reply_to, future)  # type: ignore[arg-type]
        if not future.done():
            future.set_exception(BridgeError(message.code, message.message, message.in_reply_to))

    # --- requests ---------------------------------------------------------

    def _require_open(self) -> asyncio.AbstractEventLoop:
        if self._closed or self._writer is None:
            raise BridgeError("closed", "connection is closed; the run is over, never replayed")
        return asyncio.get_running_loop()

    async def _request_status(self, request: Hello | ClockRequest) -> Status:
        loop = self._require_open()
        async with self._lock:
            future: asyncio.Future = loop.create_future()
            self._status_future = future
            self._register(request.seq, future)
            await self._send(request)
            try:
                result = await self._await(request.seq, future)
            finally:
                if self._status_future is future:
                    self._status_future = None
        assert isinstance(result, Status)
        return result

    async def observe_once(self) -> Observation:
        loop = self._require_open()
        async with self._lock:
            request = ObserveRequest(
                session=self._session,
                seq=self._next_seq(),
                game_s=None,
                wall_utc=_utcnow(),
                mode="once",
            )
            error: asyncio.Future = loop.create_future()
            self._register(request.seq, error)
            self._once_pending += 1
            await self._send(request)
            getter = asyncio.create_task(self._once.get())
            try:
                done, _ = await asyncio.wait(
                    {getter, error}, timeout=self._timeout_s, return_when=asyncio.FIRST_COMPLETED
                )
            except Exception:
                getter.cancel()
                raise
            self._forget(request.seq, error)
            if error in done:
                getter.cancel()
                self._once_pending = max(0, self._once_pending - 1)
                raise error.exception() or BridgeError("closed", "observe failed")
            if getter in done:
                if not error.done():
                    error.cancel()
                return getter.result()
            getter.cancel()
            if not error.done():
                error.cancel()
            self._once_pending = max(0, self._once_pending - 1)
            raise BridgeError("timeout", f"no observation for request seq {request.seq}")

    async def subscribe(self, rate_hz: float = 20.0) -> None:
        self._require_open()
        async with self._lock:
            request = ObserveRequest(
                session=self._session,
                seq=self._next_seq(),
                game_s=None,
                wall_utc=_utcnow(),
                mode="subscribe",
                rate_hz=rate_hz,
            )
            self._subscribed = True
            await self._send(request)

    async def next_observation(self) -> Observation:
        try:
            # No shield: cancelling a queue get is safe and leaves no orphan task.
            return await asyncio.wait_for(self._stream.get(), self._timeout_s)
        except asyncio.TimeoutError as exc:
            raise BridgeError("timeout", "no streamed observation arrived") from exc

    async def unsubscribe(self) -> None:
        self._require_open()
        async with self._lock:
            request = ObserveRequest(
                session=self._session,
                seq=self._next_seq(),
                game_s=None,
                wall_utc=_utcnow(),
                mode="unsubscribe",
            )
            self._subscribed = False
            await self._send(request)
            while not self._stream.empty():
                try:
                    self._stream.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def _prepare_act_locked(
        self,
        *,
        action_id: str,
        choice_id: str,
        action: str,
        params: dict | None = None,
        based_on_seq: int,
        max_age_s: float,
        depends_on: list[str] | tuple[str, ...] = (),
        lease_s: float | None = None,
        with_first: bool = False,
    ) -> tuple[ActRequest, asyncio.Future, asyncio.Future | None]:
        """Build and register one act request. The caller holds the lock."""
        loop = asyncio.get_running_loop()
        request = ActRequest(
            session=self._session,
            seq=self._next_seq(),
            game_s=None,
            wall_utc=_utcnow(),
            action_id=action_id,
            choice_id=choice_id,
            action=action,
            params=dict(params or {}),
            based_on_seq=based_on_seq,
            max_age_s=max_age_s,
            depends_on=list(depends_on),
            lease_s=lease_s,
        )
        done: asyncio.Future = loop.create_future()
        first: asyncio.Future | None = loop.create_future() if with_first else None
        entry: dict = {"records": [], "done": done}
        if first is not None:
            entry["first"] = first
        self._exec[action_id] = entry
        self._register(request.seq, first if first is not None else done)
        return request, done, first

    async def act(
        self,
        *,
        action_id: str,
        choice_id: str,
        action: str,
        params: dict | None = None,
        based_on_seq: int,
        max_age_s: float,
        depends_on: list[str] | tuple[str, ...] = (),
        lease_s: float | None = None,
    ) -> list[ExecutionRecord]:
        """Send one bounded action and wait for its terminal record.

        A rejection comes back as data (a terminal record with status
        `rejected`), never as an exception to repair.
        """
        self._require_open()
        async with self._lock:
            request, done, _first = self._prepare_act_locked(
                action_id=action_id,
                choice_id=choice_id,
                action=action,
                params=params,
                based_on_seq=based_on_seq,
                max_age_s=max_age_s,
                depends_on=depends_on,
                lease_s=lease_s,
            )
            try:
                await self._send(request)
                records = await self._await(request.seq, done)
            finally:
                self._exec.pop(action_id, None)
        assert isinstance(records, list) and records
        return records  # type: ignore[return-value]

    async def act_with_inspections(
        self,
        *,
        action_id: str,
        choice_id: str,
        action: str,
        params: dict | None = None,
        based_on_seq: int,
        max_age_s: float,
        depends_on: list[str] | tuple[str, ...] = (),
        lease_s: float | None = None,
        inspection_stage: str,
        interval_s: float,
        max_samples: int,
    ) -> tuple[list[ExecutionRecord], list[InspectResult]]:
        """Send one action once and sample one inspection stage while it runs.

        This is generic instrumentation for a code-driven preflight probe, not a
        policy: the supplied action is sent exactly once and is never retried,
        ranked, replaced or synthesized. The client lock is held for the whole
        operation, so no other request can interleave with the sampled window.

        The first execution record is awaited. A terminal rejection there returns
        immediately with no inspections. Otherwise the first sample is taken
        immediately, later samples are separated by `interval_s`, and sampling
        stops at `max_samples` or when the terminal record arrives. The terminal
        record is always awaited before returning.

        If inspection fails, or the operation is cancelled or times out after the
        action was sent, the input is released with `stop` after the lock is
        released, and the original exception is re-raised.
        """
        interval_s = _finite_positive(interval_s, "interval_s")
        if (
            isinstance(max_samples, bool)
            or not isinstance(max_samples, int)
            or not 1 <= max_samples <= MAX_INSPECTION_SAMPLES
        ):
            raise ValueError(f"max_samples must be an integer in 1..{MAX_INSPECTION_SAMPLES}")
        self._require_open()
        sent = False
        try:
            async with self._lock:
                request, done, first = self._prepare_act_locked(
                    action_id=action_id,
                    choice_id=choice_id,
                    action=action,
                    params=params,
                    based_on_seq=based_on_seq,
                    max_age_s=max_age_s,
                    depends_on=depends_on,
                    lease_s=lease_s,
                    with_first=True,
                )
                assert first is not None
                try:
                    sent = True
                    await self._send(request)
                    first_record = await self._await(request.seq, first)
                    assert isinstance(first_record, ExecutionRecord)
                    inspections: list[InspectResult] = []
                    while (
                        first_record.status not in TERMINAL_STATUSES
                        and len(inspections) < max_samples
                        and not done.done()
                    ):
                        inspections.append(
                            await self._inspect_locked(inspection_stage, keep_late_reply=True)
                        )
                        if len(inspections) >= max_samples or done.done():
                            break
                        await self._pause_between_samples(done, interval_s)
                    records = await self._await(request.seq, done)
                    assert isinstance(records, list) and records
                    return records, inspections
                finally:
                    if done.done() and not done.cancelled():
                        try:
                            done.exception()
                        except BaseException:
                            pass
                    self._exec.pop(action_id, None)
        except BaseException:
            if sent:
                await self._stop_after_failure()
            raise

    async def _pause_between_samples(self, done: asyncio.Future, interval_s: float) -> None:
        """Wait one sampling interval, returning early once the terminal record arrives."""
        try:
            await asyncio.wait_for(asyncio.shield(done), interval_s)
        except asyncio.TimeoutError:
            pass

    async def _stop_after_failure(self) -> None:
        """Best-effort release for a sent action; never masks the original error."""
        task = asyncio.ensure_future(self.stop(reason="error"))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
            except BaseException:
                break
        if task.done():
            try:
                task.exception()
            except BaseException:
                pass

    async def clock(
        self,
        op: str,
        *,
        step_game_s: float | None = None,
        time_scale: float | None = None,
    ) -> Status:
        request = ClockRequest(
            session=self._session,
            seq=self._next_seq(),
            game_s=None,
            wall_utc=_utcnow(),
            op=op,  # type: ignore[arg-type]
            step_game_s=step_game_s,
            time_scale=time_scale,
        )
        return await self._request_status(request)

    async def inspect(self, stage: str) -> InspectResult:
        """Request one Editor inspection snapshot.

        Inspection exists only in the Editor. Requests are serialized so at most one is in
        flight; the reply is correlated by `in_reply_to`, never by arrival order.
        """
        self._require_open()
        async with self._lock:
            return await self._inspect_locked(stage)

    async def _inspect_locked(self, stage: str, *, keep_late_reply: bool = False) -> InspectResult:
        """Request one inspection while the caller holds the lock.

        With `keep_late_reply`, a cancelled request keeps a cancelled tombstone in
        the correlation table so a reply that arrives during cleanup is dropped
        instead of failing the connection before `stop` can be answered.
        """
        loop = asyncio.get_running_loop()
        request = InspectRequest(
            session=self._session,
            seq=self._next_seq(),
            game_s=None,
            wall_utc=_utcnow(),
            stage=stage,  # type: ignore[arg-type]
        )
        future: asyncio.Future = loop.create_future()
        self._register(request.seq, future)
        await self._send(request)
        try:
            result = await self._await(request.seq, future)
        except asyncio.CancelledError:
            if keep_late_reply:
                if not future.done():
                    future.cancel()
            else:
                self._forget(request.seq, future)
            raise
        except BridgeError as exc:
            if keep_late_reply and exc.code == "timeout" and not self._closed:
                self._register(request.seq, future)
            else:
                self._forget(request.seq, future)
            raise
        except BaseException:
            self._forget(request.seq, future)
            raise
        else:
            self._forget(request.seq, future)
        assert isinstance(result, InspectResult)
        if result.stage != request.stage:
            self._connection_lost("inspection reply stage does not match the request")
            raise BridgeError("protocol", "inspection reply stage does not match the request", request.seq)
        return result

    async def stop(self, reason: str = "finish") -> StopResult | None:
        """Cancel the action, release every held input, verify release."""
        if self._closed or self._writer is None:
            return self._stop_result
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._closed or self._writer is None:
                return self._stop_result
            request = StopRequest(
                session=self._session,
                seq=self._next_seq(),
                game_s=None,
                wall_utc=_utcnow(),
                reason=reason,  # type: ignore[arg-type]
            )
            future: asyncio.Future = loop.create_future()
            self._stop_future = future
            self._register(request.seq, future)
            try:
                await self._send(request)
            except BridgeError:
                return self._stop_result
            try:
                result = await self._await(request.seq, future)
            except BridgeError:
                return self._stop_result
            assert isinstance(result, StopResult)
            self._stop_result = result
            self._clean_close = True
            self._connection_lost("stopped")
            return result

    async def aclose(self) -> StopResult | None:
        """Best-effort `stop` on shutdown, then close. Never raises."""
        if self._closed:
            return self._stop_result
        try:
            return await self.stop(reason="disconnect")
        except BridgeError:
            return self._stop_result
        except Exception:  # noqa: BLE001 - shutdown must not raise
            return self._stop_result

    def _connection_lost(self, message: str) -> None:
        if self._closed:
            return
        clean = self._clean_close
        self._closed = True
        exc = BridgeError("closed", message)
        for future in list(self._by_seq.values()):
            self._fail(future, exc)
        self._by_seq.clear()
        for slot in [self._status_future, self._stop_future]:
            self._fail(slot, exc)
        self._status_future = None
        self._stop_future = None
        for entry in self._exec.values():
            self._fail(entry["done"], exc)
        self._exec.clear()
        if self._writer is not None:
            try:
                self._writer.close()
            except OSError:
                pass
        if not clean and self._reader_task is not None:
            for queue in (self._stream, self._once):
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
