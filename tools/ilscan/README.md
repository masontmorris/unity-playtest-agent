# IL write scan

`Playtest.IlScan` records which methods directly store which fields in a Unity project's
compiled script assemblies. Preflight stage 1 uses it as evidence of how a field changes. The
tool loads assemblies with `MetadataLoadContext`, for inspection only: it never executes them
and needs no Unity. This used to run inside the Editor. It moved out of the Unity bridge
package (`bridge/`, #118) for two reasons. That package alone is being relicensed (#119). And
the same scan can later read a player build (#97).

The tool stays under the repository's AGPL-3.0-only licence. #119 changes the licence of the
Unity bridge package only. It does not change this tool's licence, and other bridges are licensed
case by case. The tool compiles one file from the Unity bridge package,
`bridge/Editor/Preflight/ReflectionIds.cs`, which derives type and field ids, so discovery rows
and write sets name each declaration with the same id.

## How preflight runs it

1. The bridge's `discovery` stage reports what to scan as `il_inputs`: the assemblies that
   contain authored scripts, every assembly file the Editor loaded, and whether that selection
   was complete. The `il_script_files` and `il_assemblies` caps stay in the snapshot.
2. `scripts/unity/capture-preflight.ps1` saves that snapshot as `discovery-bridge.json`. It then
   runs `python -m playtest.preflight.il`, which calls this tool and replaces `il_inputs` with
   the tool's sections in `discovery.json`. `playtest-inspect --stage discovery` does the same.
3. The tool adds the data sections `il_write_sets`, `il_unlisted_writes`, `il_issues`,
   `il_scope` and `il_complete`, the caps `il_types`, `il_fields`, `il_methods`, `il_writes`
   and `il_issues`, and any errors. These are the same sections and limits the Editor used to
   produce.

The .NET 10 SDK must be on `PATH`. To run the tool by hand:

```powershell
dotnet run --project tools/ilscan/src/Playtest.IlScan.csproj --configuration Release -- `
  --input il_inputs.json --output il-scan.json
```

The input has exactly the keys `assemblies` (`[{"name", "path"}]`), `references` (paths) and
`complete` (bool). The output must not already exist. Exit code 0 means the document was
written, 2 means the arguments or inputs were invalid, and 1 means the scan failed.

## What it measures

The scan reports direct `stfld` and `stsfld` stores, and the fields declared by the scanned
types. It never classifies a field as configuration or unused. The following are recorded as
issues, never read as "no writes": every address of a field taken by `ldflda` or `ldsflda`,
every body without IL (P/Invoke, internal calls, a delegate's runtime methods), every field
token that cannot be resolved, and every body that cannot be decoded. Any issue, any cap and any
incomplete input leaves `il_complete` false, and every later count unknown.

`MetadataLoadContext` cannot resolve tokens itself, so `MetadataFieldResolver` reads the
metadata tables directly. It follows the rules of Mono, the Editor's runtime:

- An assembly reference resolves by simple name to the file the Editor loaded.
- A type reference resolves by namespace and name, following type forwarders.
- A field reference looks its field up by name and signature on its parent type, then on the
  parent's base types.
- A generic type instantiated over its own type parameters is its generic type definition. A
  store inside `Foo<T>` therefore names the field `Foo<T>` declares.

## Tests

```powershell
dotnet test tools/ilscan/tests/Playtest.IlScan.Tests
```

CI runs this. The fixture assembly targets .NET Standard 2.0 with C# 9, like a Unity script
assembly. It covers generic types and methods, nested and compiler-generated types, closed
instantiations, other assemblies, struct fields and bodies without IL. The parity tests require
the document from inspection-only loading to equal the one from live reflection with runtime
token resolution, which is the Editor's former method.

The same comparison was also run under Unity 6000.5.4f1's own Mono runtime
(`MonoBleedingEdge`). It ran the original bridge scanner at `a13278f` on that fixture, and the
documents were identical.
