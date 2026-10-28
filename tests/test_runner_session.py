"""The runner end to end against the fake game: the probe, the driver's stop rules, the run output and the CLI.

The production client, loop, choices, probe and driver run for real; only S1 (a scripted port or a fake
`send`) and the game (a protocol peer) are fakes. No network, no Unity, no key.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from loop_fakes import TOKEN, FakeHelp, FakeS1, GameThread, TransformsGame, fail, pick
from runner_manifest import action, hold, manifest
from s1_fakes import Send, approval, reply
from turn_trace_check import problems

from playtest.contracts.bridge import ExecStatus, ExecutionRecord
from playtest.contracts.investigation import RunRecord
from playtest.contracts.loop import TurnOutcome
from playtest.core.bridge_client import BridgeClient, BridgeError
from playtest.core.execution import Execution
from playtest.core.ports import S1Answer, S1Call
from playtest.runner import cli
from playtest.runner.choices import build_choices
from playtest.runner.output import build_run_record, close_trace, open_trace, secret_values, write_trace
from playtest.runner.probe import DisplacementProbe, make_snapshot
from playtest.runner.session import run_session

OBJECTIVE = "Try each control once, then finish."


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """No test sees, scans for or can print a credential from the machine's own environment."""
    for name in [n for n in os.environ if n.endswith("_API_KEY")]:
        monkeypatch.delenv(name)


def two_actions():
    """`move` runs the fake game's `increment`, `look` runs its `hold`; both are held for their manifest lease."""
    return manifest(
        action("move", "w", hold_s=0.2, holds=[hold(0.2)]),
        action("look", "a", hold_s=0.25, holds=[hold(0.25, mean=0.0, effective=False)]),
        controls={"w": "increment", "a": "hold"})


def harness_trace(clock_wall: float = 600.0):
    record = build_run_record(
        run_id="r1", project=Path("game"), objective=OBJECTIVE, s1_provider="fake", s1_model="fake-1",
        prompt_sha256="0" * 64, wall_limit_s=clock_wall, max_usd=None, manifest_fingerprint="f" * 64,
        game_commit=None, unity_version=None, bridge_version=None, input_mode=None)
    return open_trace(record)


def ticking(step: float = 1.0):
    """A clock that advances `step` seconds each time it is read, so a wall limit is reached in a set number of turns."""
    state = {"now": -step}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


async def connected(game_class=TransformsGame, **kwargs):
    game = game_class(token=TOKEN, **kwargs)
    port = await game.start()
    client = BridgeClient("127.0.0.1", port, TOKEN)
    await client.connect()
    return game, client


async def closed(game, client) -> None:
    try:
        await client.aclose()
    finally:
        await game.stop()


# --- the effect probe ---------------------------------------------------------------------


class Readings:
    """A snapshot source that yields scripted readings (or raises), and counts how often it is read."""

    def __init__(self, *items: Any) -> None:
        self._items = list(items)
        self.reads = 0

    async def __call__(self) -> dict[str, Any]:
        self.reads += 1
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def reading(seq: int, x: float, game_s: float) -> dict[str, Any]:
    return {"inspection_seq": seq, "position": [x, 0.0, 0.0], "game_time_s": game_s}


def terminal(duration: float | None = 0.21) -> Execution:
    wall = datetime(2026, 10, 28, tzinfo=timezone.utc)
    record = ExecutionRecord(session="s", seq=3, game_s=1.0, wall_utc=wall, action_id="a", status=ExecStatus.completed,
                             applied_duration_s=duration, release_verified=True)
    return Execution(request={}, records=(record,))


def probe_for(readings: Readings, expected=None):
    return DisplacementProbe(readings, {"move": expected})


EXPECTED = {"source": "calibration", "control": "w", "requested_hold_s": 0.2, "net_displacement": 6.04,
            "sample_variance": 0.0001, "valid_trials": 2, "effective_in_all_repeats": True, "unit": "world units"}


