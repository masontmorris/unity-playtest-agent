"""S1 on Baseten System One: the provider adapter for `mammoth-1`.

The request is the shape proven live: one `choice` question, `next_action`, whose criteria are the
offered choices, and the context payload as `state`. The API answers one choice per question, so when
that answer names an escalate or delegate choice in real time the adapter makes ONE follow-up call in
the same shape, with the question `while_pending` over the wait choice and the executable (action and
skill) choices, and the same state. Its answer becomes the `while_pending` of S1's decision; a failed or
invalid follow-up leaves it absent and the loop applies the recorded policy default of wait.

Every call reserves a ledger row before it is sent (`playtest.bench.call_budget`), and the blocking
HTTP call runs in a worker thread so the bridge client keeps reading on the event loop. The adapter
holds no policy: it never retries, repairs, ranks or substitutes, and the returned distribution is
recorded and never used to select.

`instructions` selects the primary question's text (a named variant, default `INSTRUCTIONS`); the
payload must carry that same text, and `prompt_sha256` hashes exactly the texts this adapter sends.

Every call that was sent also reports its tokens to an injected meter (`playtest.runner.pacing`
paces the next turn from them): the provider's `usage.input_tokens` plus `usage.output_tokens` when
it gave them, else an estimate from the request body, reported as an estimate.

`compact_state` is opt-in and off by default (`playtest.runner.state_codec`).
- `"object"` keeps `state` a JSON object, restructured by the lossless rules with a `_s1_compact` key naming
  them, so the server renders it as it renders any state.
- `"format"`, `"default"` and `"all"` send `state` as one string: a header, then compact JSON. On d1-ultra a
  string state is a different prompt and moves near-tie choices.

Either way the body alone restores the exact context (`state_codec.decode_object` / `decode`).

`state_order` is opt-in and off by default: `canonical` sends the body exactly as this adapter always
has, and `stable-first/1` (`playtest.runner.state_order`) sends the same values with `state`'s
top-level keys in a declared stable-first order. Only that serialization changes; the context record,
the payload and every value are untouched, and the Meta arm keeps its own stable-first text.

`s1_context` selects what S1 reads. `full` (default) sends the payload the turn loop built, byte for byte.
`compact-menu` sends `playtest.runner.menu`'s shorter state built from that same payload: the same offered
ids, one comparable label per choice, the run's measured positions and the game-view note. It needs the
options layer's `OutcomeMemory` (`--options`) for the measured cell, and it bypasses the layout, the state
codec and option ordering rather than combining with them. Positions are accumulated here, per context, and
cleared at a reset boundary: the loop rebinds its own scene-bound ports after a host restart but not this
adapter, so it reads the loop's `reset_summary` (present on the first context after a restart) and, when an
observation carries one, a changed `session` id.

`note_turn` is where the menu's `recent` comes from: the harness hands this adapter each finished turn's
report, and it keeps the last six executed choices with the duration each measurably took and the result it
measurably had — an option's own `terminated_by` and `duration_s`, a plain action's or skill's verdict from
the same rule the options memory files, the applied hold the bridge reported, and `wait`/`finished` (or the
turn's own measured outcome) for the choices that send no input. With `wait_result="unmeasured"` a wait's
result is `unmeasured` instead, since nothing measured it. Unknown stays `unmeasured`. Nothing is
ranked, summarised or recommended, and the adapter can be run without ever being told a turn.

`contingent` is opt-in, off by default, and needs the compact menu, whose request asks `if_blocked` and
`if_succeeded` beside `next_action` over the same criteria. When it is on, the adapter reads those two
answers out of the response it already has and puts them in the decision (`Decision.contingent`), so the
loop can execute what S1 chose for the branch that measurably happened without asking again. The request
is unchanged and the response is already recorded in the `s1_call` entry; the loop's `parse_decision`
binds each id to the offered set and nulls a branch that names nothing offered.

Every request that is sent is also kept in the run folder as `requests/s1/<turn>-<question>.json`
(`playtest.runner.s1_requests`): the body as posted, the digest the `s1_call` entry records, and how to
reproduce that digest. The trace's `context` is the payload the core built, which the compact menu
replaces; the file is what S1 was actually asked.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSet
from playtest.core.context import INSTRUCTIONS, INSTRUCTIONS_KEEP_PLAYING, canonical
from playtest.core.ports import S1Answer, S1Call
from playtest.runner import menu, s1_requests
from playtest.runner.image_transport import IMAGE_TOKENS, IMAGE_TRANSPORT, attach_images, image_url
from playtest.runner.option_order import OptionOrder, ordered_ids
from playtest.runner.request_layout import RequestLayout, encode as encode_request_body
from playtest.runner import state_codec
from playtest.runner.state_order import StateOrder, state_keys
from playtest.runner.wire import render
from playtest.frontier.provider import _cost as meta_cost

# The default S1 arm since #100 (29 October): Liquid's d1-ultra matched mammoth-1 on both S1 benches and kept every
# live action fresh where Baseten's latency did not (docs/design/s1-model.md). `--s1-arm` picks any other arm.
ARM = "liquid-d1-ultra"
PRIMARY = "next_action"
FOLLOW_UP = "while_pending"
FOLLOW_UP_INSTRUCTIONS = (
    "Help has been requested and is pending. Choose exactly one id from the listed choices to say what "
    "happens to the game's input while it is pending. A wait choice sends no input; any other listed "
    "choice is executed exactly as chosen. Facts are measured; null means unknown, never zero. "
    "When an image is present, it is the current game view."
)
# The follow-up's wording when the primary text is `policy/3`: the same rules, plus that the answer can take
# tens of seconds and the input should keep playing unless that would spoil what was asked about.
FOLLOW_UP_KEEP_PLAYING = (
    "Help has been requested and is pending; its answer can take tens of seconds. Choose exactly one id from "
    "the listed choices to say what happens to the game's input while it is pending: keep playing unless "
    "continuing would spoil what you asked about. A wait choice sends no input; any other listed choice is "
    "executed exactly as chosen. Facts are measured; null means unknown, never zero. When an image is "
    "present, it is the current game view."
)


def follow_up_for(instructions: str) -> str:
    """The follow-up question's text that goes with a primary instruction text."""
    return FOLLOW_UP_KEEP_PLAYING if instructions == INSTRUCTIONS_KEEP_PLAYING else FOLLOW_UP_INSTRUCTIONS


