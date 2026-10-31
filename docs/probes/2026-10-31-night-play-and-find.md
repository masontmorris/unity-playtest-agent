# Overnight: playing FPSKit's combined dev build and finding its seeded bugs (31 October 2026)

The user approved a $10 Meta API budget plus the rest of the Liquid d1-ultra credit for one night of work. The goal
was an agent that plays FPSKit "functionally, semi-smoothly" and finds seeded bugs. Every session ran on
`eval/fpskit-dev/all` (7265d2f), the development build carrying all four dev bugs (b01–b04). The development key is
readable; nothing here touches the held-out eval set. Full traces, frames and clips stay local under `runs/night/`.

**Headline.**
- **b02 found** in session 01 and **reproduced 5/5** on both dispenser timers (f-18, f-29).
- **b01 found** by a new post-session review of session 04, **reproduced 5/5**, with **0/5 on the unseeded base
  build**. The review flagged b01's other half again on session 05.
- b03 and b04 were not found: the evidence for b03 is ambiguous, and S1 never walked into a 0.25 riser.
- Play went from 24 of 97 turns executed (session 01) to 148 of 151 (session 03), with pacing waits down from
  395 s to 8 s.
- Meta API spend was about **$8.72** of the $10, and d1-ultra about $0.14 before its credit ran out at session 02, turn 60 (HTTP 402).

None of this is a blind result: b01–b04 are the development set, the night tuned on them, and session 06's
objective named the stepped structures.

## Why two bugs could not be found, and why measured bugs were explained away

1. **No verbs.** The b04-hunt manifest offered 14 actions, all movement or look on two devices each. Preflight
   listed "crouch dash aim and fire controls show no calibrated movement effect" as uncontrollable. Sprint and fire
   could never be chosen, so b01 and b03 were unreachable. → #217 (PR #221, #224, #226).
2. **Expectations from a contaminated source.** Suspicions compare play with calibration, and calibration measured
   the same build. A bug present from the start never deviates. Discovery already read the authored values
   (`stepOffset` 0.3, `dashSpeed` 10.5, `dashForwardThreshold` 0.8, `moveSpeed` 5.0), and they were stripped
   before any model saw them. → #218 (PR #225): the frontier sees the entity's serialized, inspector-visible
   values as `declared_configuration`.
3. **The frontier explained a blocked step away.** Offline replays of b04 session 15, turn 28 (12 Standard calls,
   about $0.35) showed it read the probe line `forward cast 0.15 up hit at 0.38` as "a 0.38 m blocker". When asked
   for an explicit expectation, it wrote "a player expects a wall or step to stop forward motion at contact".
   → #219 (PR #220): each hypothesis states `what_one_would_expect`, and every probe number says what it is. At
   the main flight's blocked spot the probe reads +0.36 at 0.6 ahead, which exceeds 0.3. The decisive spot is a
   side flight's low end (+0.25), and no session produced a walk into it.
4. **An action's measured effect is displacement only.** A verb that changes a reader without moving (dash's
   blend, fire's recoil on pitch) reads "0 world units, as calibrated" whether it worked or not. With its bounded
   window and call cap, the frontier never compared like actions across the session.
   → #232: post-session review (below).

## Smoother play

