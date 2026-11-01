"""A fast form of the S1 context: the full payload minus the recaps, plus a statement of what is missing.

NOT WIRED IN. `playtest.runner.fast_context` is a pure function used only by its tests and by the
sizing study in `docs/design/multi-speed-s1.md`. Nothing in the turn loop calls it, and no run flag
selects it, so every existing mode keeps its exact bytes.

Why it exists. On the 31 October trials one decision cost about 9.1k billed tokens, of which the
question text, the reading, the measured facts, the choice declarations and the view are about 6.6k:
three quarters of the bill is what S1 must see to decide at all. The rest is mostly two recaps,
`recent_history` and `directive_history`, which restate in a second shape what `facts` already states
(the latest turn's outcome; each directive's goal and status). This module derives the payload
without those recaps and, because a shorter context may never silently hide an unresolved
hypothesis, a failed attempt, a contradiction or a negative result, attaches a `context_scope` entry
that names every field it left out, every count it withheld and every field it shortened.

The rules it follows:

* It never withholds `choices`, `observation`, `facts`, `objective`, `as_of`, `directive`,
  `pending_help`, `choice_set_version`, `instructions` or the view fields. S1 sees the same options,
  the same reading and the same measured evidence, and may choose anything it could choose before.
* It never withholds `guidance` or `directive_provenance` by default: the first carries refute
  conditions and negative results, the second is the only statement of how old a directive is.
* Every field left out is listed in `context_scope.withheld`; every field shortened is listed in
  `context_scope.truncated` with its counts; each produces one measured sentence in
  `context_scope.statements`. A field this version does not recognise is withheld and reported as
  unclassified, so nothing is dropped in silence.
* The input payload is deep-copied; the caller's payload is never changed.

What it does not do: it does not rank, summarise, reorder or rewrite a fact, a choice or a reading,
it does not decide anything, and it adds no recommendation. The caller still owns whether a fast
context is used at all, and when the full one is.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

FAST_CONTEXT_VERSION = "fast-context/1"

# Keys a decision needs, and that a fast context therefore always keeps.
REQUIRED = (
    "objective", "as_of", "observation", "facts", "directive", "pending_help",
    "choice_set_version", "choices", "instructions",
)
# Present only when a view was measured; kept whenever they are there.
VIEW_KEYS = ("game_view", "game_view_sha256", "game_view_status")
# A reset boundary is a statement about this observation, not a recap of an earlier one.
KEPT_IF_PRESENT = ("reset_summary",)
# The recaps this module may leave out, each with why it is a recap rather than unique evidence.
RECAP_FIELDS = {
    "recent_history": ("the newest turn is already stated in facts, and the block reports its own "
                       "total and omitted count"),
    "directive_history": ("facts state every directive's goal and status; what is lost is a closed "
                          "directive's replan conditions"),
    "guidance": ("open hypotheses, their refute conditions, and the help results adopted from them"),
    "directive_provenance": "when each directive was asked and stated, and therefore its age",
}


def derive_fast_context(
    payload: Mapping[str, Any],
    *,
    history_turns: int = 0,
    directive_history: int = 0,
    guidance_open_hypotheses: int | None = None,
    directive_provenance: bool = True,
) -> dict[str, Any]:
    """The fast form of `payload`: the same options, reading and facts, minus the recaps it names.

    `history_turns` keeps that many of the newest `recent_history` rows per block; `0` withholds the
    block. `directive_history` keeps that many of the newest closed directives. `guidance_open_hypotheses`
    keeps guidance whole when `None`, otherwise the newest n open hypotheses only. `directive_provenance`
    says whether the advice ages are kept.

    Raises `ValueError` for a negative count, for a boolean where a count belongs, and for a payload
    that does not carry the keys a decision needs.
    """
    for name, value in (("history_turns", history_turns), ("directive_history", directive_history)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if guidance_open_hypotheses is not None and (
            isinstance(guidance_open_hypotheses, bool) or not isinstance(guidance_open_hypotheses, int)
            or guidance_open_hypotheses < 0):
        raise ValueError("guidance_open_hypotheses must be a non-negative integer or None")
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"the payload does not carry {missing}")

    source = copy.deepcopy(dict(payload))
    out: dict[str, Any] = {}
    withheld: dict[str, Any] = {}
    truncated: dict[str, Any] = {}
    statements: list[str] = []

    for key in REQUIRED + VIEW_KEYS + KEPT_IF_PRESENT:
        if key in source:
            out[key] = source.pop(key)

    history = source.pop("recent_history", None)
    if history is not None:
        kept, entry = _history(history, history_turns)
        if kept is None:
            withheld["recent_history"] = entry
        else:
            out["recent_history"] = kept
            if any(omitted for omitted in entry["turns_omitted"].values()):
                truncated["recent_history"] = entry

    closed = source.pop("directive_history", None)
    if closed:
        kept, entry = _directives(closed, directive_history)
        if kept:
            out["directive_history"] = kept
            if entry["omitted"]:
                truncated["directive_history"] = entry
        else:
            withheld["directive_history"] = entry

    guidance = source.pop("guidance", None)
    if guidance is not None:
        kept, entry = _guidance(guidance, guidance_open_hypotheses)
        if kept is not None:
            out["guidance"] = kept
        if entry.get("truncated"):
            truncated["guidance"] = entry

    provenance = source.pop("directive_provenance", None)
    if provenance is not None:
        if directive_provenance:
            out["directive_provenance"] = provenance
        else:
            withheld["directive_provenance"] = {
                "shown": False, "entries": len(provenance),
                "note": "the age of any directive in this request is unknown",
                "why": RECAP_FIELDS["directive_provenance"],
            }

    for key in source:  # a field this version does not know is reported, never dropped in silence
        withheld[key] = {"shown": False, "unclassified": True,
                         "note": "a field this version does not know"}

    for key, entry in withheld.items():
        statements.append(_withheld_sentence(key, entry))
    for key, entry in truncated.items():
        statements.append(_truncated_sentence(key, entry))
    scope = {
        "version": FAST_CONTEXT_VERSION,
        "complete": not withheld and not truncated,
        "withheld": withheld,
        "truncated": truncated,
        "statements": statements,
    }
    out["context_scope"] = scope
    return out


def _history(history: Any, turns: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The newest `turns` rows of each history block, or `None` when the block is left out."""
    if not isinstance(history, Mapping):
        return None, {"shown": False, "why": RECAP_FIELDS["recent_history"]}
    blocks: dict[str, Any] = {}
    totals: dict[str, int] = {}
    for part in ("turns", "action_attempts"):
        block = history.get(part)
        if not isinstance(block, Mapping):
            continue
        items = list(block.get("items") or [])
        total = block.get("total")
        totals[part] = total if isinstance(total, int) and not isinstance(total, bool) else len(items)
        if turns:
            shown = items[-turns:]
            blocks[part] = {"limit": turns, "total": totals[part],
                            "omitted": max(0, totals[part] - len(shown)), "items": shown}
    if not turns or not blocks:
        return None, {"shown": False, "turns_total": totals,
                      "note": "the choices and outcomes of earlier turns are not shown",
                      "why": RECAP_FIELDS["recent_history"]}
    shown = {part: len(block["items"]) for part, block in blocks.items()}
    omitted = {part: max(0, totals[part] - shown[part]) for part in shown}
    entry = {"shown": True, "turns_total": totals, "turns_shown": shown,
             "turns_omitted": omitted, "why": RECAP_FIELDS["recent_history"]}
    return ({"epoch": history.get("epoch"), "boundary": history.get("boundary"), **blocks}, entry)


