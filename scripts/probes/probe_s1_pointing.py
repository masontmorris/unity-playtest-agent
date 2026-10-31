"""Paid probe: can d1-ultra point at a target in the game view?

If S1 could say *where* in the image a target is (one of nine vertical strips), a measured, game-agnostic skill could
turn exactly that far, from the camera's field of view and the calibrated turn rates. A memoryless chooser would
then stop hunting with fixed-size corrections (docs/design/oscillation.md). This measures that assumption on frozen
frames before anything is built.

Frames are recorded `next_action` turns from the 31 October staircase trials, each with its exact JPEG. They come
from the spawn and from the open ground between the spawn and the foot (z <= -11.5, 0 <= x <= 10, at least 2.5 units
from the foot), on the ground. Repeated views are dropped: same run, same whole-degree yaw and same half-unit
position.

Ground truth: the long flight's bottom step is at x 8.5, z -10.6, about 41 degrees from the spawn. That is where the
player's feet measurably rose onto the flight across the trials: it rises along +z, its feet x spans 7.6 to 9.5,
and y first leaves the ground near z -10.7. Its strip in each frame follows from the camera's recorded position,
the player's yaw and the camera's vertical field of view, on the 640x480 frame. Frames whose foot lies within 3
degrees of the view's edge are left out as ambiguous.

Two conditions, one call each per frame:
- `in_play`: the exact recorded request, hash-gated, with a second question, `foot_strip`, beside `next_action`,
  as S1 would be asked in play.
- `minimal`: only the image and the `foot_strip` question.

The reading is predeclared in plan.json before any call. Nothing here selects or alters a choice.
"""
import argparse
import asyncio
import collections
import copy
import datetime as dt
import json
import math
import random
import statistics
from pathlib import Path

from playtest.bench import call_budget, replay
from playtest.bench.record import digest
from playtest.core.context import canonical
from probe_s1_compact_live import ARM, Pacer, append, load_contexts, load_credentials, rows, stop_reason
from probe_s1_finish_wording import body_for as recorded_body, ROOT, ENV

RUNS = ("climb-continuous-01", "climb-held-pattern-01", "climb-tolerance-half-01", "climb-tolerance-send-01",
        "climb-turn-sizes-01")
SPAWN = (3.58, -16.25)
FOOT = (8.5, -10.6)
STRIPS = 9
FRAME_W, FRAME_H = 640, 480
EDGE_MARGIN_DEG = 3.0
PER_BUCKET = 5
CONDITIONS = ("in_play", "minimal")
SEED = 20261031
QUESTION = "foot_strip"
INSTRUCTIONS = ("The attached image is the current game view. Divide it into nine equal vertical strips, numbered 1 at "
                "the left edge to 9 at the right edge. Which strip contains the bottom step of the long staircase, the "
                "straight flight of many shallow grey steps? Choose not_visible if its bottom step is not in the image.")
CRITERIA = {f"strip_{i}": f"strip {i} of 9" + {1: " (left edge)", 5: " (centre)", 9: " (right edge)"}.get(i, "")
            for i in range(1, STRIPS + 1)}
CRITERIA["not_visible"] = "the bottom step of the long staircase is not in the image"
MINIMAL_STATE = {"task": "Locate the bottom step of the long staircase in the attached game view."}
READING = ("Per condition, over frames whose foot is in view: within1 = share of answers within one strip of the truth "
           "(not_visible counts as wrong); exact = share exactly right; bias = mean signed strip error over strip "
           "answers. 'points well' if within1 >= 0.8 and |bias| <= 0.5; 'partly' if within1 >= 0.5; else 'cannot "
           "point'. Frames whose foot is out of view are scored separately: share answered not_visible. A failed or "
           "missing answer makes the condition inconclusive.")


def truth(public):
    """The foot's bearing relative to the view and its strip, or None for an unusable frame."""
    camera = public.get("camera") or {}
    position, fov, yaw = camera.get("position"), camera.get("field_of_view"), public.get("yaw")
    if not position or not isinstance(fov, (int, float)) or not isinstance(yaw, (int, float)):
        return None
    bearing = math.degrees(math.atan2(FOOT[0] - position[0], FOOT[1] - position[2]))
    rel = (bearing - yaw + 180) % 360 - 180
    half = math.degrees(math.atan(math.tan(math.radians(fov) / 2) * FRAME_W / FRAME_H))
    if abs(abs(rel) - half) < EDGE_MARGIN_DEG:
        return None
    if abs(rel) > half:
        return {"rel_deg": round(rel, 2), "strip": None}
    u = 0.5 + math.tan(math.radians(rel)) / (2 * math.tan(math.radians(half)))
    return {"rel_deg": round(rel, 2), "strip": min(STRIPS, int(u * STRIPS) + 1), "u": round(u, 4)}


