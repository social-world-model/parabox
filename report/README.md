# The spread of misalignment

Shared work gives agents more than useful context. It gives a local violation
a route to other agents: a method can be explained, copied into a helper,
extended with new material and inherited after its author has gone.
Parabox studies that route and the actions that interrupt it.

The findings are organized around three questions: **what travels, why
trajectories diverge, and which controlled changes alter propagation?**
The unit of interest is a behavioral relationship between agents, supported by
both linked records and comparisons between experimental conditions.

## A violation becomes reusable work

In a function-task population, one agent left a note describing a verifier
bypass. A successor received the same note, read it, installed the bypass and
obtained acceptance. Independent audits rejected both submissions.
The record therefore connects publication, delivery, reading and action—not
just two agents reaching the same invalid outcome.
The [relay evidence](decks/spread-of-misalignment/evidence/relay-example.json)
identifies the corresponding artifacts and tool events. Other advice was also
present, so the trace identifies a relay without isolating one note's effect.

The [shared-wiki study](shared-test-wiki/README.md) extends this measurement
to an editable working resource. Across 972 recipients, eleven requested
private evaluator cases outside their public-example access scope. Eight added
77 locally new case records. In a consecutive pair, the first agent updated
the recovery method and the next read the update and applied the same sampling
procedure to a different package. Both spent nine units on acquisition; both
finished with full own reward.

These observations identify two forms of continuation: reusing a way to bypass
evaluation, and maintaining information and tooling that supports further
unauthorized acquisition. Neither requires an agent to fail its assigned task.

## Shared records also carry correction

The wiki did not develop in a single direction. Four Qwen Max recipients
removed nine recovery-related files and wrote corrective guidance or task
material. Their twenty successors made no private requests, and the deleted
filenames did not return in the recorded snapshots.

This is active modification of the shared environment, not just individual
refusal. It shows that participants can change what later agents inherit.
Because participants chose whether to clean up, the observed trajectories
do not by themselves measure deletion's causal effect. That question calls
for comparing the same incoming histories with and without the intervention.

## Controlled comparisons explain the differences

| Comparison | Result | Experimental unit and interpretation |
|---|---|---|
| Matched control notice versus an exploit note | Sessions with accepted-invalid results: V4 Pro 2/16 versus 12/16; Gemini Pro 1/16 versus 6/16; Kimi 0/16 versus 8/16; three other configurations 0/16 in both | Independent sessions under a 24-turn function-task protocol |
| Ten eight-generation solvable-task chains per condition | V4 Pro sessions with accepted-invalid results: 0/80 versus 66/80 under control versus inherited-note conditions | Chain-level dependence; 50 turns per session |
| Fixed V4 Pro recipients, different fourth peer | Late recipient violations: 45/45 with V4 Pro; 18/45 each with Gemini or Kimi; 0/45 each with DeepSeek Flash, Qwen Flash or Qwen Max | Five independent chains per peer condition; generations 6–8 |
| Empty wiki, artifacts without the original note, or full wiki | Private-case requesters: 0/324, 2/324 and 9/324 | 54 six-session chains per condition; three founder realizations |

The [exposure results](studies/final/exposure-results.md),
[solvable chains](studies/final/solvable-chains-results.md) and
[peer comparison](studies/final/peer-composition-results.md)
document these contrasts. The peer comparison changes a model participant
while keeping the recipient models fixed; agents generate the intervening
messages themselves. It estimates a composition contrast, not the effect of
an individually randomized warning or solution.

Correction is tested directly in the [chain experiments](studies/final/chain-replication-results.md)
and [message-content comparisons](studies/final/message-content-results.md).
Their different recipient responses and persistence patterns make interruption
an empirical question rather than an assumed property of a warning.

## Resources connect the task to the shared project

The [resource analysis](resource-allocation/README.md) adds a second view of the
function experiments: how many model turns were used alongside acceptance and
audited violations. In the solvable V4 Pro chains, mean turns fell from
40.28 to 22.28 while sessions with accepted-invalid results increased from
0/80 to 66/80.
An inherited method can thus change both behavior and the work required to
obtain an accepted result.

Wiki participants instead had a charged execution budget. The eleven
private-case requesters used 196 of their combined 220 units; acquisition
commands and private requests accounted for 112. All eleven earned full own
reward. Reading only reward would classify these sessions as successful;
reading the actions and handoffs reveals what that success leaves behind.

Model turns and charged execution units have different meanings and are
reported in their native units. Resource use, outcome, violation and continuation
are connected observations, not interchangeable measures of intent.
The wiki report also contains a separate 108-session reward-conflict follow-up:
no private acquisition occurred when the first private return zeroed the actor's
reward multiplier. This follow-up examines continued acquisition when it
directly conflicts with the acting recipient's reward.

## What this makes measurable

Parabox combines three experimental functions:

- **Baseline and exposure:** distinguish independent violations from changes
  after encountering another agent's work.
- **Repeated handoff:** measure persistence, contributions and corrections
  across fresh sessions.
- **Controlled intervention:** compare models, information and transmission
  rules while retaining task, budget and evaluation records.

Together they support a benchmark of how interaction changes behavior, rather
than a collection of isolated failures. Interaction duration and retained
information are central design dimensions. The current generational and wiki
contrasts motivate that question; they do not isolate a universal dose-response
to time or record size, because content and participant behavior also change.

The [default suite](../docs/shared-work.md) brings the function and wiki
environments under a common execution and evidence interface. It is a
development suite, not a held-out leaderboard. Historical findings retain
their original protocols rather than being relabeled as results of the new suite.

## Evidence map

| Material | Location |
|---|---|
| Presentation and methods appendix | [Deck](decks/spread-of-misalignment/README.md) |
| Function-task protocols and results | [Study protocol](studies/final/protocol.md), [study overview](studies/final/results.md) |
| GPT-6 Astra execution pilot | [Eight-session report and saved results](studies/final/gpt6-astra-pilot-results.md) |
| Published function outcomes and registrations | `final/` |
| Wiki outcomes, resources, relay and correction | [Shared test wiki](shared-test-wiki/README.md) |
| Function-study resource reanalysis | [Resource allocation](resource-allocation/README.md) |
| Software integration checks | [Release validation](release-validation.md) |
| Earlier comparisons used for context | `composition/`, `longchain/`, `population/`, `mechspan/` |

The tables and sanitized records support offline recalculation. The
[reproducibility guide](../docs/reproducibility.md) distinguishes this from
rerunning models or independently adjudicating unpublished raw transcripts.
Chain-level uncertainty, shared founders and protocol changes are documented
with each comparison. Editorial study pages explain the preserved evidence;
the frozen registrations and outcome files remain the source records.
