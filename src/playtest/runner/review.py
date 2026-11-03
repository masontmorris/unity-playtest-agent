"""Read-only post-session review packet construction."""
import math
from collections.abc import Mapping
from datetime import datetime

from playtest.runner.declared import declared_configuration
from playtest.runner.probe import describe_surroundings


def _round(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


def _reference(value):
    return isinstance(value, Mapping) and ("id" in value or "kind" in value)


def _epoch(value):
    """An ISO-8601 UTC time or epoch seconds as epoch seconds, or None when it is neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _behind(probe):
    """The ground's height behind the feet, by distance, as `describe_surroundings` states the ground ahead."""
    rows = {}
    for sample in (probe or {}).get("backward") or ():
        if isinstance(sample, Mapping) and isinstance(sample.get("forward"), (int, float)):
            delta = sample.get("delta_y") if sample.get("hit") is True else None
            rows.setdefault(abs(sample["forward"]), []).append(delta if isinstance(delta, (int, float)) else None)
    parts = []
    for distance in sorted(rows):
        known = [value for value in rows[distance] if value is not None]
        if not known:
            parts.append(f"{distance:g} behind no ground found")
        elif min(known) == max(known):
            parts.append(f"{distance:g} behind {known[0]:+.2f}")
        else:
            parts.append(f"{distance:g} behind {min(known):+.2f} to {max(known):+.2f}")
    return ("ground height against the ground under the feet: " + ", ".join(parts)) if parts else None


def _inputs(trace_rows):
    """Every input the game received, one per plain execution or option, with its measured window; and the count
    of inputs that never ran (rejected, failed), by status. A window is (start, end) on the bridge's wall clock."""
    decided = {row["trace_seq"]: row["turn"] for row in trace_rows if row["kind"] == "decision"}
    effects = {}
    for row in trace_rows:
        if row["kind"] == "effect":
            for ref in row.get("refs") or ():
                effects[ref] = row["body"]
    inputs, not_run = [], {}
    for row in trace_rows:
        body = row.get("body") or {}
        if row["kind"] == "execution" and isinstance(row.get("turn"), int):
            records = body.get("records") or []
            terminal = records[-1] if records else {}
            status = terminal.get("status")
            start = _epoch(terminal.get("input_start_wall_s"))
            if status not in ("completed", "cancelled") or start is None:
                reason = status if status != "rejected" else f"rejected {terminal.get('reject_reason')}"
                not_run[str(reason or "unrecorded")] = not_run.get(str(reason or "unrecorded"), 0) + 1
                continue
            held = terminal.get("applied_duration_s")
            end = _epoch(terminal.get("input_end_wall_s")) or (start + held if isinstance(held, (int, float)) else None)
            effect = effects.get(row["trace_seq"]) or {}
            request = body.get("request") or {}
            inputs.append({"turn": row["turn"], "trace_seq": row["trace_seq"],
                           "choice": request.get("choice_id") or request.get("action"), "action": request.get("action"),
                           "kind": "input", "stopped_early": status == "cancelled", "held_s": _round(held),
                           "displacement": _round((effect.get("observed") or {}).get("net_displacement")),
                           "start": start, "end": end})
        elif row["kind"] == "option":
            outcome = body.get("outcome") or {}
            receipts = [r for r in body.get("receipts") or () if isinstance(r, Mapping) and r.get("type") == "execution"]
            started = [_epoch(r.get("input_start_wall_s")) for r in receipts if r.get("status") != "rejected"]
            started = [value for value in started if value is not None]
            if not started:
                not_run["option sent no input"] = not_run.get("option sent no input", 0) + 1
                continue
            ended = [_epoch(r.get("input_start_wall_s")) + r["applied_duration_s"] for r in receipts
                     if isinstance(r.get("applied_duration_s"), (int, float)) and _epoch(r.get("input_start_wall_s"))]
            turn = next((decided[ref] for ref in row.get("refs") or () if ref in decided), row["turn"])
            inputs.append({"turn": turn, "trace_seq": row["trace_seq"], "choice": body.get("option_id"),
                           "action": None, "kind": "option", "stopped_early": outcome.get("terminated_by") == "interrupted",
                           "held_s": _round(outcome.get("duration_s")), "displacement": _round(outcome.get("displacement")),
                           "option_ended": outcome.get("terminated_by"),
                           "start": min(started), "end": max(ended) if ended else _epoch(row.get("wall_utc"))})
    inputs.sort(key=lambda item: item["start"])
    return inputs, not_run


CAMERA = ("position", "rotation", "field_of_view")  # what a view-changing input (look, crouch, aim) moves


def _flat(values):
    """The readers as they are, with the camera split into the parts a view-changing input moves and the
    controlled entity reduced to its position."""
    out = {key: value for key, value in values.items() if key not in ("camera", "controlled")}
    controlled = values.get("controlled")
    if isinstance(controlled, Mapping) and "position" in controlled:
        out["controlled.position"] = controlled["position"]  # the entity's identity fields never change in a row
    camera = values.get("camera")
    if isinstance(camera, Mapping):
        for part in CAMERA:
            if part in camera:
                out[f"camera.{part}"] = camera[part]
    return out


def build_review_packet(trace_rows, manifest, discovery, guards=None):
    """One row per input the game received, measured over its own window.

    A row compares the last observation before the input started with the first one after it ended, so an input
    that ran in the background, or a reading that lagged, is measured over the input's own time. Inputs that never
    reached the game (rejected, failed) are left out and counted in `inputs_not_run`. Each row's evidence is the
    trace_seq of those two observations, kept in `windows` for `findings_from_answer`.
    """
    observations = sorted(((_epoch((row.get("body") or {}).get("wall_utc")), row) for row in trace_rows
                           if row["kind"] == "observation"), key=lambda pair: (pair[0] is None, pair[0] or 0))
    timed = [(at, row) for at, row in observations if at is not None]
    public = lambda row: ((row or {}).get("body") or {}).get("public") or {}
    # A reference may be null in one observation and populated in another.
    reference_names = {key for _, row in observations for key, value in public(row).items() if _reference(value)}
    inputs, not_run = _inputs(trace_rows)
    actions, windows = [], {}
    for item in inputs:
        before = next((row for at, row in reversed(timed) if at <= item["start"]), None)
        after = next((row for at, row in timed if item["end"] is not None and at >= item["end"]), None)
        if before is None or after is None:
            not_run["no observation after it"] = not_run.get("no observation after it", 0) + 1
            continue
        first, last = _flat(public(before)), _flat(public(after))
        changed = {key: [_round(first.get(key)), _round(last.get(key))]
                   for key in sorted(set(first) | set(last))
                   if key != "probe" and key not in reference_names and first.get(key) != last.get(key)}
        # What an input changes only while it is held (a stance, a zoom) is gone by the observation after it ends:
        # the observations taken inside its window are read against the one before it too.
        during = {}
        for at, row in timed:
            if item["start"] < at < (item["end"] or item["start"]):
                for key, value in _flat(public(row)).items():
                    if key != "probe" and key not in reference_names and value != first.get(key):
                        during.setdefault(key, [_round(first.get(key))]).append(_round(value))
        first = public(before)
        position = (first.get("probe") or {}).get("anchor", {}).get("position")
        row_id = len(actions)
        # The readers the row measured changing, after it or while it was held: a contrast for another row's check.
        windows[row_id] = (before["trace_seq"], after["trace_seq"], item, frozenset(changed) | frozenset(during))
        entry = {"row": row_id, "turn": item["turn"], "choice": item["choice"], "kind": item["kind"],
                 "held_s": item["held_s"], "stopped_early": item["stopped_early"],
                 "measured_over_s": _round(_epoch(after["body"].get("wall_utc")) - _epoch(before["body"].get("wall_utc"))),
                 "height_before": _round(position[1]) if position else None,
                 "references_set": sorted(key for key in reference_names if first.get(key) is not None),
                 "displacement": item["displacement"],
                 "surroundings_before": describe_surroundings(first.get("probe")),
                 "behind_before": _behind(first.get("probe")),
                 "changed": changed}
        if during:
            entry["changed_while_held"] = during
        if item["kind"] == "input":
            entry["action"] = item["action"]
        if item.get("option_ended") is not None:
            entry["option_ended"] = item["option_ended"]
        actions.append(entry)
    intents = {a["name"]: a["intent"] for a in manifest["actions"]} | {
        s["name"]: s["purpose"] for s in manifest["skills"]}
    start = next((public(row).get("probe", {}).get("anchor", {}).get("position") for _, row in observations
                  if public(row).get("probe")), None)
    packet = {"intents": intents, "declared_configuration": declared_configuration(discovery, manifest["entity_id"]),
              "start_height": _round(start[1]) if start else None, "inputs_not_run": not_run, "actions": actions}
    if guards:
        # What each verb did at rest, while a direction was held and in the air, measured before play.
        from playtest.runner.guards import summary_lines
        packet["measured_guards"] = summary_lines(guards)
    return packet, windows


SYSTEM = (
    "You review one finished playtest session of a game. Each row is one input the game received: a plain input "
    "(its action, how long it was held, whether it was stopped early) or a multi-input option (how it ended). Code "
    "measured which public readers and camera parts (position, rotation, field of view) changed between the last "
    "observation before the input started and the first one after it ended (measured_over_s is that span), and, in "
    "changed_while_held, what differed at observations taken while it was still held; the "
    "entity's height before it (start_height is where it stood "
    "when the session began), which held references were set, the net displacement, and the measured surroundings "
    "before it: casts ahead at two heights above the feet, the ground's height ahead and behind against the ground "
    "under the feet. Inputs the game never received (rejected, failed) are not rows; inputs_not_run counts them, so "
    "a missing effect is never a rejected input. You also get each action's stated intent and the values the game's "
    "authors set (declared_configuration), read before play. "
    "Find behaviour that is wrong: an input that has none of its effect in a situation where someone playing would "
    "expect it to work; the same input or a like one producing an effect in one situation and not in another (on "
    "the ground and in the air, standing and moving, forward and backward); or a measurement outside what a "
    "declared value says should happen, with the arithmetic. Before calling a motion missing, read the surroundings: "
    "a surface close ahead or behind, or ground that rises or ends, stops a body. Readers change for other reasons "
    "too (an input stopped early, gravity, a timer), so compare rows that differ in one situation and cite every row "
    "you rely on; a single row is weak evidence. Report only behaviour the rows show, not an intent you cannot "
    "check, and nothing about the measurement method. For each finding give a repro_check a replay can score, or "
    "null when no reader can show it: a reader the bug makes cross a value; or, for a plain input that has none of "
    "its effect, after_action set to that row's action with the reader it should change and op unchanged, so a "
    "replay checks the readings while and right after each execution of it. Give after_action only when a contrast "
    "row, cited in contrast_rows, shows that same reader changing after this or a like input in another situation: "
    "without one a replay cannot tell a missing effect from a rule of the game that withholds it here. Use op "
    "unchanged only for a reader with a few distinct values (a flag, a count); for one that moves continuously (a "
    "blend, a timer, a position), give the bound the input's effect crosses in the contrast row instead, such as < "
    "half of the value it reached there, since a small drift is not the effect. "
    "measured_guards, when present, was measured before play: each verb pressed at rest, while a direction was "
    "held, and in the air, each against that state without the verb, and what it changed there. A verb with no "
    "effect in a state where it never had one is how this build measurably behaves, which by itself is neither a "
    "rule nor a bug: a claim about it needs another oracle (a declared value, an intent the effect contradicts, or "
    "a like input that works there). A row that differs from measured_guards in the same state is a contrast. "
    "Observations are measured; null means unknown, not zero."
)
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["findings"], "properties": {"findings": {
    "type": "array", "maxItems": 5, "items": {"type": "object", "additionalProperties": False,
    "required": ["title", "expected", "expected_source", "actual", "evidence_rows", "contrast_rows", "uncertainty", "repro_check"],
    "properties": {"title": {"type": "string"}, "expected": {"type": "string"},
                   "expected_source": {"type": "string", "enum": ["declared_value", "contrast", "interpretation"]},
                   "actual": {"type": "string"}, "evidence_rows": {"type": "array", "items": {"type": "integer"}},
                   "contrast_rows": {"type": "array", "items": {"type": "integer"}}, "uncertainty": {"type": "string"},
                   "repro_check": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                        "required": ["reader", "op", "value", "after_action"], "properties": {"reader": {"type": "string"},
                        "op": {"type": "string", "enum": ["<", "<=", ">", ">=", "==", "!=", "unchanged"]},
                        "value": {"type": ["number", "boolean", "null"]},
                        "after_action": {"type": ["string", "null"]}}}]}}}}}}

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

