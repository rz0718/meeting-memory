# Meeting Memory — Positioning Narrative

*A company's knowledge is already being produced. It just isn't being kept.*

---

## 0. The corpus, before the pitch

Every number below is from a live deployment, not a projection.

| | |
|---|---|
| Source documents ingested | **542** (228 Slack snapshots, 314 meeting notes) |
| Days of history | **77** |
| Canonical statements produced | **867** |
| Evidence entries (source + SHA-256 + line range) | **1,757** |
| Statements with a named owner | **329** |
| Pages a human wrote | **0** |

Breakdown by category:

| decisions | processes | policies | systems | projects | ownership | metrics |
|---|---|---|---|---|---|---|
| 338 | 196 | 117 | 87 | 78 | 33 | 19 |

By status: **715 approved · 122 proposed · 30 unclear.**
By confidence: **692 high · 165 medium · 10 low.**

Roughly **11 durable facts per working day**, extracted overnight by a cron job, at a
cost measured in cents. Nobody changed how they work. Nobody wrote anything.

---

## 1. The knowledge is in the trace

A company's real policy is not in its wiki. It is in a Slack message sent at 10:42:

> hi @ZHAO RUI @Michael Chan — kindly giving a heads up, today we have 1 Union Bank
> Philippines payment for making a new RCBC account under Flow Exchange. For this one
> I need your help to approve the payment yaa **since Union Bank payment needs 2
> approvers.**

Nobody would ever write that message into Confluence. It is an operational ping about
one ticket on one afternoon. But buried in a subordinate clause is a standing control:
*Union Bank Philippines payments require two approvers.*

That is where policy actually gets made — in passing, inside work. The wiki is a lagging,
lossy, optional re-enactment of it.

**So the trace is the knowledge base.** It is just unindexed, unreconciled, and
write-only.

---

## 2. Documentation failed because it is a second job

Confluence asks the person with the least remaining energy, at the moment of least
understanding, to stop working and write. So it happens late, once, by one person — and
then never again.

The result is not an empty wiki. It is worse: a wiki that is **half true, and you cannot
tell which half.** Every reader has to independently re-verify, which is precisely the
cost documentation existed to remove.

We are not a better wiki. A wiki produces *containers* — a page has one owner, a review
cycle nobody honors, and mixed staleness. We do not produce containers.

---

## 3. The unit of knowledge is the statement, not the page

One fact. One file. Its own status, owner, confidence, effective date, evidence, and
change history.

```yaml
id: policy-union-bank-philippines-payments-require-two-approvers
status: approved
confidence: high
last_confirmed: '2026-07-16'
evidence:
  - source: meetings/2026-07-16/slack-c0194tgl94h.md
    source_sha256: e09a598ef7023965fbd7ab30a16b41870bc29e63e4c9eda8ada971b6c8b0713a
    anchor: Union Bank payment needs 2 approvers
    line_start: 19
statement: Union Bank Philippines payments require two approvers to be approved.
```

```
## History
- 2026-07-16: Initially recorded as approved.
- 2026-08-03: Human review reconfirmed by Rui.
- 2026-08-03: Merged process-union-bank-philippines-payments-require-two-approvers
  (duplicate) into this record; 1 evidence entry added.
- 2026-08-03: Merged process-union-bank-philippines-payment-requires-2-approvers
  (numeric-wording variant) into this record; retain the policy record.
```

Why this matters commercially: **staleness becomes per-fact, and therefore
machine-maintainable.**

867 pages cannot be kept true by humans. 867 statements can be kept true by a nightly
job plus a review queue, because each one is small enough for a machine to re-check and
a human to judge in fifteen seconds.

Atomicity is the unlock. Not the extraction.

---

## 4. Why now: the cost of writing knowledge went to zero

Extracting a day of durable knowledge costs cents, and the price falls roughly an order
of magnitude a year. A human writing the same thing costs thirty minutes and does not
happen. **The crossover is behind us, not ahead.**

Which moves the bottleneck. The scarce resource is no longer *capacity to produce
knowledge* — it is **trust that the produced knowledge is true.**

