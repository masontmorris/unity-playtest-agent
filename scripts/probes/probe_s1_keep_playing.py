"""Frozen FPSKit keep-playing wording comparison; no calls before the exact hash gate."""
import argparse
import asyncio
import collections
import copy
import datetime as dt
import json
import random
import statistics
import tempfile
from pathlib import Path

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.contracts.common import ClockMode
from playtest.core.context import canonical, INSTRUCTIONS_KEEP_PLAYING
from playtest.runner.s1 import SystemOneS1
from probe_s1_compact_live import Pacer, append, direct, load_contexts, load_credentials, rows, stop_reason

ROOT = Path(r'C:\Users\mason\.t3\worktrees\unity-playtest-agent\t3-d30009ac\runs\s1-trial')
ENV = Path(r'C:\Users\mason\unity-playtest-agent\.env')
ARMS = ('current', 'keep-playing')
SEED = 20261031
SESSION = 'climb-stairs-02'


def units(root):
    contexts, calls = {}, {}
    for line in (root / SESSION / 'trace.jsonl').read_text(encoding='utf-8').splitlines():
        r = json.loads(line)
        if r.get('kind') == 'context': contexts[r['turn']] = r['body']['payload']
        if r.get('kind') == 's1_call': calls[r['turn'], r['body'].get('question')] = r['body']
    found = []
    for (turn, q), call in sorted(calls.items()):
        answer = ((call.get('response') or {}).get('answers') or {}).get(q) or {}
        if q == 'while_pending': kind = 'follow-up'
        elif q == 'next_action' and answer.get('choice') == 'escalate': kind = 'escalate primary'
        elif q == 'next_action' and answer.get('choice') == 'wait' and contexts[turn].get('pending_help') is not None: kind = 'pending primary'
        else: continue
        found.append({'key': f'{SESSION}/{turn}/{q}', 'turn': turn, 'question': q, 'kind': kind, 'sha': call['request_sha256'], 'tokens': (call.get('usage') or {}).get('input_tokens')})
    if not all(any(u['kind'] == k for u in found) for k in ('follow-up', 'escalate primary', 'pending primary')): raise ValueError('Missing unit kind')
    return found


def approval(path, n):
    if path.exists(): return json.loads(path.read_text(encoding='utf-8'))
    now = dt.datetime.now(dt.timezone.utc)
    arms = {a: {'max_calls': n*5, 'per_call_max_usd': .0012, 'max_cost_usd': round(n*5*.0012, 6), 'verified_balance_usd': None, 'balance_checked_at_utc': None, 'allow_unverified_price': True, 'estimated_price_basis': 'Liquid d1-ultra $0.042 per million input tokens', 'estimate_use_by_utc': (now+dt.timedelta(hours=8)).isoformat()} for a in ARMS}
    data = {'approved_by': 'Mason Morris', 'approved_at_utc': now.isoformat(), 'approval_scope': 'Keep-playing check, approximately $0.05', 'approval_source': 'Conversation: "yeah go for it", after a ~$0.05 keep-playing check was proposed', 'max_calls': n*10, 'max_cost_usd': round(n*10*.0012, 6), 'arms': arms, 'unverified_balance_override': {'approved_by_user': True, 'reason': 'No verified balance available; stop on 402 or other specified failure.'}}
    path.write_text(json.dumps(data, indent=2), encoding='utf-8'); return data


async def capture(ctx, unit, arm):
    payload = copy.deepcopy(ctx['payload'])
    if arm == 'keep-playing': payload['instructions'] = INSTRUCTIONS_KEEP_PLAYING
    captured = []
    def send(_arm, body):
        if isinstance(body, str): body = json.loads(body)
        captured.append(copy.deepcopy(body))
        q = next(iter(body['questions'])); criteria = body['questions'][q]['criteria']
        choice = 'escalate' if q == 'next_action' and unit['question'] == 'while_pending' else next(iter(criteria))
        return 200, {'model': 'd1-ultra', 'answers': {q: {'type': 'choice', 'choice': choice}}, 'usage': {'input_tokens': 1, 'cost': 0}}, 1, None
    with tempfile.TemporaryDirectory() as tmp:
        a = approval(Path(tmp)/'approval.json', 1)
        ledger = call_budget.Ledger(Path(tmp), a, arm)
        port = SystemOneS1(ledger, max_calls=2, send=send, arm='liquid-d1-ultra', compact_state='object', game_view=True, clock_mode=ClockMode.realtime, instructions=payload['instructions'])
        await port.decide(payload, image=ctx['image'])
    target = [b for b in captured if unit['question'] in b['questions']]
    if len(target) != 1: raise ValueError(f"capture failed: {unit['key']}")
    return target[0]


async def send_body(ledger, body, episode, seq):
    q = next(iter(body['questions']))
    if q == 'next_action': return await direct(ledger, body, episode, seq)
    text = canonical(body); sha = digest(text)
    reservation = ledger.reserve(episode=episode, turn=0, based_on_seq=seq, request_sha256=sha, requested_model=body['model'], extra={'question': q})
    try:
        status, data, latency, problem = await asyncio.to_thread(replay.send_systemone, 'liquid-d1-ultra', text)
    except Exception as exc:
        status, data, latency, problem = None, None, None, f'adapter error: {type(exc).__name__}'
    answer = {'answers': {'next_action': ((data or {}).get('answers') or {}).get(q)}}
    choice, dist, error = replay.parse_answer(answer if status == 200 else None, set(body['questions'][q]['criteria']))
    usage = (data or {}).get('usage') or {}; cost = usage.get('cost')
    record = reservation.finish(http_status=status, choice=choice, distribution=dist, problem=problem or error, latency_ms=latency, resolved_model=(data or {}).get('model'), usage=usage, cost_usd=cost if isinstance(cost, (int,float)) else None)
    return {'status': status, 'choice': choice, 'distribution': dist, 'input_tokens': usage.get('input_tokens'), 'cost': cost, 'latency_ms': latency, 'request_sha256': sha, 'failure': problem or error, 'halt': 'cost bound exceeded' if record.get('cost_bound_exceeded') else None}


