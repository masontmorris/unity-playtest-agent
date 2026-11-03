# Mammoth-1 on Modal: latency pilot proposal

Status: **completed, with the Baseten arm omitted**. See the
[pilot record](../probes/2026-11-02-mammoth-modal-fast-pilot.md). This was a
bounded pilot for choosing a next deployment to investigate, not a claim that
any configuration is fastest.

Execution note, 2 November: the user approved the proposed $20 ceiling and
reported more than $100 in Modal credits. The Baseten balance is not verified,
so omit that arm. The remaining maximum is 45 HTTP inference requests plus the
three reference-runtime startup warmups, 48 explicit calls. This reduces the
approved count and cannot provide a contemporaneous Baseten comparison. No paid
call had been made when this note was added.

Pre-inference setup correction: the first cloud image build succeeded but the
server definition was rejected because the requested ephemeral disk size was
below Modal's minimum. No GPU container or model call started. The prototype
now leaves ephemeral disk at Modal's default. The failed setup and its roughly
$0.01 metered charge remain in the local ledger. This correction does not add
an inference call or change the approved $20 ceiling; inference retries remain
disallowed.

## Candidate

Try [Flide 0.1.7](https://docs.flide.org/models/mammoth/) with the BF16
`baselabs/mammoth-1` checkpoint at
`61cec44c281c693992e3d6ebeb44dd3402dfa186`. Flide documents Mammoth's trained
block-bidirectional mask, slot readout and image support, and reports an H200
median of 110.3 ms on its image GQA workload versus 611.4 ms for the release
engine. These are the engine authors' numbers, not this project's measurements.

Use a private, authenticated Modal Server, with its compute and routing regions
both set to `us-east`, and one container at a time. Run separate Flide H200 and
B200 arms. Keep a container warm only for its bounded session, then stop it.
Pin Flide and all resolved dependencies/image digests before running, record
actual GPU, CUDA and kernel builds, and retain BF16, default debias=1, batch
window zero, one in-flight request and no session cache for the initial test.
Do not silently fall back to a different GPU, precision, engine or model.
The prior publisher-runtime H100 pilot is in the separate local worktree's
`docs/probes/2026-10-28-mammoth-pilot.md`; its region-corrected server has not
been GPU-tested. Do not copy its historical approval to this pilot.

## Protocol

Decision: is a Flide configuration worth a larger project-specific validation,
and which of H200 or B200 should be measured next? Unit: a frozen request body
constructed from an actual recorded S1 context and, where relevant, its game
frame. Select four available contexts before running: short text, long text,
short plus image, and long plus image. Record hashes, actual rendered token
counts, option order, source commit and image provenance. If representative
contexts or frames are unavailable, stop rather than substitute synthetic inputs
and call them production workloads.

Recent Liquid d1-ultra sessions reported roughly 20,000–23,000 tokens per turn
([record](s1-token-budget.md)); those are Liquid token counts, not Mammoth token
counts. Mammoth's published maximum sequence length is 16,384 tokens, so check
each frozen context with its tokenizer and image expansion. An oversized context
is a measured applicability limitation; do not truncate it to make it fit.

Four endpoint configurations: Flide/H200, Flide/B200, the pinned published
OneJev runtime/H200 as a local reference, and Baseten's Mammoth API. For each,
send three warmup requests and then each of the four contexts three times:
**15 HTTP inference requests per arm, 60 total**, no retries. The existing
OneJev server also performs three native startup warmups if reused: **63 explicit
inference requests total**. Flide kernel compilation and CUDA-graph capture can
execute internal model operations; record them separately and include their
time and cost, rather than counting them as client requests. If the actual
startup behavior adds explicit model calls, stop and revise the plan before
spending further. Randomize measurement order within each arm, record seed and
wall times, and avoid overlapping GPUs. Keep connections persistent and use the
same client machine and settings.

The independent variable for H200 versus B200 is GPU type; all other Flide
settings and semantic inputs stay fixed. The OneJev and Baseten observations
also change serving implementation and are separate comparisons. For the
OneJev comparison, verify rendered prompt tokens, block boundaries, image
preprocessing and option order agree before interpreting probability or latency
differences as engine effects. Baseten's internal renderer and resolved weights
may be opaque; if equivalence cannot be verified, its result is an operational
endpoint observation, not proof of equivalent internal computation or quality.
Flide expects `media` plus `<image:N>` markers, while Baseten accepts images in
`state.messages`: a transport adapter must preserve the input, and its rendered
equivalence must be verified, not assumed. Do not alter the production S1
adapter or execute any returned choice in this pilot.

Primary measurement: client-observed warm request latency, per context and arm,
with matched request ordering and successful/attempted counts. Also retain
server queue/planning/compute timings where available, cold-start time, actual
region, cost and input token counts. Report each context's median and individual
samples; three repeats are inadequate for a tail-latency claim. Record offered
choice validity, top-choice agreement and per-option probability differences,
including within-arm repeat noise, without treating agreement as proof of game
quality. A valid response must select an offered ID and return finite probabilities
for exactly the offered IDs summing to 1 within 0.001. For the two Flide GPU
arms, nominate the lower median of the four per-context median latencies for a
larger trial only if every measured response is valid and no selected choice
differs from the OneJev reference on a context whose reference top-two probability
margin exceeds 0.05; report all other flips and probability deltas. If prompt or
image equivalence cannot be verified, or any required result is missing, the
nomination is inconclusive. This rule is a pilot screen, not an equivalence or
quality guarantee. A failed or missing response is unknown and remains in the ledger.
This pilot may be inconclusive; no production switch follows automatically.

## Proposed budget and stop

Request approval for a **$20 combined ceiling**, including image build, all
startup, GPU, CPU, RAM, storage, egress, Baseten calls and shutdown. At Modal's
published rates on 2 November 2026, 30 minutes each of H200, B200 and H200 is
about $7.66 in GPU time at base rates; with a conservative 1.75 region multiplier
it is about $13.41 before CPU, RAM, build, network and storage. The OneJev
container requests four physical CPU cores and 64 GiB: base pricing adds about
$0.70/hour per such container before regional multipliers. Baseten publishes
$0.10 per million input tokens; 15 requests capped at 16,384 input tokens each
would be about $0.025. The $20 ceiling is an operator stop rule, not a
provider-enforced hard cap; real usage can exceed estimates during delayed
billing or shutdown. Verify current rates, the actual build resource limits and
available balances before running. Stop on an inference, auth,
unexpected restart or billing error, approaching the budget, or 30 minutes per
GPU arm; stop all created apps and verify no containers remain. Do not retry or
switch hardware without a new plan and approval.

Sources: [Modal pricing](https://modal.com/pricing), [Modal GPU support](https://modal.com/docs/guide/gpu),
[Modal Server](https://modal.com/docs/examples/server),
[Baseten pricing](https://www.baseten.co/resources/changelog/elephant-1-and-mammoth-1-pricing/),
[Flide Mammoth](https://docs.flide.org/models/mammoth/), and
[Mammoth model card](https://huggingface.co/baselabs/mammoth-1).

## Proposed follow-up: image-planner latency

This is **not yet approved**. The decision is whether a single planner is worth
further validation for a single-client B200 deployment. Compare Flide B200 with
`--planners 4` versus `--planners 1`, in a randomized arm order. Keep checkpoint,
dependencies, GPU, region, dtype, four frozen contexts, client, request bodies,
and all other settings fixed. Per arm send three warmups (one short text, one
short image, one long image), then three measurements of each context in a
seeded randomized order: **15 HTTP calls per arm, 30 total, no retries**.
Account for graph-capture work separately. The former pilot's text-only warmups
do not replace these predeclared calls.

Regenerate the two Flide image bodies with the corrected state-before-image
serializer and freeze their new hashes before running. The historical Flide
bodies put the image before the state and are not equivalent to the reference
arm; keep those artifacts unchanged. Check the actual rendered prompt and
image preprocessing rather than treating equal token totals as equivalence.

Primary outcome is the paired difference in the median client latency for the
two image contexts, with individual samples and Flide `plan_s` reported.
Nominate one planner for a larger trial only if both image-context medians are
lower than the four-planner arm and all measured responses are valid offered
choices; otherwise call the result inconclusive. Record choice flips and
per-option probability changes, and do not infer game quality or tail behavior
from three repeats. Stop at the first inference, auth or billing failure, an
unexpected restart, or a 25-minute GPU deadline per arm. Shut down the apps and
delete their temporary tokens and cache volume.

Proposed maximum is **$15 additional metered spend**, keeping the prior observed
$3.40 plus this follow-up below the existing $20 overall ceiling. At published
rates, two 25-minute B200 sessions with four CPU cores and 64 GiB RAM at a
conservative 1.75 region multiplier are about $10.14 before build, storage and
network. The remainder is contingency; this is not a provider-enforced cap.
Recheck the billing summary and available balance, freeze the implementation
and inputs, and obtain approval of this call count and envelope before starting.
