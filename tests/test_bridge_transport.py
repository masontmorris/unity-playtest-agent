"""Transport checks: subscribe, clock and session binding.

These cover the verbs the invariant scenarios do not touch, against the
same production client and fake game.
"""

from __future__ import annotations

import asyncio

import pytest

from playtest.contracts.common import ClockMode
from playtest.core.bridge_client import BridgeClient, BridgeError

from fake_game import FakeGame

TOKEN = "transport-test-token"


async def _started(**kwargs):
    game = FakeGame(token=TOKEN, **kwargs)
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return game, client, port


def test_subscribe_streams_and_unsubscribe_drops():
    async def main():
        game, client, _port = await _started()
        try:
            await client.subscribe(rate_hz=60.0)
            first = await client.next_observation()
            second = await client.next_observation()
            third = await client.next_observation()
            assert first.seq < second.seq < third.seq
            assert set(third.public) == {"entity", "counter"}
            assert third.frame_ref is None and third.ui is None
            await client.unsubscribe()
            client._timeout_s = 0.2
            with pytest.raises(BridgeError):
                await client.next_observation()
        finally:
            await client.aclose()
            await game.stop()

    asyncio.run(main())


def test_clock_pause_step_and_rejected_clock():
    async def main():
        game, client, _port = await _started()
        try:
            ack = await client.clock("pause")
            assert ack.type == "status"
            frozen = await client.observe_once()
            assert frozen.paused is True
            again = await client.observe_once()
            assert again.game_s == frozen.game_s
            await client.clock("resume")
            assert (await client.observe_once()).paused is False
        finally:
            await client.aclose()
            await game.stop()

        stepped, stepper, _port2 = await _started(clock_mode=ClockMode.stepped)
        try:
            before = await stepper.observe_once()
            assert before.game_s == 0.0
            await stepper.clock("step", step_game_s=0.5)
            after = await stepper.observe_once()
            assert after.game_s == 0.5
            assert after.clock_mode is ClockMode.stepped
        finally:
            await stepper.aclose()
            await stepped.stop()

        locked, stranger, _port3 = await _started(clock_control=False)
        try:
            with pytest.raises(BridgeError) as exc_info:
                await stranger.clock("pause")
            assert exc_info.value.code == "invalid"
        finally:
            await stranger.aclose()
            await locked.stop()

    asyncio.run(main())


def test_second_hello_is_busy_first_session_survives():
    async def main():
        game, first, port = await _started()
        try:
            second = BridgeClient("127.0.0.1", port, TOKEN)
            with pytest.raises(BridgeError) as exc_info:
                await second.connect()
            assert exc_info.value.code == "busy"
            await second.aclose()
            obs = await first.observe_once()
            assert obs.public["counter"] == 0
        finally:
            await first.aclose()
            await game.stop()

    asyncio.run(main())


def test_wrong_token_is_refused():
    async def main():
        game = FakeGame(token=TOKEN)
        port = await game.start()
        try:
            stranger = BridgeClient("127.0.0.1", port, "wrong-token")
            with pytest.raises(BridgeError) as exc_info:
                await stranger.connect()
            assert exc_info.value.code == "auth"
            await stranger.aclose()
        finally:
            await game.stop()

    asyncio.run(main())


def test_inspect_round_trip_and_concurrent_requests():
    async def main():
        game, client, _port = await _started()
        try:
            first, second = await asyncio.gather(
                client.inspect("inventory"),
                client.inspect("discovery"),
            )
            assert first.type == "inspection"
            assert first.stage == "inventory"
            assert second.stage == "discovery"
            assert first.in_reply_to < second.in_reply_to
            assert first.caps["records"].limit == 64
            assert first.caps["records"].observed == 2
            assert first.caps["records"].truncated is False
            assert second.data["stage"] == "discovery"
        finally:
            await client.aclose()
            await game.stop()

    asyncio.run(main())


def test_inspect_error_is_typed():
    async def main():
        game = FakeGame(token=TOKEN, inspect_supported=False)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        try:
            with pytest.raises(BridgeError) as exc_info:
                await client.inspect("inventory")
            assert exc_info.value.code == "internal"
            assert exc_info.value.in_reply_to is not None
        finally:
            await client.aclose()
            await game.stop()

    asyncio.run(main())


def test_inspect_rejects_a_reply_for_the_wrong_stage():
    class WrongStageGame(FakeGame):
        def _inspection(self, request, seq, session):
            return super()._inspection(request, seq, session).model_copy(update={"stage": "discovery"})

    async def main():
        game = WrongStageGame(token=TOKEN)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        try:
            with pytest.raises(BridgeError) as exc_info:
                await client.inspect("inventory")
            assert exc_info.value.code == "protocol"
            assert client.closed
        finally:
            await client.aclose()
            await game.stop()

    asyncio.run(main())


def test_inspect_times_out_without_corrupting_the_connection():
    async def main():
        game = FakeGame(token=TOKEN, inspect_silent=True)
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN)
        await client.connect()
        try:
            client._timeout_s = 0.2
            with pytest.raises(BridgeError) as exc_info:
                await client.inspect("inventory")
            assert exc_info.value.code == "timeout"
            assert (await client.observe_once()).public["counter"] == 0
        finally:
            await client.aclose()
            await game.stop()

    asyncio.run(main())
