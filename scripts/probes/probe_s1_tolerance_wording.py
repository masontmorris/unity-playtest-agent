"""Frozen-frame objective tolerance probe: does a wider alignment tolerance stop d1-ultra hunting?

Units are frozen `next_action` turns from the 31 October FPSKit staircase trials, each with its exact JPEG:
- spawn units from the two trials with the "centre" objective, where S1 was turning left and right in place.
  `far`: yaw 30–50°, where continuing the hunt is `mouse_turn_left`. `near`: yaw within 10° of 0°, where
  `mouse_turn_right` turns toward the staircase.
- `foot` units from all four trials: at the foot of the long flight, still at the approach heading (yaw
  30–50°). There, `mouse_turn_left` lines up with the flight's axis (0°), and walking on crosses it.

Arms change only the objective's two alignment phrases. Band arms widen both. `half_approach` widens only
the approach and keeps "rise straight ahead of you". Foot units are asked under `centre`, `half` and
`half_approach` only. Each unit's recorded arm must rebuild its recorded request exactly (hash gate).

Dry and summary phases are offline. Nothing here selects or alters a choice.
"""
import argparse
import asyncio
import collections
import copy
import json
import random
import statistics
from pathlib import Path

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.core.context import canonical
from probe_s1_compact_live import Pacer, append, direct, load_contexts, load_credentials, rows, stop_reason
from probe_s1_finish_wording import body_for as original_body, ROOT, ENV

SPAWN_RUNS = ("climb-continuous-01", "climb-held-pattern-01")
FOOT_RUNS = (*SPAWN_RUNS, "climb-tolerance-half-01", "climb-tolerance-send-01")
BANDS = {"third": "the middle third of the view", "half": "the middle half of the view", "two_thirds": "the middle two thirds of the view", "view": "the view"}
ARMS = ("centre", *BANDS, "half_approach")
FOOT_ARMS = ("centre", "half", "half_approach")
PHRASES = ("is in the centre of the view", "rise straight ahead of you")
TARGETS = {"far": ("p_back", "mouse_turn_left"), "near": ("p_toward", "mouse_turn_right"), "foot": ("p_align", "mouse_turn_left")}
REPEATS = 2
SEED = 20261031
READING = ("Spawn, per arm: mean far p(mouse_turn_left) and p(move_forward*), mean near p(mouse_turn_right) and "
           "p(move_forward*). An arm settles if far left <= half the centre arm's and far forward >= centre's + .15. "
           "It over-widens if near forward > centre's + .15 or near right < half centre's. Band reading: the "
           "narrowest settling band arm that does not over-widen (third, half, two_thirds, view), else 'all settling "
           "arms over-widen' or 'no wording effect'. Foot, per foot arm: mean p(mouse_turn_left) (line-up), "
           "p(move_forward*) and p(running_jump). Final reading: half_approach if it settles without over-widening "
           "and its foot p(mouse_turn_left) exceeds half's by >= .10; otherwise the band reading. Incomplete results "
           "are inconclusive. Tied modes are unknown.")


def _widened(band):
    return f"is ahead of you, anywhere in {band}", f"rise ahead of you, anywhere in {band}"


def recorded_arm(text):
    """The arm whose wording a recorded objective carries: `half` for the half-band trials, else `centre`."""
    return "half" if all(text.count(p) == 1 for p in _widened(BANDS["half"])) else "centre"


def objective_for(text, arm):
    """`text` with only its alignment phrases set for `arm`. A recorded half-band objective is mapped back first."""
    if arm not in ARMS:
        raise ValueError(arm)
    half = _widened(BANDS["half"])
    if recorded_arm(text) == "half":
        text = text.replace(half[0], PHRASES[0]).replace(half[1], PHRASES[1])
    for phrase in PHRASES:
        if text.count(phrase) != 1:
            raise ValueError(f"expected exactly one occurrence of {phrase!r}")
    if arm in BANDS:
        wide = _widened(BANDS[arm])
        text = text.replace(PHRASES[0], wide[0]).replace(PHRASES[1], wide[1])
    elif arm == "half_approach":
        text = text.replace(PHRASES[0], half[0])
    return text


def body_for(ctx, plan, arm):
    ctx = copy.deepcopy(ctx)
    ctx['payload']['objective'] = objective_for(ctx['payload']['objective'], arm)
    return original_body(ctx, plan, 'current')


