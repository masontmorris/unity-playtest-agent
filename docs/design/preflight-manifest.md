# Preflight selection and local manifest

Stages 4–6 use one project-specific `StageAnswer` schema for readers, actions and progress. The
runner sends an append-only conversation, records every attempt before the next call, and retries
validation errors at most three times with their exact messages. A transport error stops the chain.
If action validation is exhausted, progress may still run on accepted readers; a partial chain does
not produce a manifest.

The packet is built from saved Editor inventory and discovery inspections and a calibration record.
The offered Editor field identifiers are mapped bijectively to short, deterministic selection IDs;
the manifest stores both the selected ID and a descriptor with runtime values omitted. The model sees the source
excerpts, inspection caps, calibrated control summaries and a screenshot with its capture provenance.
Runtime field values and enum names are omitted from the packet; game source excerpts are sent to the
model provider. The packet identifies fields outside the calibrated entity's scene without silently
removing them.
Fields reported as unreadable are not offered as reader IDs and are listed separately. A field that
was null can still be offered, with its kind and current value unknown.
When there are no offered fields or controls, the corresponding selection arrays are constrained
to be empty and the model must report the missing capability as a gap. Unreachable placeholder
enum values satisfy provider schema requirements but cannot be emitted in a valid nonempty list.
Freshness, clock and reset samples are summarized for the prompt; hashes identify the complete source
data in the local calibration record. Matching field IDs across separate captures does not establish
field identity, so reader freshness in this manifest is `null` with a reason.
Reuse is deliberately conservative: the calibrated entity, loaded-scene ID and binding descriptors
must still match. A different scene load order or hierarchy can therefore require a fresh capture;
the adapter does not infer identity from names.

The model may declare actions with up to three simultaneous physical inputs and bounded skills of
one to eight ordered hold/wait steps. These selections remain in the manifest even when they have no
execution evidence. `execution_supported` is true only when a completed execution record with
observed input release, an exactly matching recorded declaration (inputs, values, holds, waits
and `max_s`) and receipts for that declaration's ordered devices, controls, values, holds, chord
window and step waits is bound to it. Static backend capability, an individual key's separate
calibration or a schema shape never sets it. Unproven declarations stay explicitly false for a later
named preflight probe and are excluded from the exported action catalog. Code never removes or
replaces a selected action. The model's gap lists are reports relative to the offered and selected
evidence. Separate coverage facts describe harness limitations, offered and unoffered fields and
controls, and whether selection caps were reached. Reaching a cap does not prove truncation.

An action's `hold_effect` is the repeated entity-displacement result for a matching tested requested
hold, or null when no such result exists. It is not a general claim about other effects. A combined
input, a different value or a different hold has an unknown effect unless its own repeated evidence
exists. For a tap, the manifest records the tested *requested* hold rather than treating the shortest
observed applied duration as a requested minimum.

## Exploration coverage

The selection prompt asks the model to compare calibrated per-hold displacement when choosing
useful exploration steps, and to declare both signs of each two-way axis component it uses. The
manifest reports a coverage gap for each control and component with only one declared sign across
actions and skill steps. One-way triggers are excluded; opposite signs need not have equal magnitude,
and their presence does not prove either direction is executable or has a measured effect.

For a single-input action at the exact calibrated value and a tested hold, a separate coverage gap
reports when its repeated mean net entity displacement is **less than 25%** of that control's
largest repeated mean. Calibration must establish an effective response in at least one repeated
hold. The fixed threshold distinguishes the example's short steps (about 4–7% of the maximum)
from its longer steps (about 33–47%), without requiring the largest hold. It is a diagnostic, not
a locomotion classifier: other entity displacement can also trigger it, and net displacement does
not describe every effect. A chord, a different axis value, an untested or unresolved hold, or an
insufficiently repeated measurement remains unknown and cannot produce this comparison. A tap is
compared only if its tested requested hold was resolved. The validator recomputes these coverage
facts against the packet and unchanged selections; it does not reject a selection for either gap,
alter a hold, or add an input. Older manifests are checked against the current coverage rules;
an older manifest claiming a measured `hold_effect` for a different axis value also fails the
current cross-field validation and needs reassembly from its original selections and evidence.

## Turn coverage

Displacement says nothing about a look control: an input that only turns the view moves the entity
0. So calibration also measures how far the **game view** turns. The bridge reports the game-view
camera's world yaw and pitch on every observation (`Observation.view`, in degrees, `null` when
unknown; the camera is the first one the frame capture composites, see `bridge/README.md`).
Calibration reads it from the observation that binds each action and from one taken after the same
settle as the entity's read, and records the change per trial: yaw wrapped to [-180, 180), so a turn
across the seam is small, and pitch as a plain difference. A positive yaw turns right and a positive
pitch looks down. Each hold's `mean_view_turn` is the mean over its valid trials that read the view
both times, with the count and `spread_deg`, the largest difference between any two of those trials
along yaw or pitch; a view that was not read is `null`, never a turn of zero. The change is not
corrected for any turn the view makes with no input. Two readings cannot show a turn of 180 degrees or
more in one apply: it wraps and reads as a smaller one, and its repeats then scatter. On FPSKit a
right stick held 1.2 s read 37, 37, 72 and 62 degrees, a mean of 52 with a spread of 35, where the
holds up to 0.4 s read 18, 36, 72 and 144 degrees with spreads under 0.1. The spread is the signal, so
the mean is never presented without it.

