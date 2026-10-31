"""Manifest-declared reach regions: one measured pose, a distance and membership, and nothing else.

Every volume here is an opaque synthetic declaration. No game field, asset or scene name is involved, and
nothing in this module claims an objective was completed or a trial was run. A missing measurement stays
unknown with a reason; it is never a distance of zero and never a success.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from s1_fakes import ARM, Send, approval, reply
from test_frontier_packet import build as build_packet
from test_runner_session import OBJECTIVE, two_actions
from playtest.bench import call_budget
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import ChoiceSet
from playtest.contracts.manifest import PreflightManifest, ReachBounds, ReachRegion
from playtest.core.context import build_context, canonical
from playtest.core.trace_sink import MemorySink
from playtest.frontier import provider
from playtest.frontier.media import Clip
from playtest.roles import frontier
from playtest.runner.choices import build_choices
from playtest.runner.directives import DirectiveTracker
from playtest.runner.frontier_help import FrontierHelp
from playtest.runner.progress import (POSE_SOURCES, ProgressFacts, ProgressProbe, fact_line, point_box_distance,
                                      progress_rows, read_region)
from playtest.runner.s1 import SystemOneS1

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contracts"


def region(region_id: str = "zone_a", **overrides) -> ReachRegion:
    values = {
        "region_id": region_id,
        "label": "Zone A",
        "purpose": "An opaque test volume",
        "frame": "world",
        "scene": "scene",
        "bounds": ReachBounds(min=[1.0, 2.0, 3.0], max=[4.0, 5.0, 6.0]),
        "pose": "probe_anchor",
        "author": "test operator",
        "source": "synthetic declaration for tests",
        "verification": "operator_asserted",
    }
    values.update(overrides)
    return ReachRegion(**values)


def probe_public(position) -> dict:
    """The bridge observation's own probe geometry, as the observation source carries it."""
    return {"probe": {"anchor": {"kind": "path", "name": "Anchor", "position": position}}}


def observation(seq: int = 1, game_s: float = 1.0, *, scene: str = "scene", session: str = "run-1",
                frame: int | None = None, public: dict | None = None) -> Observation:
    return Observation(session=session, seq=seq, game_s=game_s,
                       wall_utc=datetime(2026, 10, 31, tzinfo=timezone.utc), clock_mode=ClockMode.realtime,
                       scene=scene, playing=True, paused=False, time_scale=1.0,
                       frame=seq if frame is None else frame, public={} if public is None else public)


def reach_manifest() -> PreflightManifest:
    path = FIXTURES / "preflight_manifest" / "valid-reach-regions.json"
    return PreflightManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def directive(field: str, op: str, value, deadline: float = 9.0, directive_id: str = "d-0") -> dict:
    spec = {"directive_id": directive_id, "model": provider.MODEL, "based_on_seq": 0,
            "text": f"Test the declared distance ({directive_id}).",
            "expected": {"field": field, "op": op, "value": value}, "deadline_game_s": deadline}
    return {**spec, "kind": "number", "baseline": 0, "scene": "scene", "last_frame": 0, "last_game_s": 0,
            "directive": dict(spec)}


# --- geometry and readings --------------------------------------------------------------------------------------


def test_distance_is_zero_only_inside_the_declared_volume_and_membership_follows_it():
    inside = read_region(region(), observation(public=probe_public([2.0, 3.0, 4.0])))
    assert (inside["distance"], inside["inside"], inside["reason"]) == (0.0, True, None)
    assert inside["measured_pose"] == [2.0, 3.0, 4.0]
    assert inside["region_id"] == "zone_a" and inside["bounds"] == {"min": [1.0, 2.0, 3.0], "max": [4.0, 5.0, 6.0]}
    # The declaration travels with the reading: it is what the author asserted, not a measurement.
    assert (inside["author"], inside["source"], inside["verification"]) == (
        "test operator", "synthetic declaration for tests", "operator_asserted")
    assert (inside["frame"], inside["scene"], inside["pose"]) == ("world", "scene", "probe_anchor")
    boundary = read_region(region(), observation(public=probe_public([4.0, 5.0, 6.0])))
    assert (boundary["distance"], boundary["inside"]) == (0.0, True)      # the declared box includes its faces