def test_the_probe_reports_expected_and_observed_displacement_around_one_input():
    async def main():
        readings = Readings(reading(10, 1.0, 5.0), reading(12, 7.12, 6.5))
        probe = probe_for(readings, EXPECTED)
        move = build_choices(manifest(action("move", holds=[hold(0.2)]))).choices[0]
        await probe.before_turn(3)
        assert readings.reads == 1                     # one reading before the turn's observation...
        report = await probe.measure(move, terminal())
        assert readings.reads == 2                     # ...and one right after the terminal record, no others
        assert report.expected == EXPECTED
        assert report.observed["net_displacement"] == pytest.approx(6.12)
        assert (report.observed["from_inspection_seq"], report.observed["to_inspection_seq"]) == (10, 12)
        assert report.observed["window_game_s"] == pytest.approx(1.5) and report.observed["applied_duration_s"] == 0.21
        assert report.fact == ("move: net displacement between inspection seq 10 and 12: 6.120 world units; "
                               "calibrated for a 0.2-second hold: 6.040 (sample variance 0.000100, n=2).")

    asyncio.run(main())


def test_a_reading_that_cannot_be_taken_leaves_the_observed_value_null_not_zero():
    async def main():
        move = build_choices(manifest(action("move", holds=[hold(0.2)]))).choices[0]
        failed = Readings(ValueError("the selected entity is missing from the transform snapshot"), reading(2, 5.0, 1.0))
        probe = probe_for(failed, EXPECTED)
        await probe.before_turn(0)
        report = await probe.measure(move, terminal())
        assert report.observed is None and report.expected == EXPECTED
        assert report.fact == "move: net displacement was not measured (the selected entity is missing from the transform snapshot)."
        missing_after = probe_for(Readings(reading(1, 0.0, 0.0), ValueError("transform snapshot has no objects list")))
        await missing_after.before_turn(0)
        report = await missing_after.measure(move, terminal())
        assert report.observed is None and report.expected is None and "not measured" in report.fact

    asyncio.run(main())


def test_an_input_without_a_calibrated_value_still_reports_what_was_observed():
    async def main():
        probe = probe_for(Readings(reading(1, 0.0, 0.0), reading(2, 3.0, 1.0)), None)
        move = build_choices(manifest(action("move", holds=[hold(0.2)]))).choices[0]
        await probe.before_turn(0)
        report = await probe.measure(move, terminal(duration=None))
        assert report.expected is None and report.observed["net_displacement"] == 3.0
        assert report.observed["applied_duration_s"] is None
        assert report.fact.endswith("3.000 world units; no calibrated value for this hold.")

    asyncio.run(main())


def test_a_closed_bridge_is_not_swallowed_by_the_probe():
    async def main():
        probe = probe_for(Readings(BridgeError("closed", "connection is closed")))
        with pytest.raises(BridgeError):
            await probe.before_turn(0)

    asyncio.run(main())


def test_the_live_probe_measures_a_real_displacement_on_the_protocol_peer():
    async def main():
        game, client = await connected()
        try:
            snapshot = make_snapshot(client, "entity")
            before = await snapshot()
            assert before["position"] == [0.0, 0.0, 0.0] and isinstance(before["inspection_seq"], int)
            built = build_choices(two_actions())
            probe = DisplacementProbe(snapshot, built.expected)
            await probe.before_turn(0)
            observation = await client.observe_once()
            records = await client.act(action_id="a1", choice_id="move", action="increment", params={},
                                       based_on_seq=observation.seq, max_age_s=2.0, lease_s=0.2)
            report = await probe.measure(built.choices[0], Execution(request={}, records=tuple(records)))
            assert report.observed["net_displacement"] == pytest.approx(6.0)
            assert report.expected["net_displacement"] == 6.04
            assert report.observed["to_inspection_seq"] > report.observed["from_inspection_seq"]
        finally:
            await closed(game, client)

    asyncio.run(main())


# --- the session driver -------------------------------------------------------------------


