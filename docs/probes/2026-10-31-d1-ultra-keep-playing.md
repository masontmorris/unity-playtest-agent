# d1-ultra: does "keep playing while help is pending" stop the waiting? (31 October 2026)

**Verdict, by the rule declared before any call: no effect.** The wording moves S1 in the intended
direction but misses the bar: `wait` while help was pending fell from 90% to 62.5% of calls, and needed to
reach 45% or less. The mean P(`wait`) halved, from 0.478 to 0.224. Help was not suppressed: the escalate
share went from 46.7% to 40.0%, inside the 20-point allowance. Most of the non-wait picks went to in-place
turning (`mouse_turn_right` ×11), not to progress.

## Design

- **Units:** 11 frozen turns from `climb-stairs-02`, each with its exact JPEG:
  - pending primary turns 23, 25, 40, 41 and 42;
  - `while_pending` follow-ups at turns 22, 24 and 39;
  - escalate primary turns 22, 24 and 39.
- **Request bodies:** built through the real adapter. The `current` arm passed the hash gate on all 11 units.
- **Arms:**
  - `current` (`policy/2`);
  - `keep-playing` (`policy/3` plus the `FOLLOW_UP_KEEP_PLAYING` follow-up).
- **Repeats:** 5 per unit and arm, 110 calls in all, in a seeded order.
- **Reading:**
  - inconclusive if `current` waits on less than half of pending calls;
  - suppresses help if the escalate share drops more than 20 points;
  - works if the `wait` share halves;
  - no effect otherwise.

## Results (110 calls, 110 OK, $0.046)

| Arm | `wait` share on pending calls | Mean P(`wait`) | Escalate share | p50 latency |
|---|---:|---:|---:|---:|
| current | 90.0% | 0.478 | 46.7% | 282 ms |
| keep-playing | 62.5% | 0.224 | 40.0% | 297 ms |

The script is `scripts/probes/probe_s1_keep_playing.py`; it was written by gpt-6.1-terra and run by the
orchestrator. `policy/3` stays an opt-in and was not used in the continuous-input trial that followed.
