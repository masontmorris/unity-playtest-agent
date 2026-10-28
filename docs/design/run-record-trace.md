# Run record and trace

Every run begins with one `RunRecord`, then appends typed trace entries. A trace entry has a
contiguous sequence id, run id, kind, author, optional turn, earlier-entry references, UTC wall time
and a kind-specific body. The entry model rejects forward references, invalid turn placement,
incorrect typed bodies and unknown closed vocabulary values.

`TraceWriter` creates its file exclusively, validates each entry before writing, flushes each JSONL
line and never rewrites earlier lines. It appends one end record when closed. Secret values are
redacted in serialized lines, and the end record records the number of replacements. The reader
validates every line and reports an incomplete or malformed last line.

The run record builder measures the game commit with Git and reads Unity and bridge versions from
the supplied bridge status. It receives the scene, manifest fingerprint, clock and input modes,
models and prompt hashes, objective, budget and reset method from its caller. It records the game
directory name, generated run id and installed core version. Missing bridge status and optional
inputs are written as `unknown`; the commit is `unknown` when Git cannot measure it. No absolute
project path is stored.

Replay checks sequence and run identity, backward references, end placement, turn completeness,
observation and choice-set binding, increasing set versions, context hashing and contents, decision
parsing, turn record agreement, execution provenance, help request lifecycle and adherence. It
reports discrepancies without repairing or choosing actions.

Replay does not yet verify provider request hashes, provider response equivalence, effect semantics,
or the correspondence between execution purposes and all choice kinds. It also cannot infer a
missing terminal bridge record from a truncated execution body. These require stronger vocabulary
rules or additional measured facts in later trace entries.