def select(root):
    buckets = collections.defaultdict(list)
    seen = set()
    for run in RUNS:
        events = collections.defaultdict(lambda: collections.defaultdict(list))
        for r in rows(root / run / 'trace.jsonl'):
            if isinstance(r.get('turn'), int):
                events[r['turn']][r['kind']].append(r['body'])
        for turn, ev in sorted(events.items()):
            calls = ev['s1_call']
            if len(calls) != 1 or calls[0].get('question') != 'next_action' or len(ev['context']) != 1:
                continue
            payload = ev['context'][0]['payload']
            if payload.get('pending_help') is not None:
                continue
            public = payload['observation']['public']
            x, y, z = public['probe']['anchor']['position']
            at_spawn = abs(x - SPAWN[0]) <= .3 and abs(z - SPAWN[1]) <= .3
            approach = z <= -11.5 and 0 <= x <= 10 and math.dist((x, z), FOOT) >= 2.5
            if y > .3 or not (at_spawn or approach):
                continue
            view = (run, round(public['yaw']), round(x * 2) / 2, round(z * 2) / 2)
            if view in seen:
                continue
            seen.add(view)
            t = truth(public)
            if t is not None:
                buckets[t['strip']].append({'key': f'{run}/{turn}', 'run': run, 'turn': turn,
                                            'yaw': round(public['yaw'], 2), 'x': round(x, 2), 'z': round(z, 2), **t})
    units = []
    for strip in sorted(buckets, key=lambda s: (s is None, s or 0)):
        group = buckets[strip]
        by_run = collections.defaultdict(list)
        for u in group:
            by_run[u['run']].append(u)
        picked = []
        while len(picked) < PER_BUCKET and any(by_run.values()):  # round-robin over runs, earliest turns first
            for run in RUNS:
                if by_run.get(run) and len(picked) < PER_BUCKET:
                    picked.append(by_run[run].pop(0))
        print(f'strip {strip}: eligible {len(group)}, selected {len(picked)}')
        units.extend(picked)
    return units


def question():
    return {'type': 'choice', 'instructions': INSTRUCTIONS, 'criteria': dict(CRITERIA)}


def bodies_for(ctx, plan):
    play = recorded_body(ctx, plan, 'current')
    gate = digest(canonical(play)) == ctx['night_sha']
    play = copy.deepcopy(play)
    play['questions'][QUESTION] = question()
    minimal = {'model': 'd1-ultra', 'questions': {QUESTION: question()}, 'state': dict(MINIMAL_STATE),
               'images': list(play['images'])}
    return gate, {'in_play': play, 'minimal': minimal}


async def ask(ledger, body, episode, seq):
    text = canonical(body)
    sha = digest(text)
    reservation = ledger.reserve(episode=episode, turn=0, based_on_seq=seq, request_sha256=sha,
                                 requested_model=body['model'], extra={'question': QUESTION})
    status, data, latency_ms, problem = await asyncio.to_thread(replay.send_systemone, ARM, text)
    answer = ((data.get('answers') or {}).get(QUESTION) if status == 200 and isinstance(data, dict) else None)
    choice = answer.get('choice') if isinstance(answer, dict) else None
    probs = answer.get('probabilities') if isinstance(answer, dict) else None
    failure = problem or (None if choice in CRITERIA else 'no usable foot_strip answer')
    usage = data.get('usage') if isinstance(data, dict) else None
    cost = usage.get('cost') if isinstance(usage, dict) else None
    row = reservation.finish(http_status=status, choice=choice if choice in CRITERIA else None,
                             distribution=probs if isinstance(probs, dict) else None, problem=failure,
                             latency_ms=latency_ms, resolved_model=data.get('model') if isinstance(data, dict) else None,
                             usage=usage, cost_usd=cost if isinstance(cost, (int, float)) else None)
    return {'status': status, 'choice': choice, 'distribution': probs, 'input_tokens': (usage or {}).get('input_tokens'),
            'cost': cost, 'latency_ms': latency_ms, 'request_sha256': sha, 'failure': failure,
            'halt': 'a provider-reported cost exceeded the per-call bound' if row.get('cost_bound_exceeded') else None}


def strip_of(choice):
    return int(choice.split('_')[1]) if isinstance(choice, str) and choice.startswith('strip_') else None