# Asked beside `next_action` in the opt-in batched mode. The questions are answered independently, so this one is
# conditional: it is applied only when the primary answer requests help on a real-time turn.
BATCH_INSTRUCTIONS = (
    "If help is requested on this turn, choose exactly one id from the listed choices to say what "
    "happens to the game's input while it is pending. A wait choice sends no input; any other listed "
    "choice is executed exactly as chosen. Facts are measured; null means unknown, never zero. "
    "When an image is present, it is the current game view."
)


def prompts(instructions: str = INSTRUCTIONS, *, batched: bool = False) -> dict[str, str]:
    """Every prompt text this adapter sends for one primary-instruction variant and pending-question mode.

    The follow-up question keeps its own fixed text: it already asks one direct question, it is never
    duplicated in `state` (which carries the primary text), and a run should vary one prompt at a time.
    """
    texts = {PRIMARY: instructions, FOLLOW_UP: follow_up_for(instructions)}
    return {**texts, "batched_while_pending": BATCH_INSTRUCTIONS} if batched else texts


_HELP_KINDS = ("escalate", "delegate")
_PENDING_KINDS = ("wait", "action", "skill")
CHARS_PER_TOKEN = 2.5  # request characters per token, for the estimate used when a reply reports no usage

# The opt-in context modes. `full` is the payload the loop builds, sent as it always has been; `menu` sends
# the compact menu's own shorter state (`playtest.runner.menu`) instead, and nothing else about the arm changes.
FULL_CONTEXT = "full"
MENU_CONTEXT = "compact-menu"


class TokenMeter(Protocol):
    def record(self, tokens: int, *, estimated: bool = False, sent_ago: float = 0.0) -> None:
        """Count one call's tokens; `estimated` says the provider did not report them and `sent_ago` how long
        before the call the request was sent (a send-anchored budget stamps its window with it)."""
        ...


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _tokens(usage: Any) -> int | None:
    """Input plus output tokens when the provider reported input tokens; `None` when it did not."""
    if not isinstance(usage, dict):
        return None
    used = _count(usage.get("input_tokens", usage.get("prompt_tokens")))
    if used is None:
        return None
    return used + (_count(usage.get("output_tokens", usage.get("completion_tokens"))) or 0)


# State keys that do not change from turn to turn in a run, sent first so the provider's prompt cache covers them.
_STABLE_STATE = ("choices", "instructions", "objective")


def _stable_first(criteria: Mapping[str, str], state: Mapping[str, Any]) -> str:
    """The Meta arm's user text: `{"criteria": ..., "state": {...}}` with the state's turn-stable keys first.

    Every value is the canonical JSON it would be in one canonical object, and every key is present once; only the
    order of the state's top-level keys differs, so the text carries exactly what `canonical` would.
    """
    order = [k for k in _STABLE_STATE if k in state] + sorted(k for k in state if k not in _STABLE_STATE)
    fields = ",".join(f"{canonical(k)}:{canonical(state[k])}" for k in order)
    return f'{{"criteria":{canonical(criteria)},"state":{{{fields}}}}}'


def _without_image(value: Any, image: bytes | None) -> Any:
    """Do not retain an echoed data URL or encoded image in a provider response trace."""
    if image is None:
        return value
    encoded = base64.b64encode(image).decode("ascii")

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return item.replace("data:image/jpeg;base64," + encoded, "[image redacted]").replace(encoded, "[image redacted]")
        if isinstance(item, list):
            return [clean(part) for part in item]
        if isinstance(item, dict):
            return {clean(key): clean(part) for key, part in item.items()}
        return item

    return clean(value)


def _noted(text: str | None, store_problem: str | None) -> str | None:
    """A call's recorded problem, with any failure to keep its request beside it.

    The request is stored whatever happens to the answer, so a storage failure is reported the same way a
    provider problem is: in the `s1_call` entry, next to the call it belongs to.
    """
    if store_problem is None:
        return text
    return f"{text}; {store_problem}" if text else store_problem


def _compact_rules(value: str | Sequence[str] | None) -> tuple[state_codec.Rule, ...] | None:
    """`None`/`"off"`: send `state` as built. `"object"`: every rule, `state` kept an object. `"format"`: compact
    text only. `"default"`/`"all"`: those rule sets as text."""
    if value is None or value == "off":
        return None
    named = {"format": (), "default": state_codec.DEFAULT_RULES, "all": state_codec.ALL_RULES,
             "object": state_codec.ALL_RULES}
    if isinstance(value, str):
        if value not in named:
            raise ValueError(f"unknown compact-state setting {value!r}")
        return named[value]
    try:
        return tuple(state_codec.Rule(rule) for rule in value)
    except ValueError:
        raise ValueError(f"unknown compact-state rule in {list(value)!r}") from None


_PLAIN_KINDS = (ChoiceKind.action, ChoiceKind.skill)
WAIT_RESULTS = ("wait", menu.UNMEASURED)  # what a wait's `recent` row states as its result


