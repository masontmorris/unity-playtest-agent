"""How the turn loop sends a helper's guidance layout (#170).

A port that offers `render_guidance` and `restore_guidance` has its layout sent only where `restore_guidance`
gives back, exactly, the list S1 would otherwise read. Anything else (no layout, a lossy one, a malformed one, a
fault) sends today's list unchanged. The layout changes how adopted results are laid out and nothing else: what is
adopted, when, and what the loop does about cancellation, a scene boundary or an operator's result stay as they are.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from hashlib import sha256

import pytest

from playtest.core.adherence import check_adherence
from playtest.core.context import build_context, canonical
from playtest.core.ports import HelpResult
from playtest.core.turn_loop import TurnLoop
from test_context import choices as context_choices, observation as context_observation
from test_decision_memory import Bridge, S1, WALL, choices, observation
from test_frontier_publication import _answer_for, _helper, _reply
from test_loop_help_publication import live_choices

GOLDEN = "b88c8e7b32fb34401b3d4e4ca20a06d7aac5e3fb306e63511f0fa3b109ef6c5d"
ADVICE = {"advice": "try the other control"}
ADOPTED = {"source": "fake-helper", "kind": "escalate", "choice_id": "h", "requested_turn": 0, "adopted_turn": 1,
           "age_s": 0.0, "waited_s": 0.0, "result": ADVICE}


# --- the builder -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("guidance", [(), [], {}])
def test_the_builder_bytes_without_guidance_are_unchanged(guidance):
    payload = build_context(objective="Explore.", observation=context_observation(), choices=context_choices(),
                            guidance=guidance)
    assert "guidance" not in payload
    assert sha256(canonical(payload).encode("utf-8")).hexdigest() == GOLDEN


def test_the_builder_takes_a_list_as_before_and_copies_a_mapping():
    kwargs = {"objective": "o", "observation": context_observation(), "choices": context_choices()}
    items = [{"source": "x", "age_s": 1.0, "result": {"k": [1]}}]
    listed = build_context(**kwargs, guidance=items)
    assert listed["guidance"] == items and listed["guidance"] is not items and listed["guidance"][0] is not items[0]
    layout = {"current": [{"k": [1]}], "ledger": [{"age_s": None}]}
    mapped = build_context(**kwargs, guidance=layout)
    assert mapped["guidance"] == layout and mapped["guidance"] is not layout
    layout["current"][0]["k"].append(2)
    assert mapped["guidance"]["current"][0]["k"] == [1]  # later changes to what the caller held cannot alter the request
    mapped["guidance"]["ledger"][0]["age_s"] = 5.0
    assert layout["ledger"] == [{"age_s": None}]


# --- a helper with no layout, a lossless one and a faulty one -------------------------------------------------------------


class Port:
    """An attached helper that answers at once. It offers no layout, so S1 reads the list as adopted."""

    def __init__(self, result=None, source="fake-helper"):
        self.result = copy.deepcopy(ADVICE) if result is None else result
        self.source = source

    async def run(self, request):
        return HelpResult(source=self.source, result=copy.deepcopy(self.result))


class Laid(Port):
    """The same helper with a layout whose two halves the test chooses."""

    def __init__(self, render, restore, **kwargs):
        super().__init__(**kwargs)
        self._render, self._restore = render, restore

    def render_guidance(self, items):
        return self._render(items)

    def restore_guidance(self, view):
        return self._restore(view)


async def adopt_once(port):
    """Escalate on turn 0, let the helper finish, and read two more contexts: the first adopts, the second keeps it."""
    s1 = S1(["h", "w", "w"])
    loop = TurnLoop(Bridge([observation(100 + i) for i in range(3)]), choices=choices(), objective="Inspect.", s1=s1,
                    help=port, clock=lambda: WALL)
    try:
        await loop.run_turn()
        await loop._pending.task
        await loop.run_turn()
        await loop.run_turn()
    finally:
        await loop.stop("user")
    return s1.contexts, loop


def entries(items):
    return {"entries": items}


def test_a_helper_without_a_layout_gets_the_list_exactly_as_adopted():
    contexts, loop = asyncio.run(adopt_once(Port()))
    assert "guidance" not in contexts[0]
    assert contexts[1]["guidance"] == [ADOPTED] and contexts[2]["guidance"] == [ADOPTED]
    assert check_adherence(loop.turns) == [] and loop.adoptions == [ADVICE]


def test_half_a_layout_is_no_layout():
    class OnlyRender(Port):
        def render_guidance(self, items):
            return entries(items)

    class OnlyRestore(Port):
        def restore_guidance(self, view):
            return view["entries"]

    for port in (OnlyRender(), OnlyRestore()):
        contexts, _ = asyncio.run(adopt_once(port))
        assert contexts[1]["guidance"] == [ADOPTED]


def test_a_layout_that_restores_the_list_exactly_is_what_s1_reads():
    contexts, loop = asyncio.run(adopt_once(Laid(entries, lambda view: view["entries"])))
    assert "guidance" not in contexts[0]
    assert contexts[1]["guidance"] == entries([ADOPTED]) and contexts[2]["guidance"] == entries([ADOPTED])
    assert check_adherence(loop.turns) == [] and loop.adoptions == [ADVICE]  # what is adopted is the helper's, not the layout


def drop_age(items):
    return {"entries": [{k: v for k, v in i.items() if k != "age_s"} for i in items]}


def edit_in_place(items):
    items[0]["result"] = None  # a renderer that rewrites what it was handed
    return {"entries": items}


def lossy_cases():
    yield "drops a key", drop_age, lambda view: view["entries"]
    yield "restores a different type", lambda items: entries(items), lambda view: [
        {**view["entries"][0], "waited_s": 0}]  # 0 is not 0.0
    yield "restores fewer items", lambda items: entries(items), lambda view: []
    yield "restores more items", lambda items: entries(items), lambda view: view["entries"] * 2
    yield "edits its argument in place", edit_in_place, lambda view: view["entries"]
    yield "restores something that is not a list", entries, lambda view: "entries"


def faults():
    def boom(*_args):
        raise RuntimeError("the layout is broken")

    yield "render raises", boom, lambda view: view["entries"]
    yield "restore raises", entries, boom
    yield "render returns nothing", lambda items: None, lambda view: []
    yield "render returns text", lambda items: "layout", lambda view: []
    yield "restore returns nothing", entries, lambda view: None
    yield "restore cannot read the view", entries, lambda view: view["missing"]


@pytest.mark.parametrize("name,render,restore", [*lossy_cases(), *faults()])
def test_a_lossy_or_faulty_layout_is_never_sent_and_the_turn_is_unchanged(name, render, restore):
    contexts, loop = asyncio.run(adopt_once(Laid(render, restore)))
    assert contexts[1]["guidance"] == [ADOPTED] and contexts[2]["guidance"] == [ADOPTED]
    assert [t.raw_choice for t in loop.turns] == ["h", "w", "w"] and check_adherence(loop.turns) == []
    assert loop.adoptions == [ADVICE]


def test_the_layout_is_given_copies_so_it_cannot_reach_what_the_loop_stored_or_what_it_sends():
    seen = {}

    def render(items):
        seen["handed"] = items
        view = entries(copy.deepcopy(items))
        items[0]["result"]["advice"] = "changed through the argument"
        return view

    def restore(view):
        items = copy.deepcopy(view["entries"])
        view["entries"].clear()  # a restore that eats its argument
        return items

    contexts, loop = asyncio.run(adopt_once(Laid(render, restore)))
    # the list the layout was compared against was never the one it edited, and the view that was sent was not the one it emptied
    assert contexts[1]["guidance"] == entries([ADOPTED]) and contexts[2]["guidance"] == entries([ADOPTED])
    assert seen["handed"][0]["result"]["advice"] == "changed through the argument"
    assert loop.adoptions == [ADVICE]


# --- the frontier's own layout through the real loop ------------------------------------------------------------------


def counting(observations):
    return [observation(seq).model_copy(update={"public": {"counter": 0}}) for seq in observations]


def revising_send():
    """A frontier that restates the open hypothesis it was sent, reworded and with more evidence, and otherwise states a new one."""
    def send(body):
        packet = json.loads(body["messages"][2]["content"])
        answer = _answer_for(packet)
        if packet["hypotheses"]:
            held = packet["hypotheses"][0]
            answer["hypotheses"][0].update(id=held["id"], statement=f"{held['statement']} Again.",
                                           supporting_seq=[packet["observation"]["seq"]])
            answer["directive"] = None
        return _reply(answer)

    return send


def adopted_items(sink):
    """The list S1 reads, rebuilt from the trace's own `help` entries (the clock is frozen, so every age is 0.0)."""
    return [{"source": e["body"]["source"], "kind": e["body"]["kind"], "choice_id": e["body"]["choice_id"],
             "requested_turn": e["body"]["opened_turn"], "adopted_turn": e["turn"], "age_s": 0.0, "waited_s": 0.0,
             "result": e["body"]["result"]} for e in sink.of_kind("help") if e["body"]["event"] == "adopted"]


