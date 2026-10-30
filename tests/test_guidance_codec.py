"""The guidance layout (#170): lossless, exact, and sent only where `restore` gives the list back.

Synthetic data with neutral words; no trace, no model call. `render` lays the adopted frontier guidance out as
current rows, earlier versions and a ledger, and `restore` is its exact inverse. How the turn loop uses the pair is
in `test_loop_guidance_layout.py`.
"""

from __future__ import annotations

import copy
import random
import types
from pathlib import Path

import pytest

from playtest.core.context import canonical
from playtest.runner import guidance_codec as gc
from playtest.runner.frontier_help import FrontierHelp
from test_name_blind import BANNED, words

REF = "other_fields_same_as_requested_turn"
FIELDS = ("status", "statement", "confirm_if", "refute_if", "supporting_seq", "contradicting_seq")


def row(hid, status="open", statement="S", confirm="C", refute="R", sup=(), con=()):
    return {"confirm_if": confirm, "contradicting_seq": list(con), "id": hid, "refute_if": refute,
            "statement": statement, "status": status, "supporting_seq": list(sup)}


def item(turn, rows, *, adopted=None, age=1.5, waited=2.0, directive="d", extra=None, source="frontier"):
    result = {"hypotheses": rows, **({"directive_id": directive} if directive is not None else {}), **(extra or {})}
    return {"source": source, "kind": "escalate", "choice_id": "escalate", "requested_turn": turn,
            "adopted_turn": turn + 2 if adopted is None else adopted, "age_s": age, "waited_s": waited, "result": result}


def neutral():
    return [
        item(4, [row("h-1", statement="The counter rises after the action.", confirm="The counter is higher later.",
                     refute="The counter is unchanged later.", sup=[11]),
                 row("h-2", statement="The flag clears by itself.", confirm="The flag is false later.",
                     refute="The flag stays true.", sup=[12])], age=30.0, directive="d-4"),
        item(9, [row("h-1", statement="The counter rises by about two after the action.",
                     confirm="The counter is higher later.", refute="The counter is unchanged later.", sup=[20]),
                 row("h-2", status="contradicted", statement="The flag clears by itself.", confirm="The flag is false later.",
                     refute="The flag stays true.", con=[21]),
                 row("h-3", statement="The timer restarts.", confirm="The timer reads zero.", refute="It does not.")],
             age=20.0, directive="d-9"),
        item(15, [row("h-1", statement="The counter rises by about two after the action.",
                      confirm="The counter is at least two higher on the next reading.",
                      refute="The counter is unchanged later.", sup=[30])],
             age=4.25, directive="d-15", extra={"candidate_finding": {"finding_id": "f-15", "reason": None}}),
    ]


def independent_restore(view):
    """A second reading of the documented layout. It shares no code with the codec and looks a referenced version up by (id, turn)."""
    versions = {}
    for r in view["current_hypotheses"]:
        versions[(r["id"], r["requested_turn"])] = {f: r[f] for f in FIELDS}
    for r in view["earlier_versions"]:  # newest first within an id
        base = versions[(r["id"], r[REF])] if REF in r else {}
        versions[(r["id"], r["requested_turn"])] = {f: (r[f] if f in r else base[f]) for f in FIELDS}
    out = []
    for entry in view["help_adoptions"]:
        if not isinstance(entry, dict) or "hypothesis_ids" not in entry:
            out.append(entry)
            continue
        rows = [dict(sorted({"id": hid, **versions[(hid, entry["requested_turn"])]}.items()))
                for hid in entry["hypothesis_ids"]]
        out.append({**{k: v for k, v in entry.items() if k not in ("hypothesis_ids", "result")},
                    "result": {"hypotheses": rows, **entry["result"]}})
    return out


def roundtrips(items):
    before = copy.deepcopy(items)
    view = gc.render(items)
    assert gc.same(items, before), "render changed its input"
    assert gc.same(gc.restore(view), items) and gc.same(independent_restore(view), items)
    return view


# --- the neutral example in docs/design/turn-loop.md ------------------------------------------------------------------


