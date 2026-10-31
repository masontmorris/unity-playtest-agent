"""Paid A/B probe: does conditional finish wording stop d1-ultra's premature `finish` choices?

Frozen units come from the 31 October FPSKit trials under `runs/s1-trial/`, each with its exact JPEG:
- 11 finish-relevant `next_action` turns, where `finish` won or nearly won;
- 5 normal turns.

Two arms:
- `current` rebuilds the request each trial sent. The gate requires its hash to equal the recorded one.
- `conditional` changes only the instructions (`policy/2`) and the finish choice's label
  (`FINISH_LABELS["conditional"]`), encoded exactly as that trial encoded its request.

5 repeats each, seeded order, ledgered, paced. The reading is written to `plan.json` before any call.
Only the wording varies; nothing here selects or alters a choice.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import copy
import datetime as dt
import json
import random
import statistics
from pathlib import Path

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.core.context import canonical, INSTRUCTIONS_FINISH_ONLY_WHEN_MET
from playtest.runner import request_layout, state_codec
from playtest.runner.choices import FINISH_LABELS
from probe_s1_compact_live import Pacer, append, direct, load_contexts, load_credentials, rows, stop_reason

ROOT = Path(r'C:\Users\mason\.t3\worktrees\unity-playtest-agent\t3-d30009ac\runs\s1-trial')
ENV = Path(r'C:\Users\mason\unity-playtest-agent\.env')
RELEVANT = ['compact-object-01/21', 'compact-object-01/22', 'compact-object-02/22', 'compact-object-02/23', 'climb-stairs-01/12', 'climb-stairs-01/15', 'climb-stairs-01/16', 'climb-stairs-01/17', 'climb-stairs-control-01/17', 'climb-stairs-control-01/18', 'climb-stairs-control-01/19']
NORMAL = ['compact-object-01/5', 'compact-object-01/12', 'climb-stairs-01/6', 'climb-stairs-control-01/11', 'climb-stairs-control-01/14']
ARMS = ('current', 'conditional')
SEED = 20261031


def body_for(ctx, plan, arm):
    p = copy.deepcopy(ctx['payload'])
    if arm == 'conditional':
        p['instructions'] = INSTRUCTIONS_FINISH_ONLY_WHEN_MET
        matches = [c for c in p['choices'] if c['id'] == 'finish']
        if len(matches) != 1:
            raise ValueError('expected exactly one finish choice')
        matches[0]['label'] = FINISH_LABELS['conditional']
    criteria = {c['id']: c['label'] for c in p['choices']}
    state = p
    compact = plan.get('s1_compact_state')
    if compact == 'object':
        state = state_codec.encode_object(p, instructions=p['instructions'], criteria=criteria)[0]
    elif compact:
        raise ValueError(f'unsupported compact state: {compact}')
    body = {'model': 'd1-ultra', 'questions': {'next_action': {'type': 'choice', 'instructions': p['instructions'], 'criteria': criteria}}, 'state': state}
    layout = plan.get('s1_layout', 'full')
    if layout == 'dup-state-ref/1':
        body = request_layout.encode(body)
    elif layout not in ('full', None):
        raise ValueError(f'unsupported layout: {layout}')
    body['images'] = ['data:image/jpeg;base64,' + base64.b64encode(ctx['image']).decode('ascii')]
    return body


def approval(path, n):
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    now = dt.datetime.now(dt.timezone.utc)
    arms = {a: {'max_calls': n * 5, 'per_call_max_usd': .0012, 'max_cost_usd': round(n * 5 * .0012, 6), 'verified_balance_usd': None, 'balance_checked_at_utc': None, 'allow_unverified_price': True, 'estimated_price_basis': 'Liquid d1-ultra $0.042 per million input tokens', 'estimate_use_by_utc': (now + dt.timedelta(hours=8)).isoformat()} for a in ARMS}
    data = {'approved_by': 'Mason Morris', 'approved_at_utc': now.isoformat(), 'approval_scope': 'Finish wording A/B test, approximately $0.10', 'approval_source': 'Conversation: "hell yeah do it all", after a ~$0.10 finish-wording test was proposed', 'max_calls': n * 10, 'max_cost_usd': round(n * 10 * .0012, 6), 'arms': arms, 'unverified_balance_override': {'approved_by_user': True, 'reason': 'No verified balance available; stop on 402 or other specified failure.'}}
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')
    return data


def summarize(directory, keys):
    data = rows(directory / 'results.jsonl')
    by = collections.defaultdict(list)
    for r in data:
        if r['status'] == 200 and r['choice'] and not r.get('failure'):
            by[r['key'], r['arm']].append(r)
    def mode(rs):
        counts = collections.Counter(r['choice'] for r in rs)
        if not counts: return None
        top = counts.most_common()
        return top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else None
    def mean(xs): return statistics.mean(xs) if xs else None
    self_agree = []
    for k in NORMAL:
        rs = by[k, 'current']
        for i, r in enumerate(rs):
            m = mode(rs[:i] + rs[i+1:]) if len(rs) >= 3 else None
            if m is not None: self_agree.append(r['choice'] == m)
    out = {'units': len(keys), 'normal_current_leave_one_out_self_agreement': mean(self_agree), 'arms': {}}
    for arm in ARMS:
        allrows = [r for r in data if r['arm'] == arm]
        good = [r for k in keys for r in by[k, arm]]
        rel = [r for r in good if r['key'] in RELEVANT]
        normal = [r for r in good if r['key'] in NORMAL]
        modes = [mode(by[k, arm]) if len(by[k, arm]) >= 3 else None for k in RELEVANT if k in keys]
        agrees = [r['choice'] == m for r in normal if (m := mode(by[r['key'], 'current'])) is not None and len(by[r['key'], 'current']) >= 3]
        def finish_prob(rs): return mean([r['distribution']['finish'] for r in rs if isinstance(r.get('distribution'), dict) and isinstance(r['distribution'].get('finish'), (int, float))])
        lat = [r['latency_ms'] for r in allrows if isinstance(r.get('latency_ms'), (int, float))]
        # A unit "finishes" when most of its successful repeats choose finish: robust to 2-2-1 ties.
        majority = sum(1 for k in RELEVANT if k in keys and len(by[k, arm]) >= 3
                       and sum(r['choice'] == 'finish' for r in by[k, arm]) * 2 > len(by[k, arm]))
        out['arms'][arm] = {'finish_relevant': {'finish_choice_share': mean([r['choice'] == 'finish' for r in rel]), 'finish_majority_units': majority, 'finish_modal_units': modes.count('finish'), 'known_modal_units': sum(m is not None for m in modes), 'mean_finish_probability': finish_prob(rel)}, 'normal': {'agreement_with_current_modal': mean(agrees), 'mean_finish_probability': finish_prob(normal)}, 'calls_ok': len(good), 'calls_failed': len(allrows)-len(good), 'billed_input_tokens': sum(r['input_tokens'] for r in allrows if isinstance(r.get('input_tokens'), (int,float))), 'cost_usd': sum(r['cost'] for r in allrows if isinstance(r.get('cost'), (int,float))), 'latency_ms_p50': statistics.median(lat) if lat else None}
    a = out['arms']['conditional']; c = out['arms']['current']
    baseline = out['normal_current_leave_one_out_self_agreement']; agreement = a['normal']['agreement_with_current_modal']
    if len(data) != len(keys)*10 or any(out['arms'][arm]['calls_failed'] for arm in ARMS) or len(keys) != 16 or baseline is None or agreement is None:
        out['reading'] = 'inconclusive: incomplete data'
    elif c['finish_relevant']['finish_majority_units'] < 3:
        out['reading'] = 'inconclusive: the current wording reproduces too few finishes on replay'
    elif abs(agreement - baseline) > .10: out['reading'] = 'disturbs other choices'
    elif a['finish_relevant']['finish_majority_units'] <= 2: out['reading'] = 'works'
    else: out['reading'] = 'no effect'
    (directory / 'summary.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(json.dumps(out, indent=2))
    return out


async def run(args):
    directory = Path(args.run_dir); directory.mkdir(parents=True, exist_ok=True)
    keys = []; contexts = {}; missing = []
    for key in RELEVANT + NORMAL:
        try: contexts.update(load_contexts(Path(args.night_dir), [key])); keys.append(key)
        except (KeyError, FileNotFoundError, StopIteration) as e: missing.append(f'{key}: {type(e).__name__} {e}')
    if missing:
        print('Missing units:', *missing, sep='\n'); raise SystemExit('Cannot run the predeclared comparison with missing units')
    plans = {s: json.loads((Path(args.night_dir)/s/'plan.json').read_text(encoding='utf-8')) for s in {k.split('/')[0] for k in keys}}
    bodies = {}; errors = []
    for k in keys:
        try:
            bodies[k] = {a: body_for(contexts[k], plans[k.split('/')[0]], a) for a in ARMS}
            got = digest(canonical(bodies[k]['current']))
            if got != contexts[k]['night_sha']: errors.append(f'{k}: hash mismatch reconstructed={got} recorded={contexts[k]["night_sha"]}')
        except (KeyError, ValueError) as e: errors.append(f'{k}: {e}')
    if errors: raise SystemExit('Hash gate failed:\n' + '\n'.join(errors))
    print(f'Hash gate passed: {len(keys)}/{len(keys)} units')
    plan = {'units': keys, 'finish_relevant': RELEVANT, 'normal': NORMAL, 'arms': list(ARMS), 'repeats': 5, 'seed': SEED, 'reading': 'A finish-relevant unit finishes when most of its successful repeats choose finish. Inconclusive if any call failed, or if the current wording finishes on fewer than 3 of the 11 units. Otherwise: disturbs other choices if normal-unit per-call agreement with the current modal differs from current leave-one-out self-agreement by more than 10 points; works if the conditional wording finishes on at most 2 of 11 units; no effect otherwise.', 'attrition': 'Failed calls excluded; ties and fewer than three successful repeats have unknown modal choice.'}
    path = directory/'plan.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8')) != plan: raise SystemExit('Existing plan differs')
    path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    approved = approval(directory/'approval.json', len(keys)); replay.validate_approval(approved, list(ARMS))
    ledgers = {a: call_budget.Ledger(directory, approved, a) for a in ARMS}
    load_credentials(Path(args.env_file))
    jobs = [(k,a,i) for k in keys for a in ARMS for i in range(5)]; random.Random(SEED).shuffle(jobs)
    existing = rows(directory/'results.jsonl'); done = {(r['key'],r['arm'],r['repeat']) for r in existing}
    failures = 0
    for index,(k,a,i) in enumerate(jobs):
        if (k,a,i) in done: continue
        estimate = contexts[k]['night_tokens'] or 13000
        await PACE.wait(estimate)
        row = {'key': k, 'arm': a, 'repeat': i, **await direct(ledgers[a], bodies[k][a], index+1, contexts[k]['payload']['as_of']['seq'])}
        PACE.add(row['input_tokens'] or estimate); append(directory/'results.jsonl', row)
        failures = 0 if row['status'] == 200 and row['choice'] and not row['failure'] else failures+1
        if stop_reason(row) or failures >= 3:
            print('STOP:', stop_reason(row) or 'three consecutive failures'); summarize(directory, keys); return 2
    summarize(directory, keys); return 0


PACE = Pacer(350000)
if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--run-dir', required=True); parser.add_argument('--night-dir', default=str(ROOT)); parser.add_argument('--env-file', default=str(ENV))
    raise SystemExit(asyncio.run(run(parser.parse_args())))
