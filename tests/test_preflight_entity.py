from __future__ import annotations

import pytest
from types import SimpleNamespace

from playtest.preflight import entity
from playtest.preflight.entity import analyze, candidate_controls, resolve_viewpoints


def frame(positions: dict[str, tuple[float, float, float]], parents: dict[str, str | None], t: float = 1.0) -> dict:
    return {"position_space": "world", "position_unit": "unity_world_unit", "position_order": ["x", "y", "z"],
            "time_unit": "seconds", "frame": 1, "real_time_s": t, "game_time_s": t,
            "game_objects": [{"id": key, "parent_id": parents[key], "position": list(value)}
                        for key, value in positions.items()]}


def attempt(control: str, start: dict, end: dict, parents: dict, idle: list[dict] | None = None) -> dict:
    baseline = idle or [start] * 5
    return {"control": control, "idle": [frame(p, parents, i * 0.1) for i, p in enumerate(baseline)],
            "before": frame(start, parents, 0.4), "after": frame(end, parents, 0.8)}


def test_candidate_controls_are_derived_from_vector_bindings_and_unsupported_are_visible():
    bindings = [
        {"id": "a", "expected_control_type": "Vector2", "binding_effective_path": "<Keyboard>/w",
         "source_object_id": "owner"},
        {"id": "b", "expected_control_type": "Vector2", "binding_effective_path": "<keyboard>/W",
         "source_object_id": "owner"},
        {"id": "c", "expected_control_type": "Vector2", "binding_effective_path": "<Gamepad>/leftStick"},
        {"id": "d", "expected_control_type": "Vector2", "binding_effective_path": "2DVector", "binding_is_composite": True},
        {"id": "e", "expected_control_type": "Button", "binding_effective_path": "<Keyboard>/space"},
    ]
    candidates, unsupported = candidate_controls({"bindings": bindings})
    assert candidates == [{"control": "w", "action": "preflight_key_w", "binding_ids": ["a", "b"],
                           "source_object_ids": ["owner"]}]
    assert unsupported == [{"binding_id": "c", "path": "<Gamepad>/leftStick"}]


def test_measures_repeatable_motion_collapses_children_and_excludes_idle_animation():
    parents = {"root": None, "child": "root", "independent": None, "still": None}
    start = {"root": (0, 0, 0), "child": (0, 1, 0), "independent": (2, 0, 0), "still": (3, 0, 0)}
    end = {"root": (0, 0, 1), "child": (0, 1, 1), "independent": (3, 0, 0), "still": (3, 0, 0)}
    middle = dict(start, independent=(2.5, 0, 0))
    # The autonomous object ends its idle window at its starting point; sampling the
    # middle of the window prevents its motion from cancelling itself out.
    idle = [start, middle, start, middle, start]
    result = analyze([attempt("w", start, end, parents, idle), attempt("w", start, end, parents, idle)])
    assert result["status"] == "identified"
    assert result["entity_id"] == "root"
    assert result["independent_candidates"] == ["root"]
    assert result["objects"]["child"]["w"]["responsive"] is True
    assert result["objects"]["independent"]["w"]["idle_quiet"] is False


def test_independent_responders_are_ambiguous():
    parents = {"a": None, "b": None}
    start = {"a": (0, 0, 0), "b": (0, 1, 0)}
    end = {"a": (1, 0, 0), "b": (1, 1, 0)}
    result = analyze([attempt("x", start, end, parents), attempt("x", start, end, parents)])
    assert result["status"] == "ambiguous"
    assert result["entity_id"] is None
    assert result["independent_candidates"] == ["a", "b"]


def test_idle_motion_on_any_control_disqualifies_an_object_globally():
    parents = {"a": None}
    start = {"a": (0, 0, 0)}
    end = {"a": (1, 0, 0)}
    idle = [start, {"a": (0.5, 0, 0)}, start, start, start]
    result = analyze([attempt("x", start, end, parents), attempt("x", start, end, parents),
                      attempt("y", start, end, parents, idle), attempt("y", start, end, parents, idle)])
    assert result["status"] == "inconclusive"
    assert result["entity_id"] is None


def _viewpoint_case(*, mismatched=False, owns_input=False, has_body=False):
    parents = {"body": None, "view": None}
    start = {"body": (0, 0, 0), "view": (0, 2, 0)}
    x_end = {"body": (1, 0, 0), "view": (1 if not mismatched else 2, 2.01, 0)}
    z_end = {"body": (0, 0, 1), "view": (0, 2.01, 1)}
    trials = [attempt("x", start, x_end, parents), attempt("x", start, x_end, parents),
              attempt("z", start, z_end, parents), attempt("z", start, z_end, parents)]
    discovery = {"game_objects": [{"id": key, "parent_id": value} for key, value in parents.items()],
                 "components": [{"object_id": "view", "type": "UnityEngine.Camera"}]}
    if has_body:
        discovery["components"].append({"object_id": "view", "type": "UnityEngine.Rigidbody"})
    candidates = [{"control": key, "source_object_ids": ["body"] + (["view"] if owns_input else [])}
                  for key in ("x", "z")]
    return resolve_viewpoints(analyze(trials), discovery, candidates)


