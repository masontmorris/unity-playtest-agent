# Run record and trace

Every run begins with one `RunRecord`, then appends typed trace entries. A trace entry has a
contiguous sequence id, run id, kind, author, optional turn, earlier-entry references, UTC wall time
and a kind-specific body. The entry model rejects forward references, invalid turn placement,
incorrect typed bodies and unknown closed vocabulary values.

`TraceWriter` creates its file exclusively, validates each entry before writing, flushes each JSONL
line and never rewrites earlier lines. It appends one end record when closed. Secret values are
redacted in serialized lines, and the end record records the number of replacements. The reader
validates every line and reports an incomplete or malformed last line. Non-strict reading returns
the valid prefix and tail error separately; invalid non-final lines remain fatal.

The run record builder measures the game commit and worktree state with Git and reads Unity and bridge versions from
the supplied bridge status. Modified paths are repo-relative and empty for a clean or unknown worktree. It receives the scene, manifest fingerprint, clock and input modes,
models and prompt hashes, objective, budget and reset method from its caller. It records the game
directory name, generated run id and installed core version. Missing bridge status and optional
inputs are written as `unknown`; the commit is `unknown` when Git cannot measure it. No absolute
project path is stored. The manifest fingerprint is whatever the caller supplies: `playtest-run` passes the
SHA-256 of the manifest file, not the preflight cache's authored-structure fingerprint
(see `turn-loop.md`).

Replay checks sequence and run identity, backward references, end placement, turn ordering and
completeness, required references, observation and choice-set binding, increasing set versions,
context hashing and contents, decision parsing, S1's pending-help policy, execution request and
terminal-record evidence, purpose and choice agreement, effect linkage, help request lifecycle and
adherence. The execution request body has the eight bridge request fields and excludes its wire
envelope. Replay reports discrepancies without repairing or choosing actions.

Replay cannot infer a missing terminal bridge record from evidence that was never written.
