# Third-party software and test subjects

This repository contains no third-party code or assets. Dependencies are referenced by version and installed by the package manager; they are not vendored.

## Runtime and development dependencies

| Package | Use | Licence |
|---|---|---|
| [pydantic](https://github.com/pydantic/pydantic) | Contracts and validation | MIT |
| [Newtonsoft.Json](https://www.newtonsoft.com/json) via `com.unity.nuget.newtonsoft-json` | Unity bridge wire serialization | MIT |
| [Unity Input System](https://docs.unity3d.com/Packages/com.unity.inputsystem@1.19/manual/index.html) via `com.unity.inputsystem` | Unity bridge keyboard input backend | Unity Companion License |
| [System.Reflection.MetadataLoadContext](https://www.nuget.org/packages/System.Reflection.MetadataLoadContext) | IL write scan tool: loads game assemblies for inspection only | MIT |
| [NUnit](https://github.com/nunit/nunit), [NUnit3TestAdapter](https://github.com/nunit/nunit3-vstest-adapter), [Microsoft.NET.Test.Sdk](https://github.com/microsoft/vstest) | .NET tests for the bridge harness and the IL write scan tool (dev) | MIT |
| [httpx](https://github.com/encode/httpx) | Model provider HTTP (optional `probe` extra) | BSD-3-Clause |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Local `.env` loading (optional `probe` extra) | BSD-3-Clause |
| [hatchling](https://github.com/pypa/hatch) | Build backend | MIT |
| [pytest](https://github.com/pytest-dev/pytest) | Tests (dev) | MIT |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | Exported-schema tests (dev) | MIT |
| [gitleaks](https://github.com/gitleaks/gitleaks) | Secret scan in CI and pre-commit (dev tool) | MIT |
| [pre-commit](https://github.com/pre-commit/pre-commit) | Local hooks (dev tool) | MIT |
| [FFmpeg](https://ffmpeg.org/) | Generates synthetic test-pattern frames and clips for the model probes (external dev tool, not distributed) | LGPL-2.1+ / GPL-2.0+ depending on build |

## Test games

The agent is tested on these open-source Unity games. They are **test subjects, not part of the Submission**. Their code and assets stay in their own repositories. Bugs are seeded into separate forks, which keep the original licence and copyright notice.

| Game | Author | Upstream | Code licence | Bundled third-party assets | Demo footage |
|---|---|---|---|---|---|
| FPS Kit | Thomas Iché | [peeweek/FPSKit](https://github.com/peeweek/FPSKit), test base `8965ad4` (19 Mar 2026) | MIT | The `SourceAssets/Models/YBot-Hands` model appears to derive from Mixamo's Y Bot. No Asset Store packs at the top level of `Assets/` | Planned. The Y Bot provenance must be confirmed first |
| DragonBlaze | Vaggelis Kavouras | [dacrab/dragon-blaze](https://github.com/dacrab/dragon-blaze) | MIT | Unity Asset Store packs: `Assets/Cainos`, `Assets/JMO Assets` | Pending the Asset Store EULA check (#43) |
| Street Racing | Lucas Coutinho | [is-cout/street-racing-unity](https://github.com/is-cout/street-racing-unity) | MIT | Asset packs under `Programming-Theory/Assets` (car, road and `BrokenVector` packs) | Kept out of the demo video |

The MIT licence covers each game's own code. It does not grant rights to bundled third-party assets, which are governed by their own terms.