def classify(public):
    """`far`/`near` at the spawn, `foot` at the flight's foot at the approach heading, else None."""
    try:
        x, y, z = public['probe']['anchor']['position']
        yaw = public['yaw']
        if abs(x - 3.58) <= .5 and abs(z + 16.25) <= .5:
            if 30 <= yaw <= 50: return 'far'
            if 0 <= yaw <= 10 or 350 <= yaw <= 360: return 'near'
            return None
        if 5.5 <= x <= 9.5 and -14.5 <= z <= -8.5 and y < .6 and 30 <= yaw <= 50:
            return 'foot'
    except (KeyError, TypeError, ValueError):
        return None
    return None


def arms_for(unit):
    return FOOT_ARMS if unit['class'] == 'foot' else ARMS


def select(root):
    units = []
    for run in FOOT_RUNS:
        events = collections.defaultdict(lambda: collections.defaultdict(list))
        for r in rows(root / run / 'trace.jsonl'):
            if isinstance(r.get('turn'), int): events[r['turn']][r['kind']].append(r['body'])
        candidates = {'far': [], 'near': [], 'foot': []}
        for turn, ev in sorted(events.items()):
            calls = ev['s1_call']
            if len(calls) != 1 or calls[0].get('question') != 'next_action' or len(ev['turn']) != 1 or len(ev['context']) != 1: continue
            result = ev['turn'][0]; payload = ev['context'][0]['payload']
            if payload.get('pending_help') is not None: continue
            public = payload['observation']['public']; cls = classify(public); choice = result.get('effective_choice')
            if cls in ('far', 'near') and (run not in SPAWN_RUNS or result.get('outcome') != 'executed'
                                           or choice not in ('mouse_turn_left', 'mouse_turn_right')):
                continue
            if cls:
                candidates[cls].append({'key': f'{run}/{turn}', 'run': run, 'turn': turn, 'yaw': public['yaw'], 'class': cls,
                                        'choice': choice, 'recorded_arm': recorded_arm(payload['objective'])})
        for cls, group in candidates.items():
            if cls == 'foot':
                picked = group  # every foot decision point
            else:
                n = min(4, len(group))
                picked = [group[round(i * (len(group)-1)/(n-1))] for i in range(n)] if n > 1 else group[:n]
            if group: print(f'{run} {cls}: eligible {len(group)}, selected {len(picked)}')
            units.extend(picked)
    return units


def _mean(values):
    return statistics.mean(values) if values else None