def test_the_frontier_layout_reaches_s1_and_restores_to_the_list_the_trace_holds(tmp_path):
    async def scenario():
        helper, tracker, sink = _helper(tmp_path, revising_send())
        s1 = S1(["h", "h", "h", "w"])
        loop = TurnLoop(Bridge(counting(range(100, 104))), choices=live_choices(), objective="Inspect.", s1=s1,
                        help=helper, facts=tracker, sink=sink, clock=lambda: WALL)
        try:
            for _ in range(3):
                await loop.run_turn()
                await loop._pending.task
            await loop.run_turn()
        finally:
            await loop.stop("user")
        return helper, sink, s1.contexts

    helper, sink, contexts = asyncio.run(scenario())
    assert "guidance" not in contexts[0]
    for turn, count in ((1, 1), (2, 2), (3, 3)):
        guidance = contexts[turn]["guidance"]
        assert set(guidance) == {"current_hypotheses", "earlier_versions", "help_adoptions"}
        assert len(guidance["help_adoptions"]) == count
        # S1's layout restores to the list the trace's help entries rebuild: nothing in it is the layout's own
        assert canonical(helper.restore_guidance(guidance)) == canonical(adopted_items(sink)[:count])
        row = sink.of_kind("context")[turn]["body"]  # the trace holds the context as sent
        assert canonical(row["payload"]["guidance"]) == canonical(guidance)
        assert row["sha256"] == sha256(canonical(row["payload"]).encode("utf-8")).hexdigest()
    last = contexts[3]["guidance"]
    assert [r["id"] for r in last["current_hypotheses"]] == ["hyp-0-0"]
    assert last["current_hypotheses"][0]["requested_turn"] == 2
    assert [r["requested_turn"] for r in last["earlier_versions"]] == [1, 0]  # newest first, nothing dropped
    assert [e["hypothesis_ids"] for e in last["help_adoptions"]] == [["hyp-0-0"]] * 3
    assert [e["result"].get("directive_id") for e in last["help_adoptions"]] == ["d-0", None, None]