def test_matching_camera_is_recorded_as_viewpoint_with_two_directions():
    result = _viewpoint_case()
    assert result["status"] == "identified"
    assert result["entity_id"] == "body"
    assert result["raw_responsive_roots"] == ["body", "view"]
    assert result["viewpoint_matches"][0]["viewpoint_id"] == "view"
    assert len(result["viewpoint_matches"][0]["displacement_comparisons"]) == 4


def test_matching_camera_without_two_distinct_directions_remains_ambiguous():
    parents = {"body": None, "view": None}
    start = {"body": (0, 0, 0), "view": (0, 2, 0)}
    end = {"body": (1, 0, 0), "view": (1, 2, 0)}
    trials = [attempt("x", start, end, parents), attempt("x", start, end, parents)]
    discovery = {"game_objects": [{"id": key, "parent_id": value} for key, value in parents.items()],
                 "components": [{"object_id": "view", "type": "UnityEngine.Camera"}]}
    result = resolve_viewpoints(analyze(trials), discovery, [{"control": "x", "source_object_ids": ["body"]}])
    assert result["status"] == "ambiguous"
    assert result["viewpoint_matches"] == []


def test_matching_viewpoint_does_not_hide_an_independent_responder():
    parents = {"body": None, "view": None, "other": None}
    start = {"body": (0, 0, 0), "view": (0, 2, 0), "other": (0, 4, 0)}
    x_end = {"body": (1, 0, 0), "view": (1, 2, 0), "other": (1, 4, 0)}
    z_end = {"body": (0, 0, 1), "view": (0, 2, 1), "other": (0, 4, 1)}
    trials = [attempt("x", start, x_end, parents), attempt("x", start, x_end, parents),
              attempt("z", start, z_end, parents), attempt("z", start, z_end, parents)]
    discovery = {"game_objects": [{"id": key, "parent_id": value} for key, value in parents.items()],
                 "components": [{"object_id": "view", "type": "UnityEngine.Camera"}]}
    candidates = [{"control": key, "source_object_ids": ["body"]} for key in ("x", "z")]
    result = resolve_viewpoints(analyze(trials), discovery, candidates)
    assert result["status"] == "ambiguous"
    assert result["entity_id"] is None
    assert result["independent_candidates"] == ["body", "other"]
    assert result["viewpoint_matches"][0]["viewpoint_id"] == "view"


@pytest.mark.parametrize("condition", ["mismatched", "owns_input", "has_body"])
def test_camera_with_independent_evidence_remains_ambiguous(condition):
    result = _viewpoint_case(**{condition: True})
    assert result["status"] == "ambiguous"
    assert result["entity_id"] is None
    assert result["independent_candidates"] == ["body", "view"]
    assert result["viewpoint_matches"] == []


def test_two_inconsistent_responses_do_not_identify_an_entity():
    parents = {"a": None}
    start = {"a": (0, 0, 0)}
    result = analyze([attempt("x", start, {"a": (1, 0, 0)}, parents),
                      attempt("x", start, {"a": (-1, 0, 0)}, parents)])
    assert result["status"] == "inconclusive"
    assert result["entity_id"] is None


def test_missing_positions_or_changed_objects_never_become_zero():
    parents = {"a": None}
    start = {"a": (0, 0, 0)}
    bad = attempt("x", start, start, parents)
    bad["after"]["game_objects"][0]["position"] = None
    with pytest.raises(ValueError, match="unknown position"):
        analyze([attempt("x", start, start, parents), bad])
    bad = attempt("x", start, start, parents)
    bad["after"]["game_objects"] = []
    with pytest.raises(ValueError, match="object set changed"):
        analyze([attempt("x", start, start, parents), bad])


def test_idle_and_action_windows_must_advance_and_be_comparable():
    parents = {"a": None}
    start = {"a": (0, 0, 0)}
    good = attempt("x", start, {"a": (1, 0, 0)}, parents)
    bad = attempt("x", start, {"a": (1, 0, 0)}, parents)
    bad["idle"][1]["real_time_s"] = bad["idle"][0]["real_time_s"]
    with pytest.raises(ValueError, match="do not advance"):
        analyze([good, bad])
    bad = attempt("x", start, {"a": (1, 0, 0)}, parents)
    bad["after"]["real_time_s"] = 10.0
    with pytest.raises(ValueError, match="not comparable"):
        analyze([good, bad])