def _directives(closed: Any, keep: int) -> tuple[list[Any], dict[str, Any]]:
    items = list(closed)
    shown = items[-keep:] if keep else []
    return shown, {"shown": bool(shown), "kept": len(shown), "total": len(items),
                   "omitted": max(0, len(items) - len(shown)),
                   "note": "facts state each directive's goal and status; a withheld closed "
                           "directive's replan conditions are not shown",
                   "why": RECAP_FIELDS["directive_history"]}


def _guidance(guidance: Any, open_hypotheses: int | None) -> tuple[Any, dict[str, Any]]:
    """Guidance whole, or the newest n open hypotheses with every other count stated."""
    if open_hypotheses is None or not isinstance(guidance, Mapping):
        return guidance, {"shown": True, "kept": "all"}
    hypotheses = guidance.get("current_hypotheses")
    if not isinstance(hypotheses, list):
        return guidance, {"shown": True, "kept": "all"}
    open_ones = [h for h in hypotheses if not isinstance(h, Mapping) or h.get("status") != "closed"]
    closed = len(hypotheses) - len(open_ones)
    kept = open_ones[-open_hypotheses:] if open_hypotheses else []
    out = {key: value for key, value in guidance.items()
           if key not in ("current_hypotheses", "earlier_versions")}
    out["current_hypotheses"] = kept
    withheld_open = max(0, len(open_ones) - len(kept))
    return out, {
        "shown": True, "hypotheses_shown": len(kept), "hypotheses_open_total": len(open_ones),
        "hypotheses_closed_total": closed, "open_hypotheses_withheld": withheld_open,
        "closed_hypotheses_withheld": closed,
        "earlier_versions_withheld": "earlier_versions" in guidance,
        "truncated": bool(withheld_open or closed or "earlier_versions" in guidance),
        "why": RECAP_FIELDS["guidance"],
    }


