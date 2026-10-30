"""Check the guidance layout (#170) against a saved live trace. Offline; not part of the product.

Every `context` row of the trace is replayed through the code a live run uses: the turn loop's own
`_guidance_view` (with `FrontierHelp.render_guidance` and `restore_guidance` behind it) and the production
`build_context`. No model call, no Editor, no key. The raw trace stays under runs/ (gitignored); report only the
numbers this prints.

    uv run python -I scripts/guidance_layout_trace.py [TRACE] [--out AUDIT.json] [--compare PROTOTYPE_AUDIT.json]

For every context that carries guidance it requires that the loop sent the layout and not the list, that the layout
restores to the traced list exactly (canonical text, types, and key order), that the traced context hash is rebuilt from
the restored list, and that `build_context` yields the traced payload with the layout in place of the list. For the
default trace it also requires the sizes measured in the design (final guidance 22,342 characters, final payload
48,258). Sizes are canonical-JSON characters, not tokens. Exits 1 on any miss.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playtest.contracts.bridge import Observation, ReaderError
from playtest.contracts.common import ClockMode
from playtest.contracts.investigation import Directive
from playtest.contracts.loop import Choice, ChoiceSet
from playtest.core.context import build_context, canonical
from playtest.core.turn_loop import TurnLoop, _Guidance
from playtest.runner.frontier_help import FrontierHelp

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRACE = ROOT / "runs" / "b04-hunt" / "session-05" / "trace.jsonl"
SESSION_05 = {"contexts": 94, "with_guidance": 77, "final_guidance_chars": 22342, "final_payload_chars": 48258}
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def size(value) -> int:
    return len(canonical(value))


def strict_equal(a, b) -> bool:
    """Type-exact structural equality, written apart from the codec's own comparison."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return repr(a) == repr(b)
    return a == b


def key_order_equal(a, b) -> bool:
    return json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def adopted(listed) -> list[_Guidance]:
    """The loop's own record of each adoption, rebuilt so that `_guidance_view(NOW)` reads back the traced item."""
    return [_Guidance(
        source=i["source"], kind=i["kind"], choice_id=i["choice_id"], requested_turn=i["requested_turn"],
        adopted_turn=i["adopted_turn"],
        finished_wall=NOW - timedelta(seconds=i["age_s"]) if i["age_s"] is not None else NOW + timedelta(seconds=1),
        waited_s=i["waited_s"], result=copy.deepcopy(i["result"])) for i in listed]


