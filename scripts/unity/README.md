# Unity bridge scripts

Two PowerShell scripts connect this repository's generic bridge package
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

PlayMode, which runs the measurement test against the project's scene:

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
   Unity.exe -batchmode -nographics -projectPath <project> -runTests
             -testPlatform <EditMode|PlayMode> -testResults <results> -logFile <log>
             [-testFilter <filter>]
   ```

   `-quit` is deliberately not passed; it is incompatible with `-runTests`.
7. Reads `<test-run result=... total=... passed=... failed=... skipped=...>`
   from the NUnit XML and prints one summary line plus the result and log paths.
   A missing `failed` attribute is reported as unknown and is not treated as
   zero. A missing total or a zero-test "Passed" run is a failure: zero tests is
   not evidence.

### What the PlayMode run measures

`-TestPlatform PlayMode` passes `-testPlatform PlayMode`, so Unity enters Play
mode in batch mode. The PlayMode suite loads the first enabled scene from the
project's Build Settings additively (the FPSKit test scene in that fork), makes
it the active scene, starts the loopback server with the Unity status and
observation sources, then subscribes with `observe` at 20 Hz. Additive loading
keeps the test runner's own scene and controller alive; the loaded scene is
unloaded again during cleanup.

It measures, rather than assumes:

- 45-75 observations in a 3 s window and a 40-67 ms median interval (20 Hz
  +/- 25%; Windows timer resolution rounds a 50 ms wait to ~62.5 ms in batch
  mode), with no gap over 250 ms.
- The wall-clock age of each observation, which must stay at or under 150 ms.
- Strictly increasing `seq`, an advancing `frame`, the loaded scene name,
  `playing`, `time_scale`, a non-null `ui` object and `reader_errors` as an
  array.
- At most one in-flight observation after `observe unsubscribe`, then silence.

The test sends no `stop`; that verb belongs to a later issue. A project needs
at least one enabled scene in Build Settings, or the test fails with that
message instead of skipping. Loading the scene affects only the test run; it
does not save anything into the project. The one-Editor rule below still
applies, so nothing else may drive the project while the run is in flight.
Expect a PlayMode run to take longer than EditMode: Unity has to enter Play
mode and load the scene before the 3 s measurement window begins.

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
