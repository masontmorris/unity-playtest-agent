# FPSKit preflight manifest

**Historical v1 run:** this run used a bridge-limited prompt that forced skills to be empty and did
not permit chords. The later v2 contract preserves those model selections instead. The v1 artifacts
and hashes remain local as evidence of this run, but the current v2 validator does not revalidate
the superseded v1 format.

## Revised v2 run

After the scope review, the Contributor chain was run again with chords and bounded skills available
to the model. This continuation used the remaining nine calls of the previously approved 12-call
envelope. It needed three calls, all accepted on their first attempt, with an estimated token cost of
**$0.0476946**. Together the two runs used six calls and an estimated **$0.0725293**; provider-reported
cost remains unknown. Every v2 call resolved to `muse-spark-1.3-contributor`.

The v2 manifest retains 16 readers, 13 actions, 3 skills, no progress fields and 1 model-proposed
invariant. Two actions are chords; five actions and all three skills are marked unsupported by the
current bridge. The 16-reader cap was reached, leaving 217 offered fields unselected. Coverage also
records 79 unoffered fields, two unoffered controls and 15 unsupported bindings. Reaching the cap
does not establish that the model's list was truncated.

`playtest-validate-manifest` passed with the Contributor model required, checking the v2 schema,
references, semantic consistency, source packet, reconstructed request hashes and recorded answers.
The v2 manifest SHA-256 is `0ba96c0b049d270996db5abcca97c3068df6450660311dba2915edca0bea8805`;
the ledger SHA-256 is `6ab692c22e7b4d00e7bdb2a9025daec5e494c6b6e78566c36a19cfa80dfca316`.
The full v2 records remain local under `runs/preflight/issue27-fpskit-02/`.

The invariant's cited source and claimed failure meaning are model proposals; type and reference
validation does not prove that the source guarantees the rule. Reader installation, action and skill
execution, effects and reset remain unverified. Callable reset candidates were not emitted by the
discovery collector, so reset binding selection is unknown rather than asserted absent.

## Historical v1 run

Issue #27. A single production preflight chain produced an FPSKit manifest from saved Editor and
calibration records. The user explicitly selected `muse-spark-1.3-contributor` and authorized the
run with the balance unverified. The approved envelope was 12 calls, estimated at $1.28 using the
recorded Contributor rates and the conservative token bounds; this is not a provider charge cap.
Contributor mode permits Meta to use submitted source, images, prompts and responses for training.

Three calls were made: readers, actions and progress each passed on the first attempt. Their
estimated token costs were $0.0232983, $0.0007038 and $0.0008326 respectively, or **$0.0248347**
total; provider-reported cost was unavailable. The model resolved to the requested Contributor
variant on all three calls. No quality comparison is claimed.

The accepted manifest has 16 readers, 8 actions, no skills, no progress fields and 3 invariants.
It preserves 4 unobservable, 4 uncontrollable and 3 unjudgeable gaps from the model, plus 11
coverage gaps. The model's gaps include missing visibility, controls and an objective measure;
these are its reported limitations, not an independent assessment of all game capabilities.

The source packet offered 233 fields and 8 measured keyboard controls; 79 unreadable fields were
recorded as unoffered. The game source excerpts, Editor snapshots and calibration describe FPSKit
`25e43fb`. The screenshot is a hash-checked frame from a later, separate FPSKit session: its file
modification time is known, but its capture game time is not. Reader freshness remains unknown because
field identity across the separately captured records is not established.

`playtest-validate-manifest` passed the schema, reference and semantic checks, reconstructed the
source packet and request hashes, and matched the accepted ledger answers to the manifest. This
checks internal consistency, not provider-signed provenance or game meaning. The local manifest
SHA-256 is `a629e29ac0bbea460db6fc923597b5cf9e82811afe158e37988160b1555bdaf9`; the ledger SHA-256
is `c5643195b509d8ec3df937c2cc092db46c19375e12370159352c63b51b4b9000`. Full records and image
remain local under `runs/preflight/issue27-fpskit-01/` and the source run directories.

This run did not install readers or an action catalog into the live bridge, exercise selected actions,
verify their effects, or verify reset. It produced no S1 turn or gameplay finding; live adherence and
reproduction review are not applicable. Those limits are recorded in the manifest rather than treated
as successful checks.
