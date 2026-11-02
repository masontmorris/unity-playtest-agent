"""The `place` verb: the client asks, the bridge measures, and nothing is invented.

These run the production client against a small protocol peer that speaks the verbs the
test needs, so the wire shape, the settle-extended timeout and the malformed-target
refusals are all exercised without Unity.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import (
    ExecutionRecord,
    Hello,
    InputBackend,
    LockState,
    PlaceRequest,
    PlaceResult,
    Status,
)
from playtest.contracts.bridge import BridgeError as WireError
from playtest.contracts.common import PROTOCOL
from playtest.core.bridge_client import BridgeClient, BridgeError

TOKEN = "place-test-token"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FakeBridge:
    """A peer that binds one session and answers `place` with a configured result."""

    def __init__(
        self,
        *,
        token: str = TOKEN,
        unplaced_reason: str | None = None,
        offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
        moved: float | None = None,
        delay_s: float = 0.0,
        error: tuple[str, str] | None = None,
        cancelled_action_id: str | None = None,
    ) -> None:
        self._token = token
        self._unplaced_reason = unplaced_reason
        self._offset = offset
        self._moved = moved
        self._delay_s = delay_s
        self._error = error
        self._cancelled_action_id = cancelled_action_id
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.places: list[PlaceRequest] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self._server.sockets or []
        return sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.close()
            await self._server.wait_closed()
            self._server = None

    def _envelope(self, seq: int, session: str) -> dict:
        return {
            "protocol": PROTOCOL,
            "session": session,
            "seq": seq,
            "game_s": 12.5,
            "wall_utc": _utcnow(),
        }

    def _status(self, seq: int, session: str) -> Status:
        return Status(
            **self._envelope(seq, session),
            type="status",
            bridge_version="0.0.1-fake",
            unity_version="6000.5.4f1",
            project="fake",
            input_backends=[InputBackend.input_system],
            clock_control=True,
            capture=False,
            capture_self_test="unknown",
            lock=LockState(held=True),
        )

    def _placed(self, seq: int, session: str, request: PlaceRequest) -> PlaceResult:
        if self._unplaced_reason is not None:
            return PlaceResult(
                **self._envelope(seq, session),
                type="placed",
                placed=False,
                reason=self._unplaced_reason,
            )
        position = [value + step for value, step in zip(request.position, self._offset)]
        moved = self._moved
        if moved is None:
            moved = sum((after - before) ** 2 for after, before in zip(position, request.position)) ** 0.5
        return PlaceResult(
            **self._envelope(seq, session),
            type="placed",
            placed=True,
            position_after_settle=position,
            moved_during_settle=moved,
        )

    def _cancelled(self, seq: int, session: str, action_id: str) -> ExecutionRecord:
        return ExecutionRecord(
            **self._envelope(seq, session),
            type="execution",
            action_id=action_id,
            status="cancelled",
        )

    def _error_message(
        self, seq: int, session: str, code: str, message: str, in_reply_to: int | None
    ) -> WireError:
        return WireError(
            **self._envelope(seq, session),
            type="error",
            code=code,  # type: ignore[arg-type]
            message=message,
            in_reply_to=in_reply_to,
        )

    async def _send(self, writer: asyncio.StreamWriter, message: object) -> None:
        writer.write(message.model_dump_json().encode("utf-8") + b"\n")  # type: ignore[attr-defined]
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        seq = 0
        session: str | None = None

        def next_seq() -> int:
            nonlocal seq
            out, seq = seq, seq + 1
            return out

        try:
            while True:
                line = await reader.readline()
                if line == b"":
                    return
                body = json.loads(line.decode("utf-8"))
                kind = body.get("type")
                if kind == "hello":
                    hello = Hello.model_validate(body)
                    if hello.token != self._token:
                        await self._send(
                            writer,
                            self._error_message(next_seq(), hello.session, "auth", "wrong token", hello.seq),
                        )
                        return
                    session = hello.session
                    await self._send(writer, self._status(next_seq(), session))
                    continue
                if session is None:
                    raise AssertionError("the peer expects hello first")
                if kind == "place":
                    request = PlaceRequest.model_validate(body)
                    self.places.append(request)
                    if self._delay_s:
                        await asyncio.sleep(self._delay_s)
                    if self._error is not None:
                        code, message = self._error
                        await self._send(
                            writer,
                            self._error_message(next_seq(), session, code, message, request.seq),
                        )
                        continue
                    if self._cancelled_action_id is not None:
                        await self._send(
                            writer,
                            self._cancelled(next_seq(), session, self._cancelled_action_id),
                        )
                    await self._send(writer, self._placed(next_seq(), session, request))
                    continue
                await self._send(
                    writer,
                    self._error_message(next_seq(), session, "invalid", "unknown verb", body.get("seq")),
                )
                return
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()


async def _started(**kwargs) -> tuple[FakeBridge, BridgeClient]:
    bridge = FakeBridge(**kwargs)
    port = await bridge.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return bridge, client


def test_place_returns_the_measured_result():
    async def main():
        bridge, client = await _started(offset=(0.0, -0.01, 0.0))
        try:
            result = await client.place([4.0, 1.5, -2.0], 0.25)

            assert result.type == "placed"
            assert result.placed is True
            assert result.position_after_settle == [4.0, 1.49, -2.0]
            assert result.moved_during_settle == pytest.approx(0.01)
            assert result.reason is None

            assert len(bridge.places) == 1
            request = bridge.places[0]
            assert request.type == "place"
            assert request.position == [4.0, 1.5, -2.0]
            assert request.settle_s == 0.25
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())


def test_place_returns_an_unplaced_reason_without_a_measurement():
    async def main():
        bridge, client = await _started(unplaced_reason="the project is not playing")
        try:
            result = await client.place([1.0, 2.0, 3.0], 0.1)

            assert result.placed is False
            assert result.reason == "the project is not playing"
            assert result.position_after_settle is None
            assert result.moved_during_settle is None
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())


def test_place_tolerates_a_cancelled_record_before_the_measurement():
    async def main():
        bridge, client = await _started(cancelled_action_id="a-held")
        try:
            result = await client.place([1.0, 2.0, 3.0], 0.1)

            assert result.placed is True
            assert result.position_after_settle == [1.0, 2.0, 3.0]
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())


def test_place_extends_the_request_timeout_by_the_settle():
    async def main():
        bridge, client = await _started(delay_s=0.4)
        try:
            client._timeout_s = 0.1
            result = await client.place([1.0, 2.0, 3.0], 0.5)
            assert result.placed is True, "the settle wait is the bridge's, so it must not time out"
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())


def test_place_still_times_out_when_the_reply_outlasts_the_settle():
    async def main():
        bridge, client = await _started(delay_s=0.6)
        try:
            client._timeout_s = 0.1
            with pytest.raises(BridgeError) as exc_info:
                await client.place([1.0, 2.0, 3.0], 0.05)
            assert exc_info.value.code == "timeout"
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())


def test_place_surfaces_a_wire_error_as_a_typed_failure():
    async def main():
        bridge, client = await _started(error=("internal", "no placement source is attached to this bridge"))
        try:
            with pytest.raises(BridgeError) as exc_info:
                await client.place([1.0, 2.0, 3.0], 0.1)
            assert exc_info.value.code == "internal"
            assert "placement source" in exc_info.value.message
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())


@pytest.mark.parametrize(
    ("position", "settle"),
    [
        ([1.0, 2.0], 0.1),
        ([1.0, 2.0, 3.0, 4.0], 0.1),
        ([1.0, float("nan"), 3.0], 0.1),
        ([1.0, float("inf"), 3.0], 0.1),
        ("1,2,3", 0.1),
        (None, 0.1),
        ([1.0, 2.0, 3.0], 0.0),
        ([1.0, 2.0, 3.0], -0.1),
        ([1.0, 2.0, 3.0], float("nan")),
        ([1.0, 2.0, 3.0], float("inf")),
    ],
)
def test_place_refuses_a_malformed_target_before_sending(position, settle):
    async def main():
        bridge, client = await _started()
        try:
            with pytest.raises(ValueError):
                await client.place(position, settle)
            assert bridge.places == [], "a malformed target is never sent"
        finally:
            await client.aclose()
            await bridge.stop()

    asyncio.run(main())