def _recent_entry(report: Any, primitives: Any = None, wait_result: str = "wait") -> dict[str, Any] | None:
    """One finished turn as a `recent` entry: the executed choice, its measured duration and measured result.

    An option states its own `duration_s` and `terminated_by`. A plain action or skill states the verdict the
    options memory files for it, from the same rule (`playtest.runner.session.choice_outcome`) and the same
    applied hold. A wait says `wait_result` (`wait` unless configured `unmeasured`) and a finish says `finished`,
    whatever the turn reports for the rest of those kinds. A rejected input ran nothing, so it is `rejected` at the id the turn selected. Anything the
    turn did not measure is `None` (duration) or `unmeasured` (result), never zero or a success.
    """
    record, choice = getattr(report, "record", None), getattr(report, "selected", None)
    if record is None or choice is None:
        return None
    kind = getattr(choice, "kind", None)
    if kind is ChoiceKind.option:
        outcome = getattr(report, "option_outcome", None)
        body = outcome if isinstance(outcome, Mapping) else {}
        return _entry(record.executed_action or record.effective_choice,
                      body.get("duration_s"), body.get("terminated_by"))
    if kind in _PLAIN_KINDS:
        seconds, result = _plain_verdict(report, choice, primitives)
        entry = _entry(record.effective_choice, seconds, result)
        if entry is None:
            return None
        executed = next((sent for sent in getattr(report, "executed", ())
                         if getattr(getattr(sent, "choice", None), "id", None) == choice.id
                         and getattr(sent, "purpose", None) == "choice"), None)
        if executed is not None:
            observed = executed.effect.observed if executed.effect is not None else None
            fields = observed if isinstance(observed, Mapping) else {}
            for source, target in (("displacement", "displacement"), ("to_position", "end_position")):
                value = fields.get(source)
                if isinstance(value, (list, tuple)) and value:
                    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value):
                        entry[target] = list(value)
            terminal = executed.execution.terminal if executed.execution is not None else None
            if terminal is not None:
                entry["execution_status"] = terminal.status.value
            entry["goal_outcome"] = None
        return entry
    if kind is ChoiceKind.wait:
        return _entry(record.effective_choice, None, wait_result)
    return _entry(record.effective_choice, None, getattr(record.outcome, "value", record.outcome))


def _plain_verdict(report: Any, choice: Any, primitives: Any) -> tuple[float | None, str]:
    """The duration and verdict a plain action or skill measurably had, or `(None, "unmeasured")`.

    Both come from the record the runner's memory files: the measured effect's displacement against the same
    success margin, and the applied hold the bridge reported. Without the measured primitives, or without an
    execution to read, the verdict is unmeasured rather than assumed.
    """
    if primitives is None:
        return None, menu.UNMEASURED
    executed = next((sent for sent in getattr(report, "executed", ())
                     if getattr(getattr(sent, "choice", None), "id", None) == choice.id
                     and getattr(sent, "purpose", None) == "choice"), None)
    if executed is None:
        return None, menu.UNMEASURED
    from .session import choice_outcome  # the listener is only called by it, so this import is never circular

    outcome = choice_outcome(choice, executed, primitives=primitives, scene=None)
    return outcome.duration_s, outcome.terminated_by


def _entry(choice_id: Any, seconds: Any, result: Any) -> dict[str, Any] | None:
    """One `recent` row, or `None` when the turn names no choice: a quantity, never a claim."""
    if not isinstance(choice_id, str) or not choice_id:
        return None
    measured = (float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                and math.isfinite(seconds) else None)
    return {"id": choice_id, "s": measured,
            "result": result if isinstance(result, str) and result else menu.UNMEASURED}


class _Reply:
    """One call's outcome: its `s1_call` record, the choice it named, and why there is none.

    `conditional` is the batched `while_pending` answer as the ledger records it, or `None` when none was asked.
    `branches` is the compact menu's `if_blocked`/`if_succeeded` answers, or `None` when they were not asked.
    """

    __slots__ = ("call", "choice", "distribution", "failure", "halt", "conditional", "branches")

    def __init__(self, call: S1Call, choice: str | None = None, distribution: Any = None,
                 failure: str | None = None, halt: str | None = None,
                 conditional: dict[str, Any] | None = None,
                 branches: dict[str, str] | None = None) -> None:
        self.call, self.choice, self.distribution, self.failure, self.halt = call, choice, distribution, failure, halt
        self.conditional = conditional
        self.branches = branches


