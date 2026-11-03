# Mammoth image-path correctness check

Status: **complete, 2 November 2026; both checks passed on all four units.**
Results are in `docs/probes/2026-11-02-mammoth-modal-fast-pilot.md`. The user asked to test the
corrected image path and confirmed the previously stated **30 HTTP calls / $15
additional** envelope; the prior Modal balance statement exceeded $100. This
changes the previous follow-up's comparison from planner count to image-path
correctness. Do not claim this tests Baseten's opaque renderer.

## Decision and frozen comparison

The decision is whether the corrected Flide image path agrees with the local
published-runtime reference closely enough to justify a larger validation.
The unit is one frozen FPSKit request. Two arms run one H200 each, one after
the other, on US East private Modal Servers with BF16, the same pinned Mammoth
revision, single concurrency and no session cache. The arm variable is the
serving implementation: Flide 0.1.7 versus the previously piloted patched
OneJev multimodal reference. Models, rendered prompt, choices, image bytes and
processor settings must match; record any dependency differences.

Four units: the corrected short and long image contexts, plus the same two
contexts with the image replaced by a black 640×480 JPEG. The black-image
controls deliberately keep all state text, including its original image hash,
unchanged to isolate the image itself; those controls are diagnostic only and
must never be executed as game decisions. Verify the real JPEG hashes against
the traces and record both control hashes. The corrected Flide and reference
transport encodings differ, so verify their **rendered** prompt text, image
position, options and token IDs, not merely JSON-object equality. If the
rendered prompts differ in this engine comparison, stop and mark it inconclusive.

Before first paid inference, freeze input, source, dependency and model hashes,
arm and case order (seed 20261102), prompt hashes and transport mapping. Per arm
send three warmups, then three repeats per unit, randomized with the same seed:
**15 requests per arm, 30 HTTP inference requests total**. Reference startup
must not add its old three synthetic inference calls; client warmups replace
them. Flide CUDA-graph capture can execute internal model operations; account
for its time/cost separately. Do not run automatic inference retries. No action
or escalation is executed from any response.

The CPU audit used the exact two container images from the previous deployments and no
model weights. For all four units, expanded IDs, prompt hash, block boundary,
grid and float32 pixel tensor matched exactly, including 300 image tokens.
Its temporary Volume was deleted. The GPU runs additionally log hashes of the
actual reference tensors and compare Flide's actual GPU pixel tensors with its
CPU processor output. This instrumentation can change latency; these are
correctness observations, not a latency comparison.

## Measurements and rule

For each unit, collect both runtimes' rendered prompt hash, complete expanded
token-ID sequence hash, prefix/block boundary, image grid, decoded image hash,
processor pixel-tensor shape/dtype/hash and numerical difference if available.
Capture actual values from the same code path as inference rather than a second
inference. Log one record per call, including invalid/error responses. Hashes
and compact measurements may leave the container; raw frames and tensors stay
local and are never committed. For probabilities, record every option, the
selected choice, repeats and within-arm noise.

The structural check passes for a unit only if prompt text and expanded token
IDs, image grid and relevant block boundaries match exactly, there are 300 image
tokens for 640×480, and corresponding pixel tensors agree within absolute
`1e-5`; missing tensor or boundary evidence is **unknown**, not a pass. The
decision-output check requires valid offered choices on all repeats, matching
top choices for real-image pairs when the reference top-two margin exceeds 0.05,
and per-option absolute probability differences no larger than 0.05. Compare
real and black controls with the text fixed; a changed probability or choice is
evidence of image sensitivity, while no change is inconclusive rather than proof
of a broken vision path. A small two-image sample cannot prove gameplay quality,
full model equivalence, or Baseten equivalence. Report all repeat differences,
attempted/succeeded counts, missing evidence and `inconclusive` as needed.

## Budget and stop

Two H200 arms, at most 25 GPU minutes each, with four CPU cores, 64 GiB RAM and
a conservative 1.75 US East multiplier: approximately $7.64 for requested GPU,
CPU and RAM, plus image builds, checkpoint download, storage and network. Stop
before the **$15 additional** envelope is at risk; usage reports may settle
late, so this is an operator limit rather than an enforced cap. Stop on a failed
inference, auth/billing failure, prompt mismatch, unexpected restart, or the
25-minute GPU deadline. Shut down all pilot apps, delete only pilot-created
tokens and cache volumes, and verify no pilot containers remain. The existing
user app is outside this test.