def test_a_session_runs_manifest_actions_with_their_leases_and_finishes():
    async def main():
        game, client = await connected()
        built = build_choices(two_actions())
        sink = harness_trace()
        try:
            s1 = FakeS1("move", "look", "wait", "finish")
            result = await run_session(
                client, built, s1=s1, objective=OBJECTIVE, probe=DisplacementProbe(make_snapshot(client, "entity"), built.expected),
                sink=sink, max_age_s=2.0, wall_limit_s=60.0)
            assert (result.status, result.ending, result.error) == ("complete", "finish", None)
            assert result.turns == 4 and result.outcomes == {"executed": 2, "finished": 2}
            assert result.stop is not None and result.stop.release_verified is True and result.adherence == []
            assert game.applied() == ["increment", "hold"]
            # Each input ran under the lease its manifest action declares, not the bridge's default.
            leases = {e["body"]["request"]["choice_id"]: e["body"]["request"]["lease_s"] for e in sink.of_kind("execution")}
            assert leases == {"move": 0.2, "look": 0.25}
            assert sink.of_kind("execution")[0]["body"]["request"]["action"] == "increment"
            effects = {e["body"]["choice_id"]: e["body"] for e in sink.of_kind("effect")}
            assert effects["move"]["observed"]["net_displacement"] == pytest.approx(6.0)
            assert effects["move"]["expected"]["net_displacement"] == 6.04
            assert effects["look"]["observed"]["net_displacement"] == pytest.approx(0.0)
            assert any(f.startswith("move: net displacement between inspection seq") for f in s1.contexts[1]["facts"])
            assert s1.contexts[0]["objective"] == OBJECTIVE
            assert [c["id"] for c in s1.contexts[0]["choices"]] == ["move", "look", "wait", "escalate", "delegate", "finish"]
            assert ("held", "stop") in game.releases() or ("held", "expired") in game.releases()
            close_trace(sink, status=result.status, error=result.error, turns=result.turns, secrets=[])
            assert problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_the_wall_clock_limit_ends_a_run_normally():
    async def main():
        game, client = await connected()
        try:
            result = await run_session(client, build_choices(two_actions()), s1=FakeS1(*["wait"] * 50),
                                       objective=OBJECTIVE, wall_limit_s=3.0, clock=ticking())
            assert (result.status, result.ending) == ("complete", "wall_limit") and result.turns == 2
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_consecutive_provider_failures_stop_the_run_and_a_good_turn_resets_the_count():
    async def main():
        game, client = await connected()
        try:
            steps = [fail("transport error"), fail("transport error"), "wait", fail("transport error"),
                     fail("HTTP error (HTTP 500)"), fail("transport error"), "wait"]
            sink = harness_trace()
            result = await run_session(client, build_choices(two_actions()), s1=FakeS1(*steps), objective=OBJECTIVE,
                                       sink=sink, wall_limit_s=60.0)
            assert (result.status, result.ending) == ("stopped", "provider_failures")
            assert result.turns == 6 and result.provider_failures == 5      # the wait at turn 2 reset the streak
            assert result.error == "3 consecutive provider failures"
            assert game.applied() == [] and result.stop.release_verified is True
            stop = sink.of_kind("stop")[0]["body"]
            assert stop["reason"] == "error" and stop["error"] == "3 consecutive provider failures"
            assert [t.outcome for t in result.loop.turns].count(TurnOutcome.rejected_invalid) == 5
        finally:
            await closed(game, client)

        game, client = await connected()
        try:
            steps = [fail("transport error"), fail("transport error"), "wait", fail("transport error"),
                     fail("transport error"), "finish"]
            result = await run_session(client, build_choices(two_actions()), s1=FakeS1(*steps), objective=OBJECTIVE,
                                       wall_limit_s=60.0)
            assert (result.status, result.ending, result.turns) == ("complete", "finish", 6)
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_failure_limit_of_one_stops_at_the_first_failure():
    async def main():
        game, client = await connected()
        try:
            result = await run_session(client, build_choices(two_actions()), s1=FakeS1(fail("x"), "wait"),
                                       objective=OBJECTIVE, max_provider_failures=1)
            assert (result.ending, result.turns) == ("provider_failures", 1)
            with pytest.raises(ValueError):
                await run_session(client, build_choices(two_actions()), s1=FakeS1(), objective=OBJECTIVE,
                                  max_provider_failures=0)
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_port_that_asks_to_halt_stops_the_run_after_recording_its_turn():
    async def main():
        game, client = await connected()
        try:
            halt = S1Answer(answer=None, calls=(S1Call(problem="the run's call cap was reached"),),
                            failure="the run's call cap was reached", halt="the run's call cap was reached")
            result = await run_session(client, build_choices(two_actions()), s1=FakeS1("wait", halt, "wait"),
                                       objective=OBJECTIVE, wall_limit_s=60.0)
            assert (result.status, result.ending, result.turns) == ("stopped", "halt", 2)
            assert result.error == "the run's call cap was reached"
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_a_lost_bridge_ends_the_run_inconclusive_with_the_cause_and_still_stops():
    async def main():
        game, client = await connected()
        sink = harness_trace()
        try:
            def cut(_context):
                client._writer.close()          # an abrupt disconnect: no stop is sent
                return "wait"

            result = await run_session(client, build_choices(two_actions()), s1=FakeS1("look", cut, "wait"),
                                       objective=OBJECTIVE, sink=sink, max_age_s=2.0, wall_limit_s=60.0)
            assert (result.status, result.ending) == ("inconclusive", "error")
            assert "BridgeError" in result.error and "input release was not verified" in result.error
            assert result.stop is None
            stop = sink.of_kind("stop")[0]["body"]
            assert stop["reason"] == "error" and stop["result"] is None and "BridgeError" in stop["error"]
            for _ in range(200):                # the bridge releases what was held when the connection drops
                if ("held", "disconnect") in game.releases():
                    break
                await asyncio.sleep(0.01)
            assert ("held", "disconnect") in game.releases()
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_an_input_whose_record_never_arrives_is_traced_then_stopped_and_released():
    async def main():
        game = TransformsGame(token=TOKEN, action_run_s=5.0, action_stall=True)    # accepts, never completes
        port = await game.start()
        client = BridgeClient("127.0.0.1", port, TOKEN, timeout_s=0.3)
        await client.connect()
        sink = harness_trace()
        try:
            result = await run_session(client, build_choices(two_actions()), s1=FakeS1("look", "wait"),
                                       objective=OBJECTIVE, sink=sink, max_age_s=2.0, wall_limit_s=60.0)
            assert (result.status, result.ending) == ("inconclusive", "error") and result.turns == 0
            assert "BridgeError" in result.error and "timeout" in result.error
            # The request that may have reached the bridge stays in the trace, with no records to show.
            sent = sink.of_kind("execution")[0]["body"]
            assert sent["request"]["choice_id"] == "look" and sent["records"] == []
            # The stop released and verified the input, but a run that lost an input's record is not complete.
            assert result.stop is not None and result.stop.release_verified is True
            assert sink.of_kind("stop")[0]["body"]["result"]["release_verified"] is True
            assert [t for t in problems(sink.entries) if "stops before its turn entry" in t]    # reported, not skipped
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_an_escalation_with_no_helper_stays_pending_until_the_run_stops_and_is_cancelled_there():
    async def main():
        game, client = await connected()
        sink = harness_trace()
        try:
            result = await run_session(client, build_choices(two_actions()),
                                       s1=FakeS1(pick("escalate"), "wait", "wait", "finish"), objective=OBJECTIVE, sink=sink,
                                       wall_limit_s=60.0)
            assert (result.status, result.ending) == ("complete", "finish")
            assert [e["body"]["event"] for e in sink.of_kind("help")] == ["opened", "cancelled"]
            assert result.loop.helper_count == 0 and result.loop.cancelled_helpers == 1
            close_trace(sink, status=result.status, error=None, turns=result.turns, secrets=[])
            assert problems(sink.entries) == []
        finally:
            await closed(game, client)

    asyncio.run(main())


