# Mammoth-1 on Modal: Flide H200/B200 pilot

**Image comparison correction, 2 November:** A later offline audit found that
the Flide image requests placed the image placeholder before the state, while
the reference adapter placed it after the state. Canonical JSON sorting caused
the order change. Therefore the image-arm comparisons against the reference
changed the rendered prompt and must not be interpreted as controlled
engine-to-engine comparisons. The Flide H200 and B200 used the same image
prompt as each other. The preparation script now preserves the reference's state
order. The corrected path was then GPU-tested (see "Corrected image path"
below). Corrected local inputs have been regenerated separately; the text
bodies and reference bodies are unchanged, and both JPEG hashes match the
historical inputs. Historical run artifacts are unchanged.

On 2 November 2026, the B200 had the lowest observed warm text-request medians:
226 ms at 4,196 input tokens and 387 ms at 7,476. Flide on H200 took 379 ms and
649 ms on those respective contexts; the publisher-runtime reference on H200
took 562 ms and 1,070 ms. Three measurements per context are a pilot, not a tail
latency estimate or proof of a fastest configuration. No current Baseten arm ran.

## Inputs and setup

The four contexts were selected before the calls from two recorded FPSKit
sessions: turns 0 and 89 from `issue138/run-01` without images, and turns 0 and
37 from `b04-hunt/session-15` with the exact recorded JPEGs. Each image matched
the SHA-256 recorded with its context. The payload, choices and instructions
were retained; the image-bearing requests used each server's required media
transport. Raw inputs, manifests, ledgers, logs and frozen source hashes remain
local under `runs/mammoth-modal-fast-pilot-20261102/`.

All three arms used `baselabs/mammoth-1` revision
`61cec44c281c693992e3d6ebeb44dd3402dfa186`, BF16, one request at a time, one
GPU, Modal Server with private proxy authentication, US East compute and
routing, and the same client machine. Flide was 0.1.7 with pinned Python
dependencies, default four planning threads, session cache off and batch window
zero. The reference was the published OneJev runtime with the project's separate
multimodal mask wiring and a full-prompt forward per question. Flide used a
different image transport and runtime; the baseline API was not Baseten.

The user approved the proposed $20 envelope and stated that Modal credit exceeded
$100. The Baseten balance was not verified, so that arm was omitted. Each of the
three arms sent three client warmups and 12 measured requests, in the same seeded
order: **45/45 HTTP responses succeeded**, with 36 measured responses. The
reference runtime also made three startup warmup calls. Flide's internal graph
capture did GPU work but is not counted as an HTTP model request. All offered-ID,
probability and normalization checks passed.

## Client-observed latency

Each cell is the median of **three** measured HTTPS requests from the same client;
parentheses give all three samples in request order. Input token counts agree
across all three servers. The image-bearing counts include the image.

| Recorded context | Input tokens | Flide H200, ms | Flide B200, ms | Reference H200, ms |
| --- | ---: | ---: | ---: | ---: |
| Short text | 4,196 | 379 (379, 372, 385) | **226** (224, 228, 226) | 562 (564, 559, 562) |
| Long text | 7,476 | 649 (655, 649, 644) | **387** (428, 387, 381) | 1,070 (9,220, 1,070, 1,044) |
| Short + image | 8,798 | 1,820 (3,444, 1,820, 770) | 1,488 (3,865, 1,488, 470) | **1,311** (9,182, 1,311, 1,305) |
| Long + image | 16,290 | **1,497** (2,502, 1,488, 1,497) | 1,992 (2,027, 912, 1,992) | 2,779 (10,756, 2,779, 2,772) |

The first reference request for several new shapes had an 8–10 s model-call
time; three short-text client warmups did not warm those shapes. It would be
misleading to treat the medians as fully warmed image performance. There was
also a brief overlap between reference readiness polling and the start of its
client run, so some reference HTTP timings may include that contention.

Flide exposed per-request timing. On B200, the long-image forwards were
870–894 ms, while two of the three image requests spent about 1.1 s in planning;
the other spent 11 ms there. The short-image requests showed a similar planning
pattern, and the first also had a 2.67 s forward. H200 showed comparable
one-second planning spikes. Flide's source initializes an image processor lazily
per planning thread; the default is four threads. That makes per-thread image
initialization a plausible explanation, **not a verified cause**. A one-planner,
image-warmed test is the next targeted experiment.

## Decision fidelity and limits

All three repeats on all three servers selected the same offered choice for
each of the four contexts. Within each arm/context, reported probabilities did
not change across the three repeats. Compared with the reference, Flide's largest
absolute per-option probability difference was 0.03351 on H200 and 0.03276 on
B200. The two Flide GPUs reported the same rendered prompt hash and token count
for each context; their largest per-option probability difference was 0.01006.
Those observations do not establish game quality or general equivalence.