Every competitor spends their compute budget on generation. We spend ours on
verification:

- every write is gated on a dry run whose preview a human has seen;
- apply re-verifies its inputs under a repository mutation lock, then checks expected
  byte digests immediately before the first write;
- AI suggestions are **append-only and advisory** — a suggestion can never resolve
  itself or touch canonical knowledge;
- resolutions are **refused on canonical drift, with no override** — applying a stale
  decision could overwrite newer curated knowledge;
- accepting a suggestion untouched is recorded as `accepted`; changing one field is
  recorded as `overridden`, and the audit trail says which *before* you apply;
- no bulk accept, no inline auto-save, no undo. Reversing a decision is a new audited
  action.

**We are not a summarizer. We are a ledger.**

---

## 5. The payoff is alignment, not recall

Misalignment inside a company is not a communication-skills problem. It is a data
problem: two people are each holding a different statement, both sincerely, and nothing
in the org's tooling ever puts those two statements next to each other.

Confluence structurally *cannot* surface this. The conflict lives in two different pages,
and nobody diffs pages.

Here the conflict is a first-class object. When the reconciler compares a new candidate
against canonical knowledge and cannot safely merge — a changed threshold, a flipped
polarity, a status change, an ambiguous identity — it stops and files a review
containing both statements, a word-level diff, both evidence excerpts, and a stated
reason why automatic reconciliation halted.

And `_index/unclear-and-proposed.md` is a standing, generated list of **everything the
organization believes it agreed on but has not**: 152 statements sitting at `proposed`
or `unclear`.

> **The demo that sells the product:** do not open the ask box. Open the conflict queue
> and show a leader two statements from their own team, three weeks apart, that
> disagree — with both source lines. Nobody has ever shown them that before. Then open
> `unclear-and-proposed` and say: *these 152 things are not decided.*

---

## 6. Ask is credible only because of the receipts

In a regulated company, an LLM answer with no provenance is unusable for anything that
matters. This corpus is fintech — Fireblocks budgets, KYC migrations, withdrawal SLAs,
two-approver payment controls. An unattributed answer there is a liability, not a
feature.

Three properties enterprise buyers recognize immediately:

1. **Retrieval and answering are two separate calls.** Objects, excerpts, and omissions
   render *before* the model responds. A slow or unconfigured model still leaves you the
   evidence, and `Retrieve only` skips the provider entirely. The evidence is the
   product; the prose is a convenience.
2. **Citations are validated.** An answer citing an unknown object ID or unknown source
   evidence is rejected, not displayed.
3. **The answer shows its own negative space** — the objects retrieved and *not* cited,
   the material the character budget dropped, and the exact packet the model read.
   Almost no RAG product will show you what it declined to look at.

Project scopes carry an honest receipt too: objects and sources in scope against the
totals, plus the objects that also cite evidence *outside* the scope. A scope bounds
what is retrieved and inspectable, but a statement was synthesized from all of its
evidence — so provenance is not scoped with it. We say so on the screen.

Products that state their own limits are the ones enterprises trust.

---

## 7. Seven more reasons to buy

**Onboarding.** A new hire's first 90 days is mostly asking people questions that were
already answered in meetings they were not invited to. Hand them a project-scoped Ask
tab. Measure time-to-first-answer and questions resolved without pinging a human. This
is the door-opening wedge — it needs no org-wide decision.

**Audit and incident review.** "Who approved the two-approver rule, when, and on what
evidence?" Confluence answers *page last edited by X*. We answer with a named reviewer,
a timestamp, a rationale written as a decision record, and a hash-verified line of
source. In regulated industries that is a budget line, not a nice-to-have.

**Bus factor.** 329 statements name an owner, grouped in a generated owner index. When
someone resigns, enumerate exactly which facts leave with them and hand the successor
the list. No other tool produces that report.

**The org's beliefs have a diff.** Page history is not fact history. Because statements
carry supersession, refinement, and reconfirmation events, you can answer *"what changed
about our withdrawal policy this quarter, and why?"* — a quarterly-review artifact
nobody currently supplies.