# --- behaviour that must be exactly today's ----------------------------------------------------------------------------


class ListOnly:
    """The real helper behind a port that offers no layout: the loop as it behaved before the layout existed."""

    def __init__(self, helper):
        self._helper = helper

    async def run(self, request):
        return await self._helper.run(request)

    def adopt_result(self, request_turn, result):
        return self._helper.adopt_result(request_turn, result)

    def discard_result(self, request_turn):
        self._helper.discard_result(request_turn)


def both_ways(tmp_path, observations, steps, script):
    """Play one scenario twice on the real helper, with the layout and without. `script(loop, gate)` drives the turns."""
    async def play(name, laid_out):
        gate = {"entered": threading.Event(), "release": threading.Event()}
        send = revising_send()

        def gated(body):
            gate["entered"].set()
            assert gate["release"].wait(5), "the test did not release the fake transport"
            return send(body)

        helper, tracker, sink = _helper(tmp_path / name, gated)
        s1 = S1(steps)
        loop = TurnLoop(Bridge(observations()), choices=live_choices(), objective="Inspect.", s1=s1,
                        help=helper if laid_out else ListOnly(helper), facts=tracker, sink=sink, clock=lambda: WALL)
        try:
            await script(loop, gate)
        finally:
            gate["release"].set()
            await loop.stop("user")
        return helper, sink, s1.contexts, loop

    laid = asyncio.run(play("laid", True))
    plain = asyncio.run(play("plain", False))
    helper, sink_a, contexts_a, loop_a = laid
    _, sink_b, contexts_b, loop_b = plain
    assert len(contexts_a) == len(contexts_b)
    for a, b in zip(contexts_a, contexts_b):
        # nothing but the guidance field differs ...
        assert canonical({k: v for k, v in a.items() if k != "guidance"}) == canonical(
            {k: v for k, v in b.items() if k != "guidance"})
        assert ("guidance" in a) == ("guidance" in b)
        if "guidance" in a:  # ... and that restores to the list the loop sends when it has no layout
            assert isinstance(a["guidance"], dict) and isinstance(b["guidance"], list)
            assert canonical(helper.restore_guidance(a["guidance"])) == canonical(b["guidance"])
    assert [(e["turn"], e["body"]) for e in sink_a.of_kind("help")] == [(e["turn"], e["body"]) for e in sink_b.of_kind("help")]
    assert [{k: v for k, v in t.model_dump().items() if k != "latency_ms"} for t in loop_a.turns] == [
        {k: v for k, v in t.model_dump().items() if k != "latency_ms"} for t in loop_b.turns]  # latency is wall time
    assert loop_a.adoptions == loop_b.adoptions and loop_a.cancelled_helpers == loop_b.cancelled_helpers
    assert check_adherence(loop_a.turns) == []
    return contexts_a, contexts_b, loop_a


