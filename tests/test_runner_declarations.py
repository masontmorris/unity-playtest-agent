"""A supported declaration runs exactly as declared, through the real exporter, choice builder, loop and host seam.

Each test starts at a manifest whose declarations carry exact release-verified evidence, writes the catalog
with the real exporter CLI, lets the client verify it and hands it to `CatalogGame` (the host contract's test
double), builds S1's choices with the production builder and runs the production loop. What S1 read, what the
request named, what the host ran and what its receipts report are then compared declaration by declaration.
A chord that runs as its first key, a mouse or stick value that is lost, a skill that is never offered, or a
skill whose bound is replaced fails here. Unsupported declarations are never offered and never run, and a
rejection is data: nothing is retried, repaired or substituted.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

from catalog_game import CatalogGame
from loop_fakes import TOKEN, FakeS1, Sleeps, pick
from runner_manifest import action, all_modes, hold, manifest, skill
from turn_trace_check import problems

from playtest.contracts.action_catalog import declaration_requirements, receipt_problems
from playtest.contracts.bridge import ExecStatus, ExecutionRecord, RejectReason
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Budget, RunRecord
from playtest.contracts.loop import ChoiceKind, TurnOutcome
from playtest.contracts.trace import TraceAuthor, TraceKind
from playtest.core.adherence import check_adherence
from playtest.core.bridge_client import BridgeClient
from playtest.core.execution import ExecutionController
from playtest.core.trace_sink import MemorySink
from playtest.core.turn_loop import TurnLoop
from playtest.preflight import catalog as exporter
from playtest.preflight.catalog import export, serialize
from playtest.runner.catalog import verify_host_catalog
from playtest.runner.choices import build_choices
from playtest.runner.probe import DisplacementProbe, make_snapshot
from playtest.runner.session import run_session

OBJECTIVE = "Run each declared input once, then finish."
EXPORTED = serialize(export(all_modes()))["actions"]
SUPPORTED = list(EXPORTED)           # the catalog file lists names alphabetically; the order that matters is the manifest's
MANIFEST_ORDER = ["key_hold", "key_chord", "device_chord", "pointer_impulse", "stick_value", "trigger_value",
                  "dpad_value", "pad_button", "mouse_button", "key_sequence", "pointer_sequence"]


def harness_trace() -> MemorySink:
    sink = MemorySink()
    record = RunRecord(run_id="r1", game_project="game", clock_mode=ClockMode.realtime, models=[], objective=OBJECTIVE,
                       budget=Budget(), core_version="0.0.1")
    sink.append(TraceKind.run, record.model_dump(mode="json"), author=TraceAuthor.harness)
    return sink


def end_trace(sink: MemorySink, result) -> None:
    """The harness's closing entry, as `TraceWriter.close` writes it."""
    sink.append(TraceKind.end, {"status": result.status, "error": result.error, "turns": result.turns,
                                "redactions": 0}, author=TraceAuthor.harness)


async def serve(tmp_path: Path, value, *, effects=None, **game_kwargs):
    """The catalog the real exporter writes, verified by the client, served by the host double; plus S1's choices."""
    source, path = tmp_path / "manifest.json", tmp_path / "catalog.json"
    source.write_text(value.model_dump_json(), encoding="utf-8")
    assert exporter.main(["--manifest", str(source), "--output", str(path)]) == 0
    host = verify_host_catalog(value, path, hashlib.sha256(path.read_bytes()).hexdigest())
    game = CatalogGame(token=TOKEN, catalog=host.catalog, effects=effects, **game_kwargs)
    client = BridgeClient("127.0.0.1", await game.start(), TOKEN)
    await client.connect()
    return game, client, build_choices(value)


async def close(game, client) -> None:
    try:
        await client.aclose()
    finally:
        await game.stop()


async def run(game, client, built, *steps, probe: bool = False, **kwargs):
    sink = harness_trace()
    s1 = FakeS1(*steps)
    result = await run_session(
        client, built, s1=s1, objective=OBJECTIVE, sink=sink, max_age_s=2.0, wall_limit_s=60.0, sleep=Sleeps(),
        probe=DisplacementProbe(make_snapshot(client, "entity"), built.expected) if probe else None, **kwargs)
    return result, s1, sink


def executions(sink: MemorySink) -> list[dict[str, Any]]:
    return [entry["body"] for entry in sink.of_kind("execution")]


def declared_inputs(definition: dict[str, Any]) -> list[tuple[int, str, str, float | None, float | None]]:
    steps = definition["steps"] if "steps" in definition else [{"inputs": definition["inputs"]}]
    return [(index, item["device"], item["control"], item["x"], item["y"])
            for index, step in enumerate(steps) for item in step["inputs"]]