def _withheld_sentence(key: str, entry: Mapping[str, Any]) -> str:
    """One measured sentence per field left out: what is missing, and what still states it."""
    if entry.get("unclassified"):
        return f"Fast context: the field {key!r} is not shown in this request."
    if key == "recent_history":
        totals = entry.get("turns_total") or {}
        parts = ", ".join(f"{count} earlier rows in {part}" for part, count in totals.items())
        parts = parts or "no rows"
        return (f"Fast context: recent history is not shown ({parts}), so the choices and outcomes "
                f"of earlier turns are not in this request; facts still state the latest turn's "
                f"outcome and each directive's status.")
    if key == "guidance":
        return (f"Fast context: {entry.get('open_hypotheses_withheld')} open and "
                f"{entry.get('closed_hypotheses_withheld')} closed hypotheses are not shown; their "
                f"refute conditions and the help results behind them are not in this request.")
    if key == "directive_provenance":
        return ("Fast context: directive provenance is not shown, so the age of any directive in "
                "this request is unknown.")
    if key == "directive_history":
        return (f"Fast context: {entry.get('total')} earlier closed directive(s) are not shown; their "
                f"replan conditions are not in this request, while facts still state each directive's "
                f"goal and status.")
    return f"Fast context: {key} is not shown in this request."


def _truncated_sentence(key: str, entry: Mapping[str, Any]) -> str:
    """One measured sentence per shortened field: how many rows are shown of how many."""
    if key == "recent_history":
        shown = entry.get("turns_shown") or {}
        kept = ", ".join(f"{part} {count}" for part, count in shown.items()) or "no rows"
        return (f"Fast context: recent history shows only the newest rows ({kept} of "
                f"{entry.get('turns_total')}); earlier turns' choices and outcomes are not shown.")
    if key == "directive_history":
        return (f"Fast context: {entry.get('kept')} of {entry.get('total')} closed directives are "
                f"shown; the replan conditions of the other {entry.get('omitted')} are not in this "
                f"request.")
    if key == "guidance":
        return (f"Fast context: guidance shows {entry.get('hypotheses_shown')} of "
                f"{entry.get('hypotheses_open_total')} open hypotheses and none of "
                f"{entry.get('hypotheses_closed_total')} closed ones; the withheld refute "
                f"conditions and help results are not in this request.")
    return f"Fast context: {key} is shortened in this request."
