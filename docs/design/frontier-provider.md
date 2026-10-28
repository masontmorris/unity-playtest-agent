# Live frontier integration and clip capture (#33)

This is the bounded live integration of the frontier into the S1 smoke loop. It is deliberately
limited to the provider boundary: added/pruned choices await the runtime choice builder, and
hypotheses and candidate findings wait for the investigation store (#35). Directive scoring is
also deferred (#34). There is no production preflight manifest yet (#27).

## Usage

The frontier is off by default. Turn it on for a run with the PowerShell switch or the environment
variable:

```powershell
scripts/unity/run-live-s1.ps1 -ProjectPath <game> -EntityPath <entity.json> `
    -CalibrationPath <calibration.json> -OutputDirectory <new-folder> -ApprovalPath <approval.json> `
    -Frontier
```

```text
UPA_LIVE_FRONTIER=1 uv run python -m playtest.bench.live ...
```

With the switch on, `live.main` additionally requires `META_API_KEY` and `ffmpeg` on `PATH`.
`BASETEN_API_KEY` is still required for S1. The switch and every other variable the script sets
are restored in its `finally` block.

## Budget envelope

S1 and the frontier reserve rows in the same `<output>/calls` folder, exclusively, before each
POST. The ledger counts:

- every row against the approval's `max_calls` and `max_cost_usd`;
- only rows of one arm against that arm's `max_calls` and `max_cost_usd`.

Before connecting, a frontier run checks the longest possible plan: every S1 turn plus at most one
frontier call for each S1 turn. If that envelope does not fit, the run is rejected before any
connection. It is never made to fit by suppressing an escalation later. An existing `in_flight`
row, an unreadable reservation or an expired pricing deadline also stops the next send. A failed
call keeps its reservation with an unknown cost; nothing is retried.

Approval file shape (placeholders only; the real file is never committed):

```json
{
  "approved_by": "<user>",
  "approved_at_utc": "<iso-8601>",
  "max_calls": "<positive-integer>",
  "max_cost_usd": "<usd>",
  "arms": {
    "mammoth-1": {
      "max_calls": "<positive-integer>",
      "max_cost_usd": "<usd>",
      "per_call_max_usd": "<usd>",
      "verified_balance_usd": "<usd>",
      "balance_checked_at_utc": "<iso-8601>",
      "price_valid_until_utc": "<iso-8601>"
    },
    "muse-spark-1.3": {
      "max_calls": "<positive-integer>",
      "max_cost_usd": "<usd>",
      "per_call_max_usd": "<usd>",
      "verified_balance_usd": "<usd>",
      "balance_checked_at_utc": "<iso-8601>",
      "price_valid_until_utc": "<iso-8601>"
    }
  }
}
```

Every placeholder is replaced with a measured value before use; the validator rejects the strings.
`replay.validate_approval` maps `muse-spark-1.3` to the Meta provider, so its limit is checked
against the Meta balance rather than the Baseten balance. `ARMS` is unchanged.

## When a call happens

A call happens only after the production `TurnLoop` returns `escalated`, now records the pending
helper as `escalate`, and the pending helper was created by that same turn. A delegate, a wait, a
finish, or facts growing during ordinary turns never call the frontier. `while_pending` input is
already executed by the loop and is not replaced or repeated.

The call blocks the next S1 turn; the game keeps running in real time. Encoding and the POST run in
`asyncio.to_thread`. No second Unity observer is started.

## Packet provenance

| Packet field | Measured source |
|---|---|
| `objective` | the live smoke objective |
| `as_of` | the exact decision observation's `seq`/`game_s`, and the age measured in `LiveS1.decide` |
| `observation` | the exact `Observation` the escalated choice set was built from, including `public`, `ui`, and `reader_errors` |
| `escalation` | `TurnRecord` fields only; S1's `reason` and `distribution` are never copied |
| `choice_set_version`, `choices` | the exact `ChoiceSet` S1 answered |
| `directive`, `directive_history` | the adapter's adopted live directive and superseded history |
| `facts` | `Facts.evidence()`: the game-derived prose S1 saw, with explicit `seqs` recorded at fact construction |
| `recent` | one row per episode turn with explicit `seq`, `game_s`, `wall_utc`, `public`, `reader_errors`, the selected raw/effective/executed values and the execution record |
| `hypotheses`, `new_hypothesis_ids` | empty; the investigation store is deferred (#35) |
| `clip` / `clip_unavailable_reason` | the media result, or the explicit reason there is no clip |

`Facts.evidence()` cites the position from its `inspection_seq`, displacement from both inspection
sequences, and execution-status facts from the turn's `based_on_seq` plus the execution sequence
when known. The note about an invalid S1 answer is about S1, not the game, so it is not citable
game evidence.

## Answer handling

One decoded answer resolves the matching pending escalation exactly once. Its review problems are
recorded per item. A directive that is not rejected is converted with a receipt game time measured
by a fresh `observe_once` after the response; that observation is added to the loop store and the
directive reaches S1's next payload. A superseded directive is kept in history with outcome
`unknown` because no comparator scores it yet. A rejected directive is not replaced, and
`directive: null` keeps the current one. Added choices, pruned choices, hypotheses and a candidate
finding are recorded in the call row as `received_unapplied`; the offered choices are unchanged.

A refused, truncated, structurally undecodable or failed answer stops the run as a `ProviderStop` with
no retry, no repair and no substitute answer. The turn that requested help remains in the trace.

`schema_valid` means the structural decode plus the dynamic enum values of the exact rendered
schema (manifest fields, action and skill names). It is not `review.clean`: a citation or setpoint
type problem rejects one item without making the answer schema-invalid, while an unknown dynamic
enum value does make it schema-invalid. Offline tests check this against `jsonschema`; the runtime
does not take a `jsonschema` dependency.

Decoding rejects type coercion: for example, a string deadline is rejected rather than converted
to a number. A directive with no checkable postcondition has outcome `unknown`.

## Clip capture and its timestamp limitation

The clip comes from the bridge's existing offscreen-composite JPEG window at
`<project>/Temp/playtest-bridge-frames`. Capture is attempted only when the bridge status says
`capture` is enabled and `capture_self_test` is `passed`. The window is the last four seconds up to
the escalating observation's wall time, clamped by the episode start wall time so frames from an
older session are excluded.

The only available timestamp is the JPEG file's modification time, recorded as
`timestamp_basis: frame_file_mtime_utc`. It is the writer's completion time, not the capture time
or game time. `frame_game_s` is `null`, and the descriptor caveat states that alignment with the
trace is approximate. Encoded frame durations follow the measured modification-time gaps; a long
gap is kept, not compressed into a nominal 10 Hz sequence. The MP4 is H.264 `yuv420p`, 640×360 with
letterboxing, and has no audio. Copied frames, their hashes, sizes, order and modification times are
kept in the per-call local `manifest.json`, together with `frames_skipped`, the number of listed
frames that could not be measured. The bridge's `self-test.jpg` artifact in that folder is not a
game frame and is never captured. Only relative references are recorded in the call row;
no local absolute path reaches the packet, the request or public documentation.

`target_duration_s` is the encoder's requested duration; `encoded_duration_s` is measured with
`ffprobe`, or null when it cannot be measured. The final still frame has an explicit nominal
0.1-second hold. Recent action records retain the bridge's measured input start/end times,
execution timestamp, and the actual executed choice, including an S1 `while_pending` continuation.

The bridge keeps a rolling window, so a listed frame can be deleted before it is measured or copied.
Such a frame is skipped rather than failing the call, and an unusable window is scanned at most
`WINDOW_ATTEMPTS` (three) times. A frame that still exists but changed while it was read is not an
eviction and fails closed. A window with fewer than two measurable frames, a missing encoder or an
encoding failure returns no clip and a concrete reason. No stand-in video is written. A run may still
make a text-only call with that reason, but live acceptance with a clip requires an actual nonempty
clip.

## Acceptance and scope

Live acceptance for this integration requires a completed run with a frontier call that had HTTP
200, a parsed answer, exact schema validity and a nonempty attached clip. A completed run with no
S1 escalation is `inconclusive`, not passed; the ordinary S1 summary is unchanged. A run that
stopped before completion, or a call that lacks any required part, does not pass.

Deferred work: directive scoring (#34), the runtime choice builder, the investigation and finding
lifecycle (#35), and the production preflight manifest (#27). Four completed live runs met the rule
above (`issue33-live-01`, `-02`, `-04`, `-05`); the last two also exercised the clip-capture race fix
live. The evidence, costs and limits are in `docs/probes/2026-10-28-frontier-live-clips.md`.

Offline ownership review covers scripted escalation, delegation, wait, finish, rejected answers,
and pending continuation. Fake-game traces replay with zero adherence violations. The provider has
no game-input port, and timers or facts cannot route help. Live-game adherence was 1.0 with no
violations in all nine completed episodes of those runs; the sample size and the generic directives
are the limits named in the note.
The packet's citation sources and separate current/history directive fields are listed above.
No persisted contract or C# DTO changed; schema export must remain byte-identical.

The transport uses Meta's [Chat Completions endpoint](https://dev.meta.ai/docs/protocols/chat-completions)
and the inline MP4 `video_url` form exercised by the project's Day 1 probe. Current
[video documentation](https://dev.meta.ai/docs/video-understanding) emphasizes the Responses API;
the Chat Completions form is the one the live runs above used, and every attached clip returned an
HTTP 200 answer. Token cost uses the published
[Standard-mode rates](https://dev.meta.ai/docs/pricing-rate-limits): $1.25 input, $0.15 cached input,
and $4.25 output per million tokens. Missing token counts keep cost null.
