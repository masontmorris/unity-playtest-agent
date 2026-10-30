# Live decision context audit and regression validation

Issue #168, using the saved b04 development session 4 at `109cb53`.
This is evidence of information-flow defects and offline regression checks.
It does not establish improved model decisions or gameplay, and no new paid run is included.

## Observed gaps

- S1 received the most recent turn's outcome/effect, but no action chronology.
  A wait replaced that immediate outcome in the next context. The live frontier builder passed
  `recent=[]`; the saved provider-request hashes alone cannot reconstruct full frontier requests.
- Context trace 1160 (turn 154) exposed open directive `d-150` while its help remained pending.
  Adoption occurred at 1164. The helper mutated the tracker from its background task before adoption.
- The open hypothesis `hyp-1-0` was revised at trace 238, including contrary-direction evidence.
  Context 243 retained its older full text and only an ID for the revision, because compaction
  compared status rather than complete content.
- Context 415 labelled a control generally ineffective from zero net displacement. A completed
  execution at 418 was followed by a changed view orientation at observation 421. The displacement
  criterion did not measure general effectiveness.
- The final frontier slot was used by the turn-150 request. Context 1198 did not expose remaining
  slots; a subsequent request was refused at 1204. No later escalation was observed. This shows
  missing availability information, not a proven cause of ending or repeated requests.

The existing tracker distinguishes setpoint satisfaction from executed inputs. A passive reader
change can satisfy a setpoint with zero completed inputs, and the window states that fact.
This patch preserves that scoring rule rather than treating `met` as proof a requested action ran.
The session's lone cooldown candidate was not reproduced; the stair defect remained unknown.

## Implemented evidence flow

1. Independent chronological windows retain eight completed turns and eight actual execution
   attempts. Waits do not displace attempts; rejections, unknown effects and missing release evidence
   remain explicit. Window omissions and reset/scene/clock boundaries are recorded.
2. Frontier receives those same records with bridge observation citations distinguished from trace
   and effect references. It no longer starts from an empty live action history.
3. Directive/hypothesis updates are staged until adoption. A result finishing during observation
   cannot publish into that turn. Discarded or replaced results cannot publish later.
4. Any changed hypothesis text or evidence is delivered in full, including same-status revisions.
   Earlier support/contradiction citations remain available; identical versions may be restated by ID.
5. Session facts expose remaining wall time and measured call counts. Unknown counters stay null;
   disabled help and an exhausted cap are distinct. Counts never choose, hide or replace a choice.
6. Choice labels qualify the displacement criterion and include measured view-turn calibration
   where available. Inputs and measured values are unchanged.

## Offline validation and limitations

Tests exercise the actual serialized S1 context and frontier request, including a measured action
shortfall followed by nine waits, a stale rejection, continuation while help is pending, source
references, unknown values, reset boundaries and immutable earlier payloads. A controlled await
boundary reproduces completion during observation capture; adoption and cancellation tests confirm
the directive cannot appear early or survive discard. Parser/adherence checks cover no replacement
actions or automatic escalation. Direct builder calls without history preserve their prior bytes.

A local structural reconstruction verified all 171 saved S1 context hashes and history references.
Adding the history windows alone contributed a median 7,704 and maximum 9,483 JSON characters on
that trace. These are character counts, not measured model tokens. The old trace did not store the
probe's full `effect_fact` text, so that reconstruction leaves it null; it is not an exact new-runtime
request or a latency measurement. Existing guidance and directive facts also grow during a run.
Request size, latency and stale-action rejections must therefore be measured in a new live validation.

## Proposed next validation (not yet authorized or run)

One development session on the same game commit, preflight-3 manifest, calibration, initial scene,
real-time clock, objective, model arms, 280,000-token pacing and 0.8-second age limit as session 4.
No paid preflight is needed: no bridge or action declaration changes. Freeze the new code, prompt,
manifest/catalog hashes, objective and start conditions before launch. Record any mismatch.

The envelope is at most 400 S1 calls at $0.002 and 15 Standard frontier calls at $0.05: $1.55 maximum,
with about $0.35–0.60 expected and balances explicitly unverified if the user approves that override.
Stop on the existing session rules or envelope; no automatic retry or extra session.

Predeclare operational checks: correct history in the actual requests; publication only at adoption;
preserved revisions; valid references; unknowns retained; no adherence violation; verified input release;
actual call counts, cost, latency, payload size and action-rejection rate. Separately report completed
executable choices, waits/help use, observable interactions and findings. The reviewer sees trace,
recording and citations; a candidate is supported only to the extent those records show its claimed
behavior. A timer's growth alone does not establish a defect. Reproduction remains a separate step.

This bundled change differs in several context fields from session 4. A single development session
can reveal failures and useful behavior; it cannot isolate a mechanism or establish a quality gain.
No code steers the agent toward a known defect, substitutes actions, or turns a score/timer into help.
