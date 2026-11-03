# Guards: an input's effect depends on the game's state

A Unity Input System game is a guarded transition system. Its state is the scene, the body, stances, cooldowns, inventory, UI and saved data. An input action takes the game from one state to another only when its guard holds:
- dash only while a direction is held;
- attack only on the ground;
- aim only while the key is held.

The effect it has is set by the code or by a declared value.

A seeded bug is a wrong transition:
- a missing effect (an aim key that does nothing);
- an inverted guard (a sprint gate reversed);
- a wrong magnitude (a jump-cancel multiplier squared);
- a counter that is off;
- state lost across a transition.

Calibration measures every action from rest, so before this change an effect read as a function of the action alone. Read from rest, a guard looks like a missing effect, and a missing effect looks like a guard. The 2026-11-02 blind hunts showed both:
- "dash does nothing" (it needs a direction);
- "jump_attack never attacks" (no attacking mid-air);
- both reproduced 5/5 on the control build too.

## The guard table (`playtest.runner.guards`, `--measure-guards`)

Before play, each verb is pressed in a few states the measured primitives can put the body in. Each press is compared against a baseline of that state without the verb:

| State | How | Baseline |
|---|---|---|
| rest | the verb alone | an idle window of the same length, plus a 3 s idle pre-pass |
| moving | the verb chorded with each of two measured opposite directions (`--guard-chords`) | the direction alone |
| air | the rise, then the verb at the measured apex | the rise alone |

How the measurement works:
- **What a verb is:** an executable action whose controls are none of the measured directions' or the rise's. It is told apart by its inputs, never by its name.
- **Sampling:** readers, the camera parts and the body position are sampled while the input is held and once after it ends.
- **Trials:** each state is pressed twice, and only what changed both times counts.
- **Ambient keys:** a key that changes by itself is ambient. It still counts when a press moves it against its idle trend, which is a reset.
- **Output:** the table is saved as `guards.json`. It decides nothing: no choice is added, removed or ranked.
- **Pairs:** verbs are chorded with every measured opposite pair of directions (`pairs`): the body's own forward
  and back and the world's (a first-person body's strafes, a side-view body's left and right). An asymmetry
  compares the two moves of one pair only.
- **Asymmetries that are not the verb's** are left out of the list S1, the frontier and the review see, each with
  its reason when asked: the same key asymmetric the same way for three or more verbs is the direction's (the body
  walks into a pickup); a key where the opposite cell began at the end the verb drives it to is at a limit (a view
  already pitched to its clamp, Euler angles unwrapped); with the manifest, a key on another object than the
  controlled body is not the body's own. On clean FPSKit with both pairs this took 67 raw asymmetries over three
  fresh sessions to 14, and the ones in most sessions from many to the dash that works only forward, a design rule
  the expectations judge with the declarations; the dev build's seeded inverted dash gate kept all three rows, 3 of 3.
- **For a scorer:** each cell also keeps each trial's readings before its press (`starts`), the keys its baseline changed (`baseline_changed`) and the furthest the body got against the baseline's (`peak_displacement`). The in-air press goes in at the rise alone's highest reading, and `timing` records how. [`expectations.md`](expectations.md) scores declared expectations against these.

## Oracles

What a playtester can know is wrong without the source:

| Oracle | What it compares |
|---|---|
| declared value | a measurement against the value the authors set, with the arithmetic |
| symmetry | the same input in states the game is evidently built to treat alike; opposite directions are measured directly as **guard asymmetries** |
| UI vs state | a displayed value against the state it shows |
| across reset | state that should survive or be restored across a respawn, reload or level change |
| intent vs effect | an input whose stated intent its measured effect contradicts everywhere |

The review must name one oracle for each claim (`oracle`), and the finding's conditions record it.

## From a measured asymmetry to a reproduced finding

1. **Detect.** `guards.asymmetries` lists every effect seen while one direction was held and not while its measured opposite was. No model is involved.
2. **Reproduce.** N fresh guards-only sessions on the seeded build, each a clean Play Mode start, give each asymmetry's k of n (`python -m playtest.runner.guards tally`). This needs nothing from S1's play.
3. **Control.** The same on the unseeded build (`guards compare`). An asymmetry is **specific** only when the control never shows it. Anything else is the game's normal behaviour, or noise.
4. **Judge.** The review and the frontier receive the asymmetries as symmetry candidates.

Trace-based findings follow the same rule: `reproduce-with-control.ps1` replays the recorded inputs on both builds, and only a specific result moves a finding to reproduced.

## Evidence (FPSKit and DragonBlaze dev builds)

- **FPSKit `eval/fpskit-dev/all`:** dash changes `dash_blend`, field of view and motion while move_back is held, and none of them while move_forward is held. This is the reversed sprint gate, b01, and it appeared in two of two sessions. A trace replay with a while-held bound reproduced it 5/5, and 0/5 on the unseeded control.
- **DragonBlaze `eval/dragonblaze-dev/all`:** dash has no effect at rest or in the air, and sets `is_dashing` only while a direction is held, in both directions. That is a guard, not a bug, and the table shows it as one.
