# S1 arm `mammoth-1-modal`: Mammoth-1 on our own Modal server

Status: available as an option beside the default `liquid-d1-ultra`. Added 2 November 2026 because
the generic Baseten balance will not cover the hackathon; Modal credit will.

## What it is

`scripts/modal/mammoth_flide.py` deploys `baselabs/mammoth-1` (revision `61cec44c…`) on Flide 0.1.7 in
BF16 on one B200 in US East. This is the configuration the 2 November pilot measured
(`docs/probes/2026-11-02-mammoth-modal-fast-pilot.md`):

- **Speed.** Warm client-observed medians were 241 / 399 ms for text at 4.2k / 7.5k tokens, and
  483 / 933 ms for image requests at 8.8k / 16.3k tokens.
- **Image correctness.** Image requests matched the published OneJev reference runtime on prompt,
  token IDs and pixel tensors, and stayed within 0.017 per option.

The S1 adapter treats it like the Liquid arms: the same request body, with the game view as a top-level
`images` array. A proxy inside the container rewrites that for Flide exactly as the verified pilot
bodies were built, byte for byte. Before taking traffic, the container warms every Flide planning thread
with a synthetic image, which removes the one-second first-image delay.

Two measured options are deliberately not used:

- **FP8:** 8–19% faster, but it broke the 0.05 per-option probability limit on the text contexts.
- **Session caching:** Mammoth reads its prompt prefix bidirectionally, so nothing from one turn can be
  reused on the next.

## Verification (2 November)

The four pilot contexts were sent twice each through the client's own path. The S1 adapter's body,
in the `images` transport, went through `replay.send_systemone` and the deployed proxy.

- **From a cold start:** the server woke in 117 s. Flide was ready at 88 s, and the warmups took the rest.
  Every answer matched the B200 BF16 benchmark exactly, with a per-option difference of 0.0 and the same
  choice and token count. The first call took 321 ms; warm calls were 225–923 ms.
- **On an earlier container:** text again matched exactly, but images differed by 0.006 and 0.016. That
  was enough to flip the long-image context's near-tie (benchmark margin 0.005). Sending the
  benchmark's exact bytes to that container gave the same difference, so it came from that container's
  numerics, not from the proxy.
- **The size warmups were added after a cold start without them.** There, the first real 4k-token call
  took 6.9 s and the first image call 1.3 s.

## Using it

The three variables live in the main checkout's `.env`: `MODAL_MAMMOTH_URL` (the server's base URL),
`MODAL_MAMMOTH_TOKEN_ID` and `MODAL_MAMMOTH_TOKEN_SECRET` (a Modal proxy token). Load them into the session
like any other provider key, then:

    scripts/unity/run-session.ps1 ... -S1Arm mammoth-1-modal

The runner wakes the server before it touches the game (`playtest.runner.modal_wake`). A cold start
takes about 90 s once the image and weights are cached. The weights stay in the
`mammoth-1-flide-weights` Volume between starts.

## Cost and approval

Modal bills container time, not tokens, and the server reports no per-call cost, so each call's
`cost_usd` is null. The approval's per-call bound is therefore an operator figure, not a measurement:

    playtest-approval --arm mammoth-1-modal=CALLS --bound mammoth-1-modal=0.01 ...

At list price, B200 plus the container's 4 cores and 64 GiB is about $6.95 an hour, and up to about 1.75×
that with the US East region pin. At about 30 decisions a minute, that is roughly $0.004–0.007 of
container time per decision, so $0.01 is a safe bound. Each session also pays for the idle window
after its last call (`MAMMOTH_FLIDE_IDLE_S`, default 300 s), about $0.60–1.00.

## Redeploying

    MAMMOTH_FLIDE_GPU=B200 modal deploy scripts/modal/mammoth_flide.py

`MAMMOTH_FLIDE_GPU=H200` is the measured fallback: about 1.6× slower, at 75% of the price. The app has
at most one container, which up to 8 concurrent requests share, so two S1 sessions can run against it
at once. Flide queues their GPU rounds.