def summarize(directory, us):
    data = rows(directory/'results.jsonl'); kinds = {u['key']: u['kind'] for u in us}
    out = {'units': len(us), 'arms': {}}
    mean = lambda x: statistics.mean(x) if x else None
    for arm in ARMS:
        allrows = [r for r in data if r['arm'] == arm]; good = [r for r in allrows if r['status'] == 200 and r.get('choice') and not r.get('failure')]
        pending = [r for r in good if kinds[r['key']] != 'escalate primary']; esc = [r for r in good if kinds[r['key']] == 'escalate primary']
        probs = [r['distribution']['wait'] for r in pending if isinstance(r.get('distribution'), dict) and isinstance(r['distribution'].get('wait'), (int,float))]
        lat = [r['latency_ms'] for r in allrows if isinstance(r.get('latency_ms'), (int,float))]
        out['arms'][arm] = {'pending_wait_share': mean([r['choice']=='wait' for r in pending]), 'mean_wait_probability': mean(probs), 'top_non_wait_choices': collections.Counter(r['choice'] for r in pending if r['choice'] != 'wait').most_common(), 'escalate_share': mean([r['choice']=='escalate' for r in esc]), 'calls_ok': len(good), 'calls_failed': len(allrows)-len(good), 'billed_input_tokens': sum(r['input_tokens'] for r in allrows) if allrows and all(isinstance(r.get('input_tokens'), (int,float)) for r in allrows) else None, 'cost_usd': sum(r['cost'] for r in allrows) if allrows and all(isinstance(r.get('cost'), (int,float)) for r in allrows) else None, 'latency_ms_p50': statistics.median(lat) if lat else None}
    c, k = out['arms']['current'], out['arms']['keep-playing']
    if len(data) != len(us)*10 or any(out['arms'][a]['calls_failed'] for a in ARMS) or c['pending_wait_share'] is None or c['pending_wait_share'] < .5: out['reading'] = 'inconclusive'
    elif k['escalate_share'] < c['escalate_share'] - .20: out['reading'] = 'suppresses help'
    elif k['pending_wait_share'] <= c['pending_wait_share']/2: out['reading'] = 'works'
    else: out['reading'] = 'no effect'
    (directory/'summary.json').write_text(json.dumps(out, indent=2), encoding='utf-8'); print(json.dumps(out, indent=2)); return out


async def run(args):
    directory = Path(args.run_dir); directory.mkdir(parents=True, exist_ok=True); root = Path(args.night_dir)
    us = units(root); keys = sorted({f"{SESSION}/{u['turn']}" for u in us}); contexts = load_contexts(root, keys)
    bodies = {}
    for u in us:
        ctx = contexts[f"{SESSION}/{u['turn']}"]; bodies[u['key']] = {a: await capture(ctx, u, a) for a in ARMS}
        got = digest(canonical(bodies[u['key']]['current']))
        if got != u['sha']: raise SystemExit(f"Hash gate failed {u['key']}: {got} != {u['sha']}")
    print(f'Hash gate passed: {len(us)}/{len(us)} units'); print('Units:', [(u['kind'], u['turn']) for u in us])
    plan = {'units': us, 'arms': list(ARMS), 'repeats': 5, 'seed': SEED, 'reading': 'Inconclusive if any call failed or current chooses wait on less than 50% of pending calls (primary plus follow-up). Otherwise suppresses help if keep-playing escalate share on escalate units is more than 20 percentage points below current; works if keep-playing wait share on pending calls is at most half current; no effect otherwise.'}
    path = directory/'plan.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8')) != plan: raise SystemExit('Existing plan differs')
    path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    approved = approval(directory/'approval.json', len(us)); replay.validate_approval(approved, list(ARMS))
    ledgers = {a: call_budget.Ledger(directory, approved, a) for a in ARMS}; load_credentials(Path(args.env_file)); pace = Pacer(350000)
    jobs = [(u,a,i) for u in us for a in ARMS for i in range(5)]; random.Random(SEED).shuffle(jobs)
    existing = rows(directory/'results.jsonl'); done = {(r['key'],r['arm'],r['repeat']) for r in existing}; failures = 0
    for prior in reversed(existing):
        if prior['status'] == 200 and prior.get('choice') and not prior.get('failure'): break
        failures += 1
    if existing and (stop_reason(existing[-1]) or failures >= 3):
        print('STOP: previous run hit a stop condition'); summarize(directory, us); return 2
    for index,(u,a,i) in enumerate(jobs):
        if (u['key'],a,i) in done: continue
        estimate = u['tokens'] or 13000; await pace.wait(estimate)
        row = {'key': u['key'], 'arm': a, 'repeat': i, **await send_body(ledgers[a], bodies[u['key']][a], index+1, contexts[f"{SESSION}/{u['turn']}"]['payload']['as_of']['seq'])}
        pace.add(row['input_tokens'] or estimate); append(directory/'results.jsonl', row)
        failures = 0 if row['status'] == 200 and row['choice'] and not row['failure'] else failures+1
        if stop_reason(row) or failures >= 3: print('STOP:', stop_reason(row) or 'three consecutive failures'); summarize(directory, us); return 2
    summarize(directory, us); return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--run-dir', required=True); parser.add_argument('--night-dir', default=str(ROOT)); parser.add_argument('--env-file', default=str(ENV))
    raise SystemExit(asyncio.run(run(parser.parse_args())))

