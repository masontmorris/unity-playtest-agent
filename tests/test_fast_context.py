"""The fast S1 context: same options and evidence, and every withheld field stated out loud.

`playtest.runner.fast_context` is not wired into the loop; these tests pin the properties a future
wiring would have to keep: it never withholds an option, a reading or a fact, it never drops a field
without naming it, it never changes the caller's payload, and its scope statement carries measured
counts rather than a judgement.
"""

from __future__ import annotations

import copy

import pytest

from playtest.runner.fast_context import FAST_CONTEXT_VERSION, derive_fast_context


def payload() -> dict:
    """A payload in the shape `build_context` produces, with two history blocks and guidance."""
    return {
        "objective": "Reach the marked place.",
        "as_of": {"seq": 12, "game_s": 30.5, "age_s": 0.01, "sample_gap_s": 1.9},
        "observation": {"scene": "scene-a", "playing": True, "paused": False, "time_scale": 1.0,
                        "public": {"read_a": 1.0}, "ui": {"canvases": []}, "reader_errors": []},
        "facts": ["Turn 11: move_a was executed.", "read_a: now 1.0; previous 0.0."],
        "directive": {"directive_id": "d-3", "text": "Move to the marked place.", "outcome": "open"},
        "directive_history": [
            {"directive_id": "d-1", "text": "Look first.", "outcome": "not_met"},
            {"directive_id": "d-2", "text": "Try the other route.", "outcome": "met"},
        ],
        "pending_help": None,
        "choice_set_version": 7,
        "choices": [
            {"id": "move_a", "kind": "action", "label": "Move a", "params": {"duration_game_s": 0.4}},
            {"id": "look_b", "kind": "action", "label": "Look b", "params": {}},
            {"id": "escalate", "kind": "escalate", "label": "Ask for help"},
            {"id": "finish", "kind": "finish", "label": "Finish"},
        ],
        "instructions": "Choose exactly one id from choices.",
        "game_view": "frames/12.jpg",
        "game_view_sha256": "0" * 64,
        "game_view_status": "sent",
        "guidance": {
            "current_hypotheses": [
                {"id": "h1", "status": "open", "refute_if": "no movement"},
                {"id": "h2", "status": "closed", "statement": "old"},
                {"id": "h3", "status": "open", "refute_if": "no turn"},
            ],
            "earlier_versions": [{"id": "h0", "status": "open"}],
            "help_adoptions": [{"source": "frontier", "kind": "escalate"}],
        },
        "recent_history": {
            "epoch": 0,
            "boundary": None,
            "turns": {"limit": 8, "total": 11, "omitted": 3,
                      "items": [{"turn": t, "raw_choice": "move_a", "outcome": "executed"}
                                for t in range(3, 11)]},
            "action_attempts": {"limit": 8, "total": 4, "omitted": 0,
                                "items": [{"turn": 1}, {"turn": 2}, {"turn": 5}, {"turn": 9}]},
        },
        "directive_provenance": {"d-3": {"requested": {"turn": 3}, "age_game_s": 12.0}},
    }


def test_a_fast_context_keeps_every_option_and_every_measured_row():
    fast = derive_fast_context(payload())

    assert [c["id"] for c in fast["choices"]] == ["move_a", "look_b", "escalate", "finish"]
    assert fast["choices"] == payload()["choices"]
    assert fast["observation"] == payload()["observation"]
    assert fast["facts"] == payload()["facts"]
    assert fast["directive"] == payload()["directive"]
    assert fast["objective"] == payload()["objective"]
    assert fast["instructions"] == payload()["instructions"]
    assert fast["game_view"] == "frames/12.jpg"


def test_the_recaps_are_left_out_and_named_with_measured_counts():
    fast = derive_fast_context(payload())
    scope = fast["context_scope"]

    assert "recent_history" not in fast
    assert scope["version"] == FAST_CONTEXT_VERSION
    assert scope["complete"] is False
    entry = scope["withheld"]["recent_history"]
    assert entry["turns_total"] == {"turns": 11, "action_attempts": 4}
    text = " ".join(scope["statements"])
    assert "recent history is not shown" in text
    assert "11 earlier rows in turns" in text and "4 earlier rows in action_attempts" in text


def test_guidance_and_provenance_are_kept_by_default():
    """Refute conditions and the age of advice are the parts a shortened context must not hide."""
    fast = derive_fast_context(payload())

    assert fast["guidance"] == payload()["guidance"]
    assert fast["directive_provenance"] == payload()["directive_provenance"]
    assert "guidance" not in fast["context_scope"]["withheld"]
    assert "directive_provenance" not in fast["context_scope"]["withheld"]


def test_history_can_be_shortened_and_states_how_much_it_shows():
    fast = derive_fast_context(payload(), history_turns=2)
    turns = fast["recent_history"]["turns"]

    assert [r["turn"] for r in turns["items"]] == [9, 10]
    assert turns["limit"] == 2 and turns["total"] == 11 and turns["omitted"] == 9
    entry = fast["context_scope"]["truncated"]["recent_history"]
    assert entry["turns_shown"] == {"turns": 2, "action_attempts": 2}
    assert entry["turns_omitted"] == {"turns": 9, "action_attempts": 2}
    assert "shows only the newest rows" in " ".join(fast["context_scope"]["statements"])


