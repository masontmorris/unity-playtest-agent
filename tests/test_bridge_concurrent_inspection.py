"""Concurrent inspection sampling over the real bridge protocol.

`act_with_inspections` is transport instrumentation for the preflight probe: one
supplied action runs while one inspection stage is sampled. The fake game emits
a running record, serves inspection requests, then emits the terminal record, so
these tests drive the production `BridgeClient` against a protocol peer.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import (
    TERMINAL_STATUSES,
    ExecutionRecord,
    ExecStatus,
    InspectResult,
    RejectReason,
)
from playtest.core.bridge_client import BridgeClient, BridgeError

from fake_game import FakeGame

TOKEN = "concurrent-inspection-token"


class ProbeGame(FakeGame):
    """Record act requests, inspection times and terminal sends."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.act_requests: list[tuple[str, str]] = []
        self.inspection_times: list[float] = []
        self.terminal_times: dict[str, float] = {}
        self.inspected = asyncio.Event()

    def _apply(self, request, seq, session):
        self.act_requests.append((request.action, request.action_id))
        return super()._apply(request, seq, session)

    def _inspection(self, request, seq, session):
        self.inspection_times.append(time.monotonic())
        self.inspected.set()
        return super()._inspection(request, seq, session)

    async def _send(self, writer, message) -> None:
        if isinstance(message, ExecutionRecord) and message.status in TERMINAL_STATUSES:
            self.terminal_times[message.action_id] = time.monotonic()
        await super()._send(writer, message)


class WrongStageGame(ProbeGame):
    def _inspection(self, request, seq, session):
        result = super()._inspection(request, seq, session)
        return result.model_copy(update={"stage": "discovery"})


async def _started(game_type=ProbeGame, **kwargs):
    game = game_type(token=TOKEN, **kwargs)
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return game, client


async def _closed(game, client) -> None:
    await client.aclose()
    await game.stop()


def _sampling(observation, **overrides):
    values = {
        "action_id": "probe-1",
        "choice_id": "probe-1",
        "action": "hold",
        "params": {"tag": "sample"},
        "based_on_seq": observation.seq,
        "max_age_s": 2.0,
        "lease_s": 10.0,
        "inspection_stage": "transforms",
        "interval_s": 0.01,
        "max_samples": 4,
    }
    values.update(overrides)
    return values


def test_sampling_waits_for_running_not_just_requested_or_validated():
    async def main():
        client = BridgeClient("127.0.0.1", 1, TOKEN)
        first = asyncio.get_running_loop().create_future()
        done = asyncio.get_running_loop().create_future()
        client._exec["probe"] = {"records": [], "first": first, "done": done}
        for status in (ExecStatus.requested, ExecStatus.validated):
            client._on_execution(ExecutionRecord(session=client.session, seq=1,
                wall_utc=datetime.now(timezone.utc), action_id="probe", status=status))
            assert not first.done()
        client._on_execution(ExecutionRecord(session=client.session, seq=2,
            wall_utc=datetime.now(timezone.utc), action_id="probe", status=ExecStatus.running))
        assert first.result().status is ExecStatus.running
    asyncio.run(main())


def test_late_inspection_tombstone_survives_unrelated_sweeps():
    async def main():
        client = BridgeClient("127.0.0.1", 1, TOKEN)
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        client._register(8, future)
        client._sweep()
        assert 8 in client._by_seq
        client._on_inspection(InspectResult(session=client.session, seq=2, wall_utc=datetime.now(timezone.utc),
            stage="transforms", in_reply_to=8, data={}, caps={}, errors=[]))
        assert 8 not in client._by_seq
        assert client.closed is False
    asyncio.run(main())