@pytest.mark.parametrize("name", MANIFEST_ORDER)
def test_every_proven_declaration_runs_exactly_as_declared(tmp_path: Path, name: str):
    async def main():
        value = all_modes()
        game, client, built = await serve(tmp_path, value)
        try:
            result, s1, sink = await run(game, client, built, name, "finish")
            assert (result.status, result.ending, result.error) == ("complete", "finish", None)
            # What S1 chose is what was bound and executed: raw, effective and executed agree for the whole name.
            first = result.loop.turns[0]
            assert (first.raw_choice, first.effective_choice, first.executed_action, first.outcome) == (
                name, name, name, TurnOutcome.executed)
            assert check_adherence(result.loop.turns) == [] and result.stop.release_verified is True
            offered = next(c for c in s1.contexts[0]["choices"] if c["id"] == name)
            declared = EXPORTED[name]
            is_skill = "steps" in declared
            assert offered["kind"] == ("skill" if is_skill else "action")
            assert offered["skill" if is_skill else "action"] == name and ("action" if is_skill else "skill") not in offered
            # The request names the declaration itself, carries no params, and a skill carries no lease.
            (sent,) = executions(sink)
            request = sent["request"]
            assert (request["choice_id"], request["action"], request["params"]) == (name, name, {})
            assert request["lease_s"] == (None if is_skill else declared["default_lease_s"])
            # The host ran exactly the exported declaration: every input, value, hold, wait and bound.
            (ran,) = game.runs()
            assert (ran["action"], ran["choice_id"], ran["declaration"]) == (name, name, declared)
            record = ExecutionRecord.model_validate(sent["records"][-1])
            assert record.status is ExecStatus.completed and record.release_verified is True
            assert [(r.step_index, r.device, r.control, r.x, r.y) for r in record.applied_inputs] == declared_inputs(declared)
            definition = export(value).actions[name]
            assert receipt_problems(
                declaration_requirements(definition), record.applied_inputs, declared=definition,
                applied_total_s=record.applied_duration_s, terminal_start_s=record.input_start_wall_s,
                terminal_end_s=record.input_end_wall_s) == []
            end_trace(sink, result)
            assert problems(sink.entries) == []
        finally:
            await close(game, client)

    asyncio.run(main())


def test_the_whole_manifest_runs_in_order_and_each_turn_runs_what_s1_chose(tmp_path: Path):
    async def main():
        game, client, built = await serve(tmp_path, all_modes(), effects={"key_hold": (6.0, 0.0, 0.0)})
        try:
            result, s1, sink = await run(game, client, built, *MANIFEST_ORDER, "finish", probe=True)
            assert (result.status, result.ending, result.turns) == ("complete", "finish", len(MANIFEST_ORDER) + 1)
            assert [c["id"] for c in s1.contexts[0]["choices"]][:len(MANIFEST_ORDER)] == MANIFEST_ORDER
            assert game.applied() == MANIFEST_ORDER                               # each ran once, under its own name
            assert [t.executed_action for t in result.loop.turns][:-1] == MANIFEST_ORDER
            assert check_adherence(result.loop.turns) == [] and result.outcomes == {"executed": 11, "finished": 1}
            assert [r["declaration"] for r in game.runs()] == [EXPORTED[n] for n in MANIFEST_ORDER]
            assert not [n for n in game.applied() if n.startswith("preflight_key_")]   # no per-key action ever ran
        finally:
            await close(game, client)

    asyncio.run(main())


def test_a_chord_never_runs_as_its_first_key(tmp_path: Path):
    async def main():
        game, client, built = await serve(tmp_path, manifest(action("strafe", ["w", "a"], hold_s=0.2)))
        try:
            result, _, sink = await run(game, client, built, "strafe", "finish")
            assert result.outcomes == {"executed": 1, "finished": 1}
            (ran,) = game.runs()
            assert [(r["control"], r["step_index"]) for r in ran["receipts"]] == [("w", 0), ("a", 0)]
            assert ran["declaration"]["inputs"] == [
                {"device": "Keyboard", "control": "w", "x": None, "y": None},
                {"device": "Keyboard", "control": "a", "x": None, "y": None}]
            record = ExecutionRecord.model_validate(executions(sink)[0]["records"][-1])
            assert [r.control for r in record.applied_inputs] == ["w", "a"]      # the trace keeps every receipt
        finally:
            await close(game, client)

    asyncio.run(main())