from playtest.bench import call_budget, replay
from playtest.contracts.investigation import Finding
from playtest.frontier import provider


def request_body(packet):
    return {"model": "muse-spark-1.3", "reasoning_effort": "low", "max_tokens": 8000,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(packet, separators=(",", ":"))}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "review", "strict": True, "schema": SCHEMA}}}


def findings_from_answer(answer, windows, manifest):
    """Candidate findings from the review's answer; each cites the two observations around every row it names."""
    readers = {r['alias']: r['kind'] for r in manifest['readers']}
    ran = {item['action'] for _, _, item, _ in windows.values() if item['kind'] == 'input' and item['action']}
    findings, rejected = [], []
    for n, item in enumerate(answer['findings'], 1):
        try:
            cited = [windows[row] for row in item['evidence_rows'] if row in windows]
            seq = list(dict.fromkeys(entry for before, after, _, _ in cited for entry in (before, after)))
            if not seq:
                raise ValueError('no observation evidence for cited rows')
            steps = [json.dumps(input_['choice'], ensure_ascii=False) for _, _, input_, _ in cited
                     if input_['choice'] is not None]
            check = item['repro_check']
            uncertainty = item['uncertainty']
            if check is not None:
                kind = readers.get(check.get('reader'))
                value, op, after = check.get('value'), check.get('op'), check.get('after_action')
                if op == 'unchanged':
                    # Unchanged is only scoreable against the reading right before an input that ran.
                    valid = kind is not None and value is None and after in ran
                else:
                    valid = (((kind == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) and op in ('<', '<=', '>', '>=', '==', '!='))
                              or (kind == 'bool' and isinstance(value, bool) and op in ('==', '!=')))
                             and (after is None or after in ran))
                contrasted = after is None or any(check['reader'] in windows[row][3]
                                                  for row in item['contrast_rows'] if row in windows)
                if valid and not contrasted:
                    # Without a row where the reader did change, a replay cannot tell an input that lost its effect
                    # from a rule of the game that withholds it in this situation.
                    check = None
                    uncertainty += (' Repro check rejected: no contrast row shows that reader changing, so a replay '
                                    'could not tell a missing effect from a rule of the game.')
                elif valid:
                    check = {'field': 'public.' + check['reader'], 'op': op, 'value': value}
                    if after is not None:
                        check['after_action'] = after
                else:
                    check = None
                    uncertainty += (' Repro check rejected: reader alias, comparator, value type or after_action '
                                    'is invalid.')
            findings.append(Finding.model_validate({'finding_id': f'r-{n}', 'state': 'candidate',
                'title': item['title'], 'expected': item['expected'],
                'expected_source': {'declared_value': 'developer_assertion', 'contrast': 'interpretation',
                                    'interpretation': 'interpretation'}[item['expected_source']],
                'actual': item['actual'],
                'conditions': ('Contrast rows: ' + ', '.join(map(str, item['contrast_rows']))) if item['contrast_rows'] else None,
                'steps': steps, 'evidence_seq': seq, 'uncertainty': uncertainty,
                'repro_check': check, 'severity': 'unknown'}))
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({'item': item, 'reason': str(exc)})
    return findings, rejected


