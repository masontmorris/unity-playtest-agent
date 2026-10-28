# Unity bridge scripts

PowerShell scripts connect this repository's generic bridge package
(`bridge/`, package id `com.playtest.bridge`) to a Unity test project. The
bridge has no game-specific knowledge; the only project-specific values in
these scripts are the default paths for this worktree and the external test
fork, and both are overridable.

The package is referenced in place as a local UPM package. It is never copied
into the project's `Assets/`, and the scripts never write to `Assets/`,
`ProjectSettings/` or `packages-lock.json`.

## Wire the package into a test project

```powershell
powershell -File scripts/unity/wire-fpskit.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0"
```

`-ProjectPath` has no personal-path default; pass it or set
`FPSKIT_BRIDGE_PROJECT`. `-PackagePath` defaults to `bridge/` in this checkout.
Override either:

```powershell
powershell -File scripts/unity/wire-fpskit.ps1 -ProjectPath "C:\path\to\project" -PackagePath "C:\path\to\bridge"
```

What it does:

- Checks that the package directory exists and that its `package.json` declares
  `com.playtest.bridge`; it refuses to wire anything else.
- Sets `dependencies["com.playtest.bridge"]` in `Packages/manifest.json` to the
  absolute package path. The value looks like:

  ```json
  "com.playtest.bridge": "file:C:/path/to/checkout/bridge"
  ```

- Adds `com.playtest.bridge` to `testables`. A package's test assemblies are only
  compiled and discovered when the package is listed there; that covers both the
  EditMode and PlayMode suites.
- Prints the before and after value, the manifest path, and re-reads the file to
  confirm the value it wrote.
- Leaves existing entries and their order alone; only the two additions differ.

### Restore the project

```powershell
powershell -File scripts/unity/wire-fpskit.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0" -Remove
```

That removes the dependency and the `testables` entry (and the now-empty
`testables` array), returning the manifest to its original content. Run this
before handing the fork back or diffing it against upstream.

## Run the bridge tests

EditMode is the default:

```powershell
powershell -File scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0"
```

EditMode with a graphics device, which measures the capture self-test outside play mode:

```powershell
powershell -File scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0" -Graphics
```

PlayMode, which runs the measurement and capture tests against the project:

```powershell
powershell -File scripts/unity/run-editmode-tests.ps1 -ProjectPath "C:\path\to\FPSKit-bridge-v0" -TestPlatform PlayMode
```

Optional parameters:

| Parameter | Default |
|---|---|
| `-UnityPath` | `$env:UNITY_EDITOR`, then `C:\Program Files\Unity\Hub\Editor\6000.5.4f1\Editor\Unity.exe` |
| `-TestPlatform` | `EditMode`; `PlayMode` enters Play mode in batch mode |
| `-TestFilter` | none; passed through as Unity's `-testFilter` |
| `-ResultsPath` | `<repo>/runs/bridge-tests/<project>/<platform>-results.xml` |
| `-LogPath` | `<repo>/runs/bridge-tests/<project>/<platform>.log` |
| `-MutexTimeoutMinutes` | `15` |
| `-NoGraphics` | off for PlayMode (a graphics device is created); EditMode always uses `-nographics` unless `-Graphics` is passed |
| `-Graphics` | off for EditMode; runs EditMode with a graphics device for the capture self-test. PlayMode already defaults to graphics |

`<platform>` is `editmode` or `playmode`, so the two suites keep separate
result and log files. Relative result and log paths are resolved against the
project.

What it does, in order:

1. Requires `Packages/manifest.json` and `ProjectSettings/ProjectVersion.txt`,
   and requires valid JSON in the manifest.
2. Reads `m_EditorVersion` and requires it to equal both the editor folder name
   (`<version>\Editor\Unity.exe`) and the version reported by `Unity.exe`. It
   also cross-checks the revision in `m_EditorVersionWithRevision` when present.
   A mismatch is a setup error; pass `-UnityPath` for a different editor.
3. Refuses to run when `<ProjectPath>/Temp/UnityLockfile` exists, which means an
   Editor has the project open.
4. Sets `UPA_REPO_ROOT` to this repository's root so the EditMode fixture tests
   find `fixtures/contracts`.
5. Takes the named mutex `unity-playtest-agent-editmode-tests` (global, falling
   back to local), waiting up to `-MutexTimeoutMinutes`. The name is unchanged
   from the EditMode-only version so one run still excludes the other. A run
   that was killed leaves the mutex abandoned; the next run takes it over.
6. Deletes stale result and log files, then runs:

   ```
   Unity.exe -batchmode [-nographics] -projectPath <project> -runTests
             -testPlatform <EditMode|PlayMode> -testResults <results> -logFile <log>
             [-testFilter <filter>]
   ```

   `-nographics` is always used for EditMode and is used for PlayMode only with
   `-NoGraphics`. PlayMode defaults to a graphics device because the capture
   tests render to a render target. `-quit` is deliberately not passed; it is
   incompatible with `-runTests`.