def test_neutral_example_layout_and_size():
    items = neutral()
    view = roundtrips(items)
    assert list(view) == ["current_hypotheses", "earlier_versions", "help_adoptions"]
    current = {r["id"]: r for r in view["current_hypotheses"]}
    assert set(current) == {"h-1", "h-2", "h-3"}
    assert current["h-1"]["requested_turn"] == 15 and current["h-2"]["status"] == "contradicted"
    earlier = [(r["id"], r["requested_turn"], sorted(k for k in r if k in FIELDS), r.get(REF))
               for r in view["earlier_versions"]]
    assert earlier == [("h-1", 9, ["confirm_if", "supporting_seq"], 15),
                       ("h-1", 4, ["statement", "supporting_seq"], 9),
                       ("h-2", 4, ["contradicting_seq", "status", "supporting_seq"], 9)]
    ledger = view["help_adoptions"]
    assert [e["hypothesis_ids"] for e in ledger] == [["h-1", "h-2"], ["h-1", "h-2", "h-3"], ["h-1"]]
    assert ledger[2]["result"] == {"directive_id": "d-15", "candidate_finding": {"finding_id": "f-15", "reason": None}}
    assert [e["age_s"] for e in ledger] == [30.0, 20.0, 4.25]
    assert (len(canonical(items)), len(canonical(view))) == (1836, 1790)


def test_every_old_statement_and_condition_survives_in_the_rendered_text():
    items = neutral()
    text = canonical(roundtrips(items))
    for it in items:
        for r in it["result"]["hypotheses"]:
            for f in ("statement", "confirm_if", "refute_if"):
                assert canonical(r[f])[1:-1] in text


def test_a_current_row_states_every_field_and_a_marker_names_the_version_it_equals():
    view = roundtrips(neutral())
    assert all(set(r) == {"id", "requested_turn", *FIELDS} for r in view["current_hypotheses"])
    stated = {(r["id"], r["requested_turn"]): r for r in view["current_hypotheses"] + view["earlier_versions"]}
    restored = {(r["id"], entry["requested_turn"]): r
                for entry, original in zip(view["help_adoptions"], independent_restore(view))
                for r in original["result"]["hypotheses"]}
    marked = [r for r in view["earlier_versions"] if REF in r]
    assert marked and all((r["id"], r[REF]) in stated for r in marked)
    for r in marked:  # every field the row leaves out is equal, as the frontier wrote it, in the version it names
        for f in FIELDS:
            if f not in r:
                assert gc.same(restored[(r["id"], r["requested_turn"])][f], restored[(r["id"], r[REF])][f])
    for r in view["earlier_versions"]:  # and the stated ones differ, so nothing is left out for being "the same" by default
        newer = restored[(r["id"], r[REF])] if REF in r else None
        assert all(newer is None or not gc.same(r[f], newer[f]) for f in FIELDS if f in r)


# --- unknowns, empties, types -----------------------------------------------------------------------------------------


def test_unknown_ages_and_empty_or_null_values_are_values_not_omissions():
    items = [item(1, [row("a", sup=[1, 1, 3, 2])], age=None, waited=None),
             item(2, [row("a", sup=[], statement=None)], age=None, waited=0.0),
             item(3, [row("a", sup=[], statement=None, confirm="")], age=0.0, waited=None),
             item(4, [row("a", sup=[3, 3], statement="S", con=[0])], age=-0.0, waited=None)]
    view = roundtrips(items)
    ledger = view["help_adoptions"]
    assert [e["age_s"] for e in ledger] == [None, None, 0.0, -0.0] and repr(ledger[3]["age_s"]) == "-0.0"
    assert ledger[0]["waited_s"] is None and ledger[1]["waited_s"] == 0.0
    # duplicate and unordered citations stay exactly as the frontier wrote them
    assert gc.restore(view)[0]["result"]["hypotheses"][0]["supporting_seq"] == [1, 1, 3, 2]
    by_turn = {r["requested_turn"]: r for r in view["earlier_versions"]}
    assert by_turn[3]["confirm_if"] == "" and by_turn[3]["supporting_seq"] == []  # an empty string, an empty list: values
    # a null is stated wherever it occurs, never carried by the marker, even where the next newer version has one too
    assert by_turn[3]["statement"] is None and by_turn[2]["statement"] is None
    assert REF in by_turn[2] and "supporting_seq" not in by_turn[2]  # an equal empty list is carried, as any equal value