| Change | Evidence |
|---|---|
| #222 (PR #223): pacing spreads calls instead of bursting, then stalling 20–40 s | Pacing had been 54–64% of wall time in sessions 12/14/15 |
| `-S1Layout dup-state-ref/1` exposed (lossless, #202) | Choice labels were sent twice (~2.2k tokens a call) |
| `-TokensPerMinute 420000` for d1-ultra (run setting) | The rate probe saw up to 478k/min with no 429 |
| #227 (PR #230): S1 arm `meta-muse-spark-s1` | After the HTTP 402. Probe: 1.6–2.3 s, $0.0026 cached / $0.015 uncached per call |
| PR #231: Meta S1 text sends the turn-stable state first | Session 03 cached 19% of each prompt; session 05 cost $1.10 for 120 calls against $1.88 for 150 |
| #228 (PR #229): opt-in fact of which offered choices have sent no input | d1-ultra sent move/jump/turn and `dash_forward` ×2 in a 22-verb manifest |
| Objective text: keep playing while help is pending; escalate after trying | Session 01 looped escalate → wait → wait every 2–3 turns |

## Sessions

All sessions used the frontier on `muse-spark-1.3` Standard, the game view and declared configuration.
"Executed" counts turns whose input the bridge ran.

| Session | S1 | Turns (executed) | Frontier calls | Cost | Result |
|---|---|---|---|---|---|
| 01 | d1-ultra | 97 (24) | 15 | $0.57 | **b02** f-18, f-29 by turn ~18; FPs f-43 jump, f-48 strafe; S1 stayed within x 3.6..7.6 |
| 02 | d1-ultra | 63 (43) | 10 | $0.42 | HTTP 402 at turn 60; FP f-28 jump |
| 03 | Meta | 151 (148) | 0 | $1.88 | Tried every verb and reached the side flights, but never escalated |
| 04 | Meta | 130 (96) | 14 | $2.95 | b02 again (f-98); FPs f-8 jump, f-73 move overshoot; **b01 via review** |
| 05 | Meta | 121 (119) | 0 | $1.10 | Review: b01's forward half again |
| 06 | Meta | 101 (97) | 0 | $0.92 | Objective named the stepped structures; S1 met a side face (0.5–0.75 rise, a real block), not a low end |

Preflights: $0.41 (first selection, rebuilt from its ledger after a 14-action contract cap failed assembly) and
$0.14 (second, with #226 holds). Stage 7 verified 17/24, then 22/22 declarations. Offline replays and the S1 probe
came to about $0.47. Meta total about $8.72; d1-ultra S1 calls about $0.14.

## The post-session review (#232)

One focused call (`muse-spark-1.3`, low effort, ~13k prompt tokens, ~$0.033) reads three things: for every
executed action, the public readers that changed between its observation and the next one, plus height, held
references, displacement and (from session 06) the measured surroundings; the action intents; and the declared
configuration. It asks for behaviour someone playing would not expect, the same action differing between like
situations, or a measurement outside a declared value, citing every turn used. Prototype:
`runs/night/review/review.py` (local). The module is in progress.

On session 04, both reps returned the reversed sprint gate. From rep 2: "dash at turn 9 changed dash_blend
0.0->0.879 with displacement 0.0, while dash_forward at turns 23, 66, 106 had displacement ~2.0 but no dash_blend
change". Rep 1 added: "despite declared dashSpeed 10.5 vs moveSpeed 5.0". Its other items were wrong:

- a fire contrast confounded by no weapon being held at turns 11–12;
- crouch measured after a hold-to-crouch release;
- a jump-count artifact.

**Reproduction** (`reproduce --finding-file`, new): the candidate "dash engages while standing still" has check
`public.dash_blend > 0.5` and replays the 8 recorded inputs through turn 10. Every attempt read 0.0 after the
reset and then satisfied the check: **5 of 5** on the dev build, **0 of 5** on the unseeded base (`bridge-v0`,
25e43fb). The replay stops before the later forward dashes, which raise the blend on a correct build.

## What did not work, and what it suggests

- **b03 (no firing while airborne).** Session 04 had the signature: `fire_airborne` with a weapon held left
  `pitch` at −2.5815 → −2.5815, while ground fire moved it −2.58 → −5.08. In session 05 the same skill moved
  pitch −2.17, which fits one shot before takeoff, but that is not established. The skill taps jump and then holds
  fire. A chord that holds fire through the whole flight would separate the two cases; stage 5 composed none.
- **b04 (stairs).** Sessions 03, 05 and 06 reached the side flights and met their north side faces (rises 0.5–1.5,
  a legitimate block for a 0.3 step). None faced up a low end, where the riser is 0.25.
- **Escalation.** Meta S1 escalated 0 times in three of four sessions. The review moves judgement after the
  session, but the in-session frontier is then idle.
- **Momentum noise.** At 2.6 s per turn the previous input is still settling when the next is measured. `look_right`
  "moved" 0.6–3.7 units and `move_forward` overshot to 2.8, which floods `effect_mismatch` suspicions.
- **Cost.** Meta S1 runs about $0.009–0.016 a call, against d1-ultra's ~$0.0008.

Local records: `runs/night/` (sessions, reproductions, review prototype and answers, timing and token analyses).