class SystemOneS1:
    def __init__(
        self,
        ledger: call_budget.Ledger,
        *,
        max_calls: int,
        send: replay.Send = replay.send_systemone,
        arm: str = ARM,
        clock_mode: ClockMode = ClockMode.realtime,
        episode: int = 1,
        budget: TokenMeter | None = None,
        game_view: bool = False,
        layout: RequestLayout | str = RequestLayout.full,
        instructions: str = INSTRUCTIONS,
        pending_question: str = "follow_up",
        option_order: OptionOrder | str = OptionOrder.sorted,
        order_seed: int | None = None,
        state_order: StateOrder | str = StateOrder.canonical,
        compact_state: str | Sequence[str] | None = None,
        s1_context: str = FULL_CONTEXT,
        menu_memory: Any = None,
        contingent: bool = False,
        requests_dir: Path | str | None = None,
        hold_met_directive: bool = False,
        wait_result: str = "wait",
    ) -> None:
        if wait_result not in WAIT_RESULTS:
            raise ValueError(f"wait_result must be one of {', '.join(WAIT_RESULTS)}")
        self._wait_result = wait_result
        if pending_question not in ("follow_up", "batched"):
            raise ValueError("unknown pending_question mode")
        if pending_question == "batched" and arm == "meta-muse-spark-s1":
            raise ValueError("meta-muse-spark-s1 has no questions map for batched pending questions")
        self._pending_question = pending_question
        if arm not in replay.ARMS:
            raise ValueError(f"unknown S1 arm {arm!r}")
        if s1_context not in (FULL_CONTEXT, MENU_CONTEXT):
            raise ValueError(f"unknown S1 context {s1_context!r}")
        self._menu = s1_context == MENU_CONTEXT
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("the call cap must be a positive integer")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("the instructions text is empty")
        if game_view and arm not in IMAGE_TRANSPORT:
            raise ValueError(f"S1 arm {arm!r} has no verified image transport")
        try:
            self._layout = RequestLayout(layout)
        except ValueError:
            raise ValueError(f"unknown S1 request layout {layout!r}") from None
        try:
            self._state_order = StateOrder(state_order)
        except ValueError:
            raise ValueError(f"unknown S1 state order {state_order!r}") from None
        if self._state_order is not StateOrder.canonical and arm == "meta-muse-spark-s1":
            # That arm already renders its own turn-stable-first user text (`_stable_first`); a second
            # order over the same state would be a different experiment wearing this one's name.
            raise ValueError(f"S1 arm {arm!r} sends its own stable-first text; the "
                             f"{self._state_order.value!r} state order does not apply to it")
        if self._layout is not RequestLayout.full and IMAGE_TRANSPORT.get(arm) != "images":
            # That arm wraps `state` in a message, so the body it sends would not be reconstructible
            # from its own bytes. Fail here rather than send a layout marker that does not bind.
            raise ValueError(f"S1 arm {arm!r} sends `state` inside a message; the "
                             f"{self._layout.value!r} layout needs a plain `state`")
        self._option_order = OptionOrder(option_order)
        if self._option_order is OptionOrder.shuffled and (isinstance(order_seed, bool) or not isinstance(order_seed, int)):
            raise ValueError("shuffled option order requires an integer seed")
        if self._option_order is not OptionOrder.sorted and arm == "meta-muse-spark-s1":
            raise ValueError("option ordering is unsupported for the Meta arm")
        if self._option_order is not OptionOrder.sorted and self._layout is not RequestLayout.full:
            raise ValueError("option ordering requires the full request layout")
        self._order_seed = order_seed
        self._compact = _compact_rules(compact_state)
        self._compact_object = compact_state == "object"
        if self._compact is not None:
            if IMAGE_TRANSPORT.get(arm) != "images":
                # The compact state is a string `state`; only an arm that sends the image beside it keeps one.
                raise ValueError(f"S1 arm {arm!r} has no plain `state` to send compactly")
            if self._layout is not RequestLayout.full:
                raise ValueError("the compact state already sends one copy of each duplicated text; "
                                 "use it with the full layout")
        if self._menu:
            # The compact menu replaces the state itself: nothing may be laid over it, and it needs the
            # options layer's memory to say what a choice did at this measured cell.
            if self._layout is not RequestLayout.full:
                raise ValueError("the compact menu sends its own state; it cannot be combined with a request layout")
            if self._compact is not None:
                raise ValueError("the compact menu sends its own state; it cannot be combined with the "
                                 "compact state codec")
            if self._option_order is not OptionOrder.sorted:
                raise ValueError("the compact menu sends its own state; it cannot be combined with option ordering")
            if arm == "meta-muse-spark-s1":
                raise ValueError(f"S1 arm {arm!r} sends its own user text; the compact menu does not apply to it")
            if menu_memory is None:
                raise ValueError("the compact menu needs the options layer's outcome memory (--options) for "
                                 "its measured cell")
            if game_view and IMAGE_TRANSPORT.get(arm) != "images":
                raise ValueError(f"S1 arm {arm!r} does not take a plain images array; the compact menu cannot "
                                 f"attach its game view")
        self._menu_memory = menu_memory
        # Opt in: a met setpoint is not the advice carried out (a check any movement meets closes at once), so the
        # compact menu keeps showing the newest directive that closed `met` until its own deadline. Directives
        # listed before a reset boundary belong to an earlier session and are never held.
        self._hold_met_directive = hold_met_directive
        self._stale_directives: set[str] = set()
        if contingent and not self._menu:
            # The branch answers are asked only by the compact menu's own request; in every other mode
            # there is nothing to surface, and a flag that quietly did nothing would be a false claim.
            raise ValueError("contingent answers require the compact menu (--s1-context compact-menu)")
        self._contingent = contingent
        self._menu_positions: list[list[float]] = []
        self._menu_session: str | None = None
        # The last measured choices, oldest first: what `note_turn` was told, newest last, at most six.
        self._recent: list[dict[str, Any]] = []
        self._menu_resets = {"requested": 0, "ready": 0}
        # The run's help record, kept across resets like the restart counts: the requests S1 opened, by the
        # turn each opened on, and the directives that reached it, by id. The escalate label states both.
        self._help_opened: set[int] = set()
        self._start_scene: str | None = None  # the scene the first context read; a restart returns to it
        self._help_directives: set[str] = set()
        self._game_view = game_view
        self._view_sent = self._view_omitted = self._view_stale = 0
        self._instructions = instructions
        self._prompts = (self._menu_prompts() if self._menu
                         else prompts(instructions, batched=pending_question == "batched"))
        self._ledger = ledger
        self._max_calls = max_calls
        self._requests_dir = Path(requests_dir) if requests_dir is not None else None
        self._send = send
        self._arm = arm
        self._clock_mode = clock_mode
        self._episode = episode
        self._budget = budget
        self._turn = 0
        self._reserved = 0

    @property
    def provider(self) -> str:
        return replay.provider_of(self._arm)

    @property
    def model(self) -> str:
        return replay.ARMS[self._arm][0]

    @property
    def prompt_sha256(self) -> str:
        """SHA-256 of every prompt text this adapter sends: the context instructions and the follow-up wording.

        In the compact-menu mode that is the menu's three question texts plus the batched pending question,
        which are exactly the texts a request can carry there.
        """
        return digest(canonical(self._prompts))

    @staticmethod
    def _menu_prompts() -> dict[str, str]:
        """Every question text a compact-menu request can carry, named as the request names it."""
        return {menu.PRIMARY: menu.MENU_INSTRUCTIONS, menu.BLOCKED_QUESTION: menu.IF_BLOCKED,
                menu.SUCCEEDED_QUESTION: menu.IF_SUCCEEDED, FOLLOW_UP: BATCH_INSTRUCTIONS}

    @property
    def s1_context(self) -> str:
        """The context this adapter sends: `full`, or the compact menu's shorter state."""
        return MENU_CONTEXT if self._menu else FULL_CONTEXT

    @property
    def instructions(self) -> str:
        """The primary question text this adapter sends and hashes; the context must carry this same text."""
        return self._instructions

    @property
    def layout(self) -> str:
        """The body layout this adapter sends. `full` renders the context unchanged, byte for byte."""
        return self._layout.value

    @property
    def compact_state(self) -> str | None:
        """The compact-state rules this adapter may apply, or `None` when `state` is sent as built."""
        if self._compact is None:
            return None
        version = state_codec.OBJECT_VERSION if self._compact_object else state_codec.VERSION
        return f"{version}:{','.join(rule.value for rule in self._compact) or 'format'}"

    @property
    def state_order(self) -> str:
        """How the request's state keys are ordered on the wire. `canonical` sorts them, as before."""
        return self._state_order.value

    @property
    def calls_reserved(self) -> int:
        return self._reserved

    @property
    def view_counts(self) -> dict[str, int | bool]:
        """Count calls attempted with or without a view; stale is a subset of omitted."""
        return {"enabled": self._game_view, "sent": self._view_sent,
                "omitted": self._view_omitted, "stale": self._view_stale}

    async def decide(self, context: Mapping[str, Any], *, image: bytes | None = None) -> S1Answer:
        if not self._menu and context.get("instructions") != self._instructions:
            # The hash names the text this run selected; a payload carrying other text would make it a lie.
            # The compact menu carries no `instructions` at all: its question texts are the menu's own, and
            # they are hashed by `prompt_sha256` whether or not any payload states them.
            raise ValueError("the context's instructions are not this adapter's instructions")
        if image is not None and (not self._game_view or digest(image) != context.get("game_view_sha256")
                                  or context.get("game_view_status") != "sent"):
            raise ValueError("image does not match the measured view")
        if self._game_view and ((image is None) != (context.get("game_view_status") != "sent")):
            raise ValueError("image presence does not match the measured view")
        turn, self._turn = self._turn, self._turn + 1
        choices = context["choices"]
        kinds = {item["id"]: item["kind"] for item in choices}
        menu_payload = self._menu_payload(context) if self._menu else None
        labels = ({item["id"]: item["label"] for item in menu_payload["choices"]} if menu_payload is not None
                  else {item["id"]: item["label"] for item in choices})
        criteria = {choice_id: labels[choice_id] for choice_id, kind in kinds.items()
                    if kind in _PENDING_KINDS}
        batch = ((self._menu or self._pending_question == "batched") and self._clock_mode is ClockMode.realtime
                 and any(kind in _HELP_KINDS for kind in kinds.values()) and bool(criteria))
        first = await self._call(PRIMARY, self._instructions, labels,
                                 context, turn, image, pending_criteria=criteria if batch else None,
                                 menu_payload=menu_payload)
        calls = [first.call]
        if first.choice is None:
            return S1Answer(answer=None, calls=tuple(calls), failure=first.failure, halt=first.halt)
        answer: dict[str, Any] = {"choice_id": first.choice, "set_version": context["choice_set_version"]}
        if first.distribution is not None:
            answer["distribution"] = first.distribution
        halt = first.halt
        if self._contingent and first.branches:
            # The menu's own request asked what to do if the chosen option ends blocked or succeeded, in the
            # same call. Those ids are S1's answers to questions about the same offered set; the loop's
            # parser binds them to that set and nulls any branch that names nothing offered.
            answer["contingent"] = dict(first.branches)
        if batch:
            secondary = (first.conditional or {}).get("choice")
            if kinds[first.choice] in _HELP_KINDS and secondary is not None:
                answer["while_pending"] = ({"mode": "wait"} if kinds[secondary] == "wait"
                                           else {"mode": "continue", "choice_id": secondary})
        if (not batch and not self._menu and kinds[first.choice] in _HELP_KINDS
                and self._clock_mode is ClockMode.realtime and halt is None):
            # The menu never makes a second call: its pending question is asked in the same request, so a
            # menu turn without it leaves `while_pending` absent rather than spending another call on it.
            criteria = {item["id"]: item["label"] for item in choices if item["kind"] in _PENDING_KINDS}
            if criteria:
                second = await self._call(FOLLOW_UP, follow_up_for(self._instructions), criteria, context, turn,
                                          image)
                calls.append(second.call)
                halt = second.halt
                if second.choice is not None:
                    answer["while_pending"] = ({"mode": "wait"} if kinds[second.choice] == "wait"
                                               else {"mode": "continue", "choice_id": second.choice})
        return S1Answer(answer=answer, calls=tuple(calls), halt=halt)

    def _menu_payload(self, context: Mapping[str, Any]) -> dict:
        """The compact menu's state for this context, from the loop's payload and this run's own memory.

        The offered choices are the same ids the loop offered, bound to the same observation, and the
        position history is every measured position this adapter has seen since the last reset boundary.
        The current position is added to it only after the payload is built, because `compact_menu` reads it
        as this turn's `now` and a position counted twice would read as a place the entity visited twice.
        """
        observation = context.get("observation")
        observation = observation if isinstance(observation, Mapping) else {}
        self._menu_clear_at_boundary(context, observation)
        pending = context.get("pending_help")
        if isinstance(pending, Mapping) and isinstance(pending.get("opened_turn"), int):
            self._help_opened.add(pending["opened_turn"])
        directive = context.get("directive")
        if directive is None and self._hold_met_directive:
            directive = self._held_directive(context)
        ident = (directive.get("directive_id") if isinstance(directive, Mapping)
                 else getattr(directive, "directive_id", None))
        if isinstance(ident, str) and ident:
            self._help_directives.add(ident)
        if self._start_scene is None and isinstance((context.get("observation") or {}).get("scene"), str):
            self._start_scene = context["observation"]["scene"]
        choices = ChoiceSet(set_version=context["choice_set_version"],
                            built_from_seq=context["as_of"]["seq"],
                            choices=[Choice.model_validate(item) for item in context["choices"]])
        payload = menu.compact_menu(
            objective=context["objective"], observation=observation, choices=choices,
            memory=self._menu_memory, directive=directive,
            pending_help=context.get("pending_help"), facts=context.get("facts") or (),
            game_view=context.get("game_view"), positions=self._menu_positions, recent=self._recent,
            help_record=(len(self._help_opened), len(self._help_directives)),
            running=context.get("running_option"), start_scene=self._start_scene)
        if self._menu_resets["requested"]:
            payload["run_resets"] = dict(self._menu_resets)
        position = menu.measured_position(observation)
        if position is not None:
            self._menu_positions.append(list(position))
        return payload

    def note_turn(self, report: Any, primitives: Any = None) -> None:
        """Remember one finished turn's executed choice, measured duration and measured result.

        The harness calls this after each turn with the loop's report and, when the options layer is on, the
        measured primitives its memory files outcomes by. It records only what the turn measured: an option's
        own `terminated_by` and `duration_s`, a plain action's or skill's verdict from the same rule the
        memory records, and the turn's own outcome for a wait, finish, escalate, delegate or restart. A turn
        that selected nothing records nothing. `recent` holds the newest six, oldest first.
        Restart operation counts persist separately; clearing a position epoch does not reset these facts.
        """
        selected, record = getattr(report, "selected", None), getattr(report, "record", None)
        if getattr(selected, "kind", None) is ChoiceKind.restart_play_mode:
            self._menu_resets["requested"] += 1
            operation = getattr(record, "operation", None)
            status = getattr(operation, "status", None)
            if getattr(status, "value", status) == "ready":
                self._menu_resets["ready"] += 1
        finished = getattr(report, "finished_option", None)
        if finished is not None:  # a background option collected on this turn, before this turn's own choice
            choice, body = finished
            done = _entry(getattr(choice, "option_id", None) or getattr(choice, "id", None),
                          body.get("duration_s"), body.get("terminated_by"))
            if done is not None:
                self._recent = [*self._recent, done][-menu.RECENT_LIMIT:]
        moved = getattr(report, "finished_move", None)
        if moved is not None:  # a background action or skill collected on this turn, read as its own turn would be
            choice, executed = moved
            done = _recent_entry(SimpleNamespace(
                record=SimpleNamespace(effective_choice=choice.id, executed_action=choice.id, outcome=None),
                selected=choice, executed=(executed,), option_outcome=None), primitives)
            if done is not None:
                self._recent = [*self._recent, done][-menu.RECENT_LIMIT:]
        if getattr(report, "option_running", False):
            return  # started or kept going in the background: its measured outcome is filed when collected
        entry = _recent_entry(report, primitives, self._wait_result)
        if entry is not None:
            self._recent = [*self._recent, entry][-menu.RECENT_LIMIT:]

    def _held_directive(self, context: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The newest closed directive when it closed `met` in this session and its deadline is still ahead.

        Measured only: its outcome, its anchored `deadline_game_s` against this context's game time, and whether
        it was listed before the last reset. Unknown time or deadline holds nothing; nothing is rescored.
        """
        history = context.get("directive_history") or ()
        last = history[-1] if history else None
        if not isinstance(last, Mapping) or last.get("outcome") != "met":
            return None
        if last.get("directive_id") in self._stale_directives:
            return None
        deadline, now = last.get("deadline_game_s"), (context.get("as_of") or {}).get("game_s")
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                      for v in (deadline, now))
        return last if numbers and now < deadline else None

    def _menu_clear_at_boundary(self, context: Mapping[str, Any], observation: Mapping[str, Any]) -> None:
        """Forget the position history when this turn follows a reset, so no distance spans two sessions.

        The loop rebinds its own scene-bound ports after a host restart and leaves this adapter alone, so the
        evidence it does hand over is read here: the `reset_summary` the first context after a restart carries,
        and, for a caller whose observation names one, a changed bridge `session`.
        """
        session = observation.get("session")
        if context.get("reset_summary") is not None or (isinstance(session, str) and self._menu_session is not None
                                                        and session != self._menu_session):
            self._menu_positions = []
            for item in (*(context.get("directive_history") or ()), context.get("directive")):
                if isinstance(item, Mapping) and isinstance(item.get("directive_id"), str):
                    self._stale_directives.add(item["directive_id"])
        if isinstance(session, str):
            self._menu_session = session

    async def _call(self, question: str, instructions: str, criteria: dict[str, str],
                    context: Mapping[str, Any], turn: int, image: bytes | None,
                    pending_criteria: dict[str, str] | None = None,
                    menu_payload: dict | None = None) -> _Reply:
        model = self.model
        compacted: list[str] | None = None
        if self._menu:
            # The menu's own state and its three questions; the batched pending question joins the same
            # request when this turn offers help. No layout, codec or ordering applies to it (refused in
            # `__init__`), and the image is attached by the same helper every other request uses.
            assert menu_payload is not None
            body = menu.menu_request(menu_payload, model, contingent=True, image=image, arm=self._arm)
            if pending_criteria is not None:
                body["questions"][FOLLOW_UP] = {"type": "choice", "instructions": BATCH_INSTRUCTIONS,
                                                "criteria": pending_criteria}
        else:
            body = {"model": model,
                    "questions": {question: {"type": "choice", "instructions": instructions, "criteria": criteria}},
                    "state": context}
            if pending_criteria is not None:
                body["questions"][FOLLOW_UP] = {"type": "choice", "instructions": BATCH_INSTRUCTIONS, "criteria": pending_criteria}
            if self._arm == "meta-muse-spark-s1":
                try:
                    content: Any = _stable_first(criteria, context)
                except ValueError:
                    return self._refused(question, None, "the request is not finite JSON; no call was made", halt=None)
                if image is not None:
                    content = [{"type": "text", "text": content},
                               {"type": "image_url", "image_url": {"url": image_url(image)}}]
                body = {"model": model, "reasoning_effort": "minimal", "max_tokens": 512,
                        "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": content}],
                        "response_format": {"type": "json_schema", "json_schema": {"name": "s1_choice", "strict": True,
                            "schema": {"type": "object", "additionalProperties": False, "required": ["choice"],
                                       "properties": {"choice": {"type": "string", "enum": list(criteria)}}}}}}
            if self._layout is not RequestLayout.full:
                # After the question is built: only a text this same request already states verbatim is moved
                # to one copy. The context passed in is untouched, and `full` never reaches this branch.
                body = encode_request_body(body)
            if self._compact is not None:
                # The traced context stays as built: only this request's copy of it is rendered compactly, and
                # every rule points at this body's own question, so the body alone restores it exactly.
                try:
                    if self._compact_object:
                        body["state"], compacted = state_codec.encode_object(
                            context, instructions=instructions, criteria=criteria, rules=self._compact)
                    else:
                        body["state"], compacted = state_codec.encode(
                            context, instructions=instructions, criteria=criteria, rules=self._compact,
                            key_order=state_keys(context) if self._state_order is not StateOrder.canonical else None)
                except state_codec.CodecError:
                    return self._refused(question, None, "the request is not finite JSON; no call was made", halt=None)
        try:
            estimate_text = canonical(_without_image(body, image))  # image tokens are counted separately
            if image is not None and not self._menu and self._arm != "meta-muse-spark-s1":
                if IMAGE_TRANSPORT[self._arm] == "messages":
                    url = image_url(image)
                    body["state"] = {"messages": [{"role": "user", "content": [
                        {"type": "text", "text": canonical(context)},
                        {"type": "image_url", "image_url": {"url": url}}]}]}
                else:
                    attach_images(body, self._arm, image)
            # Every question in the body gets its own order: a batched `while_pending` is ordered like the primary.
            orders = {name: ordered_ids(asked["criteria"], self._option_order, self._order_seed, self._episode,
                                        turn, name)
                      for name, asked in (body.get("questions") or {}).items()}
            presented = orders.get(question)
            # The default sends `canonical(body)`; an opt-in order renders and sends its own exact text.
            ordered = self._option_order is not OptionOrder.sorted or self._state_order is not StateOrder.canonical
            request_text = (render(body, criteria=orders if self._option_order is not OptionOrder.sorted else None,
                                   state=state_keys if self._state_order is not StateOrder.canonical else None)
                            if ordered else canonical(body))
            request_sha = digest(request_text)
        except ValueError:
            return self._refused(question, None, "the request is not finite JSON; no call was made", halt=None)
        if self._reserved >= self._max_calls:
            return self._refused(question, request_sha, "the run's call cap was reached; no call was made",
                                 halt="the run's call cap was reached")
        try:
            extra = {"question": question}
            if self._option_order is not OptionOrder.sorted:
                extra.update(option_order=self._option_order.value, order_seed=self._order_seed, presented_ids=presented)
                if len(orders) > 1:
                    extra["presented_ids_by_question"] = orders
            if compacted is not None:
                extra.update(state_codec=state_codec.OBJECT_VERSION if self._compact_object else state_codec.VERSION,
                             state_rules=compacted)
            reservation = self._ledger.reserve(
                episode=self._episode, turn=turn, based_on_seq=context["as_of"]["seq"],
                request_sha256=request_sha, requested_model=model, extra=extra)
        except call_budget.BudgetStop as exc:
            return self._refused(question, request_sha, f"{exc}; no call was made", halt=str(exc))
        self._reserved += 1
        store_problem: str | None = None
        if self._requests_dir is not None:
            # Keep the exact state this request carries in the run folder as the call is made: the trace's
            # `context` is the payload the core built, which is not what a compact-menu request sends.
            try:
                s1_requests.write_request(
                    self._requests_dir,
                    s1_requests.request_record(turn=turn, question=question, body=body, sha256=request_sha,
                                               image=image, rendered=ordered),
                    turn, question)
            except (OSError, ValueError, TypeError) as exc:  # the call still goes out; the loss is recorded
                store_problem = f"the S1 request was not stored ({type(exc).__name__}: {exc})"
        if self._game_view:
            if image is not None:
                self._view_sent += 1
            else:
                self._view_omitted += 1
                self._view_stale += int(context.get("game_view_status") == "stale")
        # For a send-anchored budget: the request's own tokens are counted at the moment it is posted. The
        # perf counter is monotonic and has the resolution to time a short call, so even a fast one is measured.
        sent_at = time.perf_counter()
        try:
            # An opt-in order sends the very text it hashed, so the ledger row cannot describe bytes other
            # than the ones on the wire. The default keeps handing the body to the sender.
            wire: dict[str, Any] | str = request_text if ordered else body
            status, data, latency_ms, problem = await asyncio.to_thread(self._send, self._arm, wire)
        except Exception as exc:  # noqa: BLE001 - keep the reservation: an unknown outcome is never retried
            note = _noted(f"adapter error: {type(exc).__name__}", store_problem)
            # the request may have reached the provider
            self._count_tokens(None, estimate_text, image is not None, sent_ago=time.perf_counter() - sent_at)
            reservation.finish(http_status=None, choice=None, distribution=None, problem=note, latency_ms=None,
                               resolved_model=None, usage=None, cost_usd=None)
            return _Reply(self._record(question, request_sha, None, None, None, note, None), failure=note)
        if self._arm == "meta-muse-spark-s1":
            choice, distribution, answer_problem = self._parse_meta(data if status == 200 else None, set(criteria))
        else:
            choice, distribution, answer_problem = replay.parse_answer(
                self._as_next_action(data if status == 200 else None, question), set(criteria))
        usage = _without_image((data or {}).get("usage"), image)
        self._count_tokens(usage, estimate_text, image is not None, sent_ago=time.perf_counter() - sent_at)
        raw_cost = usage.get("cost") if isinstance(usage, dict) else None
        cost = (float(raw_cost) if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                and math.isfinite(raw_cost) and raw_cost >= 0 else None)
        if self._arm == "meta-muse-spark-s1":
            cost = meta_cost(usage)
        note = _noted(_without_image(problem or answer_problem, image), store_problem)
        resolved = _without_image((data or {}).get("model"), image)
        batch_fields = {}
        branches = None
        if pending_criteria is not None:
            secondary, secondary_distribution, secondary_problem = replay.parse_answer(
                self._as_next_action(data if status == 200 else None, FOLLOW_UP), set(pending_criteria))
            batch_fields["conditional_answer"] = {"choice": secondary, "distribution": secondary_distribution,
                "problem": secondary_problem, "condition": ("unknown" if choice is None else "met" if
                next((x["kind"] for x in context["choices"] if x["id"] == choice), None) in _HELP_KINDS
                else "condition not met")}
        if self._contingent and menu_payload is not None:
            # Only when the flag is on: the two branch answers are read out of this same response, which the
            # `s1_call` entry already records in full. Nothing about the request changes.
            branches = {}
            for field, branch_question in (("if_blocked", menu.BLOCKED_QUESTION),
                                           ("if_succeeded", menu.SUCCEEDED_QUESTION)):
                branch, _, branch_problem = replay.parse_answer(
                    self._as_next_action(data if status == 200 else None, branch_question), set(criteria))
                if branch is not None and branch_problem is None:
                    branches[field] = branch
        row = reservation.finish(**batch_fields, http_status=status, choice=choice, distribution=distribution, problem=note,
                                 latency_ms=latency_ms, resolved_model=resolved, usage=usage, cost_usd=cost)
        # The provider's response is recorded as received: its `while_pending` answer is already in it.
        call = self._record(question, request_sha, status, latency_ms, resolved, note,
                            _without_image(data, image) if isinstance(data, dict) else None, usage)
        failure = None
        if choice is None:
            failure = (note or "no usable answer") + (f" (HTTP {status})" if status not in (None, 200) else "")
        halt = "a provider-reported cost exceeded the per-call bound" if row["cost_bound_exceeded"] else None
        return _Reply(call, choice, distribution, failure, halt, batch_fields.get("conditional_answer"), branches)

    def _count_tokens(self, usage: Any, request_text: str, has_image: bool, *, sent_ago: float = 0.0) -> None:
        """Report what a sent call used: the provider's own count, else an estimate marked as one.

        `sent_ago` is how long ago this call's request was posted; a send-anchored budget stamps its window
        with it instead of the moment the reply arrived. Both paths report the same value.
        """
        if self._budget is None:
            return
        used = _tokens(usage)
        if used is not None:
            self._budget.record(used, sent_ago=sent_ago)
        else:
            image_tokens = IMAGE_TOKENS[IMAGE_TRANSPORT[self._arm]] if has_image else 0
            self._budget.record(math.ceil(len(request_text) / CHARS_PER_TOKEN) + image_tokens, estimated=True,
                                sent_ago=sent_ago)
        reserve = getattr(self._budget, "reserve_next_image", None)
        if reserve is not None and self._game_view:
            reserve(0 if has_image else IMAGE_TOKENS[IMAGE_TRANSPORT[self._arm]])

    @staticmethod
    def _parse_meta(data: Any, offered: set[str]) -> tuple[str | None, None, str | None]:
        try:
            item = data["choices"][0]
            message = item["message"]
            if data.get("error") or item.get("finish_reason") != "stop" or message.get("refusal"):
                raise ValueError
            answer = json.loads(message["content"])
            if not isinstance(answer, dict) or set(answer) != {"choice"} or not isinstance(answer["choice"], str) or answer["choice"] not in offered:
                raise ValueError
            return answer["choice"], None, None
        except (TypeError, KeyError, IndexError, ValueError):
            return None, None, "invalid Meta answer"

    @staticmethod
    def _as_next_action(data: dict[str, Any] | None, question: str) -> dict[str, Any] | None:
        """`replay.parse_answer` reads the `next_action` answer; present another question's answer under that name."""
        if question == PRIMARY:
            return data
        answers = (data or {}).get("answers")
        return {"answers": {PRIMARY: answers.get(question) if isinstance(answers, dict) else None}}

    def _refused(self, question: str, request_sha: str | None, note: str, *, halt: str | None) -> _Reply:
        return _Reply(self._record(question, request_sha, None, None, None, note, None), failure=note, halt=halt)

    def _record(self, question: str, request_sha: str | None, status: int | None, latency_ms: float | None,
                resolved: Any, problem: str | None, response: Any, usage: Any = None) -> S1Call:
        return S1Call(question=question, provider=self.provider, model=self.model,
                      resolved_model=resolved if isinstance(resolved, str) else None, request_sha256=request_sha,
                      http_status=status, latency_ms=latency_ms, problem=problem, response=response, usage=usage)
