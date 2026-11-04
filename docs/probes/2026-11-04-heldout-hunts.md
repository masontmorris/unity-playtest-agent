# Blind held-out hunts with `playtest-hunt` (3–4 November 2026)

One command (`playtest-hunt --config runs/hunts/<game>.json`) ran the whole loop on each build:
- a guards-only session;
- the two pre-registered predictions;
- the code-written test plan;
- three live 600 s sessions, with S1 (`liquid-d1-ultra`) and the frontier (`muse-spark-1.3`, 12 calls each);
- a k-of-n replay of every filed finding (5 attempts from reloads, no model call);
- the scores.

The code was the same for both games; only the config's paths and measured setup differ. Between the first and last
held-out session the agent changed only infrastructure (04fb832 to 2220411: the guards session's time limit and
stale-lock clearing). Full traces, frames and clips stay local under `runs/hunts/`.

Builds:
- DragonBlaze held-out `eval/dragonblaze/all` (ced8a7fe);
- FPSKit held-out `eval/fpskit/all` (4cef835);
- the clean DragonBlaze dev run `upa/clean` (a4f37481).

## Blind score

A separate scorer read the answer key; only ids and counts came back.

| Build | Seeded bugs | Matched by a filed finding | Matched only by an unmet expectation |
|---|---|---|---|
| FPSKit held-out | 3 | 1 (b05) | 0 |
| DragonBlaze held-out | 2 | 0 | 0 |

Asked yes or no, the scorer said that at least one unmatched finding describes behaviour the key calls intended. I
did not ask which, so as not to tune against the key.

## What each hunt did

| Hunt | Sessions (ending, turns, findings) | Findings and replay | Cost |
|---|---|---|---|
| FPSKit held-out | wall limit 425, 1; error 285, 3; wall limit 436, 2 | f-28, f-27, f-26 (aim_toggle does nothing: **b05**) each **5 of 5**; f-172 0 of 5; f-64, f-91 (interact by a dispenser) 0 of 0, 5 unknown | $2.31 |
| DragonBlaze held-out | wall limit 408, 1; 515, 0; 471, 0 | f-103 (interact near the NPC never starts dialogue) **5 of 5** | $1.49 |
| DragonBlaze clean (dev set) | wall limit 443, 1; 402, 1; 502, 0 | f-79 (the same dialogue defect) 5 of 5; f-104 0 of 0, 5 unknown | $1.43 |

Notes:
- **b05 was filed in all three FPSKit sessions.** S1 escalated on its own each time, and the frontier wrote the
  replay check itself (`after_action` aim_toggle).
- **The DragonBlaze dialogue finding is a real defect of the base game, not a seeded bug.** On the clean build too,
  `DialogueController` registers in `Awake` on `Canvas/DialogueBoxIMG`, which the prefab and the scene save
  inactive, so dialogue never opens. The frontier filed it with a `within_region` check, and it reproduced 5 of 5 on
  both builds.
- **An "unknown" replay is one the route could not repeat.** No press reached the check's region: f-104 because a
  skill S1 cut short carried the player, and the replay skips stopped skills. A replay never guesses a route.
- **FPSKit session 02 ended on a Windows file-sharing error** during a restart (`request.json` held by another
  process). Its trace up to then is kept and scored.

## Failures on the way, and what changed

- **Two games' Editors at once** slowed Unity's start enough that sessions overran their host deadline. One
  DragonBlaze guards session and three DragonBlaze and one FPSKit live session were set aside and run again, one
  game at a time.
- **A stopped host leaves `Temp/UnityLockfile`**, and every later session refused to start. The hunt now removes a
  lock only when no running Unity process has that project open (2220411).
- **Replays lost routes.** An option that ended with its last input still held left no release record. That input
  is now held to the option's measured end (04fb832).