def test_history_kept_whole_still_reports_what_the_block_already_omitted():
    """The payload's own block already leaves 3 turns out; keeping it as built does not hide that."""
    fast = derive_fast_context(payload(), history_turns=20, directive_history=5)

    entry = fast["context_scope"]["truncated"]["recent_history"]
    assert entry["turns_omitted"] == {"turns": 3, "action_attempts": 0}
    assert fast["recent_history"]["turns"]["items"][0]["turn"] == 3
    assert "recent_history" not in fast["context_scope"]["withheld"]


def test_a_payload_that_keeps_everything_it_has_reports_a_complete_scope():
    source = payload()
    source["recent_history"]["turns"] = {"limit": 8, "total": 8, "omitted": 0,
                                         "items": source["recent_history"]["turns"]["items"]}
    fast = derive_fast_context(source, history_turns=20, directive_history=5)

    assert fast["context_scope"]["complete"] is True
    assert fast["context_scope"]["statements"] == []


def test_closed_directives_can_be_left_out_and_are_counted():
    fast = derive_fast_context(payload())

    assert "directive_history" not in fast
    entry = fast["context_scope"]["withheld"]["directive_history"]
    assert entry["total"] == 2 and entry["omitted"] == 2
    assert "2 earlier closed directive(s) are not shown" in " ".join(
        fast["context_scope"]["statements"])
    assert fast["directive"]["directive_id"] == "d-3"


def test_closed_directives_can_be_shortened_and_state_what_is_lost():
    fast = derive_fast_context(payload(), directive_history=1)

    assert [d["directive_id"] for d in fast["directive_history"]] == ["d-2"]
    entry = fast["context_scope"]["truncated"]["directive_history"]
    assert entry["kept"] == 1 and entry["total"] == 2 and entry["omitted"] == 1
    assert "replan conditions" in " ".join(fast["context_scope"]["statements"])
    assert fast["directive"]["directive_id"] == "d-3"


def test_guidance_can_be_reduced_to_open_hypotheses_with_counts_stated():
    fast = derive_fast_context(payload(), guidance_open_hypotheses=1)

    assert [h["id"] for h in fast["guidance"]["current_hypotheses"]] == ["h3"]
    assert "earlier_versions" not in fast["guidance"]
    entry = fast["context_scope"]["truncated"]["guidance"]
    assert entry["hypotheses_open_total"] == 2 and entry["hypotheses_closed_total"] == 1
    assert entry["open_hypotheses_withheld"] == 1
    assert entry["closed_hypotheses_withheld"] == 1
    assert "none of 1 closed ones" in " ".join(fast["context_scope"]["statements"])


def test_provenance_can_be_left_out_only_when_it_is_said_to_be_unknown():
    fast = derive_fast_context(payload(), directive_provenance=False)

    assert "directive_provenance" not in fast
    assert "directive_provenance" in fast["context_scope"]["withheld"]
    assert "age of any directive in this request is unknown" in " ".join(
        fast["context_scope"]["statements"])


def test_an_unknown_field_is_reported_not_dropped_in_silence():
    source = payload()
    source["something_new"] = {"x": 1}
    fast = derive_fast_context(source)

    assert "something_new" not in fast
    entry = fast["context_scope"]["withheld"]["something_new"]
    assert entry["unclassified"] is True
    assert "'something_new'" in " ".join(fast["context_scope"]["statements"])


def test_the_callers_payload_is_never_changed():
    source = payload()
    before = copy.deepcopy(source)
    fast = derive_fast_context(source, history_turns=1, directive_history=1,
                               guidance_open_hypotheses=1, directive_provenance=False)

    assert source == before
    fast["recent_history"]["turns"]["items"].clear()
    fast["guidance"]["current_hypotheses"].clear()
    assert source["recent_history"]["turns"]["items"]
    assert source["guidance"]["current_hypotheses"]


def test_the_derivation_is_deterministic():
    assert derive_fast_context(payload()) == derive_fast_context(payload())


@pytest.mark.parametrize("kwargs", [
    {"history_turns": -1},
    {"history_turns": True},
    {"directive_history": -2},
    {"guidance_open_hypotheses": -1},
])
def test_a_negative_or_boolean_count_is_refused(kwargs):
    with pytest.raises(ValueError):
        derive_fast_context(payload(), **kwargs)


def test_a_payload_without_choices_is_refused():
    source = payload()
    del source["choices"]
    with pytest.raises(ValueError, match="choices"):
        derive_fast_context(source)


def test_the_scope_carries_no_judgement_about_what_matters():
    scope = derive_fast_context(payload())["context_scope"]
    text = " ".join(scope["statements"]).lower()
    for word in ("important", "best", "should", "recommend", "must", "prefer"):
        assert word not in text
    assert scope["withheld"]["recent_history"]["why"]
