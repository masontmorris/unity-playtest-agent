# Preflight selection cache

The cache reuses the model's accepted stage 4–6 selections, not the observations or the
calibration from the original run. A hit reconstructs a manifest from this preflight's own
inventory, discovery and calibration. It remaps only within-session field IDs to the same
frozen scene, object, component and field descriptors. Every action, chord, skill, reader,
progress signal, invariant and model-reported gap is replayed exactly as the model declared it,
including declarations with no execution evidence; execution support, evidence, hold effects
and reader kinds are re-derived from the current packet, so neither a cached calibration nor
cached execution receipts are presented as a fresh proof. A declaration changed in any input,
value, hold, wait or `max_s` no longer matches old evidence, and the replay returns it as
unproven. A
failed resolution rejects the entire hit. No provider request is made on a hit. Stage 7 is a
separate measurement, and a hit does not assert that an action or reset was verified in the new
session.

The key hashes the contents of the project's `Assets`, `ProjectSettings`, `Packages`, and
local package dependencies, including scenes, prefabs, scripts, input assets and settings.
Local package paths are normalized while their contents and logical package names are retained.
It also covers the Unity version, the input handling setting and inventory structure, normalized
binding descriptions, the discovery's IL evidence, the requested model, manifest and answer
schemas, preflight prompt and policy code, and inspection cap limits. Runtime field values,
screenshots and transient Unity object IDs are not part of the key. Hashing all authored files
can cause a conservative miss when an unrelated asset changes. Links and junctions inside the
scanned trees, unreadable files and unresolved local packages stop capture.

The source run is eligible only if before and after snapshots bracket its inspections,
calibration, manifest and attempt ledger and the authored files match. Neither inspection may
report an error or a truncated cap, and every cap outside the IL scan must report complete
coverage. The IL scanner records any indirect field address, which ordinary struct fields
produce, as an issue and then marks its write and issue caps uncertain; that is allowed only
when the issues are recorded. It is a property of the compiled code, so the IL evidence is
hashed into the key instead, which also catches compiled code that differs while the authored
files do not. The source manifest and every ordered reservation and attempt are revalidated
against the original packet and the required model, and replaying the selection against the
source's own packet must reproduce the manifest exactly. An incomplete result is never cached:
an offered analog axis that no declared action drives stops admission and replay. Controls the
bridge could not offer, chords and skills with no exact execution evidence, and holds with no
measured effect are recorded gaps instead, re-derived from each run's packet; a new model answer
could not close them.

A hit additionally requires a new completed calibration from a different session, and an
inventory and discovery captured after the source preflight. This preflight's before snapshot
and a snapshot taken when the manifest is requested must bracket them and match. The stored
entity, reader and binding descriptors must resolve uniquely in this preflight's discovery, and
each reader's current kind must match; a reader that was null at discovery resolves only while
it is still null. The cache-hit ledger stores the fingerprint, the source hash, the resolved IDs
and both authored snapshots. Validation reconstructs the source ledger and current manifest; these
local records are consistency evidence, not cryptographic attestations from Unity or the model
provider. If anything is missing or changes, it is a cache miss. A miss cannot call a model unless
the caller supplies an approved call and cost envelope.

## Local workflow

All paths below are outside the Unity project. The cache and run records can contain game
source and stay local under `runs/` or another untracked directory. Capture the **before**
snapshot before the first inventory, discovery and calibration; after stages 4–6 finish,
capture the **after** snapshot, then seal their validated manifest and ledger. Unity rewrites
`Packages/packages-lock.json` the first time it opens a newly wired project, so open the project
once after wiring and before the first snapshot.

```powershell
uv run playtest-preflight-cache snapshot --project <game> --output <run/before.json>
# Run capture-preflight.ps1, measure-controlled-entity.ps1 and calibrate-preflight.ps1.
uv run playtest-preflight-manifest --project <game> --inventory <run/survey.json> `
  --discovery <run/discovery.json> --calibration <run/record.json> `
  --screenshot <run/frame.jpg> --screenshot-mime image/jpeg --screenshot-provenance <description> `
  --output <run/manifest.json> --ledger <run/attempts.jsonl> --model muse-spark-1.3-contributor `
  --cache-dir <local-cache> --current-before <run/before.json> `
  --approved-calls <n> --approved-estimate-usd <usd>
uv run playtest-preflight-cache snapshot --project <game> --output <run/after.json>
uv run playtest-preflight-cache store --project <game> `
  --before <run/before.json> --after <run/after.json> `
  --inventory <run/survey.json> --discovery <run/discovery.json> --calibration <run/record.json> `
  --manifest <run/manifest.json> --ledger <run/attempts.jsonl> --screenshot <run/frame.jpg> `
  --model muse-spark-1.3-contributor --cache-dir <local-cache>
```

A later preflight repeats the snapshot and the three Editor stages, then runs the same manifest
command. On a hit it writes the manifest and a one-line `cache_hit` ledger with zero model calls
and needs no spending approval; on a miss it spends only within `--approved-calls` and
`--approved-estimate-usd`, and refuses to call a model without them. Validate a hit with:

```powershell
uv run playtest-validate-manifest --manifest <run2/manifest.json> --ledger <run2/attempts.jsonl> `
  --inventory <run2/survey.json> --discovery <run2/discovery.json> --calibration <run2/record.json> `
  --screenshot <run2/frame.jpg> --expected-model muse-spark-1.3-contributor `
  --project <game> --cache-dir <local-cache>
```

## Stage 7

Stage 7 executes one explicitly named manifest action once through the bridge, compares its
measured world-space effect with the same control's repeated calibration hold, then reloads the
scene once and compares the sampled entity position and comparable fields with their pre-action
values. Both halves are judged against calibration: the effect must fall within the hold's
measured spread plus a predeclared measurement allowance, provided the bridge-measured hold is
within 10 ms of the matching calibration trials' applied holds; a longer or shorter hold is a
different input, so its effect is inconclusive rather than matched or different. The reload must
restore the entity
position and every comparable field that calibration's own reload trials restored each time, at
least twice. Fields that calibration saw come back differently, such as a frame counter or an
activation id, are still compared and reported but cannot decide the outcome; fields without
repeated calibration evidence stay unknown. The caller names the action; nothing chooses,
substitutes or retries it. A reload sample is not full game-state restoration, and save/load and
checkpoints stay unknown.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/unity/verify-preflight.ps1 `
  -ProjectPath <game> -ManifestPath <run2/manifest.json> -CalibrationPath <run2/record.json> `
  -Action <manifest action name> -OutputPath <run2/stage7.json>
```