def rebuild(payload, guidance):
    """The payload through the production builder, from the pieces of the traced one and the given guidance."""
    seen, as_of = payload["observation"], payload["as_of"]
    observation = Observation(
        session="trace", seq=as_of["seq"], game_s=as_of["game_s"], wall_utc=NOW, clock_mode=ClockMode.realtime,
        scene=seen["scene"], playing=seen["playing"], paused=seen["paused"], time_scale=seen["time_scale"], frame=0,
        public=seen["public"], ui=seen["ui"], reader_errors=[ReaderError.model_validate(e) for e in seen["reader_errors"]])
    choices = ChoiceSet(set_version=payload["choice_set_version"], built_from_seq=as_of["seq"],
                        choices=[Choice.model_validate(c) for c in payload["choices"]])
    built = build_context(
        objective=payload["objective"], observation=observation, choices=choices, facts=payload["facts"],
        directive=Directive.model_validate(payload["directive"]) if payload["directive"] else None,
        directive_history=[Directive.model_validate(d) for d in payload["directive_history"]],
        pending_help=payload["pending_help"], age_s=as_of["age_s"], sample_gap_s=as_of["sample_gap_s"],
        game_view=payload["game_view"], guidance=guidance, recent_history=payload.get("recent_history"))
    for key in ("game_view_sha256", "game_view_status"):  # the loop adds these after the builder
        if key in payload:
            built[key] = payload[key]
    return built


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("trace", nargs="?", type=Path, default=DEFAULT_TRACE)
    parser.add_argument("--out", type=Path, help="write the compact per-context audit (hashes and sizes) here")
    parser.add_argument("--compare", type=Path,
                        help="a design-time audit_hashes.json: require the same layout hash in every context")
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    contexts = sorted((r for r in rows if r["kind"] == "context"), key=lambda r: r["turn"])
    helper = FrontierHelp.__new__(FrontierHelp)  # the two layout methods read no state
    loop = TurnLoop(None, choices=[Choice(id="w", kind="wait", label="wait", source="core")], help=None)
    misses: list[str] = []
    count = {"contexts": len(contexts), "with_guidance": 0, "traced_hash_reproduced": 0, "list_reproduced": 0,
             "layout_sent": 0, "restored_canonical": 0, "restored_type_exact": 0, "restored_key_order": 0,
             "context_hash_rebuilt": 0, "builder_matches": 0, "layout_hash_matches_design": 0}
    sizes = {name: {"guidance": [], "payload": []} for name in ("list", "layout")}
    audit = []
    compared = ({a["turn"]: a for a in json.loads(args.compare.read_text(encoding="utf-8"))}
                if args.compare is not None else None)
    view_ms = 0.0
    for row in contexts:
        payload, recorded = row["body"]["payload"], row["body"]["sha256"]
        count["traced_hash_reproduced"] += sha(canonical(payload)) == recorded
        listed = payload.get("guidance")
        loop._guidance = adopted(listed or [])
        loop._help = None
        count["list_reproduced"] += canonical(loop._guidance_view(NOW)) == canonical(listed or [])
        loop._help = helper
        started = time.perf_counter()
        view = loop._guidance_view(NOW)  # the loop's choice: the layout, or the list if it could not prove the layout exact
        view_ms = (time.perf_counter() - started) * 1000
        built = rebuild(payload, view if listed else ())
        sent = {**payload, "guidance": view} if listed else payload
        count["builder_matches"] += canonical(built) == canonical(sent)
        sizes["list"]["guidance"].append(size(listed) if listed else 0)
        sizes["list"]["payload"].append(size(payload))
        sizes["layout"]["guidance"].append(size(view) if listed else 0)
        sizes["layout"]["payload"].append(size(sent))
        if not listed:
            continue
        count["with_guidance"] += 1
        laid = isinstance(view, dict)
        count["layout_sent"] += laid
        restored = helper.restore_guidance(view) if laid else None
        count["restored_canonical"] += laid and canonical(restored) == canonical(listed)
        count["restored_type_exact"] += laid and strict_equal(restored, listed)
        count["restored_key_order"] += laid and key_order_equal(restored, listed)
        rest = {k: v for k, v in payload.items() if k != "guidance"}
        count["context_hash_rebuilt"] += laid and sha(canonical({**rest, "guidance": restored})) == recorded
        entry = {"turn": row["turn"], "traced_payload_sha256": recorded, "guidance_sha256": sha(canonical(listed)),
                 "guidance_chars": size(listed), "layout_sha256": sha(canonical(view)), "layout_chars": size(view),
                 "restored_guidance_sha256": sha(canonical(restored)) if laid else None}
        audit.append(entry)
        if compared is not None:
            count["layout_hash_matches_design"] += compared[row["turn"]]["p2_ref_sha256"] == entry["layout_sha256"]
    if args.out is not None:
        args.out.write_text(json.dumps(audit, indent=0), encoding="utf-8")

    final_view = loop._guidance_view(NOW)
    blocks = {name: size(final_view[name]) for name in final_view} if isinstance(final_view, dict) else None
    calls = [r for r in rows if r["kind"] == "s1_call"]
    summed = {name: sum(sizes[name]["payload"][r["turn"]] for r in calls) for name in sizes}
    has = [g > 0 for g in sizes["list"]["guidance"]]
    for name in sizes:
        for field in ("guidance", "payload"):
            xs = sizes[name][field]
            print(f"{name:6s} {field:8s} median {statistics.median(xs):>8.0f}  max {max(xs):>6d}  final {xs[-1]:>6d}"
                  f"  | contexts with guidance: median {statistics.median([x for x, h in zip(xs, has) if h]):>8.0f}")
    larger = [b - a for a, b in zip(sizes["list"]["payload"], sizes["layout"]["payload"])]
    final = {"guidance": sizes["layout"]["guidance"][-1], "payload": sizes["layout"]["payload"][-1]}
    original = {"guidance": sizes["list"]["guidance"][-1], "payload": sizes["list"]["payload"][-1]}
    print(f"final guidance {original['guidance']} -> {final['guidance']} "
          f"({100 * (1 - final['guidance'] / original['guidance']):.1f}% smaller); "
          f"final payload {original['payload']} -> {final['payload']} "
          f"({100 * (1 - final['payload'] / original['payload']):.1f}% smaller)")
    print(f"final layout blocks: {blocks}")
    print(f"contexts larger than the list: {sum(1 for d in larger if d > 0)} (largest increase {max(0, max(larger))} chars); "
          f"S1 calls {len(calls)}: state chars {summed['list']} -> {summed['layout']} "
          f"({100 * (1 - summed['layout'] / summed['list']):.1f}% smaller)")
    print(f"_guidance_view on the final context: {view_ms:.2f} ms (render, restore, two canonical compares)")
    print("checks:", json.dumps(count))

    wanted = count["with_guidance"]
    for name in ("list_reproduced", "builder_matches", "traced_hash_reproduced"):
        if count[name] != count["contexts"]:
            misses.append(f"{name}: {count[name]}/{count['contexts']}")
    for name in ("layout_sent", "restored_canonical", "restored_type_exact", "restored_key_order", "context_hash_rebuilt"):
        if count[name] != wanted:
            misses.append(f"{name}: {count[name]}/{wanted}")
    if compared is not None and count["layout_hash_matches_design"] != wanted:
        misses.append(f"layout_hash_matches_design: {count['layout_hash_matches_design']}/{wanted}")
    if args.trace.resolve() == DEFAULT_TRACE.resolve():
        for name, got in (("contexts", count["contexts"]), ("with_guidance", wanted),
                          ("final_guidance_chars", final["guidance"]), ("final_payload_chars", final["payload"])):
            if got != SESSION_05[name]:
                misses.append(f"{name}: {got}, the design measured {SESSION_05[name]}")
    print("PASS" if not misses else "FAIL: " + "; ".join(misses))
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