def test_values_that_compare_equal_in_python_but_differ_in_json_are_changes():
    items = [item(1, [row("a", sup=[1], confirm=0)]), item(2, [row("a", sup=[1.0], confirm=False)]),
             item(3, [row("a", sup=[True], confirm=0.0)])]
    view = roundtrips(items)
    for earlier in view["earlier_versions"]:
        assert "supporting_seq" in earlier and "confirm_if" in earlier  # none of them was treated as unchanged
    restored = gc.restore(view)
    assert [type(x["result"]["hypotheses"][0]["confirm_if"]) for x in restored] == [int, bool, float]


# --- shapes the codec must carry untouched ----------------------------------------------------------------------------


def test_other_helpers_data_and_unrecognised_frontier_results_stay_byte_identical():
    foreign = item(5, [row("a", statement="looks like a hypothesis")], source="operator")
    items = [item(1, [row("a")]),
             foreign,
             {"source": "frontier", "kind": "escalate", "choice_id": "escalate", "requested_turn": 6,
              "adopted_turn": 8, "age_s": 3.0, "waited_s": 1.0,
              "result": {"error": "approved frontier call cap reached", "hypotheses": [], "directive": None}},
             {"source": "frontier", "kind": "escalate", "choice_id": "escalate", "requested_turn": 7,
              "adopted_turn": 9, "age_s": 2.0, "waited_s": 1.0,
              "result": {"error": "HTTP 500", "errors": [], "hypothesis": None, "directive": None}},
             {"source": None, "kind": "delegate", "choice_id": "x", "requested_turn": 8, "adopted_turn": 10,
              "age_s": None, "waited_s": None, "result": "replacement"},
             {"source": "frontier", "kind": "escalate", "choice_id": "escalate", "requested_turn": 9,
              "adopted_turn": 11, "age_s": 1.0, "waited_s": 1.0,
              "result": {"hypotheses": [], "restated_hypotheses": ["a"], "directive_id": None}},
             item(10, [row("a", statement="later")])]
    view = roundtrips(items)
    kept = 0
    for original, entry in zip(items, view["help_adoptions"]):
        if not gc.liftable(original):
            kept += 1
            assert canonical(entry) == canonical(original)  # not one byte of another helper's data changes
            assert entry is not original and "hypothesis_ids" not in entry
    assert kept == 5
    # the operator's frontier-looking row did not join the frontier id's chain
    assert [(r["id"], r["requested_turn"]) for r in view["earlier_versions"]] == [("a", 1)]
    assert [r["requested_turn"] for r in view["current_hypotheses"]] == [10]


def test_review_errors_candidate_and_restated_metadata_are_carried_verbatim():
    meta = {"candidate_finding": {"finding_id": "f-1", "reason": "not recorded: no observations cited"},
            "errors": ["a problem"], "review": [{"item": "directive", "reason": "why"}], "restated_hypotheses": ["b"]}
    items = [item(1, [row("a")], extra=meta), item(2, [row("a", statement="T")], directive=None)]
    view = roundtrips(items)
    assert {k: view["help_adoptions"][0]["result"][k] for k in meta} == meta
    assert view["help_adoptions"][1]["result"] == {}  # the answer that named no directive says so by having none


def test_ids_that_skip_adoptions_reorder_or_close_and_reopen():
    items = [item(1, [row("a"), row("b"), row("c")]),
             item(2, [row("c", statement="c2"), row("a", statement="a2")]),
             item(3, [row("b", status="withdrawn")]),
             item(4, [row("a", statement="a2", sup=[9]), row("b", status="open"), row("d")]),
             item(5, [row("d", con=[1, 2]), row("a")])]
    view = roundtrips(items)
    assert [r["id"] for r in view["current_hypotheses"]] == ["a", "b", "c", "d"]
    assert [e["hypothesis_ids"] for e in view["help_adoptions"]] == [["a", "b", "c"], ["c", "a"], ["b"], ["a", "b", "d"],
                                                                    ["d", "a"]]
    withdrawn = [r for r in view["earlier_versions"] if r["id"] == "b" and r["requested_turn"] == 3]
    assert withdrawn and withdrawn[0]["status"] == "withdrawn"  # a closed hypothesis stays visible, with its status