def test_only_one_trial_is_inconclusive_not_a_complete_result():
    parents = {"a": None}
    start = {"a": (0, 0, 0)}
    with pytest.raises(ValueError, match="exactly two"):
        analyze([attempt("x", start, {"a": (1, 0, 0)}, parents)])


class FakeClient:
    def __init__(self, *, release_verified=True, truncated=False, new_object=False):
        self.position = 0.0
        self.actions = []
        self.release_verified = release_verified
        self.truncated = truncated
        self.new_object = new_object
        self.time = 0.0

    async def inspect(self, stage):
        complete = SimpleNamespace(truncated=False, observed=1, returned=1, limit=100)
        if stage == "discovery":
            binding = {"id": "binding", "expected_control_type": "Vector2",
                       "binding_effective_path": "<Keyboard>/w"}
            return SimpleNamespace(stage=stage, errors=[],
                                   caps={k: complete for k in ("game_objects", "components", "bindings", "input_asset_bindings")},
                                   data={"bindings": [binding], "game_objects": [{"id": "root"}]})
        cap = SimpleNamespace(truncated=self.truncated, observed=1, returned=1, limit=100)
        positions = {"root": (self.position, 0, 0)}
        parents = {"root": None}
        if self.new_object:
            positions["new"] = (0, 0, 0)
            parents["new"] = None
        self.time += 0.1
        return SimpleNamespace(stage=stage, errors=[], caps={"game_objects": cap}, data=frame(positions, parents, self.time))

    async def observe_once(self):
        return SimpleNamespace(seq=12)

    async def act(self, **kwargs):
        self.actions.append(kwargs)
        self.position += 1
        self.time += 0.3
        return [SimpleNamespace(status=SimpleNamespace(value="completed"), release_verified=self.release_verified,
                                model_dump=lambda **_: {"status": "completed"})]


def test_probe_actuates_each_candidate_twice_and_records_execution(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(entity.asyncio, "sleep", no_delay)
    client = FakeClient()
    result = entity.asyncio.run(entity.measure(client))
    assert result["status"] == "identified"
    assert result["entity_id"] == "root"
    assert len(client.actions) == len(result["trials"]) == 2
    assert all(action["action"] == "preflight_key_w" and action["based_on_seq"] == 12 for action in client.actions)
    assert result["trials"][0]["execution"] == [{"status": "completed"}]


@pytest.mark.parametrize("release_verified,truncated,expected_actions", [(False, False, 1), (True, True, 0)])
def test_probe_fails_closed_on_unverified_release_or_capped_snapshot(monkeypatch, release_verified, truncated, expected_actions):
    async def no_delay(_):
        return None
    monkeypatch.setattr(entity.asyncio, "sleep", no_delay)
    client = FakeClient(release_verified=release_verified, truncated=truncated)
    result = entity.asyncio.run(entity.measure(client))
    assert result["status"] == "inconclusive"
    assert result["entity_id"] is None
    assert result["error"]
    assert len(client.actions) == expected_actions
    if not release_verified:
        assert result["trials"][0]["after"] is None
        assert result["trials"][0]["execution"] == [{"status": "completed"}]


def test_probe_shutdown_requires_verified_release(monkeypatch, tmp_path):
    async def no_delay(_):
        return None
    client = FakeClient()
    async def connect(*args, **kwargs):
        return client
    async def unverified_close():
        return None
    client.aclose = unverified_close
    monkeypatch.setattr(entity.asyncio, "sleep", no_delay)
    monkeypatch.setattr(entity.BridgeClient, "connect_from_project", connect)
    result = entity.asyncio.run(entity._run(tmp_path))
    assert result["status"] == "inconclusive"
    assert result["entity_id"] is None
    assert result["stop"] is None


def test_budget_expiry_reports_an_inconclusive_measurement(monkeypatch):
    original_sleep = entity.asyncio.sleep
    async def delay(_):
        await original_sleep(0.01)
    monkeypatch.setattr(entity.asyncio, "sleep", delay)
    monkeypatch.setattr(entity, "MEASUREMENT_BUDGET_S", 0.00001)
    result = entity.asyncio.run(entity.measure(FakeClient()))
    assert result["status"] == "inconclusive"
    assert result["entity_id"] is None
    assert "budget" in result["error"]


def test_changed_scene_after_discovery_stops_before_input(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(entity.asyncio, "sleep", no_delay)
    client = FakeClient(new_object=True)
    result = entity.asyncio.run(entity.measure(client))
    assert result["status"] == "inconclusive"
    assert "after discovery" in result["error"]
    assert client.actions == []