def test_a_request_cancelled_by_a_boundary_leaves_no_row_version_or_entry(tmp_path):
    async def script(loop, gate):
        gate["release"].set()
        await loop.run_turn()
        first = loop._pending.task
        await first  # the answer is staged, then the next observation changes the scene
        await loop.run_turn()
        await loop.run_turn()  # the new epoch asks again
        await loop._pending.task
        await loop.run_turn()

    laid, plain, loop = both_ways(
        tmp_path, lambda: counting([100]) + [observation(101, scene="s1").model_copy(update={"public": {"counter": 0}}),
                                             *[observation(102 + i, scene="s1").model_copy(update={"public": {"counter": 0}})
                                               for i in range(2)]],
        ["h", "a", "h", "w"], script)
    assert loop.cancelled_helpers == 1
    for context in laid[:3]:
        assert "guidance" not in context  # the cancelled answer was never adopted, shown or kept
    guidance = laid[3]["guidance"]
    assert [e["requested_turn"] for e in guidance["help_adoptions"]] == [2]  # only the request of the new epoch
    assert [r["requested_turn"] for r in guidance["current_hypotheses"]] == [2] and guidance["earlier_versions"] == []
    assert laid[1]["recent_history"]["boundary"]["reasons"] == ["scene_change"]


def test_a_scene_boundary_leaves_adopted_guidance_in_place_as_before(tmp_path):
    async def script(loop, gate):
        gate["release"].set()
        await loop.run_turn()
        await loop._pending.task
        await loop.run_turn()  # adopts
        await loop.run_turn()  # the scene changes
        await loop.run_turn()

    observations = lambda: [observation(100).model_copy(update={"public": {"counter": 0}}),
                            observation(101).model_copy(update={"public": {"counter": 0}}),
                            *[observation(102 + i, scene="s1").model_copy(update={"public": {"counter": 0}})
                              for i in range(2)]]
    laid, plain, loop = both_ways(tmp_path, observations, ["h", "w", "w", "w"], script)
    assert laid[2]["recent_history"]["boundary"]["reasons"] == ["scene_change"]
    assert laid[1]["guidance"] == laid[2]["guidance"] == laid[3]["guidance"]  # clock frozen: the ages did not move either
    assert [e["requested_turn"] for e in laid[3]["guidance"]["help_adoptions"]] == [0]


@pytest.mark.parametrize("external", [
    ADVICE,
    "replacement",
    None,
    {"hypotheses": [{"id": "hyp-0-0", "status": "open", "statement": "An operator wrote this.", "confirm_if": "c",
                     "refute_if": "r", "supporting_seq": [1], "contradicting_seq": []}], "directive_id": None},
])
def test_an_operators_result_while_the_frontier_is_attached_is_in_the_ledger_byte_for_byte(tmp_path, external):
    async def script(loop, gate):
        await loop.run_turn()
        assert await asyncio.to_thread(gate["entered"].wait, 5)  # the frontier's request is in flight
        assert loop.resolve_helper(copy.deepcopy(external), source="operator")
        gate["release"].set()
        await loop.run_turn()  # adopts the operator's result and asks the frontier again
        await loop._pending.task
        await loop.run_turn()  # adopts the frontier's answer
        await loop.run_turn()

    laid, plain, loop = both_ways(tmp_path, lambda: counting(range(100, 104)), ["h", "h", "w", "w"], script)
    operator = {"source": "operator", "kind": "escalate", "choice_id": "h", "requested_turn": 0, "adopted_turn": 1,
                "age_s": 0.0, "waited_s": 0.0, "result": external}
    assert laid[1]["guidance"]["help_adoptions"] == [operator] and plain[1]["guidance"] == [operator]
    assert laid[1]["guidance"]["current_hypotheses"] == [] and laid[1]["guidance"]["earlier_versions"] == []
    ledger = laid[2]["guidance"]["help_adoptions"]
    assert canonical(ledger[0]) == canonical(operator) and "hypothesis_ids" not in ledger[0]
    assert ledger[1]["source"] == "frontier" and ledger[1]["hypothesis_ids"] == ["hyp-1-0"]
    # only the frontier's own row is a current hypothesis; the operator's frontier-looking row never joined its chain
    assert [r["id"] for r in laid[2]["guidance"]["current_hypotheses"]] == ["hyp-1-0"]
    assert loop.adoptions[0] == external