# --- what the codec refuses, and what it carries whole -----------------------------------------------------------------


def refused_inputs():
    good = row("a")
    yield "original uses the ledger key", [{**item(1, [good]), "hypothesis_ids": ["a"]}]
    yield "original row uses the marker", [item(1, [good]), item(2, [{**row("a", statement="x"), REF: 1}])]
    yield "marker on a row of an unrecognised answer", [item(1, [{**good, REF: 1}])]
    yield "same requested_turn twice", [item(1, [good]), item(1, [row("a", statement="x")])]


@pytest.mark.parametrize("name,items", list(refused_inputs()))
def test_input_that_would_be_mistaken_for_the_layouts_own_keys_is_refused(name, items):
    with pytest.raises(gc.CodecError):
        gc.render(items)


def carried_whole_inputs():
    good = row("a")
    yield "duplicate id in one answer", [item(1, [row("a"), row("a", statement="x")])]
    yield "extra key on a row", [item(1, [{**good, "extra": 1}])]
    yield "missing key on a row", [item(1, [{k: v for k, v in good.items() if k != "status"}])]
    yield "boolean requested_turn", [item(True, [good])]
    yield "float requested_turn", [item(1.0, [good])]
    yield "row id that is not text", [item(1, [{**good, "id": 7}])]
    yield "rows that are not mappings", [item(1, ["row", None])]
    yield "hypotheses that are not a list", [{**item(1, []), "result": {"hypotheses": "none"}}]
    yield "no result", [{"source": "frontier", "requested_turn": 1}]


@pytest.mark.parametrize("name,items", list(carried_whole_inputs()))
def test_a_frontier_answer_the_codec_does_not_recognise_is_carried_whole_and_exact(name, items):
    view = roundtrips(items)
    assert view["current_hypotheses"] == [] and view["earlier_versions"] == []
    assert canonical(view["help_adoptions"]) == canonical(items)


def test_a_frontier_answer_with_an_unknown_row_shape_stays_verbatim_while_the_rest_is_compacted():
    odd = item(5, [{**row("a", statement="odd"), "confidence": 0.5}])  # a future row field the codec does not know
    items = [item(1, [row("a")]), odd, item(9, [row("a", statement="later")])]
    view = roundtrips(items)
    assert canonical(view["help_adoptions"][1]) == canonical(odd)  # carried as it came, extra field and all
    assert [(r["id"], r["requested_turn"]) for r in view["earlier_versions"]] == [("a", 1)]


def test_nothing_adopted_and_nothing_from_the_frontier():
    empty = roundtrips([])
    assert empty == {"current_hypotheses": [], "earlier_versions": [], "help_adoptions": []}
    other = [item(1, [row("a")], source="operator"), {"source": "x", "result": {"advice": "external"}}]
    view = roundtrips(other)
    assert view["current_hypotheses"] == [] and view["earlier_versions"] == []
    assert canonical(view["help_adoptions"]) == canonical(other)


def test_elements_that_are_not_mappings_are_carried_verbatim():
    items = [None, "text", 5, [1, 2, {"a": 1}], item(1, [row("a")]), {"source": "frontier"}, item(2, [row("a", statement="T")])]
    view = roundtrips(items)
    assert [e for e in view["help_adoptions"] if not (isinstance(e, dict) and "hypothesis_ids" in e)] == [
        None, "text", 5, [1, 2, {"a": 1}], {"source": "frontier"}]


def test_non_finite_numbers_are_refused_not_guessed():
    # canonical() refuses NaN and infinity, so no pair of values holding one can be proven equal or different.
    with pytest.raises(ValueError):
        gc.render([item(1, [row("a")]), item(2, [row("a", sup=[float("nan")])])])
    items = [item(1, [row("a")], age=float("nan"))]
    view = gc.render(items)
    assert view["help_adoptions"][0]["age_s"] != view["help_adoptions"][0]["age_s"]  # carried through untouched
    with pytest.raises(ValueError):
        gc.same(gc.restore(view), items)  # nothing can show it exact, so the turn loop would send the list