7. Reads `<test-run result=... total=... passed=... failed=... skipped=...>`
   from the NUnit XML and prints one summary line plus the result and log paths.
   A missing `failed` attribute is reported as unknown and is not treated as
   zero. A missing total or a zero-test "Passed" run is a failure: zero tests is
   not evidence.

### What the PlayMode run measures

`-TestPlatform PlayMode` passes `-testPlatform PlayMode`, so Unity enters Play
mode in batch mode. Nine tests run against the project:

- **Stream** loads the first enabled scene from the project's Build Settings
  additively (the FPSKit test scene in that fork), makes it the active scene,
  starts the loopback server with the Unity status and observation sources,
  then subscribes with `observe` at 20 Hz. Additive loading keeps the test
  runner's own scene and controller alive; the loaded scene is unloaded again
  during cleanup.
- **Act** sends one bounded action through the same transport, measures that
  the controlled body moved, then sends a superseding action and repeats it
  from a stale observation to confirm code does not substitute a choice.
- **Clock** pauses, resumes, changes the time scale and steps the live game
  clock, reading each effect from the next observation rather than the
  acknowledgement.
- **Stop** holds a key through the transport, stops from the main thread as a
  scene change does, and checks the released state on the live device before
  the bridge closes.
- **Input release** applies a key through the Input System backend and checks
  that the release is queued, reported and verified as no longer pressed.
- **Capture self-test** renders a temporary camera and overlay canvas through
  the offscreen composite, decodes the JPEG and checks both halves.
- **Host status** starts the Editor host and reads `capture` and
  `capture_self_test` from its `status` reply.
- **Capture window** renders a hidden camera at 10 fps, trims the frame window
  and decodes a 640-wide frame.
- **Frame reference** observes with `with_frame`, checks that the observation
  names an existing JPEG, then checks that a request without it omits the field.

The capture tests need a graphics device. PlayMode runs without `-nographics` by
default for that reason. On a machine without one, pass `-NoGraphics`; the
capture tests then fail with the self-test's message instead of pretending
frames were captured.

The stream test measures, rather than assumes:

- 45-75 observations in a 3 s window and a 40-67 ms median interval (20 Hz
  +/- 25%; Windows timer resolution rounds a 50 ms wait to ~62.5 ms in batch
  mode), with no gap over 250 ms.
- The wall-clock age of each observation, which must stay at or under 150 ms.
- Strictly increasing `seq`, an advancing `frame`, the loaded scene name,
  `playing`, `time_scale`, a non-null `ui` object and `reader_errors` as an
  array.
- At most one in-flight observation after `observe unsubscribe`, then silence.

A project needs at least one enabled scene in Build Settings for the stream
test, or it fails with that message instead of skipping. Loading the scene
affects only the test run; it does not save anything into the project. The
one-Editor rule below still applies, so nothing else may drive the project
while the run is in flight. Expect a PlayMode run to take longer than EditMode:
Unity has to enter Play mode and load the scene before the 3 s measurement
window begins.

The default result and log paths live under the repository's gitignored
`runs/bridge-tests/`, not under `<ProjectPath>/Temp`. Unity clears its project
`Temp` directory on exit, which makes a `-testResults` file written there
disappear even though the log reports it was saved.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | result is `Passed`, `failed=0`, and at least one test ran |
| `1` | the run happened but did not pass, wrote no or unusable results, or ran zero tests |
| `2` | setup failed and Unity was not started |

On any non-pass result the last 40 lines of the Unity log are printed.

### One Editor per project

Only one Unity Editor may have a project open. Close the interactive Editor
before running the tests, and never run two bridge test runs against the same
project at once. The named mutex serializes test runs across processes, and the
`Temp/UnityLockfile` check refuses to start when an Editor already owns the
project. If an Editor crashed and left a stale lock file, delete
`<ProjectPath>/Temp/UnityLockfile` once you are sure no Editor is running.

## Fast Unity-free tests

The transport layer also has a plain .NET test project that does not need Unity
or the wiring above:

```powershell
dotnet test bridge/Tests/DotNet~ --artifacts-path "$env:TEMP\upa-dotnet-<name>"
```

## Capture preflight stages 0–1

After wiring this checkout's bridge into the project, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/unity/capture-preflight.ps1 `
  -ProjectPath "C:\path\to\game" -OutputDirectory "C:\path\outside\game\preflight-run"
```

The script runs an EditMode survey and then a PlayMode inspection of the first enabled build
scene, using the same version checks, project-lock check and mutex as the test runner. It writes
`survey.json` and `discovery.json` outside the game and refuses to overwrite them. The files may
contain game source; keep them local. Discovery includes every loaded scene, including Unity's
test-runner scene. Review `caps`, `errors` and IL issues before treating a sample as complete.
This captures code and live state only; it does not perform control calibration or call a model.

## Measure the controlled entity

With this checkout's bridge wired into the project, and no other Editor using it:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/unity/measure-controlled-entity.ps1 `
  -ProjectPath "C:\path\to\game" -OutputPath "C:\path\outside\game\entity.json"