def test_samples_while_one_action_runs_and_waits_for_terminal():
    async def main():
        game, client = await _started(action_run_s=0.6)
        try:
            observation = await client.observe_once()
            records, inspections = await client.act_with_inspections(**_sampling(observation))

            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            assert [record.status for record in records] == [
                ExecStatus.running,
                ExecStatus.completed,
            ]
            assert len(inspections) == 4
            assert all(result.stage == "transforms" for result in inspections)
            replies = [result.in_reply_to for result in inspections]
            assert replies == sorted(set(replies))
            assert game.inspection_times[0] < game.terminal_times["probe-1"]
            assert game.inspection_times[-1] < game.terminal_times["probe-1"]
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_sample_cap_still_awaits_the_terminal_record():
    async def main():
        game, client = await _started(action_run_s=0.5)
        try:
            observation = await client.observe_once()
            records, inspections = await client.act_with_inspections(
                **_sampling(observation, max_samples=2)
            )
            returned_at = time.monotonic()

            assert len(inspections) == 2
            assert len(records) == 2
            assert records[-1].status is ExecStatus.completed
            assert returned_at >= game.terminal_times["probe-1"]
            assert game.act_requests == [("hold", "probe-1")]
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_rejection_returns_immediately_without_inspection_or_substitute():
    async def main():
        game, client = await _started(reject_acts=RejectReason.stale_age, action_run_s=0.5)
        try:
            observation = await client.observe_once()
            records, inspections = await client.act_with_inspections(**_sampling(observation))

            assert len(records) == 1
            assert records[0].status is ExecStatus.rejected
            assert records[0].reject_reason is RejectReason.stale_age
            assert inspections == []
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == []
            assert game.inspection_times == []
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_terminal_timeout_stops_and_releases_without_replay():
    async def main():
        game, client = await _started(action_run_s=0.6, action_stall=True)
        try:
            client._timeout_s = 0.2
            observation = await client.observe_once()
            with pytest.raises(BridgeError) as exc_info:
                await client.act_with_inspections(
                    **_sampling(observation, max_samples=1)
                )

            assert exc_info.value.code == "timeout"
            assert client.closed
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            assert ("sample", "stop") in game.releases()
            assert len(game.inspection_times) == 1
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_inspection_failure_stops_and_releases():
    async def main():
        game, client = await _started(inspect_supported=False, action_run_s=0.6)
        try:
            observation = await client.observe_once()
            with pytest.raises(BridgeError) as exc_info:
                await client.act_with_inspections(**_sampling(observation))

            assert exc_info.value.code == "internal"
            assert client.closed
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            assert ("sample", "stop") in game.releases()
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_cancellation_stops_and_releases_without_replay():
    async def main():
        game, client = await _started(action_run_s=5.0)
        try:
            observation = await client.observe_once()
            task = asyncio.create_task(
                client.act_with_inspections(
                    **_sampling(observation, max_samples=10, interval_s=0.2)
                )
            )
            await asyncio.wait_for(game.inspected.wait(), 2.0)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert client.closed
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            assert ("sample", "stop") in game.releases()
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_cancellation_during_an_inspection_reply_still_stops_and_releases():
    async def main():
        game, client = await _started(action_run_s=5.0, inspect_delay_s=0.4)
        try:
            observation = await client.observe_once()
            task = asyncio.create_task(
                client.act_with_inspections(**_sampling(observation, max_samples=3))
            )
            for _ in range(400):
                if game.inspect_requests:
                    break
                await asyncio.sleep(0.005)
            assert game.inspect_requests
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert client.closed
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            assert ("sample", "stop") in game.releases()
            stop = await client.aclose()
            assert stop is not None and stop.release_verified is True
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_disconnect_fails_closed_without_replay():
    async def main():
        game, client = await _started(action_run_s=5.0)
        try:
            observation = await client.observe_once()
            task = asyncio.create_task(
                client.act_with_inspections(
                    **_sampling(observation, max_samples=10, interval_s=0.2)
                )
            )
            await asyncio.wait_for(game.inspected.wait(), 2.0)
            assert client._writer is not None
            client._writer.close()
            with pytest.raises(BridgeError) as exc_info:
                await task

            assert exc_info.value.code == "closed"
            assert client.closed
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            for _ in range(200):
                if game.releases():
                    break
                await asyncio.sleep(0.01)
            assert ("sample", "disconnect") in game.releases()
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_mismatched_inspection_stage_fails_closed():
    async def main():
        game, client = await _started(WrongStageGame, action_run_s=0.6)
        try:
            observation = await client.observe_once()
            with pytest.raises(BridgeError) as exc_info:
                await client.act_with_inspections(**_sampling(observation))

            assert exc_info.value.code == "protocol"
            assert client.closed
            assert game.act_requests == [("hold", "probe-1")]
            assert game.applied() == ["hold"]
            for _ in range(200):
                if game.releases():
                    break
                await asyncio.sleep(0.01)
            assert ("sample", "disconnect") in game.releases()
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_invalid_sampling_parameters_are_refused_before_any_action():
    async def main():
        game, client = await _started(action_run_s=0.2)
        try:
            observation = await client.observe_once()
            for overrides in (
                {"interval_s": 0.0},
                {"interval_s": -0.1},
                {"interval_s": float("nan")},
                {"interval_s": float("inf")},
                {"max_samples": 0},
                {"max_samples": -3},
                {"max_samples": True},
                {"max_samples": 1.5},
                {"max_samples": 1025},
            ):
                with pytest.raises(ValueError):
                    await client.act_with_inspections(**_sampling(observation, **overrides))

            assert game.act_requests == []
            assert game.inspection_times == []
            assert game.applied() == []
        finally:
            await _closed(game, client)

    asyncio.run(main())


def test_act_still_collects_running_and_terminal_records():
    async def main():
        game, client = await _started(action_run_s=0.05)
        try:
            observation = await client.observe_once()
            records = await client.act(
                action_id="plain-1",
                choice_id="plain-1",
                action="hold",
                params={"tag": "plain"},
                based_on_seq=observation.seq,
                max_age_s=2.0,
                lease_s=10.0,
            )

            assert [record.status for record in records] == [
                ExecStatus.running,
                ExecStatus.completed,
            ]
            assert game.applied() == ["hold"]
        finally:
            await _closed(game, client)

    asyncio.run(main())