def test_help_that_finishes_is_adopted_and_shown_on_a_session():
    async def main():
        game, client = await connected()
        helper = FakeHelp(result={"advice": "try look"})
        helper.release.set()
        try:
            s1 = FakeS1(pick("escalate"), "wait", "wait", "finish")
            result = await run_session(client, build_choices(two_actions()), s1=s1, objective=OBJECTIVE, help=helper,
                                       wall_limit_s=60.0)
            assert result.status == "complete" and len(helper.requests) == 1
            assert result.loop.adoptions == [{"advice": "try look"}]
            # The helper finishes while turn 1 awaits the bridge, so it is adopted at the top of turn 2.
            assert s1.contexts[1]["pending_help"] is not None and "guidance" not in s1.contexts[1]
            assert s1.contexts[2]["pending_help"] is None and s1.contexts[2]["guidance"][0]["source"] == "fake-helper"
        finally:
            await closed(game, client)

    asyncio.run(main())


# --- run output ---------------------------------------------------------------------------


def test_the_trace_file_opens_with_the_run_record_closes_with_end_and_never_holds_a_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-secret-value-1234")
    monkeypatch.setenv("SHORT_API_KEY", "short")        # too short to be a credential worth scanning for
    assert secret_values() == ["sk-test-secret-value-1234"]

    async def main():
        game, client = await connected()
        sink = harness_trace()
        try:
            echo = S1Answer(answer={"choice_id": "move", "set_version": 0},
                            calls=(S1Call(question="next_action", response={"echoed": "sk-test-secret-value-1234"}),))
            result = await run_session(client, build_choices(two_actions()),
                                       s1=FakeS1(lambda _c: echo, "finish"), objective=OBJECTIVE, sink=sink,
                                       max_age_s=2.0, wall_limit_s=60.0)
            secrets = secret_values()
            assert close_trace(sink, status=result.status, error=result.error, turns=result.turns, secrets=secrets) == 1
            path = tmp_path / "trace.jsonl"
            write_trace(path, sink.entries, secrets)
            return path, result
        finally:
            await closed(game, client)

    path, result = asyncio.run(main())
    text = path.read_text(encoding="utf-8")
    assert "sk-test-secret-value-1234" not in text and "[redacted]" in text
    entries = [json.loads(line) for line in text.splitlines()]
    assert entries[0]["kind"] == "run" and entries[-1]["kind"] == "end" and entries[-1]["body"]["redactions"] == 1
    record = RunRecord.model_validate(entries[0]["body"])
    assert record.models[0].role == "s1" and record.clock_mode.value == "realtime" and record.game_commit == "unknown"
    assert [e["trace_seq"] for e in entries] == list(range(len(entries)))
    with pytest.raises(FileExistsError):
        write_trace(path, [], [])                          # a trace is never overwritten
    assert result.status == "complete"