def summarize(directory, units):
    data = rows(directory / 'results.jsonl') if (directory / 'results.jsonl').exists() else []
    by = collections.defaultdict(list)
    for r in data:
        if r.get('status') == 200 and r.get('choice') and not r.get('failure') and isinstance(r.get('distribution'), dict):
            by[r['key'], r['arm']].append(r)
    out = {'arms': {}}
    for arm in ARMS:
        metrics = {}
        for cls, (name, target) in TARGETS.items():
            selected = [u for u in units if u['class'] == cls and arm in arms_for(u)]
            if not selected: continue
            probs, forwards, jumps, modes = [], [], [], []
            for u in selected:
                rr = by[u['key'], arm]
                for r in rr:
                    d = r['distribution']
                    if isinstance(d.get(target), (int, float)): probs.append(d[target])
                    fs = [v for k, v in d.items() if k.startswith('move_forward') and isinstance(v, (int, float))]
                    if fs: forwards.append(sum(fs))
                    if isinstance(d.get('running_jump'), (int, float)): jumps.append(d['running_jump'])
                top = collections.Counter(r['choice'] for r in rr).most_common()
                modes.append(top[0][0] if top and (len(top) == 1 or top[0][1] > top[1][1]) else None)
            m = {name: _mean(probs), 'p_forward': _mean(forwards), 'known_modes': sum(x is not None for x in modes)}
            if cls == 'far': m['modal_count'] = sum(x == 'mouse_turn_left' for x in modes)
            elif cls == 'near': m['modal_count'] = sum(x is not None and x.startswith('move_forward') for x in modes)
            else: m.update(p_jump=_mean(jumps), modal_left=sum(x == 'mouse_turn_left' for x in modes))
            metrics[cls] = m
        out['arms'][arm] = metrics

    def whole(u, a):
        rr, target = by[u['key'], a], TARGETS[u['class']][1]
        return (len(rr) == REPEATS and {r.get('repeat') for r in rr} == set(range(REPEATS))
                and all(isinstance(r['distribution'].get(target), (int, float))
                        and any(k.startswith('move_forward') and isinstance(v, (int, float)) for k, v in r['distribution'].items())
                        for r in rr))
    classes = {u['class'] for u in units}
    if not {'far', 'near'} <= classes or not all(whole(u, a) for u in units for a in arms_for(u)):
        out['reading'] = 'inconclusive: incomplete data'
    else:
        cf, cn = out['arms']['centre']['far'], out['arms']['centre']['near']

        def judge(arm):
            a = out['arms'][arm]; f, n = a['far'], a['near']
            settles = f['p_back'] <= .5*cf['p_back'] and f['p_forward'] >= cf['p_forward']+.15
            over = n['p_forward'] > cn['p_forward']+.15 or n['p_toward'] < .5*cn['p_toward']
            a.update(settles=settles, over_widens=over)
            return settles, over
        recommendation, any_settles = None, False
        for arm in BANDS:
            settles, over = judge(arm); any_settles |= settles
            if settles and not over and recommendation is None: recommendation = arm
        out['band_reading'] = recommendation or ('all settling arms over-widen' if any_settles else 'no wording effect')
        settles, over = judge('half_approach')
        gain = None
        if 'foot' in classes:
            gain = out['arms']['half_approach']['foot']['p_align'] - out['arms']['half']['foot']['p_align']
        out['line_up_gain'] = gain
        out['reading'] = 'half_approach' if settles and not over and gain is not None and gain >= .10 else out['band_reading']
    (directory/'summary.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(json.dumps(out, indent=2)); return out


async def run(args):
    directory = Path(args.run_dir); directory.mkdir(parents=True, exist_ok=True)
    if args.phase == 'summary':
        return summarize(directory, json.loads((directory/'plan.json').read_text(encoding='utf-8'))['units'])
    root = Path(args.night_dir); units = select(root); keys = [u['key'] for u in units]
    contexts = load_contexts(root, keys)
    plans = {s: json.loads((root/s/'plan.json').read_text(encoding='utf-8')) for s in FOOT_RUNS}
    bodies = {}
    for u in units:
        k = u['key']; bodies[k] = {a: body_for(contexts[k], plans[u['run']], a) for a in arms_for(u)}
        if digest(canonical(bodies[k][u['recorded_arm']])) != contexts[k]['night_sha']: raise SystemExit(f'Hash gate failed: {k}')
        print(f"{u['run']} {u['turn']:3} {u['yaw']:8.3f} {u['class']:4} {u['choice']} (recorded as {u['recorded_arm']})")
    print(f'Hash gate passed: {len(keys)}/{len(keys)} recorded bodies')
    jobs = [(u['key'], a, i) for u in units for a in arms_for(u) for i in range(REPEATS)]
    # Arms differ from the recorded request by a few words, so each call is estimated at its unit's recorded bill.
    tokens = sum(contexts[k]['night_tokens'] or 13000 for k, _, _ in jobs)
    per_arm = dict(collections.Counter(a for _, a, _ in jobs))
    print(f'{len(jobs)} calls {per_arm}; estimated input tokens: {tokens}; estimated cost: ${tokens * .042 / 1_000_000:.4f}')
    plan = {'units': units, 'arms': list(ARMS), 'foot_arms': list(FOOT_ARMS), 'repeats': REPEATS, 'seed': SEED, 'reading': READING,
            'calls': len(jobs), 'calls_per_arm': per_arm, 'estimated_input_tokens': tokens, 'estimated_cost_usd': tokens * .042 / 1_000_000}
    path = directory/'plan.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8')) != plan: raise SystemExit('Existing plan differs')
    path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    if args.phase == 'dry': return
    if not args.approval: raise SystemExit('--approval is required')
    approved = json.loads(Path(args.approval).read_text(encoding='utf-8')); replay.validate_approval(approved, list(ARMS))
    ledgers = {a: call_budget.Ledger(directory, approved, a) for a in ARMS}
    load_credentials(Path(args.env_file)); pace = Pacer(350000)
    random.Random(SEED).shuffle(jobs)
    existing = rows(directory/'results.jsonl') if (directory/'results.jsonl').exists() else []
    done = {(r['key'], r['arm'], r['repeat']) for r in existing}; failures = 0
    for index, (k, a, i) in enumerate(jobs):
        if (k, a, i) in done: continue
        estimate = contexts[k]['night_tokens'] or 13000
        await pace.wait(estimate)
        row = {'key': k, 'arm': a, 'repeat': i, **await direct(ledgers[a], bodies[k][a], index+1, contexts[k]['payload']['as_of']['seq'])}
        pace.add(row['input_tokens'] or estimate); append(directory/'results.jsonl', row)
        failures = 0 if row['status'] == 200 and row['choice'] and not row['failure'] else failures+1
        if stop_reason(row) or failures >= 3:
            print('STOP:', stop_reason(row) or 'three consecutive failures'); break
    summarize(directory, units)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--phase', choices=('dry', 'run', 'summary'), required=True); parser.add_argument('--run-dir', required=True); parser.add_argument('--night-dir', default=str(ROOT)); parser.add_argument('--env-file', default=str(ENV)); parser.add_argument('--approval')
    asyncio.run(run(parser.parse_args()))
