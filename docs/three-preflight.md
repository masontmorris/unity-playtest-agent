# Prepare a three.js game for playtesting

`playtest-preflight-three` measures the game's declared readers and input catalog without a model call. It writes a separate attempt directory under the output path. A `complete.json` file marks a completed attempt. If preparation fails, inspect `study.json` and `session.partial.jsonl` in that attempt. A later invocation starts a new attempt.

Install the game's Node dependencies and Playwright browser first. Choose a declared single-input action that reliably moves one entity. For Kinema, run this command from the playtest repository in a headed GPU environment:

```powershell
$env:PLAYTEST_PAGE_GPU = '1'
$env:PLAYTEST_PAGE_HEADED = '1'
python C:/Users/mason/.gpu/gpu_lease.py run --holder upa-three-preflight --purpose 'Kinema preflight' --wait-minutes 45 -- uv run playtest-preflight-three --game 'C:\Users\mason\Documents\QQS Interactive\test-environments\Kinema' --entity-action move_forward --settle 60 --output runs/three-kinema
```

The catalog can contain at most 18 actions and 6 skills. The reader declarations can contain at most 16 readers. The command rejects larger catalogs before starting the game. It does not choose a subset.

The completed directory contains the saved study, manifest, calibration, entity, and catalog. The catalog contains only declarations with matching execution evidence. The calibration is inconclusive for freshness, clock, and reset. Empty model gap lists do not mean that the game has no gaps.

To start a hunt from a completed attempt, supply `--prepared <complete.json> --run --` followed by the runner's approval, model arm, call cap, and objective options. Prepared reuse requires the same clean checkout, declarations, and launch settings. It starts a fresh page on the exported catalog and checks the entity and readers before calling the runner. A hunt can make paid calls under the runner's existing approval and budget rules.