# --- the command line ---------------------------------------------------------------------


class Project:
    """A scratch Unity project directory whose bridge lock advertises the fake game, plus every input file."""

    def __init__(self, tmp_path: Path, port: int, *, actions=None, entity_id: str = "entity", calls: int = 12) -> None:
        self.root = tmp_path
        self.project = tmp_path / "game"
        (self.project / "Temp").mkdir(parents=True)
        (self.project / "Temp" / "playtest-bridge.lock").write_text(json.dumps({
            "protocol": "upa/0", "pid": 4242, "port": port, "token": TOKEN, "started_utc": "2026-10-28T00:00:00Z"}),
            encoding="utf-8")
        built = actions if actions is not None else two_actions()
        self.manifest = tmp_path / "manifest.json"
        self.manifest.write_text(built.model_dump_json(), encoding="utf-8")
        self.entity = tmp_path / "entity.json"
        self.entity.write_text(json.dumps({"status": "identified", "entity_id": entity_id}), encoding="utf-8")
        self.calibration = tmp_path / "calibration.json"
        self.calibration.write_text(json.dumps({"entity_id": entity_id}), encoding="utf-8")
        self.approval = tmp_path / "approval.json"
        self.approval.write_text(json.dumps(approval(calls)), encoding="utf-8")
        self.out = tmp_path / "out"
        self.summary = self.out / "run.json"

    def args(self, **overrides: Any) -> list[str]:
        values: dict[str, Any] = {
            "project": self.project, "entity": self.entity, "calibration": self.calibration, "output": self.summary,
            "manifest": self.manifest, "approval": self.approval, "max-calls": 6, "wall-limit-s": 60, "max-age-s": 2.0,
            "objective": OBJECTIVE}
        values.update({key.replace("_", "-"): value for key, value in overrides.items()})
        flags: list[str] = []
        for key, value in values.items():
            if value is not None:
                flags += [f"--{key}", str(value)]
        return flags


def with_fake_send(monkeypatch, send):
    real = cli.SystemOneS1
    monkeypatch.setattr(cli, "SystemOneS1", lambda ledger, *, max_calls: real(ledger, max_calls=max_calls, send=send))