The reference runtime did not expose its rendered prompt hash or image tensors.
Source comparison shows nearly identical OneJev prompt/media construction and
the token counts match, but the ordering defect above proves the image prompts
differed. Exact reference token, mask-boundary and image-tensor equivalence
remains **unverified**. The predeclared nomination rule is therefore
**inconclusive**, even though B200 was faster on three of four observed medians.
The largest context's 16,290 reported tokens is close to Mammoth's published
16,384-token maximum; larger real contexts may not fit and were not silently
truncated. No gameplay, production provider integration or Baseten speed
comparison was performed.

For both recorded 640×480 JPEGs, the pinned processor configuration specifies
16-pixel patches and a spatial merge of 2. Its resize rules leave those
dimensions unchanged, yielding `(640 / 32) × (480 / 32) = 300` image tokens.
An offline reconstruction of the exact Flide prompt matched the prompt hash
returned by the server for both images. That reconstruction contained one
unexpanded image token, and replacing it with 300 produces the logged totals
of 8,798 and 16,290 tokens. This supports the count; it does not verify the
actual pixel tensors, attention boundaries, or equivalence to Baseten's image
renderer.

## Corrected image path

The follow-up check is specified in `docs/design/mammoth-image-correctness.md`.
It compared corrected Flide 0.1.7 with the reference runtime, one H200 each, run
one after the other. The four units were the short and long image contexts plus
the same contexts with a black 640×480 JPEG in place of the frame. Each arm sent
3 warmups and 12 measured requests in the seeded order: **30/30 HTTP responses
succeeded**, with no retries. The reference made no startup inference calls.

**Structural check: passed.** A CPU audit using both deployed images matched
all four units exactly for prompt hash, expanded token IDs, prefix boundary
(6,553 and 14,045), image grid `[1, 30, 40]`, 300 image tokens and float32 pixel
tensor (maximum difference 0.0). On the GPUs, the reference logged
the same IDs, boundary, grid and pixel-tensor hash for all 15 of its calls.
Flide logged 14 planner records; every one matched the CPU audit. Its 14
logged GPU pixel tensors had the CPU audit's hashes and a maximum difference of
0.0 against its CPU path. One Flide call's records are absent from the retrieved
container log, so that call's GPU evidence is **unknown**.

**Decision-output check: passed.** On every repeat, both servers returned a
valid offered choice, the same top choice and token counts (8,798 and 16,290).
Within each arm and unit, probabilities did not change across repeats.

| Unit | Choice (both) | Reference top-two margin | Largest per-option difference, Flide vs reference |
| --- | --- | ---: | ---: |
| Short + real image | move_forward | 0.200 | 0.0109 |
| Short + black image | move_forward | 0.006 | 0.0089 |
| Long + real image | escalate | 0.001 | 0.0039 |
| Long + black image | escalate | 0.021 | 0.0170 |

Only the short real-image unit exceeded the 0.05 margin required for the
top-choice rule; all four still agreed. Every difference is below the 0.05
limit. The earlier 0.0335 gap was therefore probably inflated by the
prompt-order defect, but that is not proven.

**The image affects both runtimes' outputs.** With the text fixed, replacing the frame
with black changed the largest option probability by 0.216 (reference) and
0.214 (Flide) on the short context, and by 0.078 and 0.080 on the long context.
The top choice did not change. Both servers responded to the image by similar
amounts. The black frames are diagnostic controls only; no response was executed.

Both arms carried hash logging, which copies tensors to the CPU, so their
timings are not a latency result. For reference only, the median
client-observed times were 789 ms (Flide) vs 1,269 ms (reference) on the short
image context, and 1,514 ms vs 2,694 ms on the long one. Flide's timing also supports the planning
explanation above: its first four calls each spent 1.01–1.05 s in planning,
and the remaining 11 spent 6–10 ms. That pattern fits one lazy image-processor initialization
in each of its four planning threads. A warmup that sends one image request to
every planning thread should remove this cost. That warmup is still untested.

This check used two frames from one game. It does not establish gameplay
quality, full model equivalence, or equivalence with Baseten's renderer.

## B200 precision benchmark: BF16 vs FP8

**Prefix caching does not apply to Mammoth.** Its full-attention layers read the whole prefix
(`block_bidirectional`), so any changed state byte changes every prefix position from layer 3 on.
Flide therefore rejects session caching for block-attention and image plans, and no turn-to-turn reuse
is possible. The pilot's `--session-cache-gb 0` cost nothing.

