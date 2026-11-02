# S1 option presentation order

The default `sorted` mode preserves the existing canonical request. `manifest` presents criteria in the order of `context["choices"]`; `shuffled` uses a deterministic SHA-256 permutation keyed by seed, episode, turn, question and ID. Only presentation changes: S1's selected ID remains authoritative and distributions are record-only. The ledger records mode, seed and presented IDs for opt-in calls; together with the request context and question they reconstruct the wire text. Non-default ordering currently excludes Meta and the alternate request layout.

## Evidence and recommendation

Canonical JSON sorts criteria lexicographically by ID today. In 757 context records from seven traces, the median and maximum option counts were 27; 660 turns exceeded 26 options even though d1-ultra calls succeeded. The hosted labeling scheme beyond 26 is unknown. The first alphabetical ID was selected in 9 of 746 valid decisions versus about 28.4 under a uniform 1/n baseline; this observational comparison is confounded and does not establish bias. Open-source vLLM renders labels in option order, but hosted internals are unverified. Position and label biases have been studied by Zheng et al. ([ICLR 2024, arXiv 2309.03882](https://arxiv.org/abs/2309.03882)); the unknown hosted labeling may change the effect.

Compare manifest order with seeded per-turn shuffles; do not choose a winner in code. A stable order may sustain a systematic error and contribute to stuck loops; shuffling may turn that error into per-turn noise in a closed sequential loop. Shuffling also loses any meaning conveyed by manifest order. Deterministic seeds and recorded order allow replay. Neither approach guarantees improved decisions.

## Proposed paid bench (not run)

Freeze paired frames and acceptable-choice annotations before testing. Estimate top-1 acceptable rate and acceptable probability mass per frame across k presentations, comparing sorted, manifest, and k seeded shuffles. Repeat each identical presentation r times to measure provider noise; analyze paired frame differences and report uncertainty, invalid responses and costs. For N frames this requires N × (2 + k) × r calls (including both stable arms). At approximately 6,000 input tokens and $0.042 per million input tokens for d1-ultra, estimated input cost is $0.000252 per call, or $0.000252 × N × (2 + k) × r; output charges and actual token counts must be added or checked before approval. No calls are authorized by this design.
