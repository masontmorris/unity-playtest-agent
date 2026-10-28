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

The model may declare actions with up to three controls and bounded multi-step skills. These selections
remain in the manifest even though the current bridge can execute only one keyboard control at a time
and cannot execute skills. Each declaration records that bridge capability separately; code never
removes or replaces a selected action. The model's gap lists are reports relative to the offered and
selected evidence. Separate coverage facts describe harness limitations, offered and unoffered fields
and controls, and whether selection caps were reached. Reaching a cap does not prove truncation.

An action's `hold_effect` is the repeated entity-displacement result for a matching tested requested
hold, or null when no such result exists. It is not a general claim about other effects. For a tap,
the manifest records the tested *requested* hold rather than treating the shortest observed applied
duration as a requested minimum.

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
reset have not been verified in a live run. The manifest explicitly records these limits. No C#
manifest consumer exists yet; shared-fixture parity remains pending.
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