Two Flide 0.1.7 arms ran one after the other on one B200 each, US East, one request at a time, on the four
pilot contexts (corrected image bodies). The arms were BF16 (as in the pilot) and `--quantize fp8`.
Flide's FP8 quantizes the decoder weights; the vision encoder stays BF16. Each arm sent 8 warmups, the last
six of them image requests to initialize the planning threads, then 20 measured requests in a seeded order:
**56/56 succeeded**, with no retries. The plan was frozen before the first call
(`runs/.../fp8-bench-frozen-plan.json`, local).

| Context | Tokens | BF16 median, ms | FP8 median, ms | Change | Largest per-option difference |
| --- | ---: | ---: | ---: | ---: | ---: |
| Short text | 4,196 | 241 | 222 | −8% | **0.0553** |
| Long text | 7,476 | 399 | 324 | −19% | **0.0781** |
| Short + image | 8,798 | 483 | 418 | −13% | 0.0363 |
| Long + image | 16,290 | 933 | 796 | −15% | 0.0172 |

Each latency median is five client-observed HTTPS requests. Server forward times fell by similar amounts
(e.g. 872 → 722 ms on long + image). Within each arm, probabilities did not change across repeats. FP8 picked the
same choice as BF16 on every repeat, but its probabilities moved by more than the predeclared 0.05 limit
on both text contexts. **Under the frozen rule, FP8 is not adopted.** Two contexts from one game
cannot show whether that drift matters for play.

**The image warmup removed the one-second image planning delay.** The BF16 arm's measured requests planned
in at most 19 ms, except one: its first measured long-image request still took 1.21 s to plan. Only three
of the six image warmups had paid the initialization cost, so one of the four planning threads had not been
warmed. The FP8 arm's warmups reached all four, and its measured planning stayed at or below 21 ms. A warmup
should continue until every planner thread has handled an image, rather than send a fixed count. BF16 B200 image
medians are now 483 and 933 ms, compared with 1,488 and 1,992 ms in the pilot.

**The long + image context is a near-tie.** On B200, BF16 picked `move_forward` with a top-two margin
of 0.005. On H200, both Flide and the reference picked `escalate` (margin 0.0008). Neither choice
changed within a GPU. Different GPU numerics are enough to flip a tie this narrow. This does not indicate
an image-path fault.

For comparison only: on Day 1, Baseten took 169 ms at 1,407 tokens and 748 ms at 10,895 tokens.
Different contexts and dates make this indirect. B200 BF16 measured 241 ms at 4,196 tokens and 399 ms at 7,476.

Each arm took about 16–17 minutes from start to cleanup, mostly image build, checkpoint download and queueing.
With the image and weights cached, the FP8 container's Flide route was ready **90 s** after the container
started. Spend was about **$2.51 metered** ($7.1315 after vs $4.6218 at freeze, $0 billed; snapshots).
That brings the image check and this benchmark to about $3.74 of the $15 envelope. Both apps are stopped,
both temporary tokens deleted and the weight Volume deleted.

## Operational notes and spend

The first local CLI invocation failed before cloud work. The next image build
completed, but Modal rejected the server's 64 GiB explicit ephemeral-disk request
because the platform minimum was 512 GiB. No GPU or inference started on that
attempt. Omitting the disk request allowed the planned H200 arm to proceed. A
separate helper recognized the reference server's `ready: true` health response;
it used the existing container and made no additional model calls. The first
supervisor was stopped and both temporary tokens were deleted.

The first Flide H200 run downloaded the checkpoint; B200 reused the temporary
Modal Volume. The H200 arm elapsed 442 s from supervisor start to cleanup, and
the B200 arm elapsed 366 s, including queueing and startup; these are not
steady-state latencies. All pilot apps are stopped with zero tasks, no pilot
container remains, all pilot proxy tokens were deleted, and the temporary Volume
was deleted. The pre-existing `mammoth-1-system-one` app was left untouched.

Modal's observed workspace summary after cleanup reported **$3.3955 metered**
and **$0 billed**, against a zero-metered baseline; billing summaries moved
between observations, so this is a snapshot rather than a final settled bill.
The approved ceiling was $20. No Baseten calls were charged.

The image check had a separate **$15** envelope. After cleanup, the workspace
reported **$4.8118 metered** and **$0 billed**, about $1.42 above the
$3.3955 baseline. That figure is also a snapshot. It includes one stopped Flide launch:
it deployed and created a temporary token, but sent no inference request before
it was stopped. It was stopped because its local task limit was shorter than the
arm's deadlines. Its app was stopped, its token deleted and its records kept
apart from the arm's results. Afterward, no pilot app or container was running,
no temporary token remained, and the image check's weight Volume had been deleted.
