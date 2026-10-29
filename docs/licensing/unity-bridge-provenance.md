# Unity bridge provenance check

## Scope

Checked commit `9e9b7c15a0e5930faf5103a8279c8ca069628fea` (2026-10-29): all 102 tracked `bridge/**/*.cs` files, 27,696 lines.

## Sources compared

A Unity project's package cache: C#-bearing packages `com.unity.2d.animation@15.1.0`, `com.unity.2d.common@14.0.1`, `com.unity.2d.pixel-perfect@6.0.0`, `com.unity.2d.psdimporter@14.0.3`, `com.unity.2d.sprite@1.0.0`, `com.unity.2d.spriteshape@15.0.3`, `com.unity.2d.tilemap@1.0.0`, `com.unity.bindings.openimageio@1.0.2`, `com.unity.burst@1.8.29`, `com.unity.cinemachine@3.1.7`, `com.unity.collections@6.5.0`, `com.unity.ide.visualstudio@2.0.26`, `com.unity.inputsystem@1.19.0`, `com.unity.mathematics@1.4.0`, `com.unity.nuget.mono-ce@1.11.6`, `com.unity.pipeline@0.9.0-exp.1`, `com.unity.recorder@5.1.6`, `com.unity.settings-manager@2.1.1`, `com.unity.splines@2.9.0`, `com.unity.test-framework.performance@3.5.0`, `com.unity.test-framework@1.7.0`, `com.unity.timeline@1.8.12`, `com.unity.ugui@2.5.0`.

Unity built-in package source (Editor 6000.5.4f1): `com.unity.2d.sprite@1.0.0`, `com.unity.2d.tilemap@1.0.0`, `com.unity.adaptiveperformance.apple@6.0.0`, `com.unity.adaptiveperformance.google.android@6.0.0`, `com.unity.adaptiveperformance@6.0.0`, `com.unity.collections@6.5.0`, `com.unity.entities.graphics@6.5.0`, `com.unity.entities@6.5.0`, `com.unity.mathematics@1.4.0`, `com.unity.multiplayer.center@1.0.1`, `com.unity.netcode@6.5.0`, `com.unity.physics@6.5.0`, `com.unity.render-pipelines.core@17.5.0`, `com.unity.render-pipelines.high-definition-config@17.5.0`, `com.unity.render-pipelines.high-definition@17.5.0`, `com.unity.render-pipelines.universal-config@17.5.0`, `com.unity.render-pipelines.universal@17.5.0`, `com.unity.rendering.denoising@1.0.5`, `com.unity.serialization@6.5.0`, `com.unity.shaderanalysis@1.0.0`, `com.unity.shadergraph@17.5.0`, `com.unity.test-framework@1.7.0`, `com.unity.transport@6.5.0`, `com.unity.ugui@2.5.0`, `com.unity.ui.test-framework@6.5.0`, `com.unity.visualeffectgraph@17.5.0`.

Owner prototype was compared separately for project-recordkeeping; its overlap is not third-party provenance.

## Method

Removed comments and whitespace, tokenized C#, and compared every 40-token window. Searched 15 unusual exact C# lines with `gh search code`, excluding this repository and its forks; additionally made four web queries for Stack Overflow and Unity docs/samples. Grepped bridge files for URLs, attribution and license markers; reviewed all bridge-touching commit authors and co-author trailers.

## Results

| Check | Count / result |
|---|---|
| Scope | 102 files; 27,696 lines |
| Normalized overlap | 147,536 bridge windows vs. 12,499,252 reference windows; 11,221 of 11,225 enumerated C# references produced tokens; 0 Unity-source matches, 27 prototype matches |
| Public code search | 15 queries, including a repeated query; 0 distinctive third-party matches (common API idioms only); 1 hit was prototype text |
| Stack Overflow / Unity web search | 4 queries; 0 exact distinctive passage matches; Unity docs describe the same public Input System APIs |
| Marker grep | 0 markers in C#; 1 `AGPL-3.0-only` package metadata hit |
| Git authorship | 28 commits; author Mason Morris; 5 co-author trailers, all Claude Opus 5.5 |

## Findings

- Prototype overlap: `bridge/Runtime/Unity/UnityCapture.cs:495` ↔ prototype `EditorBroadcast.cs:3114` (7 windows); `:496` ↔ `:3115` (1); `:498` ↔ `:3116` (4); `:510` ↔ `:3125` (1); `:515` ↔ `:3130` (4); `:516` ↔ `:3131` (1); `:517` ↔ `:3132` (6); `:518` ↔ `:3133` (3). These windows cover camera ordering and render-state handling and are consistent with prototype reuse, recorded in `PRIOR_WORK.md`. Bridge line numbers refer to the checked commit, before SPDX headers were added.
- No distinctive match to Unity package/built-in sources or external public code was found. The web search showed Unity documentation for general Input System event APIs, not matching bridge implementation passages.

## Conclusion

No distinctive third-party match was found by these checks. Four enumerated reference files produced no tokens or could not be read; the script did not distinguish those cases. The comparison only detects exact 40-token windows after normalization, and searches cannot establish that code was never adapted or taken from an unindexed source. On 2026-10-29, Mason Morris confirmed that all code in this repository is his to license, including the bridge code with prototype overlap.