def test_an_unsupported_declaration_is_never_offered_and_never_runs(tmp_path: Path):
    async def main():
        game, client, built = await serve(tmp_path, all_modes())
        try:
            assert {row["name"] for row in built.not_offered} == {"unproven_chord", "unproven_skill"}
            assert not {"unproven_chord", "unproven_skill"} & set(game.catalog.actions)   # the host does not hold them
            # Asked directly, the host holds no such declaration (and no legacy per-key action either).
            observation = await client.observe_once()
            for name in ("unproven_chord", "unproven_skill", "preflight_key_w"):
                (record,) = await client.act(action_id=f"x-{name}", choice_id=name, action=name, params={},
                                             based_on_seq=observation.seq, max_age_s=2.0)
                assert (record.status, record.reject_reason) == (ExecStatus.rejected, RejectReason.unknown_action)
            assert game.runs() == []
            # S1 names one anyway: the answer is rejected, nothing is sent, nothing is repaired, and the run goes on.
            result, s1, sink = await run(game, client, built, pick("unproven_chord"), "unproven_skill", "key_hold", "finish")
            assert [t.outcome for t in result.loop.turns] == [
                TurnOutcome.rejected_invalid, TurnOutcome.rejected_invalid, TurnOutcome.executed, TurnOutcome.finished]
            assert [t.raw_choice for t in result.loop.turns][:2] == ["unproven_chord", "unproven_skill"]
            assert [t.effective_choice for t in result.loop.turns][:2] == [None, None]
            assert game.applied() == ["key_hold"] and len(executions(sink)) == 1
            assert all(c["id"] not in ("unproven_chord", "unproven_skill") for c in s1.contexts[0]["choices"])
        finally:
            await close(game, client)

    asyncio.run(main())


def test_a_stale_rejection_runs_nothing_and_is_not_repaired(tmp_path: Path):
    async def main():
        game, client, built = await serve(tmp_path, all_modes(), reject_acts=RejectReason.stale_age)
        try:
            result, s1, sink = await run(game, client, built, "key_chord", "wait", "finish")
            first = result.loop.turns[0]
            assert (first.raw_choice, first.effective_choice, first.executed_action, first.outcome) == (
                "key_chord", "key_chord", None, TurnOutcome.rejected_stale)
            assert game.runs() == [] and len(executions(sink)) == 1             # one request, no retry, no other input
            assert executions(sink)[0]["request"]["action"] == "key_chord"
            assert "the bridge rejected key_chord (stale_age); no input was sent." in s1.contexts[1]["facts"][0]
            assert check_adherence(result.loop.turns) == []
        finally:
            await close(game, client)

    asyncio.run(main())


def test_a_skills_bound_is_never_replaced_by_the_default_action_lease(tmp_path: Path):
    async def main():
        game, client, built = await serve(tmp_path, all_modes())
        try:
            # A loop whose default lease would replace a bound if it were applied to a skill.
            loop = TurnLoop(client, choices=built.choices, max_age_s=2.0, lease_s=0.2, leases=built.leases,
                            objective=OBJECTIVE, s1=FakeS1("key_sequence", "key_hold", "finish"), sink=harness_trace(), sleep=Sleeps())
            for _ in range(3):
                await loop.run_turn()
            requests = [e["lease_s"] for e in (r.execution.request for t in loop.reports for r in t.executed)]
            assert requests == [None, 0.2]                                      # the skill's own bound; the action's own hold
            assert [r["declaration"]["max_s"] for r in game.runs() if "max_s" in r["declaration"]] == [1.0]
            # Were the default lease sent for the skill, the host refuses it: nothing runs under a replaced bound.
            observation = await client.observe_once()
            (replaced,) = await client.act(action_id="r", choice_id="key_sequence", action="key_sequence", params={},
                                           based_on_seq=observation.seq, max_age_s=2.0, lease_s=0.2)
            assert (replaced.status, replaced.reject_reason) == (ExecStatus.rejected, RejectReason.invalid_params)
            (exact,) = await client.act(action_id="e", choice_id="key_sequence", action="key_sequence", params={},
                                        based_on_seq=observation.seq, max_age_s=2.0, lease_s=1.0)
            assert exact.status is ExecStatus.completed                          # exactly max_s is allowed
        finally:
            await close(game, client)

    asyncio.run(main())


def test_the_executor_passes_each_hosts_lease_as_data_and_only_gives_actions_the_default():
    class Client:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def act(self, **request):
            self.sent.append(request)
            return [ExecutionRecord(session="s", seq=len(self.sent), wall_utc="2026-01-01T00:00:00Z",
                                    action_id=request["action_id"], status=ExecStatus.completed)]

    async def main():
        built = build_choices(all_modes())
        client = Client()
        executor = ExecutionController(client, lease_s=0.7, leases=built.leases)
        by_id = {c.id: c for c in built.choices}
        for name in ("key_hold", "key_chord", "pointer_impulse", "key_sequence", "pointer_sequence"):
            await executor.execute(by_id[name], based_on_seq=1)
        assert [(r["choice_id"], r["lease_s"]) for r in client.sent] == [
            ("key_hold", 0.2), ("key_chord", 0.2), ("pointer_impulse", 0.1), ("key_sequence", None), ("pointer_sequence", None)]
        assert all(r["params"] == {} for r in client.sent)

    asyncio.run(main())