**The substrate for internal agents.** Every company is about to build internal agents,
and every one will discover that agents need ground truth with provenance and freshness,
not a wiki dump. Machine index, ID-addressable facts, scoped retrieval, validated
citations — already built. Sell meeting memory today; the second act is *the company's
fact layer that agents read from.*

**Writing it down causes the alignment.** When a decision becomes a visible statement in
a shared ledger within twelve hours, and the wording is wrong, someone corrects it. The
record forces the specificity the meeting avoided. The ledger is not a passive recording
of alignment — it is the instrument that produces it.

**Zero input.** Nobody tags, files, or writes anything. The only behavior change in the
entire organization is one person spending ten minutes each morning on a queue. Compare
that to every knowledge-management rollout ever attempted.

---

## 8. Where we sit

| | What it does | Why it is not this |
|---|---|---|
| **Otter / Granola / Fireflies** | Summarize *this meeting* | 500 summaries is the same problem as 500 transcripts — no cross-meeting canonicalization |
| **Glean / enterprise search** | Retrieve documents that exist | If nobody wrote it, search cannot find it |
| **Confluence / Notion** | Store what someone chose to write | A container with mixed staleness and no conflict surface |
| **Meeting Memory** | **Manufacture** the durable record from ephemeral traces, and reconcile it across time | — |

Summarizing one meeting is a commodity. Deciding that today's sentence *supersedes*,
*refines*, or merely *reconfirms* one from three weeks ago — and knowing when to stop
and ask a human — is the hard part, and it is the moat.

---

## 9. Go-to-market

**Land.** One team, one quarter of history, their Slack channels plus recurring meetings.
Value arrives the first night: 77 days became 867 facts on a laptop.

**The ritual is the product.** Three tabs are a ten-minute morning routine — *what landed
last night → work the queue → ask.* Sell the ritual, not the feature list. Products
without a daily ritual churn.

**Expand along scopes.** Project scopes are already the seed of team partitioning and,
eventually, permissions. That is the path from one team to the organization.

**Price on knowledge under management, not seats.** Per-seat pricing is wrong for a
zero-input product — most people never log in, and you would be pricing against your own
adoption. Charge for sources ingested and facts maintained; seats only for reviewers.

**Instrument three numbers.** Conflicts surfaced and resolved · questions answered
without a human ping · new-joiner time-to-first-answer.

---

## 10. What a sharp buyer will hit us with

Stated plainly, because the first sophisticated buyer will find all four. These are the
gaps between the pitch and the artifact today.

1. **No cross-source corroboration yet.** Zero of 867 objects cite more than one source
   *file*; the multiple evidence entries are re-syncs of the same file under different
   hashes. 757 statements are meeting-only, 110 are Slack-only, **0 are fused**. The
   chain-of-evidence promise stays thin until a statement can be independently confirmed
   by a different meeting on a different date. Merge is doing this by hand today (5
   objects). This is the highest-value thing to build next — it is also what makes the
   conflict story fully real.

2. **~97% of the corpus is unreviewed.** 25 of 867 objects carry a human review event,
   and the pending queue is currently empty. The verification machinery is excellent and
   barely exercised. Either raise review throughput or make the auto-admit path the
   honest headline claim.

3. **Owner identity is noisy.** `Bombay Stock Exchange` and `Steve Cohen (Guoco Tower,
   30)` appear as owners; `Zhao Rui` and `ZHAO RUI` resolve as two different people.
   Ownership drives the bus-factor and accountability stories, so identity resolution is
   not cosmetic.

4. **No lifecycle.** Eleven facts a day, linear, and nothing retires. A year of one team
   is roughly 4,000 statements with no decay — at which point we have rebuilt the thing
   we replaced. Re-verification sweeps and retirement are required; `last_confirmed` is
   already the field to drive them from.

---

## The one-liner

> **Your company already writes its knowledge down — in Slack messages and meeting
> notes it never reads again. We turn that trace into a ledger of single, owned,
> evidence-backed statements, and we show you the ones your people disagree about.**