def main():
    parser = argparse.ArgumentParser()
    for arg in ('session', 'manifest', 'discovery', 'approval', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    approval, _ = replay._load(args.approval)
    replay.validate_approval(approval, ['muse-spark-1.3'])
    if not os.environ.get('META_API_KEY'):
        parser.error('META_API_KEY is required')
    manifest, _ = replay._load(args.manifest)
    discovery, _ = replay._load(args.discovery)
    rows = [json.loads(line) for line in (args.session / 'trace.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    guards_path = args.session / 'guards.json'
    guards = json.loads(guards_path.read_text(encoding='utf-8')) if guards_path.is_file() else None
    packet, windows = build_review_packet(rows, manifest, discovery, guards)
    body = request_body(packet)
    sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = call_budget.Ledger(args.output, approval, 'muse-spark-1.3')
    reservation = ledger.reserve(episode=0, turn=0, based_on_seq=0, request_sha256=sha, requested_model='muse-spark-1.3')
    try:
        status, data, latency, problem = provider.send_meta(body)
        usage = (data or {}).get('usage') or None
        cost = provider._cost(usage)
        reservation.finish(http_status=status, latency_ms=latency, cost_usd=cost, usage=usage, problem=problem)
    except Exception:
        reservation.finish(cost_usd=None, problem='the call failed before a response was recorded')
        raise
    # The paid call is recorded before its answer is read, so a parse or conversion fault cannot lose its cost.
    answer = json.loads(data['choices'][0]['message']['content']) if status == 200 and data else None
    findings, rejected = findings_from_answer(answer, windows, manifest) if answer is not None else ([], [])
    result = {'request_sha256': sha, 'status': status, 'usage': usage, 'cost': cost, 'answer': answer, 'rejected': rejected}
    (args.output / 'review.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='\n')
    for finding in findings:
        (args.output / f'finding-{finding.finding_id}.json').write_text(finding.model_dump_json(indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