def test_no_calibrated_effect_is_reported_for_a_compound_and_the_single_keys_is_reported_for_a_single_key(tmp_path: Path):
    async def main():
        value = manifest(
            action("move", "w", hold_s=0.2, holds=[hold(0.2, mean=6.04)]),
            action("strafe", ["w", "a"], hold_s=0.2, holds=[hold(0.2, mean=6.04)]),
            skills=(skill("walk", [(["w"], 0.2, 0.0)], 1.0),))
        game, client, built = await serve(tmp_path, value, effects={"move": (6.0, 0.0, 0.0)})
        try:
            result, s1, sink = await run(game, client, built, "move", "strafe", "walk", "wait", "finish", probe=True)
            effects = {e["body"]["choice_id"]: e["body"] for e in sink.of_kind("effect")}
            assert effects["move"]["expected"]["net_displacement"] == 6.04
            assert effects["strafe"]["expected"] is None and effects["walk"]["expected"] is None
            assert effects["strafe"]["observed"]["net_displacement"] == pytest.approx(0.0)   # measured, never invented
            facts = [fact for context in s1.contexts for fact in context["facts"]]
            strafe_fact = next(f for f in facts if f.startswith("strafe: net displacement"))
            assert strafe_fact.endswith("0.000 world units; no calibrated effect is recorded for this declaration.")
            assert "6.04" not in strafe_fact and "calibrated for a" not in strafe_fact
            assert next(f for f in facts if f.startswith("move: net displacement")).endswith(
                "calibrated for a 0.2-second hold: 6.040 (sample variance 0.000100, n=2).")
        finally:
            await close(game, client)

    asyncio.run(main())


def test_the_context_choice_the_request_and_the_executed_declaration_name_the_same_thing(tmp_path: Path):
    """The fidelity chain for every supported declaration at once: payload choice -> request -> host run."""
    async def main():
        game, client, built = await serve(tmp_path, all_modes())
        try:
            result, s1, sink = await run(game, client, built, *MANIFEST_ORDER, "finish")
            payload = {c["id"]: c for c in s1.contexts[0]["choices"]}
            for body, ran, name in zip(executions(sink), game.runs(), MANIFEST_ORDER):
                chosen = payload[name]
                assert chosen["kind"] in ("action", "skill") and chosen.get("action", chosen.get("skill")) == name
                assert body["request"]["choice_id"] == body["request"]["action"] == ran["action"] == name
                assert body["request"]["based_on_seq"] == result.loop.turns[MANIFEST_ORDER.index(name)].based_on_seq
            kinds = {name: ChoiceKind(payload[name]["kind"]) for name in MANIFEST_ORDER}
            assert [n for n, k in kinds.items() if k is ChoiceKind.skill] == ["key_sequence", "pointer_sequence"]
        finally:
            await close(game, client)

    asyncio.run(main())


def test_a_pending_help_turn_continues_with_a_whole_declaration_exactly_as_chosen(tmp_path: Path):
    """On an escalate turn S1 names what to do while help is pending; a chord or skill runs whole, or not at all."""
    async def main():
        game, client, built = await serve(tmp_path, all_modes())
        try:
            sequence = {"mode": "continue", "choice_id": "key_sequence"}
            chord = {"mode": "continue", "choice_id": "key_chord"}
            result, _, sink = await run(game, client, built, pick("escalate", while_pending=sequence),
                                        pick("escalate", while_pending=chord), "finish")
            first, second = result.loop.turns[:2]
            assert (first.effective_choice, first.executed_action, first.outcome) == (
                "escalate", "key_sequence", TurnOutcome.escalated)
            assert (second.effective_choice, second.executed_action) == ("escalate", "key_chord")   # a repeat: no new help call
            assert [(r["action"], r["declaration"]) for r in game.runs()] == [
                ("key_sequence", EXPORTED["key_sequence"]), ("key_chord", EXPORTED["key_chord"])]
            requests = [body["request"] for body in executions(sink)]
            assert [(r["choice_id"], r["lease_s"]) for r in requests] == [("key_sequence", None), ("key_chord", 0.2)]
            assert [body["purpose"] for body in executions(sink)] == ["while_pending", "while_pending"]
            assert [e["body"]["event"] for e in sink.of_kind("help")][:2] == ["opened", "repeat"]
            assert check_adherence(result.loop.turns) == []
        finally:
            await close(game, client)

    asyncio.run(main())