def test_the_cli_runs_a_manifest_session_end_to_end_and_leaves_a_clean_record(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0001")
    with GameThread(TransformsGame) as thread:
        files = Project(tmp_path, thread.port)
        send = Send(files.out, reply("next_action", "move"), reply("next_action", "look"), reply("next_action", "finish"))
        with_fake_send(monkeypatch, send)
        assert cli.main(files.args()) == 0
        assert thread.game.applied() == ["increment", "hold"]
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["ending"], summary["turns"]) == ("complete", "finish", 3)
    assert summary["adherence"] == {"rate": 1.0, "violations": []} and summary["stop"]["release_verified"] is True
    assert summary["s1"]["calls"] == 3 and summary["s1"]["call_cap"] == 6 and summary["manifest"]["not_offered"] == []
    assert summary["manifest"]["offered"] == ["move", "look", "wait", "escalate", "delegate", "finish"]
    assert summary["game_worktree_changed"] is None and summary["provider_failures"] == 0
    entries = [json.loads(line) for line in (files.out / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert problems(entries) == [] and entries[0]["kind"] == "run" and entries[-1]["body"]["status"] == "complete"
    run_record = RunRecord.model_validate(entries[0]["body"])
    assert run_record.models[0].model == "decision-models/baseten-mammoth-27b-latest" and run_record.objective == OBJECTIVE
    assert run_record.models[0].prompt_hash == summary["s1"]["prompt_sha256"] != "unknown"
    assert len(list((files.out / "calls").glob("*.json"))) == 3 and (files.out / "plan.json").is_file()
    plan = json.loads((files.out / "plan.json").read_text(encoding="utf-8"))
    assert plan["max_calls"] == 6 and plan["offered"][:2] == ["move", "look"]
    everything = "".join(p.read_text(encoding="utf-8") for p in files.out.rglob("*") if p.is_file())
    assert "sk-test-cli-key-0001" not in everything and "sk-test-cli-key-0001" not in capsys.readouterr().out


def test_the_cli_stops_after_consecutive_provider_failures_and_exits_non_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0002")
    with GameThread(TransformsGame) as thread:
        files = Project(tmp_path, thread.port)
        broken = (500, None, 5.0, "HTTP error")
        with_fake_send(monkeypatch, Send(files.out, broken, broken, broken))
        assert cli.main(files.args(**{"max_provider_failures": 3})) == 1
        assert thread.game.applied() == []
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    assert (summary["status"], summary["ending"], summary["turns"]) == ("stopped", "provider_failures", 3)
    assert summary["stop"]["release_verified"] is True and summary["s1"]["calls"] == 3


def test_the_cli_refuses_to_start_without_the_approval_the_cap_or_the_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0003")
    files = Project(tmp_path, 1)
    for overrides in ({"approval": None}, {"max_calls": None}, {"manifest": None}):
        with pytest.raises(SystemExit) as refused:
            cli.main(files.args(**overrides))
        assert refused.value.code == 2
    monkeypatch.delenv("BASETEN_API_KEY")
    with pytest.raises(SystemExit) as no_key:
        cli.main(files.args())
    assert no_key.value.code == 2
    assert not files.out.exists()                      # nothing was created, nothing was contacted


def test_the_cli_refuses_an_output_inside_the_project_or_over_existing_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0004")
    files = Project(tmp_path, 1)
    with pytest.raises(SystemExit) as inside:
        cli.main(files.args(output=files.project / "run.json"))
    assert inside.value.code == 2
    files.out.mkdir()
    files.summary.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as existing:
        cli.main(files.args())
    assert existing.value.code == 2 and files.summary.read_text(encoding="utf-8") == "{}"


def test_the_cli_refuses_inputs_that_do_not_agree_before_any_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "sk-test-cli-key-0005")
    files = Project(tmp_path / "a", 1, entity_id="someone-else")
    assert cli.main(files.args()) == 1                 # the manifest, entity and calibration disagree
    small = Project(tmp_path / "b", 1, calls=2)
    assert cli.main(small.args(max_calls=5)) == 1       # the approved envelope is smaller than the cap
    no_action = Project(tmp_path / "c", 1, actions=manifest(action("look", "a", axis=True, holds=[hold(0.2)]),
                                                            controls={"a": "hold"}))
    assert cli.main(no_action.args()) == 1              # nothing the bridge can execute
    for case in (files, small, no_action):
        assert not (case.out / "plan.json").exists() and not (case.out / "calls").exists()