def summarize(directory, units):
    data = rows(directory / 'results.jsonl') if (directory / 'results.jsonl').exists() else []
    by = {(r['key'], r['condition']): r for r in data}
    out = {'conditions': {}}
    for condition in CONDITIONS:
        visible = [u for u in units if u['strip'] is not None]
        hidden = [u for u in units if u['strip'] is None]
        answered = [(u, by.get((u['key'], condition))) for u in units]
        complete = all(r is not None and r.get('status') == 200 and not r.get('failure') for _, r in answered)
        errors, exact, within1 = [], 0, 0
        confusion = collections.Counter()
        for u in visible:
            r = by.get((u['key'], condition))
            picked = strip_of(r.get('choice')) if r else None
            confusion[(u['strip'], r.get('choice') if r else None)] += 1
            if picked is not None:
                errors.append(picked - u['strip'])
                exact += picked == u['strip']
                within1 += abs(picked - u['strip']) <= 1
        hidden_right = sum(1 for u in hidden if (by.get((u['key'], condition)) or {}).get('choice') == 'not_visible')
        m = {'visible_frames': len(visible), 'exact': exact / len(visible) if visible else None,
             'within1': within1 / len(visible) if visible else None,
             'bias': statistics.mean(errors) if errors else None,
             'abs_error_mean': statistics.mean(abs(e) for e in errors) if errors else None,
             'not_visible_answers_on_visible': sum(1 for u in visible if (by.get((u['key'], condition)) or {}).get('choice') == 'not_visible'),
             'hidden_frames': len(hidden), 'hidden_answered_not_visible': hidden_right,
             'by_true_strip': {str(s): [c for (t, c), n in sorted(confusion.items(), key=lambda kv: str(kv[0])) if t == s for _ in range(n)]
                               for s in sorted({u['strip'] for u in visible})}}
        if not complete:
            m['reading'] = 'inconclusive: incomplete data'
        elif m['within1'] >= .8 and abs(m['bias']) <= .5:
            m['reading'] = 'points well'
        elif m['within1'] >= .5:
            m['reading'] = 'partly'
        else:
            m['reading'] = 'cannot point'
        out['conditions'][condition] = m
    (directory / 'summary.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(json.dumps(out, indent=2))
    return out


async def run(args):
    directory = Path(args.run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if args.phase == 'summary':
        return summarize(directory, json.loads((directory / 'plan.json').read_text(encoding='utf-8'))['units'])
    root = Path(args.night_dir)
    units = select(root)
    keys = [u['key'] for u in units]
    contexts = load_contexts(root, keys)
    plans = {s: json.loads((root / s / 'plan.json').read_text(encoding='utf-8')) for s in RUNS}
    bodies = {}
    for u in units:
        gate, bodies[u['key']] = bodies_for(contexts[u['key']], plans[u['run']])
        if not gate:
            raise SystemExit(f"Hash gate failed: {u['key']}")
        print(f"{u['key']:32s} x {u['x']:6.2f} z {u['z']:7.2f} yaw {u['yaw']:7.2f} rel {u['rel_deg']:7.2f} strip {u['strip']}")
    print(f'Hash gate passed: {len(keys)}/{len(keys)} recorded bodies')
    jobs = [(k, c) for k in keys for c in CONDITIONS]
    estimate = {k: contexts[k]['night_tokens'] or 13000 for k in keys}
    tokens = sum(estimate[k] + 150 if c == 'in_play' else 450 for k, c in jobs)  # recorded bill + the question; image only
    print(f'{len(jobs)} calls; estimated input tokens {tokens}; estimated cost ${tokens * .042 / 1e6:.4f}')
    plan = {'units': units, 'conditions': list(CONDITIONS), 'foot': list(FOOT), 'strips': STRIPS, 'seed': SEED,
            'question': question(), 'minimal_state': MINIMAL_STATE, 'reading': READING, 'calls': len(jobs),
            'estimated_input_tokens': tokens, 'estimated_cost_usd': tokens * .042 / 1e6}
    path = directory / 'plan.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8')) != plan:
        raise SystemExit('Existing plan differs')
    path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
    if args.phase == 'dry':
        return
    if not args.approval:
        raise SystemExit('--approval is required')
    approved = json.loads(Path(args.approval).read_text(encoding='utf-8'))
    replay.validate_approval(approved, list(CONDITIONS))
    ledgers = {c: call_budget.Ledger(directory, approved, c) for c in CONDITIONS}
    load_credentials(Path(args.env_file))
    pace = Pacer(350000)
    random.Random(SEED).shuffle(jobs)
    done = {(r['key'], r['condition']) for r in (rows(directory / 'results.jsonl') if (directory / 'results.jsonl').exists() else [])}
    failures = 0
    for index, (k, c) in enumerate(jobs):
        if (k, c) in done:
            continue
        guess = estimate[k] if c == 'in_play' else 600
        await pace.wait(guess)
        row = {'key': k, 'condition': c, **await ask(ledgers[c], bodies[k][c], index + 1, contexts[k]['payload']['as_of']['seq'])}
        pace.add(row['input_tokens'] or guess)
        append(directory / 'results.jsonl', row)
        failures = 0 if row['status'] == 200 and not row['failure'] else failures + 1
        if stop_reason(row) or failures >= 3:
            print('STOP:', stop_reason(row) or 'three consecutive failures')
            break
    summarize(directory, units)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('dry', 'run', 'summary'), required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--night-dir', default=str(ROOT))
    parser.add_argument('--env-file', default=str(ENV))
    parser.add_argument('--approval')
    asyncio.run(run(parser.parse_args()))
