# The S1 request layout (opt-in, one copy of each duplicated text)

S1 has no memory. Every request must carry everything S1 can see: the question it is asked, the choices
it may pick, the measured facts, the history window and the image. Nothing here shortens that. The
layout only stops the *same request* from stating the same text twice, and says where the first copy is:

* `state.instructions`, when it is byte-identical to the one question's `instructions`;
* a `state.choices[i].label`, when it is byte-identical to that question's `criteria[id]`.

Everything else is unchanged: every choice keeps its `id`, `kind`, `params`, `action`/`skill`,
`precondition` and `source`, every other key of the body and the context stays where it was, and a
`null` stays a `null` rather than becoming missing (or the reverse). Removing a label is only allowed
when that id's label is already stated in the criteria of the question being asked, so on a follow-up
question — whose criteria cover only the choices S1 may pick while help is pending — the labels of the
escalate, delegate and finish choices stay inline. They cannot disappear.

The opt-in transform is candidate A of `runs/b04-hunt/compaction-review-13/REVIEW.md`. C and B are not
applied here.

## Opt-in API

```python
from playtest.runner.request_layout import RequestLayout
from playtest.runner.s1 import SystemOneS1

SystemOneS1(ledger, max_calls=10, arm="liquid-d1-ultra",
            layout=RequestLayout.duplicate_state_reference)   # or layout="dup-state-ref/1"
```

`layout` defaults to `RequestLayout.full`, which never calls the codec: the body is the one this
adapter has always sent, byte for byte. `adapter.layout` reports the layout in force (`"full"` or the
marker version). There is no CLI flag and no default change; nothing in `playtest-run`, the benches or
the loop selects it.

The layout needs a plain `state`. The `mammoth-1`/`elephant-1` arms wrap `state` inside a message, so a
body sent that way could not be restored from its own bytes; the adapter refuses that combination at
construction instead of sending a marker that would not bind.

## What the sent body carries

The question is built first and left exactly as it was, criteria included. Then the encoded body adds one
marker inside `state` (which the provider treats as opaque):

```json
"_s1_layout": {
  "version": "dup-state-ref/1",
  "rule": "Only one copy of each duplicated text is sent: restore state.instructions from questions.next_action.instructions and each labels_removed label from questions.next_action.criteria.",
  "question": "next_action",
  "instructions_removed": true,
  "labels_removed": ["c0", "c3", "wait", "finish"],
  "body_sha256": "<sha-256 of the encoded body's text>"
}
```

A reader needs nothing outside the request: the marker names the question whose own instructions and
criteria are authoritative, and lists the ids whose labels were dropped. There is no catalog, archive or
out-of-band pointer, and no text is rewritten. `body_sha256` covers the body text (model, questions,
state) and not the transport's `images` array, which the codec never reads or edits and which the adapter
may attach after the body is encoded.

`playtest.runner.request_layout.decode(body)` takes only the encoded body, never a file, corpus or the
original context, and returns the exact body `encode` was given. It raises `CodecError` — it never
substitutes the original — when the marker is malformed or of another version, when it names a choice or
question the body no longer has, when a referenced criteria label is gone or is not text, when a choice
still states the label it should have lost, when the second copy reappears, or when the restored body no
longer hashes to `body_sha256` (so a changed referenced label, a dropped key or a swapped value is
refused rather than returned).

## What is left alone

The original context mapping is not mutated and its bytes and hashes are unchanged; the adapter
encodes a copy and reads `choice_set_version` and `as_of.seq` from the original. The frontier packet is
built from the same context before the adapter and is untouched, so its bytes do not move. The call
record's `request_sha256` is the digest of the body that was actually sent, marker included, and the
token estimate is based on those same sent bytes. Replay and the benches build their own bodies, so
their default bytes are also unchanged.

## Measured on the frozen session-13 corpus (local, offline)

The production codec was run over all 169 sent bodies of `runs/b04-hunt/session-13` (149 `next_action`,
20 `while_pending`) after that corpus's own gate reproduced every traced `request_sha256`. Each body was
encoded, serialised to JSON text, parsed back and decoded from that text alone: **169/169 exact**, zero
problems, and all eight negative controls rejected. See `runs/layout-dup-ref/` for the local run.

| | before | after | change |
|---|---|---|---|
| text chars, sum (169 calls) | 7,251,489 | 6,238,833 | −13.96% |
| median text chars saved per call | — | 6,144 | p10 4,860 / max 6,144 |
| full bytes, sum (base64 included) | 11,942,646 | 10,929,990 | −8.48% |

The marker itself costs 660–684 characters per request and is included in those figures. The image is
4,691,157 base64 characters of the full-byte total and is neither read nor changed. These are characters
and bytes of rendered sent bodies; they are not tokens, and no latency, quality or provider-acceptance
claim follows from them. Bodies whose duplicated text is shorter than the marker would grow; that did not
happen in this corpus, and the codec does not special-case it.

## Not claimed, not done

Nothing here has been sent to a provider: whether the model reads a `state` without `instructions` or
labels as well is untested, and the review's 120-call comparison would need its own budget approval. The
corpus is one manifest and one session. This change adds no CLI option, changes no live default, and is
not evidence for #91 or #36.