```

This active probe loads the first enabled scene in Play mode. It discovers keyboard bindings on
vector-valued actions, takes a sampled idle baseline, and sends each supported key through the
bridge twice. It records execution and release, positions, limits, thresholds, and unsupported
vector bindings. Objects that move during the baseline are excluded, and responding descendants
are grouped under responding ancestors. Multiple independent responding objects remain ambiguous.
An independent Unity camera is recorded as a viewpoint when it has no discovered input or
physical body and its displacement matches a directly input-bound responder in two non-parallel
directions. The original responsive roots and the comparisons remain in the evidence; matching
motion does not prove a parent-child relationship.
The result uses Unity world coordinates and world units; their relationship to physical units is
unknown unless separately measured. The probe is not calibration, and it does not cover devices
the current keyboard backend cannot drive. Evidence and logs stay under `runs/` or another local
path outside the game; the output file is never overwritten. The script uses the same Editor lock,
version checks and mutex as the bridge tests. Unity may still update its own cache or package lock.
Without an explicitly requested measurement, the normal PlayMode suite checks that the preflight
host rejects a missing catalog; that check does not measure a controlled entity.

## Calibrate the controlled entity

After a completed stage 2 record, with this checkout's bridge wired into the project and no other
Editor using it:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/unity/calibrate-preflight.ps1 `
  -ProjectPath "C:\path\to\game" -EntityPath "C:\path\outside\game\entity.json" `
  -OutputDirectory "C:\path\outside\game\calibration-run"
```

The script refuses to run unless the game is wired to this checkout, the stage 2 record is
`identified`, and the output directory is outside the game and has no earlier `calibration.json`,
`reset.json` or `record.json`. In Play mode on the first enabled scene it re-discovers the keyboard
bindings, requires them to match the stage 2 record, and then measures:

- **Per key:** net entity displacement after each of five requested holds (0.05–1.2 s), twice each,
  against an idle baseline: mean, sample variance, the lowest tested hold that responded in both
  repeats, and inversion relative to the entity's own basis where the binding names a direction.
  The keyboard backend is binary, so a dead zone is recorded as unknown, not zero.
- **Per field:** freshness from repeated samples, in two idle windows (one before the key trials and
  one after) and in held windows taken while Unity's input update reports the key held. The longest
  hold is repeated four times per key to give more windows. A change is exact inequality between
  adjacent samples, compared component by component for vectors and quaternions; values the field
  reader cannot compare (structs, collections, masks, object references) stay `unknown`. Each field
  also gets a per-key split of the held windows, the keys whose windows showed a change, and the
  scopes where repeated windows disagreed.
- **Clock:** pause, a 0.2 s step and resume. Whether stepped mode is supported stays `null`,
  because animation, physics and game timers are not fully measured.
- **Reset:** three scene-reload trials. Each holds one of the entity's movement keys for 1.2 s
  through the same input backend the bridge uses (release verified), then unloads and reloads the
  scene and compares the entity position and sampled fields before and after. Only trials that moved
  the entity at least 1 world unit count, and the result is k of n for the position and per-field
  counts of how often each field changed and came back. Save/load and checkpoints are not exercised
  and stay `null`.

Game exceptions logged during a key's input are recorded as evidence, that trial is marked invalid
and the key is not pressed again. The script writes `calibration.json` (raw samples), `reset.json`
and the combined `record.json`. These can contain game field values, so keep them local. The
calibration is a code-driven probe: every action is a discovered key sent once through the bridge,
with release verified, and no model is called.

## Verify one manifest action and reset (stage 7)

With a saved manifest and the calibration record it was built from, this checkout's bridge wired
into the project, and no other Editor using it:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/unity/verify-preflight.ps1 `
  -ProjectPath "C:\path\to\game" -ManifestPath "C:\path\outside\game\manifest.json" `
  -CalibrationPath "C:\path\outside\game\record.json" -Action <manifest action name> `
  -OutputPath "C:\path\outside\game\stage7.json"
```

The caller names exactly one manifest action; the script never picks one. It must be a single
button with a repeated effective calibration hold. In Play mode on the first enabled scene the
Editor hosts a one-entry catalog with that action's name, control and hold, and the Python client
executes it once, without retry or substitution. The measured world-space effect is compared with
the calibration hold's mean, within the hold's measured spread plus a predeclared allowance for
two reads. If the bridge-measured hold differs from the matching calibration trials' applied holds
by more than 10 ms, the input was not reproduced and the effect is inconclusive. The client then asks the Editor for one scene reload through handshake files next to
the output, and compares the reloaded entity position and sampled fields with their pre-action
values. Only fields that calibration's reload trials restored every time, at least twice, decide
the field outcome; the rest are reported with their calibration counts. The report is `verified`,
`different` or `inconclusive`, and the script fails on anything but `verified`. It never writes to
the game, refuses existing output or handshake files, and uses the same Editor lock, version checks
and mutex as the bridge tests. A scene reload sample is not full game-state restoration.