# --- restore refuses a rendering it could not have produced, and notices any edit ----------------------------------------


def null_marker():
    """A rendering whose older row leaves out a field the newer version holds as null, so its marker would stand for it."""
    view = gc.render([item(1, [row("a", statement="S")]), item(2, [row("a", statement=None)])])
    assert view[gc.EARLIER][0]["statement"] == "S"
    del view[gc.EARLIER][0]["statement"]  # the newer version holds null there, so the marker would now stand for it
    return view


def malformed_renderings():
    good = gc.render(neutral())

    def edited(change):
        view = copy.deepcopy(good)
        change(view)
        return view

    yield "a list", []
    yield "a block missing", {k: v for k, v in good.items() if k != gc.EARLIER}
    yield "an extra block", {**good, "extra": []}
    yield "a block that is not a list", {**good, gc.EARLIER: {}}
    yield "a current row that is not a mapping", edited(lambda v: v[gc.CURRENT].__setitem__(0, "row"))
    yield "two current rows for one id", edited(lambda v: v[gc.CURRENT].append(copy.deepcopy(v[gc.CURRENT][0])))
    yield "a current row without its turn", edited(lambda v: v[gc.CURRENT][0].pop("requested_turn"))
    yield "a boolean turn", edited(lambda v: v[gc.CURRENT][0].__setitem__("requested_turn", True))
    yield "a current row that omits a field", edited(lambda v: v[gc.CURRENT][0].pop("statement"))
    yield "an earlier version that is not a mapping", edited(lambda v: v[gc.EARLIER].__setitem__(0, 5))
    yield "an earlier version with an unknown key", edited(lambda v: v[gc.EARLIER][0].__setitem__("note", "x"))
    yield "an earlier version of an id with no current row", edited(lambda v: v[gc.EARLIER][0].__setitem__("id", "h-9"))
    yield "an earlier version without its id", edited(lambda v: v[gc.EARLIER][0].pop("id"))
    yield "a marker naming a version it does not follow", edited(lambda v: v[gc.EARLIER][0].__setitem__(REF, 4))
    yield "a marker that is not a number", edited(lambda v: v[gc.EARLIER][0].__setitem__(REF, "15"))
    yield "a row that omits fields and has no marker", edited(lambda v: v[gc.EARLIER][0].pop(REF))
    yield "a marker on a row that omits nothing", edited(
        lambda v: v[gc.EARLIER][0].update({f: v[gc.CURRENT][0][f] for f in FIELDS}))
    yield "a marker that would stand for a null", null_marker()
    yield "two versions of one id at one turn", edited(
        lambda v: v[gc.EARLIER].append({**copy.deepcopy(v[gc.EARLIER][0])}))
    yield "a ledger entry naming an unknown id", edited(lambda v: v[gc.LEDGER][0]["hypothesis_ids"].append("h-9"))
    yield "a ledger entry naming a version twice", edited(lambda v: v[gc.LEDGER][0]["hypothesis_ids"].append("h-1"))
    yield "a version no ledger entry names", edited(lambda v: v[gc.LEDGER][2].__setitem__("hypothesis_ids", []))
    yield "a ledger entry whose ids are not a list", edited(lambda v: v[gc.LEDGER][0].__setitem__("hypothesis_ids", "h-1"))
    yield "a ledger entry with rows left in its result", edited(
        lambda v: v[gc.LEDGER][0]["result"].__setitem__("hypotheses", []))
    yield "a ledger entry without a result", edited(lambda v: v[gc.LEDGER][0].pop("result"))
    yield "a ledger entry without its turn", edited(lambda v: v[gc.LEDGER][0].pop("requested_turn"))


@pytest.mark.parametrize("name,view", list(malformed_renderings()))
def test_restore_refuses_a_rendering_it_could_not_have_produced(name, view):
    with pytest.raises(gc.CodecError):
        gc.restore(view)