@pytest.mark.parametrize("position,expected_distance,expected_inside", [
    ([4.5, 3.5, 4.5], 0.5, False),                 # just beyond one face: adjacent is not arrived
    ([11.0, 12.0, 13.0], 147 ** 0.5, False),       # fully displaced away
    ([2.5, 9.0, 3.5], 4.0, False),                 # height gained over the volume is not membership
    ([2.5, 3.0, 3.5], 0.0, True),                  # inside in every axis
], ids=["adjacent_face", "displaced_away", "height_gain_elsewhere", "inside"])
def test_an_outside_pose_keeps_a_finite_distance_and_is_never_membership(position, expected_distance,
                                                                          expected_inside):
    row = read_region(region(), observation(public=probe_public(position)))
    assert row["measured_pose"] == [float(value) for value in position]
    assert row["distance"] == pytest.approx(expected_distance)
    assert row["inside"] is expected_inside and row["reason"] is None


def test_point_box_distance_is_zero_exactly_inside_and_euclidean_outside():
    bounds = ([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    assert point_box_distance((2.0, 3.0, 4.0), *bounds) == 0.0
    assert point_box_distance((2.0, 9.0, 3.0), *bounds) == 4.0
    assert point_box_distance((6.0, 5.0, 3.0), *bounds) == 2.0


@pytest.mark.parametrize("public,reason", [
    ({}, "is not in this observation"),
    ({"probe": {}}, "is not in this observation"),
    ({"probe": {"anchor": {"position": None}}}, "not a finite world position"),
    ({"probe": {"anchor": {"position": [1.0, 2.0]}}}, "not a finite world position"),
    ({"probe": {"anchor": {"position": [1.0, float("nan"), 3.0]}}}, "not a finite world position"),
    ({"probe": {"anchor": {"position": [1.0, float("inf"), 3.0]}}}, "not a finite world position"),
    ({"probe": {"anchor": {"position": [1.0, True, 3.0]}}}, "not a finite world position"),
    ({"probe": {"anchor": {"position": "1,2,3"}}}, "not a finite world position"),
], ids=["no_probe", "no_anchor", "null_position", "short_vector", "nan", "infinity", "bool", "text"])
def test_an_unmeasured_pose_leaves_distance_and_membership_unknown_with_a_reason(public, reason):
    row = read_region(region(), observation(seq=3, game_s=1.5, public=public))
    assert row["measured_pose"] is None
    assert row["distance"] is None and row["inside"] is None
    assert reason in row["reason"]


def test_a_different_scene_is_unknown_not_an_outside_distance():
    row = read_region(region(), observation(scene="another-scene", public=probe_public([2.0, 3.0, 4.0])))
    assert (row["measured_pose"], row["distance"], row["inside"]) == (None, None, None)
    assert "is not the declared scene" in row["reason"]


@pytest.mark.parametrize("overrides,problem", [
    ({"frame": "local"}, "is not supported"),
    ({"pose": "camera_position"}, "is not supported"),
], ids=["unsupported_frame", "unsupported_pose_source"])
def test_a_frame_or_pose_source_the_run_does_not_support_reads_unknown(overrides, problem):
    # The contract rejects these; a constructed region that slipped through must still not be measured.
    declared = region().model_copy(update=overrides)
    row = read_region(declared, observation(public=probe_public([2.0, 3.0, 4.0])))
    assert (row["distance"], row["inside"]) == (None, None) and problem in row["reason"]


@pytest.mark.parametrize("bounds,problem", [
    (ReachBounds.model_construct(min=[4.0, 5.0, 6.0], max=[1.0, 2.0, 3.0]), "ordered"),
    (ReachBounds.model_construct(min=[1.0, float("inf"), 3.0], max=[4.0, 5.0, 6.0]), "finite"),
], ids=["reversed", "nonfinite"])
def test_declared_geometry_the_contract_cannot_hold_is_refused_not_measured(bounds, problem):
    declared = region().model_copy(update={"bounds": bounds})
    with pytest.raises(ValueError, match=problem):
        read_region(declared, observation(public=probe_public([2.0, 3.0, 4.0])))


def test_the_fact_line_states_declaration_provenance_and_the_measurement_but_no_outcome():
    declared = region(verification="unverified", author="Operator A", source="surveyed by hand")
    line = fact_line(read_region(declared, observation(seq=3, game_s=1.5, public=probe_public([2.0, 3.0, 4.0]))))
    for text in ("zone_a", "Zone A", "An opaque test volume", "[1.000, 2.000, 3.000]", "[4.000, 5.000, 6.000]",
                 "world frame", "'scene'", "Operator A", "surveyed by hand", "unverified",
                 "[2.000, 3.000, 4.000]", "distance 0.000 world units", "inside True"):
        assert text in line
    assert "not a verified target" in line
    assert "completed" in line and "any particular object was used" in line
    unknown = fact_line(read_region(region(), observation(public={})))
    assert "distance and membership are unknown" in unknown and "is not in this observation" in unknown


def test_only_measured_distances_become_derived_fields():
    probe = ProgressProbe([region(), region("zone_b", scene="another-scene")])
    obs = observation(public=probe_public([2.5, 9.0, 3.5]))
    assert probe.derived(obs) == {"progress.zone_a.distance": pytest.approx(4.0)}
    assert [row["region_id"] for row in probe.readings(obs)] == ["zone_a", "zone_b"]
    assert probe.derived(observation(public={})) == {}
    assert progress_rows((), obs) == [] and ProgressProbe(()).derived(obs) == {}


def test_the_only_supported_pose_source_is_the_probe_anchors_world_position():
    assert POSE_SOURCES == {"probe_anchor": ("probe", "anchor", "position")}


# --- the S1 request path ----------------------------------------------------------------------------------------


def _choices(seq: int) -> ChoiceSet:
    return ChoiceSet(set_version=0, built_from_seq=seq, choices=build_choices(two_actions()).choices)


def test_a_manifest_without_declarations_states_exactly_the_facts_and_request_it_did_before():
    async def main():
        obs = observation(seq=7, game_s=2.0, public={"counter": 1})
        sink = MemorySink()
        tracker = DirectiveTracker(sink)
        inner = await tracker.facts([], obs)
        wrapped = await ProgressFacts(tracker, ProgressProbe(())).facts([], obs)
        assert wrapped == inner
        plain = build_context(objective=OBJECTIVE, observation=obs, choices=_choices(obs.seq), facts=inner)
        same = build_context(objective=OBJECTIVE, observation=obs, choices=_choices(obs.seq), facts=wrapped)
        assert canonical(same) == canonical(plain)

    asyncio.run(main())


def test_a_declaration_never_swallows_the_restart_or_finish_seam_of_the_facts_source():
    """The wrapper adds fact lines; a play-session boundary still reaches the source that owns it."""
    class Inner:
        def __init__(self):
            self.calls = []

        async def facts(self, reports, observation):
            return []

        def rebind(self, **values):
            self.calls.append(("rebind", values))

        def finish(self, **values):
            self.calls.append(("finish", values))

    inner, obs = Inner(), observation(seq=9, game_s=1.0)
    facts = ProgressFacts(inner, ProgressProbe([region()]))
    facts.rebind(client="client", observation=obs, generation="gen-2")
    facts.finish(turn=3, based_on_seq=9, evidence_trace_seq=None)
    assert [name for name, _ in inner.calls] == ["rebind", "finish"]
    assert inner.calls[0][1] == {"client": "client", "observation": obs, "generation": "gen-2"}
    assert inner.calls[1][1] == {"turn": 3, "based_on_seq": 9, "evidence_trace_seq": None}


def test_the_s1_request_states_each_declared_region_without_a_new_section(tmp_path: Path):
    async def main():
        declared, probe = region(), ProgressProbe([region()])
        sink = MemorySink()
        tracker = DirectiveTracker(sink, derived=probe.derived)
        tracker.adopt(directive("progress.zone_a.distance", "<=", 1.0, deadline=5.0), request_turn=0, based_on_seq=0)
        obs = observation(seq=7, game_s=2.0, public={**probe_public([2.5, 9.0, 3.5]), "counter": 1})
        facts = await ProgressFacts(tracker, probe).facts([], obs)
        assert any(line.startswith("Declared reach region zone_a") for line in facts)
        assert any("distance 4.000 world units" in line for line in facts)
        assert any("progress.zone_a.distance <= 1.0" in line and "status open" in line for line in facts)

        context = build_context(objective=OBJECTIVE, observation=obs, choices=_choices(obs.seq), facts=facts)
        plain_tracker = DirectiveTracker(MemorySink())
        plain_tracker.adopt(directive("progress.zone_a.distance", "<=", 1.0, deadline=5.0),
                            request_turn=0, based_on_seq=0)
        plain_facts = await plain_tracker.facts([], obs)
        plain = build_context(objective=OBJECTIVE, observation=obs, choices=_choices(obs.seq), facts=plain_facts)
        # A declaration adds fact lines and no key, section or observation field.
        assert set(context) == set(plain)
        assert context["observation"] == plain["observation"]
        assert "progress" not in context and "progress" not in context["observation"]
        assert declared.region_id in context["facts"][-1]

        ledger = call_budget.Ledger(tmp_path, approval(4), ARM)
        send = Send(tmp_path, reply("next_action", "wait"))
        port = SystemOneS1(ledger, max_calls=1, send=send, arm=ARM)
        got = await port.decide(context)
        assert got.answer["choice_id"] == "wait" and got.failure is None
        stated = send.bodies[0]["state"]
        assert stated["facts"] == facts
        assert any("Declared reach region zone_a" in line and "synthetic declaration for tests" in line
                   for line in stated["facts"])
        assert stated["observation"]["public"]["probe"]["anchor"]["position"] == [2.5, 9.0, 3.5]

    asyncio.run(main())


# --- scoring a declared distance --------------------------------------------------------------------------------


def test_a_declared_distance_setpoint_is_met_only_by_a_measured_reading():
    async def main():
        sink, probe = MemorySink(), ProgressProbe([region()])
        tracker = DirectiveTracker(sink, derived=probe.derived)
        tracker.adopt(directive("progress.zone_a.distance", "<=", 1.0, deadline=5.0), request_turn=0, based_on_seq=0)
        opening = await tracker.facts([], observation(seq=1, game_s=1.0, public=probe_public([2.5, 9.0, 3.5])))
        assert tracker.active is not None and "status open." in opening[-1]
        closing = await tracker.facts([], observation(seq=2, game_s=2.0, public=probe_public([2.0, 3.0, 3.5])))
        outcome = sink.of_kind("investigation")[-1]["body"]
        assert outcome["outcome"] == "met"
        assert outcome["reason"].startswith("the measured reader value satisfied the setpoint")
        assert "status met (the measured reader value satisfied the setpoint" in closing[-1]
        assert tracker.active is None

    asyncio.run(main())


def test_a_declared_distance_with_no_measurement_expires_rather_than_failing_or_passing():
    async def main():
        sink, probe = MemorySink(), ProgressProbe([region()])
        tracker = DirectiveTracker(sink, derived=probe.derived)
        tracker.adopt(directive("progress.zone_a.distance", "<=", 1.0, deadline=3.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], observation(seq=1, game_s=1.0, public={}))
        assert tracker.active is not None                    # unknown before the deadline is not a failure
        closing = await tracker.facts([], observation(seq=2, game_s=3.5, public={}))
        outcome = sink.of_kind("investigation")[-1]["body"]
        assert outcome["outcome"] == "expired"
        assert outcome["reason"].startswith("the deadline passed without a scoreable reader value")
        assert "status expired" in closing[-1]

    asyncio.run(main())


def test_a_declared_distance_without_any_declaration_in_the_run_scores_unknown_not_false():
    async def main():
        sink = MemorySink()
        tracker = DirectiveTracker(sink)                     # no derived resolver: nothing was declared
        tracker.adopt(directive("progress.zone_a.distance", "<=", 1.0, deadline=3.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], observation(seq=1, game_s=1.0, public=probe_public([2.0, 3.0, 4.0])))
        await tracker.facts([], observation(seq=2, game_s=3.5, public=probe_public([2.0, 3.0, 4.0])))
        assert sink.of_kind("investigation")[-1]["body"]["outcome"] == "expired"

    asyncio.run(main())


@pytest.mark.parametrize("restarted,problem", [
    ({"session": "run-2", "game_s": 0.5, "frame": 1}, "restarted"),
    ({"session": "run-1", "game_s": 0.5, "frame": 1}, "clock or frame reset"),
], ids=["new_session", "reset_frame"])
def test_a_restart_or_reset_invalidates_a_declared_distance_before_it_is_scored(restarted, problem):
    async def main():
        sink, probe = MemorySink(), ProgressProbe([region()])
        tracker = DirectiveTracker(sink, derived=probe.derived)
        tracker.adopt(directive("progress.zone_a.distance", "<=", 1.0, deadline=9.0), request_turn=0, based_on_seq=0)
        await tracker.facts([], observation(seq=4, game_s=4.0, frame=40, public=probe_public([2.5, 9.0, 3.5])))
        assert tracker.active is not None
        await tracker.facts([], observation(seq=4, public=probe_public([2.5, 9.0, 3.5]), **restarted))
        outcome = sink.of_kind("investigation")[-1]["body"]
        assert outcome["outcome"] == "invalidated"
        assert outcome["reason"].startswith("the session or scene changed or the measured game clock or frame reset")

    asyncio.run(main())


# --- the frontier request and review ----------------------------------------------------------------------------


def frontier_approval() -> dict:
    limit = {"max_calls": 2, "max_cost_usd": 1.0, "per_call_max_usd": 0.5,
             "verified_balance_usd": 10.0, "balance_checked_at_utc": "2026-01-01T00:00:00Z",
             "price_valid_until_utc": "2099-01-01T00:00:00Z"}
    return {"max_calls": 2, "max_cost_usd": 1.0, "arms": {provider.MODEL: limit}}


def frontier_request(public: dict, *, seq: int = 7, game_s: float = 2.0, scene: str = "scene") -> SimpleNamespace:
    offered = build_choices(two_actions()).choices
    escalate = next(choice for choice in offered if choice.kind.value == "escalate")
    context = {"as_of": {"seq": seq, "game_s": game_s, "age_s": 0.1},
               "observation": {"scene": scene, "playing": True, "paused": False, "time_scale": 1,
                               "public": public},
               "choice_set_version": 0, "choices": [choice.model_dump(mode="json") for choice in offered],
               "objective": OBJECTIVE, "facts": []}
    return SimpleNamespace(turn=0, choice_id=escalate.id, context=context)


def frontier_content(expected: dict | None = None, deadline_s: float | None = None) -> str:
    directive_row = None if expected is None else {
        "deadline_s": deadline_s, "expected": expected, "target": None, "tests": None,
        "text": "Approach the declared volume and say what the distance does."}
    return json.dumps({"add_choices": [], "candidate_finding": None, "directive": directive_row,
                       "hypotheses": [], "prune_choices": []})


def frontier_helper(tmp_path: Path, manifest: PreflightManifest, sent: list, content: str, *,
                    sink: MemorySink | None = None, progress: ProgressProbe | None = None,
                    name: str = "run") -> FrontierHelp:
    def send(body):
        sent.append(body)
        return 200, {"model": provider.MODEL,
                     "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 5,
                               "prompt_tokens_details": {"cached_tokens": 0}}}, 1.0, None

    sink = sink if sink is not None else MemorySink()
    tracker = DirectiveTracker(sink, derived=progress.derived if progress is not None else None)
    return FrontierHelp(project=tmp_path, status=None, manifest=manifest, directory=tmp_path / name,
                        approval=frontier_approval(), sink=sink, tracker=tracker,
                        started_utc=datetime.now(timezone.utc), send=send,
                        capture=lambda **_kwargs: Clip(None, None, "offline fake", None), max_calls=1,
                        progress=progress)


def test_the_frontier_request_declares_each_region_and_shows_only_the_measured_distance(tmp_path: Path):
    async def main():
        manifest, sent, sink = reach_manifest(), [], MemorySink()
        helper = frontier_helper(tmp_path, manifest, sent, frontier_content(), sink=sink,
                                 progress=ProgressProbe(manifest.reach_regions))
        result = await helper.run(frontier_request(probe_public([2.0, 1.0, 5.0])))
        assert result.source == "frontier" and helper.calls == 1
        body = sent[0]
        assert body["messages"][0]["content"] == frontier.SYSTEM + frontier.PROGRESS_SYSTEM

        message = json.loads(body["messages"][1]["content"])
        assert [row["path"] for row in message["progress"]] == [
            "progress.entry_a.distance", "progress.entry_b.distance"]
        first = message["progress"][0]
        assert first["inside_path"] == "progress.entry_a.inside" and first["kind"] == "number"
        assert (first["author"], first["source"], first["verification"]) == (
            "test operator", "synthetic declaration for contract tests", "operator_asserted")
        assert first["bounds"] == {"min": [-1.0, 0.0, 2.0], "max": [1.0, 2.0, 4.0]}
        assert "operator's declaration of an entry volume" in message["notes"]

        packet = json.loads(body["messages"][2]["content"])
        rows = {row["region_id"]: row for row in packet["progress"]}
        assert rows["entry_a"]["measured_pose"] == [2.0, 1.0, 5.0]
        assert rows["entry_a"]["distance"] == pytest.approx(2 ** 0.5) and rows["entry_a"]["inside"] is False
        assert rows["entry_a"]["reason"] is None
        assert helper.reader_fields["progress.entry_a.distance"] == "number"
        assert "progress.entry_a.inside" not in helper.reader_fields

        defs = body["response_format"]["json_schema"]["schema"]["$defs"]
        assert "progress.entry_a.distance" in defs["fields"]["enum"]
        assert "progress.entry_a.inside" not in defs["fields"]["enum"]

    asyncio.run(main())


def test_a_manifest_without_declarations_sends_the_frontier_what_it_always_did(tmp_path: Path):
    async def main():
        manifest, sent = reach_manifest(), []
        bare = manifest.model_copy(update={"reach_regions": []})
        helper = frontier_helper(tmp_path, bare, sent, frontier_content(), name="plain")
        await helper.run(frontier_request(probe_public([2.0, 1.0, 5.0])))
        body = sent[0]
        assert body["messages"][0]["content"] == frontier.SYSTEM
        message = json.loads(body["messages"][1]["content"])
        assert set(message) == {"fields", "actions", "skills", "notes"}
        assert "progress" not in message["notes"]
        assert not [path for path in helper.reader_fields if path.startswith("progress.")]
        packet = json.loads(body["messages"][2]["content"])
        assert "progress" not in packet
        defs = body["response_format"]["json_schema"]["schema"]["$defs"]
        assert not [value for value in defs["fields"]["enum"] if value.startswith("progress.")]

    asyncio.run(main())


def test_a_declaration_changes_only_the_progress_sections_of_the_frontier_request(tmp_path: Path):
    async def main():
        manifest, declared_sent, plain_sent = reach_manifest(), [], []
        bare = manifest.model_copy(update={"reach_regions": []})
        declared = frontier_helper(tmp_path, manifest, declared_sent, frontier_content(), name="declared",
                                   progress=ProgressProbe(manifest.reach_regions))
        plain = frontier_helper(tmp_path, bare, plain_sent, frontier_content(), name="plain")
        request = frontier_request(probe_public([2.0, 1.0, 5.0]))
        await declared.run(request)
        await plain.run(request)
        declared_body, plain_body = declared_sent[0], plain_sent[0]
        declared_packet = json.loads(declared_body["messages"][2]["content"])
        plain_packet = json.loads(plain_body["messages"][2]["content"])
        assert {key: value for key, value in declared_packet.items() if key != "progress"} == plain_packet
        assert "progress" in declared_packet and "progress" not in plain_packet
        assert declared_body["response_format"] != plain_body["response_format"]

    asyncio.run(main())


def setpoint(field: str, op: str, value, tolerance=None) -> dict:
    return {"field": field, "op": op, "value": value, "tolerance": tolerance}


@pytest.mark.parametrize("expected,problem", [
    (setpoint("progress.entry_a.distance", "<=", 1.0), None),
    (setpoint("progress.entry_a.distance", ">=", 1.0), "already holds"),
    (setpoint("progress.entry_a.inside", "==", True), "not a manifest field"),
    (setpoint("progress.entry_z.distance", "<=", 1.0), "not a manifest field"),
], ids=["distance_not_yet_reached", "distance_vacuous", "membership_is_not_a_field", "undeclared_region"])
def test_only_a_declared_regions_measured_distance_is_a_legal_setpoint(tmp_path: Path, expected, problem):
    async def main():
        manifest, sent, sink = reach_manifest(), [], MemorySink()
        helper = frontier_helper(tmp_path, manifest, sent, frontier_content(expected, 10.0), sink=sink,
                                 progress=ProgressProbe(manifest.reach_regions))
        finished = await helper.run(frontier_request(probe_public([2.0, 1.0, 5.0])))   # distance sqrt(2)
        if problem is None:
            assert finished.result["directive"]["directive_id"] == "d-0" and finished.result["review"] == []
        else:
            assert finished.result["directive"] is None
            assert [row["item"] for row in finished.result["review"]] == ["directive"]
            assert problem in finished.result["review"][0]["reason"]

    asyncio.run(main())


def test_a_declared_distance_the_frontier_issues_is_scored_from_the_measurement(tmp_path: Path):
    async def main():
        manifest, sent, sink = reach_manifest(), [], MemorySink()
        helper = frontier_helper(tmp_path, manifest, sent, frontier_content(
            setpoint("progress.entry_a.distance", "<=", 1.0), 10.0), sink=sink,
            progress=ProgressProbe(manifest.reach_regions))
        finished = await helper.run(frontier_request(probe_public([2.0, 1.0, 5.0])))
        assert finished.result["directive"]["directive_id"] == "d-0"
        helper.adopt_result(0, finished)
        assert helper.tracker.active is not None and helper.tracker.active["kind"] == "number"
        await helper.tracker.facts([], observation(seq=8, game_s=3.0, public=probe_public([0.5, 1.0, 3.0])))
        outcome = sink.of_kind("investigation")[-1]["body"]
        assert outcome["outcome"] == "met"
        assert outcome["reason"].startswith("the measured reader value satisfied the setpoint")

    asyncio.run(main())


def test_an_unmeasured_declared_distance_is_stated_unknown_and_expires_not_zero(tmp_path: Path):
    async def main():
        manifest, sent, sink = reach_manifest(), [], MemorySink()
        helper = frontier_helper(tmp_path, manifest, sent, frontier_content(
            setpoint("progress.entry_a.distance", "<=", 1.0), 2.0), sink=sink,
            progress=ProgressProbe(manifest.reach_regions))
        finished = await helper.run(frontier_request({"speed": 3}))          # no probe geometry in this observation
        assert finished.result["directive"]["directive_id"] == "d-0"          # unknown is not a reason to reject
        packet = json.loads(sent[0]["messages"][2]["content"])
        entry = next(row for row in packet["progress"] if row["region_id"] == "entry_a")
        assert (entry["measured_pose"], entry["distance"], entry["inside"]) == (None, None, None)
        assert "is not in this observation" in entry["reason"]
        helper.adopt_result(0, finished)
        # The relative deadline is anchored when S1 is first shown the directive (game_s 12.0 + 2.0).
        await helper.tracker.facts([], observation(seq=8, game_s=12.0, public={}))
        assert helper.tracker.active is not None            # unknown before the deadline is not a failure
        await helper.tracker.facts([], observation(seq=9, game_s=15.0, public={}))
        outcome = sink.of_kind("investigation")[-1]["body"]
        assert outcome["outcome"] == "expired"
        assert outcome["reason"].startswith("the deadline passed without a scoreable reader value")

    asyncio.run(main())


# --- the packet refuses readings the runner did not produce ------------------------------------------------------


def test_a_progress_row_that_is_not_a_consistent_reading_is_refused_not_shown(tmp_path: Path):
    measured = read_region(region(), observation(public=probe_public([2.5, 9.0, 3.5])))
    inside = read_region(region(), observation(public=probe_public([2.0, 3.0, 4.0])))
    assert build_packet(progress=[measured]).payload["progress"] == [measured]
    unmeasured = read_region(region(), observation(public={}))
    bad_rows = [
        {**unmeasured, "distance": 0.0},             # unmeasured, yet a distance was invented
        {**inside, "inside": False},                 # a distance of zero means inside
        {**measured, "inside": True},                # a distance above zero means outside
        {**measured, "reason": "measured"},          # a measured reading carries no reason
        {**measured, "distance": float("inf")},      # never a finite distance
        {**measured, "extra": True},                 # never a shape the runner did not produce
    ]
    for row in bad_rows:
        with pytest.raises(ValueError):
            build_packet(progress=[row])
    with pytest.raises(ValueError, match="appears twice"):
        build_packet(progress=[measured, measured])
