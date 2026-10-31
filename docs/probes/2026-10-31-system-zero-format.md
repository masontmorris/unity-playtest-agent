# System Zero request-format probe (31 October 2026)

Follows [G2](2026-10-31-system-zero-s1-bench.md), where `system-zero-2.0.0` chose acceptably on 7/40 and
19/45 frames, given the System One request every S1 arm sends. The user approved **$0.20** to test whether
the format caused that. Script: `scripts/probes/system_zero_format.py`.

## Predeclared (written before any call)

**Question.** Does System Zero choose acceptably when the request follows its own guidance (data in `state`,
the task in the question's `instructions`)? Or, failing that, when the rule is spelled out, or the state is
plain text?

**Fixed.** The model `system-zero-2.0.0`, the frozen Day 3 corpora rebuilt byte for byte, the labels (draft
and reviewed are identical for both corpora), and the choices (10, one flat question). Every format changes
one thing from G2's request:

| Format | Change from G2's request |
|---|---|
| F1 `task-in-instructions` | Question instructions begin with `Task: <objective>`; state unchanged |
| F2 `task-and-rule` (upper bound) | F1 plus the checking rule spelled out. It is the rule the labels encode, so a pass shows the model can follow an explicit rule, not that it infers one |
| F4 `text-state` | The same payload as indented text lines instead of a JSON object; instructions unchanged |

**Stages and order.**
- **A, screen:** each format on the 45 shuffled frames, plus the re-test's 5 repeated frames (150 calls) in one
  seeded order (seed 3110).
- **B, confirm:** each format with ≥ 44/45 in A runs the first corpus's 40 frames.
- **C, diagnostic:** on the 40 shuffled check frames, 8 boolean questions ("has cN been checked in this
  pass?"). This separates reading the facts from choosing the next step. C runs on whatever the envelope
  leaves after A and B.

**Primary metric.** Acceptable top choice (check and terminal frames), scored against the labels.

**Decision rule.** A format makes System Zero viable as S1's chooser only with ≥ 44/45 shuffled and ≥ 39/40
on the first corpus (d1-ultra: 45/45 and 40/40). Otherwise it is not viable in that format. F2 passing
alone means only "follows a spelled-out rule".

**Secondary.** Miss kinds (premature finish, re-check, wait), acceptable mass, paired per-frame wins and
losses against G2's request on the same frames, repeat stability, latency, tokens.

**Diagnostic reading (C).** At least 95% correct on the 320 judgments means System Zero reads the facts and
fails at choosing. Below 80% means it fails at reading too. In between is mixed.

**Budget and stop rule.** Per-call reservations of $0.0007 per choice call and $0.0018 per diagnostic call,
inside one $0.20 envelope. The balance is read before and between stages. A non-200 response stops that
format; nothing retries. Inconclusive is an allowed outcome.

### Amendment (written after stage A ran, before it was scored or read)

The user asked whether System Zero should see continuous information rather than one snapshot per decision.
A stream decision is an ordinary decide over the state the stream holds; the model keeps no memory between
decisions. So the stream-shaped input can be sent over HTTP as exactly the array an append-only stream would
hold. That is format **F5 `event-log`**:

- State is a JSON array. Item 0 is a header with everything in frame *k*'s payload that isn't per-turn: the
  objective, instructions, choices, directive fields, pending help and game view.
- Then one item per frame of the same pass up to and including *k*, in recorded order. Each holds that frame's
  `as_of`, its `observation`, and the facts that are new since the previous frame.
- The question instructions are unchanged (G2's), so the variable is the state's shape and its earlier
  observations.

F5 is scheduled in its own directory: the shuffled corpus's 45 frames plus the same 5 repeats, in a seeded
order. It is held to the same decision rule (≥ 44/45, then ≥ 39/40 on the first corpus) and reported next to
F1–F4. Its per-call reservation is $0.0012, since the logs are longer. Budget order after A: F5, then B for
any format that passed, then C with what is left.

## Results

270 calls, all HTTP 200, none retried, none refused. **Measured cost $0.18**: the balance read $4.91 before
and $4.73 after (A $0.08, F5 $0.04, B $0.02, C $0.04). Model `system-zero-2.0.0` throughout.

| Format | Shuffled corpus (45) | Checks / terminal | vs G2's request, frame by frame (better / worse) | Acceptable mass, mean / min | First corpus (40) |
|---|---:|---:|---:|---:|---:|
| G2's request (System One shape) | 19/45 | 14/40 / 5/5 | — | 0.457 / 0.020 | 7/40 |
| F1 task in instructions | 32/45 | 27/40 / 5/5 | 14 / 1 | 0.54 / 0.01 | not run (below the bar) |
| **F2 task plus the rule** (upper bound) | **44/45** | 39/40 / 5/5 | 25 / 0 | 0.72 / 0.16 | **36/40** (29 / 0) |
| F4 state as text | 6/45 | 1/40 / 5/5 | 1 / 14 | 0.26 / 0.01 | not run |
| F5 event log (stream shape) | 14/45 | 9/40 / 5/5 | 5 / 10 | 0.44 / 0.01 | not run |

Every miss across the formats was a premature `finish` or a re-check of a done control. The repeated frames
gave the same choice 5/5 in every format, with identical distributions, except F2: its probabilities differed
by up to 0.074. Median latency was 296–344 ms and p95 422–531 ms; the event logs averaged 3,036 input tokens
against about 2,000.

**Diagnostic (C), 30 of the 40 check frames.** The envelope after A, F5 and B left about $0.06; 30 frames in
C's seeded order kept the measured total inside $0.20. **240/240 judgments right, 30/30 frames exactly
right.** Mean P(true) was 0.955 for a checked control and 0.024 for an unchecked one, with no errors at any
stage of a pass.

## Reading against the predeclared rule

- **No format makes System Zero viable as S1's chooser.** F2, which hands it the labels' own rule, reached
  44/45 but only 36/40 on the first corpus (bar 39/40). F1, the format System Zero's docs recommend, reached
  32/45.
- **Format matters a lot.** Putting the task in the question's instructions won 14 frames and lost 1; adding
  the rule won 25 and lost none. Rendering the state as text made it worse.
- **Stream-shaped continuity did not help.** The append-only log, which is the state a stream holds, scored
  14/45 against the snapshot's 19/45, losing 10 frames and winning 5.
- **It reads perfectly and composes poorly.** By the predeclared reading (≥ 95%), System Zero reads the facts
  and fails at choosing. Asked eight narrow questions, it knows exactly which controls are done. Asked one
  question whose answer depends on combining them, it often answers `finish`.

Limits: one checklist task in one game, text only. The F2 rule is task-specific and can't be deployed as is.
C covered 30 of 40 frames. F5 is the state an append-only stream would hold, sent over HTTP; no WebSocket
stream was opened.

## Records

Local under `runs/bench/system-zero/`: `format/` (A, B, C), `format-f5/`, the approval, five balance reads,
and the reports. Scripts: `scripts/probes/system_zero_format.py` and `system_zero_event_log.py`.
