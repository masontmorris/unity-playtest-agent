# Does the frontier change approach after a failed direction? (session 12, 31 October 2026)

Issue #193 asked for a frontier that changes its approach when a direction has not succeeded. A prompt rule
shipped in PR #198, a paid trial showed the rule could never fire, PR #200 rewrote its trigger, and session 12 is
the first run with the working rule. This is what it did and what it does not show.

**Headline:** the frontier's directive chain no longer repeats. Session 12 issued nine directives on nine
different targets, one `not_met` was followed by a different part of the structure, and three directives carried a
checkable expectation where session 10 had none. The session itself got nowhere near the structures (closest 21
units), so it says nothing about `b04` and nothing about whether S1 plays better.

## The rule, and the trial that broke the first version

`- repeated attempts:` in `src/playtest/roles/frontier.py` SYSTEM. The first version triggered on a closed
directive's `outcome` being `not_met` or `expired`, or an `error_class` saying S1 could not attempt it.

Session 11 ran that version and was stopped at turn ~16 (**$0.0314**, 24 calls). Reading session 10's recorded
directives showed why: all ten set `expected: null` — no manifest field could score "did S1 get up the
structure" — so nine closed `unknown` and one `invalidated`. The trigger never occurred, and the frontier was
free to repeat.

The shipped trigger (PR #200, `da1d831`) names what is measured instead:

- `not_met` / `expired`, or an `error_class` saying S1 could not attempt it;
- the recent rows showing the action it asked for **short of the manifest's expected displacement**, or the
  change it tested never appearing;
- **an earlier directive already asking for the same action at the same part.**

Plus the guard that `unknown` alone is not a failure, and the standing one that a `not_met` directive does not by
itself contradict its hypothesis.

## Session 10's baseline (no working rule)

Six consecutive directives, from the recorded trace:

| directive | closed as | target | choices |
|---|---|---|---|
| d-79 | unknown | stepped block face directly ahead in centre of view | look_left, look_right, jump, move_forward |
| d-96 | unknown | *same* | move_back, look_left, look_right, jump, move_forward |
| d-115 | unknown | *same* | look_left, look_right, jump, move_forward |
| d-125 | unknown | *same* | look_left, look_right, jump, move_forward |
| d-140 | unknown | *same* | look_left, look_right, jump, move_forward |
| d-149 | unknown | *same* | look_left, look_right, jump, move_forward |

This is the shape #193 was opened for: one part, one approach, six times, while S1 pressed a 1.1-unit face for 98
turns.

## Session 12 (the working rule)

Same preflight-3 manifest, calibration-3, preflight-2 entity and objective as session 10; S1 `liquid-d1-ultra`
with game view, frontier `muse-spark-1.3`, 600 s wall. Only the code differs (`da1d831`). 155 turns, complete,
ending `finish`; 174 S1 calls of 350, 12 frontier calls of 15.

| directive | closed as | expectation | target | choices |
|---|---|---|---|---|
| d-8 | invalidated | null | stepped-structures-ahead | explore_forward |
| d-18 | unknown | null | stepped-structures-ahead | move_back, coarse_turn_left/right |
| d-37 | unknown | null | stepped-block-with-slabs-ahead | coarse_turn_left/right, explore_forward, jump |
| d-60 | **met** | `public.yaw` changed | open-checkered-ground-left-of-wall | coarse_turn_left, move_forward |
| d-73 | **met** | `public.yaw` changed | open-checkered-plain-around-position | coarse_turn_left/right |
| d-78 | **not_met** | `public.yaw` changed | tall-stepped-structures-out-of-view | coarse_turn_left |
| d-89 | unknown | null | **slab-steps-right-edge** | coarse_turn_right, move_forward, jump |
| d-125 | unknown | null | tilted-checkered-ramp-left-edge | coarse_turn_left, move_forward |
| d-136 | unknown | null | tilted-checkered-ramp-underfoot | move_forward, jump |

Four differences from session 10, all mechanical and countable:

1. **No target-and-choices repeat.** d-18 keeps d-8's target but changes the choices; every other directive moves
   to a new part. Session 10 repeated one target with one approach six times.
2. **The one `not_met` (d-78) is followed by a different part** — d-89 moves from "structures out of view" to
   "slab steps at the right edge", and drops the turn it had failed.
3. **Expectations are used.** Three of nine directives carry `public.yaw: changed`; session 10 set one in none of
   ten. They are not progress checks, but they make a directive scorable, which is what lets a failure be seen.
4. **S1's behaviour differed too, and not helpfully.** No jumps at all (session 10 chose `jump` 14 times),
   55 `move_forward`, 56 coarse turns, and it ended at (-2.37, 1.78, -32.62) after sweeping the west and north of
   the level. Closest approach to any of the four side flights: **21.11 units**. Camera y stayed 1.78 except a
   0.5-unit rise over raised ground at turn 139.

## What this supports

- The directive-level claim: with the fixed trigger, this frontier did not repeat a target-and-approach pair, and
  followed its only `not_met` with a different part. That is the behaviour #193 asked for, observed once.
- The cost of finding out: **$0.0314** for the inert-rule run and **$0.3536** for the working-rule run (S1
  $0.1106, frontier $0.2430) — $0.3850 for both, against a $1.55 approved ceiling.

## What this does not support

- **No b04 evidence.** S1 never got within 21 units of the structures, so the session neither reproduced nor
  avoided the bug. It is not comparable to sessions 8–10 on proximity.
- **No quality claim.** One session per arm, different nondeterministic runs; session 12 differs from session 10
  in S1's choices as much as in the frontier's. A "navigates better" claim needs several sessions per arm and a
  paired design, and none exists.
- **No claim about the rule's weight.** The rule is prompt text among many bullets; a single session cannot say
  how much of the change it caused, and session 12's frontier also faced different evidence.
- The `met` on d-60/d-73 is a yaw change, which almost any turn satisfies. Two `met` outcomes are not evidence
  that S1 obeyed a directive's intent.

## Method

- Launchers: `runs/b04-hunt/run-session-11.ps1`, `run-session-12.ps1`; approvals
  `approval-session-11.json`, `approval-session-12.json` (session 12 caps at $1.45 so the pair stays inside the
  $1.55 the user approved).
- Two attempts were refused before any call and cost nothing: `session-12-setup-error/` (a killed Unity left
  `Temp/UnityLockfile` behind) and `session-12-envelope-error/` (the envelope did not cover its own call caps).
- Directive extraction: `runs/b04-hunt/session-*/trace.jsonl`, `kind: investigation`, events `directive` and
  `outcome`, joined by `directive_id`. Local tooling only, not committed.

## Sources

Local, not committed: `runs/b04-hunt/session-10/`, `session-11aborted-rule-inert/`, `session-12/` (trace, calls,
run summary, clips), `session-12-setup-error/`, `session-12-envelope-error/`. Public: issues #193, #199 and the
rule in `src/playtest/roles/frontier.py`.