def test_restore_refuses_or_differs_on_every_single_edit():
    items = neutral()
    view = gc.render(items)
    base = canonical(items)

    def edits(v):
        for block in (gc.CURRENT, gc.EARLIER):
            for i, r in enumerate(v[block]):
                for k in list(r):
                    w = copy.deepcopy(v)
                    del w[block][i][k]
                    yield f"delete {block}[{i}].{k}", w
                    w = copy.deepcopy(v)
                    w[block][i][k] = "tampered" if isinstance(r[k], str) else [999] if isinstance(r[k], list) else 999
                    yield f"edit {block}[{i}].{k}", w
                w = copy.deepcopy(v)
                del w[block][i]
                yield f"drop {block}[{i}]", w
        for i, e in enumerate(v[gc.LEDGER]):
            w = copy.deepcopy(v)
            w[gc.LEDGER][i]["hypothesis_ids"].reverse()
            if w != v:
                yield f"reverse ids[{i}]", w
            w = copy.deepcopy(v)
            w[gc.LEDGER][i]["age_s"] = 999.0
            yield f"edit age[{i}]", w

    count = 0
    for label, w in edits(view):
        count += 1
        try:
            out = gc.restore(w)
        except gc.CodecError:
            continue
        assert canonical(out) != base, label  # an edit is either refused or visible, never silently absorbed
    assert count > 30


# --- determinism, isolation, append-only ledger -------------------------------------------------------------------------


def test_rendering_is_deterministic_and_isolated_from_the_stored_items():
    items = neutral()
    a, b = gc.render(items), gc.render(items)
    assert canonical(a) == canonical(b)
    a["current_hypotheses"][0]["statement"] = "mutated after delivery"
    a["help_adoptions"][0]["result"]["directive_id"] = "mutated"
    a["earlier_versions"][0]["supporting_seq"].append(999)
    assert canonical(gc.render(items)) == canonical(b)
    assert canonical(items) == canonical(neutral())  # the stored items were never reached through the rendering
    restored = gc.restore(b)
    restored[0]["result"]["hypotheses"][0]["supporting_seq"].append(999)
    restored[1]["result"]["directive_id"] = "mutated"
    assert canonical(b) == canonical(gc.render(neutral()))  # ... nor the rendering through what was restored from it


def test_appending_an_adoption_never_rewrites_an_older_ledger_entry():
    items = neutral()
    before, after = gc.render(items[:2]), gc.render(items)
    for old, new in zip(before["help_adoptions"], after["help_adoptions"]):
        assert {k: v for k, v in new.items() if k != "age_s"} == {k: v for k, v in old.items() if k != "age_s"}
    assert [r for r in after["current_hypotheses"] if r["id"] == "h-3"] == [
        r for r in before["current_hypotheses"] if r["id"] == "h-3"]  # an id the new answer did not list is untouched
    assert [r for r in after["current_hypotheses"] if r["id"] == "h-1"][0]["requested_turn"] == 15


# --- random sequences --------------------------------------------------------------------------------------------------


def random_items(rng: random.Random):
    ids = [f"h-{i}" for i in range(rng.randint(1, 6))]
    pools = {"status": ["open", "supported", "contradicted", "withdrawn"], "text": ["A", "B", "C", "", None, "x" * 5]}
    state = {}
    items = []
    for turn in range(rng.randint(1, 12)):
        chosen = rng.sample(ids, rng.randint(1, len(ids)))
        rows = []
        for hid in chosen:
            base = state.get(hid) or row(hid)
            new = dict(base)
            for field, pool in (("status", pools["status"]), ("statement", pools["text"]), ("confirm_if", pools["text"]),
                                ("refute_if", pools["text"])):
                if rng.random() < 0.35:
                    new[field] = rng.choice(pool)
            for field in ("supporting_seq", "contradicting_seq"):
                if rng.random() < 0.5:
                    new[field] = [rng.randint(0, 9) for _ in range(rng.randint(0, 3))]
            state[hid] = new
            rows.append(copy.deepcopy(new))
        extra = {"candidate_finding": {"finding_id": f"f-{turn}", "reason": None}} if rng.random() < 0.2 else None
        foreign = rng.random() < 0.15
        items.append(item(turn * 2, rows, age=rng.choice([None, 0.0, rng.random() * 100]), waited=rng.choice([None, 1.25]),
                          extra=extra, source="operator" if foreign else "frontier"))
        if rng.random() < 0.1:
            items.append({"source": "frontier", "kind": "escalate", "choice_id": "escalate", "requested_turn": turn * 2 + 1,
                          "adopted_turn": turn * 2 + 1, "age_s": 1.0, "waited_s": 1.0,
                          "result": {"hypotheses": [], "restated_hypotheses": ids[:1], "directive_id": None}})
    return items