The selection prompt shows `mean_view_turn` and asks for at least one coarse turn step, roughly 30
to 60 degrees, in each direction of every look axis. Fine steps may stay beside it. A held control
reaches a larger turn through its value or hold, scaled from the measured degrees per unit. A relative
impulse such as mouse delta takes a value of at most 1, so a coarse turn is several impulses as
ordered skill steps; the prompt says that such an estimate is not a measurement.

The manifest reports a coverage gap for a single-input action on a vector control (mouse delta, a stick
or a dpad: `LOOK_CONTROL_TYPES`) at the exact calibrated value and a tested hold whose repeated mean turn
is **below 20 degrees** (`MIN_USEFUL_TURN_DEG`) but at least 2 (`MIN_MEASURABLE_TURN_DEG`). A step's turn
is the larger of its absolute yaw and pitch changes. Below 2 degrees the input is not a look step; the
view must have been read in at least two valid trials (`MIN_TURN_TRIALS`) that agree, with a spread of at
most a quarter of the turn (`MAX_TURN_SPREAD_RATIO`); and a button or a trigger is not a look axis, whatever
it does to the view (on FPSKit holding the fire controls kicks the pitch 6 to 12 degrees by an amount that
differs from repeat to repeat). Otherwise, as for a view that was never measured, a different value, hold or
direction, a chord or a skill, the turn is unknown and yields no gap: unknown is not small. The gap is a
diagnostic. It does not reject a selection, alter a
hold, add a coarse step or say that a coarse step is missing; the validator recomputes it from the
packet and the unchanged selections, and manifests assembled before the view was measured have
no turn to compare and keep their coverage unchanged. Calibration declares one value per control, so
only that value's turn is known: for mouse delta, the effect of x = -1 and of a y impulse is unmeasured.

The manifest's Pydantic model is the source of `schemas/preflight_manifest.schema.json`. The exported
schema describes structural constraints; Python validation also checks cross-field rules, scoreable
checks, exact descriptor and calibration bindings, the source packet hash, and the accepted answers
in the local attempt ledger. The ledger is a consistency record, not a provider-signed attestation.
An invariant's cited source and claimed failure meaning are model proposals; schema and reference
validation do not establish that the source guarantees the rule.
Manifest and ledger files may contain game information and belong under `runs/`, not in the repository.

The packet hash identifies the particular serialized input. It is **not** the authored-structure
fingerprint or cache promised for stage 7. Selected readers are not yet installed in the live
observation source, the action catalog is not written by this command, and the selected actions and
reset are not verified by assembly itself. The manifest explicitly records these limits.
The C# catalog/execution consumer passes its shared fixtures; the stage 7 host consumes the
single-keyboard measurement subset. Python remains the authority for complete manifest validation.
The offline input probe can attach exact execution proof and export the supported subset; it
does not install readers or establish reset. See [input execution](input-actions.md).
The discovery collector does not yet emit callable reset-method candidates, so the manifest records
reset bindings as unknown with a reason; it does not claim that no such game method exists.

The ownership boundary is discovery descriptor → reversible selection ID → model answer → whole-stage
validation → unchanged selection in the manifest. A rejected answer is retained in the ledger and is
not an action. The manifest command does not build S1 choices or issue input; pending-help, escalation,
and actuator branches are therefore not applicable to this change. Live reader delivery and a live
manifest-backed trace are not yet available to audit.

## Commands

This command sends game source and a frame to Meta and makes paid Muse requests. Standard is the
default; `--model muse-spark-1.3-contributor` explicitly permits Meta to use the submitted content
for training. Supply a
budget and obtain approval before a sweep.
The maximum calls are the lower of the approved call count and three stages times one initial attempt
plus `--max-retries` (default 3). At least three calls must be approved.
`META_API_KEY` is read from the process environment. Output and ledger must be new files outside the
Unity project, and the screenshot must be a PNG or JPEG with a truthful capture description.
The approved call count and dollar estimate must cover the planned calls. The estimate uses the
recorded prices, 1 million input tokens and 32,000 output tokens per call; it is not a provider-side
charge limit. Each request is reserved in the ledger before sending, then followed by its result. An
unpaired reservation is an unresolved in-flight request, not a free or failed call.

```powershell
uv run playtest-preflight-manifest --project <game-project> `
  --inventory <survey.json> --discovery <discovery.json> --calibration <record.json> `
  --screenshot <frame.jpg> --screenshot-mime image/jpeg --screenshot-provenance <capture-description> `
  --output <runs/manifest.json> --ledger <runs/attempts.jsonl> `
  --approved-calls 12 --approved-estimate-usd 16.64

uv run playtest-validate-manifest --manifest <runs/manifest.json> --ledger <runs/attempts.jsonl> `
  --inventory <survey.json> --discovery <discovery.json> --calibration <record.json> `
  --screenshot <frame.jpg>
```

If a stage fails, the ledger remains and no manifest is written. The validator reconstructs the
packet, rechecks each attempt and compares the accepted selections against the manifest; success
does not verify a live reader, action effect, reset, or provider identity.
