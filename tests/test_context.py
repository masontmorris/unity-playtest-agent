"""The S1 payload carries exactly what the loop measured and offered, nothing ranked or dropped."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest

from playtest.contracts.bridge import Observation, ReaderError, ViewAngles
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Comparator, Directive, DirectiveOutcome, Expected
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet, ChoiceSource
from playtest.core.context import build_context, canonical


def observation(seq: int = 7, **public: object) -> Observation:
    return Observation(
        session="s",
        seq=seq,
        game_s=12.5,
        wall_utc=datetime(2026, 10, 27, tzinfo=timezone.utc),
        clock_mode=ClockMode.realtime,
        scene="scene-a",
        playing=True,
        paused=False,
        time_scale=1.0,
        frame=3,
        public=public or {"entity_position": [1.0, 0.0, 2.0], "unread": None},
        ui={"canvases": []},
        reader_errors=[ReaderError(reader="unread", error="target not found")],
    )


def choices(seq: int = 7, version: int = 3) -> ChoiceSet:
    return ChoiceSet(
        set_version=version,
        built_from_seq=seq,
        choices=[
            Choice(id="c1", kind=ChoiceKind.action, label="Move forward 1.0 s", action="move_forward",
                   params={"duration_game_s": 1.0}, preconditions=["playing"], source=ChoiceSource.manifest),
            Choice(id="c2", kind=ChoiceKind.escalate, label="Ask for help", role="frontier",
                   source=ChoiceSource.core),
            Choice(id="c3", kind=ChoiceKind.wait, label="Wait 0.5 s", params={"duration_game_s": 0.5},
                   source=ChoiceSource.core),
        ],
    )


def directive(directive_id: str = "d1", outcome: DirectiveOutcome = DirectiveOutcome.open) -> Directive:
    return Directive(
        directive_id=directive_id,
        model="m",
        based_on_seq=6,
        text="Check the thing and report it.",
        expected=Expected(field="public.unread", op=Comparator.eq, value=True),
        outcome=outcome,
    )


def test_context_binds_choices_to_the_observation_it_was_built_from():
    with pytest.raises(ValueError, match="bound to seq"):
        build_context(objective="Explore.", observation=observation(8), choices=choices(7))
    with pytest.raises(ValueError, match="objective is empty"):
        build_context(objective="  ", observation=observation(), choices=choices())
    with pytest.raises(ValueError, match="age cannot be negative"):
        build_context(objective="Explore.", observation=observation(), choices=choices(), age_s=-0.1)


def test_context_preserves_choices_and_facts_in_order():
    payload = build_context(
        objective="Explore.",
        observation=observation(),
        choices=choices(),
        facts=["3 of the last 4 inputs moved less than 0.1 m", "3 of the last 4 inputs moved less than 0.1 m"],
        age_s=0.2,
        sample_gap_s=0.05,
    )

    assert [c["id"] for c in payload["choices"]] == ["c1", "c2", "c3"]
    assert payload["choices"][0]["params"] == {"duration_game_s": 1.0}
    assert payload["choices"][0]["preconditions"] == ["playing"]
    assert payload["choices"][1]["role"] == "frontier"
    assert payload["facts"] == ["3 of the last 4 inputs moved less than 0.1 m"] * 2
    assert payload["choice_set_version"] == 3
    assert payload["as_of"] == {"seq": 7, "game_s": 12.5, "age_s": 0.2, "sample_gap_s": 0.05}


def test_context_keeps_unknown_values_reader_errors_and_history():
    payload = build_context(
        objective="Explore.",
        observation=observation(),
        choices=choices(),
        directive=directive("d2"),
        directive_history=[directive("d1", DirectiveOutcome.not_met)],
        pending_help={"choice_id": "c2", "kind": "escalate", "since_game_s": 4.0},
        game_view="frames/3.jpg",
    )

    assert payload["observation"]["public"]["unread"] is None
    assert payload["observation"]["reader_errors"] == [{"reader": "unread", "error": "target not found"}]
    assert payload["directive"]["directive_id"] == "d2"
    assert [d["directive_id"] for d in payload["directive_history"]] == ["d1"]
    assert payload["directive_history"][0]["outcome"] == "not_met"
    assert payload["pending_help"] == {"choice_id": "c2", "kind": "escalate", "since_game_s": 4.0}
    assert payload["game_view"] == "frames/3.jpg"
    assert payload["as_of"]["age_s"] is None


def test_the_game_view_the_bridge_reports_never_reaches_s1():
    """The view is calibration's measurement. A payload built from an observation that carries one is
    byte-for-byte the payload built without it, so what S1 is shown, and what it may choose, is unchanged."""
    plain = observation()
    seen = plain.model_copy(update={"view": ViewAngles(yaw_deg=41.0, pitch_deg=-3.5)})
    assert seen.view is not None and plain.view is None
    assert canonical(build_context(objective="Explore.", observation=seen, choices=choices())) == canonical(
        build_context(objective="Explore.", observation=plain, choices=choices()))
    assert "view" not in build_context(objective="Explore.", observation=seen, choices=choices())["observation"]


def test_context_has_no_ranking_or_recommendation():
    payload = build_context(objective="Explore.", observation=observation(), choices=choices())
    assert set(payload) == {
        "objective", "as_of", "observation", "facts", "directive", "directive_history",
        "pending_help", "choice_set_version", "choices", "instructions", "game_view",
    }
    assert all(not any(word in key for word in ("rank", "score", "recommend", "best")) for key in payload)
    assert len(payload["choices"]) == 3


def test_canonical_is_stable_for_equal_payloads():
    one = build_context(objective="Explore.", observation=observation(), choices=choices())
    two = build_context(objective="Explore.", observation=observation(), choices=choices())
    assert canonical(one) == canonical(two)
    assert canonical(dict(reversed(list(one.items())))) == canonical(one)


def test_recent_history_is_optional_and_independently_copied():
    kwargs = {"objective": "Explore.", "observation": observation(), "choices": choices()}
    plain = build_context(**kwargs)
    assert "recent_history" not in plain
    assert canonical(plain) == canonical(build_context(**kwargs, recent_history=None))
    assert sha256(canonical(plain).encode("utf-8")).hexdigest() == "d7ab2d84312f19970bd40acc0564dcec207c3978b12db8e55d8cbf02a94550f6"

    supplied = {"turns": {"items": [{"outcome": {"value": "original"}}]}}
    payload = build_context(**kwargs, recent_history=supplied)
    supplied["turns"]["items"][0]["outcome"]["value"] = "caller change"
    assert payload["recent_history"]["turns"]["items"][0]["outcome"]["value"] == "original"
    payload["recent_history"]["turns"]["items"].clear()
    assert supplied["turns"]["items"]
    assert build_context(**kwargs, recent_history={})["recent_history"] == {}