def test_random_sequences_roundtrip_exactly_through_both_restorers():
    rng = random.Random(170)
    for _ in range(400):
        roundtrips(random_items(rng))


# --- shapes the production publisher really emits (FrontierHelp._guidance, called unbound) ---------------------------------


def test_results_published_by_the_production_publisher_roundtrip_including_restated_ids_and_review():
    shown = types.SimpleNamespace(_shown={})
    rng = random.Random(171)
    saw_restated = saw_review = False
    for _ in range(100):
        shown._shown.clear()
        items = []
        state = {}
        for turn in range(rng.randint(1, 9)):
            rows = []
            for hid in rng.sample(["a", "b", "c", "d"], rng.randint(1, 4)):
                new = dict(state.get(hid) or row(hid))
                if rng.random() < 0.5:
                    new["statement"] = rng.choice(["X", "Y", "Z"])
                if rng.random() < 0.3:
                    new["status"] = rng.choice(["open", "supported", "contradicted", "withdrawn"])
                if rng.random() < 0.4:
                    new["supporting_seq"] = [rng.randint(0, 9)]
                state[hid] = new
                rows.append(copy.deepcopy(new))
            payload = {"hypotheses": rows, "directive": {"directive_id": f"d-{turn}"} if rng.random() < 0.7 else None}
            if rng.random() < 0.2:
                payload["errors"] = ["e"]
                payload["review"] = [{"item": "directive", "reason": "r"}]
            items.append({"source": "frontier", "kind": "escalate", "choice_id": "escalate", "requested_turn": turn * 3,
                          "adopted_turn": turn * 3 + 1, "age_s": rng.choice([None, 12.5]), "waited_s": 1.5,
                          "result": FrontierHelp._guidance(shown, payload)})
        saw_restated |= any("restated_hypotheses" in i["result"] for i in items)
        saw_review |= any("review" in i["result"] for i in items)
        roundtrips(items)
        if saw_restated and saw_review:
            break
    assert saw_restated and saw_review, "the sequence generator never produced a restated id or a review"


def test_the_layout_carries_every_field_the_frontier_states_about_a_hypothesis():
    # A row with a field the codec does not know is carried whole, which is lossless but stops compacting; this makes
    # adding a field to the answer model a visible decision here instead of a silent loss of the saving.
    from playtest.roles.frontier import HypothesisOut

    assert set(HypothesisOut.model_fields) == set(FIELDS) | {"id"} == set(gc.ROW_KEYS)
    published = HypothesisOut(id="h-1", status="open", statement="S", confirm_if="C", refute_if="R",
                              supporting_seq=[1], contradicting_seq=[]).model_dump(mode="json")
    assert gc.liftable(item(1, [published]))


def test_the_frontier_helpers_two_methods_are_the_codec_and_nothing_else():
    helper = FrontierHelp.__new__(FrontierHelp)  # the methods read no state
    items = neutral()
    assert canonical(helper.render_guidance(copy.deepcopy(items))) == canonical(gc.render(items))
    assert gc.same(helper.restore_guidance(helper.render_guidance(items)), items)
    with pytest.raises(gc.CodecError):
        helper.restore_guidance({"current_hypotheses": []})


def test_the_codec_names_no_game_or_genre_word():
    lines = Path(gc.__file__).read_text(encoding="utf-8").splitlines()
    assert not [n for n, line in enumerate(lines, 1) if words(line) & BANNED]
