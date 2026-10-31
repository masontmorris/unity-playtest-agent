"""Read-only post-session review packet construction."""
from collections.abc import Mapping

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


def build_review_packet(trace_rows, manifest, discovery):
    observations = {row["turn"]: row["body"] for row in trace_rows if row["kind"] == "observation"}
    turns = {row["body"]["turn"]: row["body"] for row in trace_rows if row["kind"] == "turn"}
    effects = {row["turn"]: row["body"] for row in trace_rows if row["kind"] == "effect"}
    public = lambda turn: (observations.get(turn) or {}).get("public") or {}
    # A reference may be null in one observation and populated in another.
    reference_names = {key for obs in observations.values() for key, value in
                       (obs.get("public") or {}).items() if _reference(value)}
    actions = []
    for turn in sorted(turns):
        record = turns[turn]
        before, after = public(turn), public(turn + 1)
        if record["outcome"] != "executed" or not after:
            continue
        changed = {key: [_round(before.get(key)), _round(after.get(key))]
                   for key in sorted(set(before) | set(after))
                   if key not in {"probe", "camera"} and key not in reference_names
                   and before.get(key) != after.get(key)}
        position = (before.get("probe") or {}).get("anchor", {}).get("position")
        actions.append({"turn": turn, "choice": record["executed_action"] or record["effective_choice"],
                        "height_before": _round(position[1]) if position else None,
                        "references_set": sorted(key for key in reference_names if before.get(key) is not None),
                        "displacement": _round(((effects.get(turn) or {}).get("observed") or {}).get("net_displacement")),
                        "surroundings_before": describe_surroundings(before.get("probe")),
                        "changed": changed})
    intents = {a["name"]: a["intent"] for a in manifest["actions"]} | {
        s["name"]: s["purpose"] for s in manifest["skills"]}
    return {"intents": intents, "declared_configuration": declared_configuration(discovery, manifest["entity_id"]),
            "actions": actions}


SYSTEM = (
    "You review one finished playtest session of a game. Code measured, for every action the player sent, which "
    "public readers changed between the observation before the action and the next one, the entity's height "
    "before it (the ground is about 0.08), whether an attachment was held, the net displacement, and the measured surroundings before it (casts ahead at two heights above the feet, and the ground's height ahead against the ground under the feet). You also get "
    "each action's stated intent and the values the game's authors set (declared_configuration), read before play. "
    "Find behaviour that is wrong: an action that has none of its effect in a situation where someone playing "
    "would expect it to work; the same action or a like one producing an effect in one situation and not in "
    "another (on the ground and in the air, standing and moving, forward and backward); or a measurement outside "
    "what a declared value says should happen, with the arithmetic. Readers change for other reasons too (a "
    "previous action still settling, gravity, a timer), so compare rows that differ in one situation and cite "
    "every turn you rely on; a single row is weak evidence. Report only behaviour the rows show, not an intent you "
    "cannot check, and nothing about the measurement method. For each finding give a repro_check a replay can "
    "score: a reader that the bug makes cross a value, or null when no reader can show it. Observations are "
    "measured; null means unknown, not zero."
)
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["findings"], "properties": {"findings": {
    "type": "array", "maxItems": 5, "items": {"type": "object", "additionalProperties": False,
    "required": ["title", "expected", "expected_source", "actual", "evidence_turns", "contrast_turns", "uncertainty", "repro_check"],
    "properties": {"title": {"type": "string"}, "expected": {"type": "string"},
                   "expected_source": {"type": "string", "enum": ["declared_value", "contrast", "interpretation"]},
                   "actual": {"type": "string"}, "evidence_turns": {"type": "array", "items": {"type": "integer"}},
                   "contrast_turns": {"type": "array", "items": {"type": "integer"}}, "uncertainty": {"type": "string"},
                   "repro_check": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                        "required": ["reader", "op", "value"], "properties": {"reader": {"type": "string"},
                        "op": {"type": "string", "enum": ["<", "<=", ">", ">=", "==", "!="]}, "value": {"type": ["number", "boolean"]}}}]}}}}}}

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


def findings_from_answer(answer, trace_rows, manifest):
    observations = {r['turn']: r['trace_seq'] for r in trace_rows if r['kind'] == 'observation'}
    turns = {r['body']['turn']: r['body'] for r in trace_rows if r['kind'] == 'turn'}
    readers = {r['alias']: r['kind'] for r in manifest['readers']}
    findings, rejected = [], []
    for n, item in enumerate(answer['findings'], 1):
        try:
            seq = list(dict.fromkeys(observations[t] for turn in item['evidence_turns']
                                     for t in (turn, turn + 1) if t in observations))
            if not seq:
                raise ValueError('no observation evidence for cited turns')
            steps = [json.dumps(turns[t].get('executed_action') or turns[t].get('effective_choice'), ensure_ascii=False)
                     for t in item['evidence_turns'] if t in turns and turns[t].get('outcome') == 'executed'
                     and (turns[t].get('executed_action') or turns[t].get('effective_choice')) is not None]
            check = item['repro_check']
            uncertainty = item['uncertainty']
            if check is not None:
                kind = readers.get(check.get('reader'))
                value, op = check.get('value'), check.get('op')
                valid = ((kind == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)
                          and math.isfinite(value) and op in ('<', '<=', '>', '>=', '==', '!='))
                         or (kind == 'bool' and isinstance(value, bool) and op in ('==', '!=')))
                if valid:
                    check = {'field': 'public.' + check['reader'], 'op': op, 'value': value}
                else:
                    check = None
                    uncertainty += ' Repro check rejected: reader alias, comparator, or value type is invalid.'
            findings.append(Finding.model_validate({'finding_id': f'r-{n}', 'state': 'candidate',
                'title': item['title'], 'expected': item['expected'],
                'expected_source': {'declared_value': 'developer_assertion', 'contrast': 'interpretation',
                                    'interpretation': 'interpretation'}[item['expected_source']],
                'actual': item['actual'], 'conditions': ('Contrast turns: ' + ', '.join(map(str, item['contrast_turns']))) if item['contrast_turns'] else None,
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
    body = request_body(build_review_packet(rows, manifest, discovery))
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
    findings, rejected = findings_from_answer(answer, rows, manifest) if answer is not None else ([], [])
    result = {'request_sha256': sha, 'status': status, 'usage': usage, 'cost': cost, 'answer': answer, 'rejected': rejected}
    (args.output / 'review.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='\n')
    for finding in findings:
        (args.output / f'finding-{finding.finding_id}.json').write_text(finding.model_dump_json(indent=2) + '\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
