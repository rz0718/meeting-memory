# Meeting Memory — Review UI Plan

A local web UI for the morning knowledge-review routine: see what last night's
run inserted, then work the review queue with AI suggestions that a human can
modify and decide. A third tab asks questions of the accumulated knowledge and
renders the answer next to the evidence it rests on, optionally bounded to a
single project's meetings and channels.

The visual language and navigation model are modeled on **Notion**
(<https://www.notion.com/>) — see [§8](#8-design-system-notion-referenced) — with
one deliberate departure documented in [§8.4](#84-where-we-deliberately-diverge-from-notion).

## 0. Shape and assumptions

- **Single reviewer, localhost only.** `meeting-memory ui` starts a FastAPI app on
  `127.0.0.1:8787`, bound to loopback, no auth. Reviewer defaults to `rui`,
  settable in the header.
- **The UI is a client of the existing Python API, not a reimplementation.** It
  imports `KnowledgeRepository`, `ReviewResolver`, `ReviewRefresher`,
  `generate_review_suggestions`, and the merge/removal entry points — the same
  objects `cli.py` wraps. No subprocess shelling: that gets typed exceptions and
  keeps the `@mutation_locked` boundary intact.
- **The UI adds zero new decision semantics.** Every write goes through
  `ReviewResolver.resolve()`. If something cannot be expressed as a `resolve()`
  call, the UI does not offer it.
- New package `src/meeting_memory/ui/` (`app.py`, `routes_*.py`, `static/`).
  Frontend is a single-page app with no build step — plain ES modules plus one
  CSS file served from `static/`. A bundler is not worth the maintenance for a
  three-tab localhost tool.

## 1. Backend read models

Three read endpoints do almost all the work, each a thin wrapper over an
existing payload builder.

| Endpoint | Backed by |
| --- | --- |
| `GET /api/runs?date=` | `.knowledge-state/runs/*.json` manifests + `repository.latest_successful_run()` |
| `GET /api/reviews?priority=&category=&source=` | `list_reviews()` + `reviews_payload()` (`review.py:120,246`) |
| `GET /api/reviews/{id}` | `review_payload(..., include_excerpts=True)` + `latest_current_suggestion()` + `suggestion_display_payload()` |
| `GET /api/knowledge/{id}` | `load_knowledge_file()` + evidence excerpts |

The run manifest already carries exactly the morning-digest shape. From the
2026-07-31 run: 144 sources examined, 58 processed, 36 `objects_created`,
3 `objects_reconfirmed`, 2 `objects_refined`, 9 `review_items_created`,
0 rejected, 0 errors. Tab 1 is largely a renderer for that file.

## 2. Tab 1 — "Today's Knowledge"

**Purpose: verify what the pipeline decided on its own.** These changes were
applied without a human, so the tab exists for fast scanning, with an audited
escape hatch when something looks wrong.

```
┌─ Run 20260731T125027Z · 2026-07-31 12:50 → 13:20 · success ──────────┐
│  36 created   3 reconfirmed   2 refined   9 → review   0 errors      │
│  58 of 144 sources processed (86 unchanged)          [prev run ▾]    │
├──────────────────────────────────────────────────────────────────────┤
│ Created (36)                          │  decision-removal-raw-log…   │
│  ▸ decision · removal-raw-log-field…  │  ── statement ──────────     │
│  ▸ ownership · wise-automation-hand…  │  Raw log field removed…      │
│  ▸ system · agether-system            │                              │
│ Refined (2)                           │  ── evidence ───────────     │
│  ▸ process · d3-app-approval-process  │  meetings/2026-07-01/        │
│    ⟨ before → after diff ⟩            │  slack-c0194tgl94h.md:41-58  │
│ Reconfirmed (3)                       │  ┃ 41  Andreas: we dropped…  │
│ Errors (0)                            │                              │
│                                       │  [Merge into…] [Flag remove] │
└──────────────────────────────────────────────────────────────────────┘
```

Design points:

- **Grouped by manifest bucket, not by category.** "What changed" is the
  question, and `refined` / `reconfirmed` warrant more scrutiny than `created`.
- **Refined objects show a before/after statement diff.** The manifest records
  IDs only, so the backend reconstructs the prior statement from the object's
  `history` block. When it cannot, the row reads "refined — prior text
  unavailable" rather than showing a fabricated diff.
- **Evidence is always one click away**, rendered with real 1-based line numbers
  from the source file, using the same excerpt reader as the review tab.
- **Actions are the two audited mutation paths that already exist:**
  - **Merge into…** → object picker → `merge --into --reviewer --note --dry-run`,
    preview, confirm. Cross-category and conflicting-number merges appear as
    explicit checkboxes mapping to `--allow-cross-category` and
    `--allow-conflicting-numbers`, never applied silently.
  - **Flag for removal** → adds the ID to a session removal basket. Removal is
    deliberately not one-click: the basket is written to an ID-inventory file and
    executed with `remove --object-id-file --confirm-count --inventory-sha256`,
    matching the friction the CLI already designs in. The UI displays the count
    and SHA-256 it will assert.
- Run selector to page back through prior manifests, plus a date-range filter for
  "everything inserted this week".

## 3. Tab 2 — Review Queue

**Purpose: reproduce `ReviewTriage` (`review_triage.py:123`) as a screen, with
the same gates.** That loop is already the correct workflow — show evidence, show
suggestion, accept/override/defer, deterministic dry-run, confirm, apply. The
UI's contribution is side-by-side evidence and diff instead of scrolled output,
and editable fields instead of a retyped command.

```
┌ Queue ─────────────┬ review-github-team-quant-and-ml-repo-sharing-2026-07-01 ┐
│ ⚑ conflict     (6) │ conflicting_evidence · systems · 2026-07-01             │
│ ▸ github-team-qu…  ├─────────────────────────┬───────────────────────────────┤
│ ▸ scb-straight2b…  │ EXISTING                │ CANDIDATE                     │
│ ▸ production-git…  │ Repos are shared via…   │ Repos are shared via team…    │
│   linked       (2) │ ⟨word-level diff highlighted across both columns⟩       │
│   unlinked     (1) ├─────────────────────────┼───────────────────────────────┤
│                    │ meetings/2026-07-01/…   │ meetings/2026-07-01/…         │
│ [conflict ▾][cat ▾]│ :112-119  [RE-VERIFIED] │ :41-58   ⚠ DRIFTED            │
│                    │ ┃112 …                  │ ┃41 …                         │
│                    ├─────────────────────────┴───────────────────────────────┤
│                    │ AI SUGGESTION  refine · confidence high                 │
│                    │ rationale · material differences · risks · findings     │
│                    │ target: system-github-repo-sharing                      │
│                    ├────────────────────────────────────────────────────────┤
│                    │ Action  (•) refine  ( ) replace  ( ) reconfirm          │
│                    │         ( ) create-separate ( ) keep-existing           │
│                    │         ( ) merge-duplicate → 1 possible duplicate ⓘ    │
│                    │ Target  [system-github-repo-sharing        ▾]           │
│                    │ Status/Owner/Confidence/Effective date  [editable]      │
│                    │ Note    [prefilled from proposed_note — must edit ✎]    │
│                    │                     [ Preview decision ]  [ Defer ]     │
└────────────────────┴────────────────────────────────────────────────────────┘
```

### 3.1 The accept/override distinction is the core interaction

Selecting the AI's exact action *and* leaving every field untouched sends
`--accept-suggestion` → recorded as `accepted`, mode `hybrid`. Touching any radio
or field flips the form to override → the same `suggestion_id` is still sent,
recorded as `overridden`, mode `hybrid`. A live badge shows
`will record: accepted` / `will record: overridden` so the human knows what the
audit trail will say before applying. This is the point of the tab and must not
be blurred.

### 3.2 Mandatory two-step apply

`[Preview decision]` calls `resolve(..., dry_run=True)` and renders
`ReviewResolutionResult` — destination status, affected object IDs, before/after
object summary, changed paths — and only then reveals `[Apply]`. **There is no
path from form to disk without an inspected dry-run.** Apply re-sends identical
arguments; the resolver re-validates under the lock, so a preview that went stale
fails loudly instead of applying something else.

### 3.3 Blocked states the code already defines

- **`requires_human: true` or `suggested_action: null`** → accept is disabled with
  the reason shown; the human must choose an action, mirroring
  `review_triage.py:171`.
- **Canonical drift** (object changed since the review was cut) → resolution is
  refused by design with no override. The UI detects this on load and replaces
  the action panel with a **Refresh** panel: `refresh --dry-run` preview → apply →
  auto-regenerate suggestion → reload. This is the most confusing CLI failure
  today and is worth a guided path.
- **Drifted candidate evidence** → `⚠ DRIFTED` badge on the excerpt, plus an
  `Allow stale evidence` checkbox rendered *only* for `replace`, `refine`,
  `reconfirm`, and `create-separate`, with copy stating the override is
  permanently recorded. Hidden for `keep-existing` and `merge-duplicate`, where
  it is invalid.
- **Multiple `possible_existing_ids`** → the target dropdown has no default;
  preview is blocked until one is chosen.
- **`possible_duplicate_ids` non-empty** → inline peek at the other review, and
  `merge-duplicate` prefilled with `--duplicate-of`.
- **Empty note** → blocked. Prefilled from the suggestion's `proposed_note` but
  marked unedited; applying an unedited AI note requires ticking "I reviewed this
  note", so the audit does not fill with model-authored rationale nobody read.
- **Lock contention or precondition failure** → error banner with the exception
  text and a reload button. Never retried automatically.

### 3.4 Queue-level affordances

- Sorted `conflict → linked → unlinked` (`review_priority`, `review.py:35`), with
  the same filters `review list` exposes: priority, reason, category, existing-id,
  source substring.
- `[Generate suggestions for all pending]` → `generate_review_suggestions` over
  the filtered set, streaming per-item progress. Failures are already isolated per
  review and recorded in the review-run manifest; the UI shows them as a per-row
  error badge rather than failing the batch.
- Keyboard: `j`/`k` move, `a` accept-and-preview, `o` focus override, `d` defer,
  `Enter` apply from preview. A typical morning queue is ~9 items, so speed
  matters more than chrome.
- **No bulk-accept.** Every resolution passes one dry-run and one confirm.
  Batching that away would defeat the audit design the repository is built on.

## 4. Tab 3 — Ask

**Purpose: make a grounded answer inspectable.** `meeting-memory ask` already
produces a trustworthy answer; what it prints is an answer paragraph followed by
two flat lists of IDs and file paths (`render_answer`, `answers.py:227`). Reading
those lists means leaving the tool and opening files by hand. The tab's entire
contribution is turning citations into evidence the reader can see without
losing the answer.

### 4.1 The traceability guarantee is already enforced in the backend

`_validate_answer` (`answers.py:130-146`) rejects any answer citing a knowledge
ID or a source path that is not in the context packet, retrying up to three
times before failing. So every citation the UI receives is guaranteed to resolve
to a real object file and a real source document. The UI renders citations as
live links with no defensive "citation not found" state — if one fails to
resolve, that is a bug in retrieval, not a model hallucination, and it should
surface as an error rather than a silent dash.

That guarantee is the reason this tab is worth building: the answer is *already*
anchored, and only the presentation is losing it.

```
┌ Ask ─────────────────────────────────────────────────────────────────┐
│ Scope  [ All knowledge ▾ ]                    8 objects · 0 conflicts│
│ ┌──────────────────────────────────────────────────────────────────┐ │
│ │ How do we hand off automation work to Wise?                      │ │
│ └──────────────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────┬───────────────────────┤
│ ✓ HIGH CONFIDENCE          model · <ask model>│ EVIDENCE              │
│                                               │                       │
│ Handoff runs through the shared automation…   │ ownership-wise-       │
│ …approved 2026-07-01 and unchanged since.     │ automation-handoff    │
│                                               │ approved · rui · high │
│ ── Grounded in ──────────────────────────     │ last confirmed 07-01  │
│ ⧉ ownership-wise-automation-handoff  ← hover  │ ─────────────────     │
│ ⧉ process-d3-app-approval-process             │ ▾ meetings/2026-07-01/│
│ 🗎 meetings/2026-07-01/slack-c0194tgl94h.md   │   slack-c0194…md      │
│ 🗎 meetings/2026-06-24/weekly-sync.md         │   :41-58  [VERIFIED]  │
│                                               │   ┃41 Andreas: we…    │
│ ⚠ 1 open conflict                             │   ┃42 …               │
│ ▸ Considered but not cited (3)                │ ▾ meetings/2026-06-24/│
│ ▸ Context omissions (2)                       │   weekly-sync.md      │
│ ▸ View the exact context sent to the model    │   :112-119 ⚠ DRIFTED  │
└──────────────────────────────────────────────┴───────────────────────┘
```

### 4.2 What the panel shows, and why each part earns its space

- **Answer first, citations under it — not inline footnotes.** The answer
  contract returns *document-level* citation lists, not per-claim spans
  (`ANSWER_KEYS`, `answers.py:15`). Rendering `[1]` markers inside sentences
  would require guessing which sentence each citation supports, which is exactly
  the kind of fabricated precision this repository avoids elsewhere. Citations
  render as a "Grounded in" chip strip; hovering a chip expands its evidence in
  the right rail. Per-claim attribution is a contract change, tracked in
  [§7](#7-known-gaps).
- **Three-level drill-down.** Chip → knowledge-object card (statement, status,
  owner, confidence, last confirmed, category) → evidence excerpt with real
  1-based line numbers. The excerpt renderer is the one Tabs 1 and 2 already use
  (`evidence_excerpts`, `presentation.py:144`), so drift and re-verification
  badges — `⚠ DRIFTED`, `[VERIFIED]` — appear identically here. **An answer
  resting on drifted evidence must say so**; a stale anchor is not visible in
  today's CLI output at all.
- **"Considered but not cited."** The packet selected N objects; the model cited
  M ≤ N. The remaining objects are listed collapsed, each with its
  `selection_reason`, `score`, and `matched_fields` from `SelectedDocument`
  (`context.py:26`). This is the single most useful inspection affordance in the
  tab: it answers "was the right object even in the room?", which distinguishes a
  retrieval failure from a reasoning failure. No other view exposes it.
- **Context omissions, always shown when non-empty.** `build_context_packet`
  silently degrades under the character budget — dropping manual notes, history,
  excerpts, related IDs, evidence references, review items, then whole objects
  (`context.py:349-405`) — and records each drop in `packet.omissions`. That list
  is the quiet failure mode of `ask`. It renders as a warning callout, not a
  footnote.
- **Open conflicts and pending review items as callouts.** The system prompt
  instructs the model to never cite an open review item as canonical
  (`answers.py:77`); the UI reinforces it visually — pending review items appear
  in a distinct "proposed, not canonical" callout that links into Tab 2, never
  in the "Grounded in" strip.
- **Confidence as icon + label + color**, per the status-palette mitigation in
  [§8.1](#81-tokens). `low` never reads as a neutral state.
- **The exact context sent to the model** is one toggle away — `packet.markdown`
  verbatim, plus the retrieval summary. Full auditability with zero
  reconstruction, since the packet already carries the rendered bytes.
- **Insufficient answers get their own empty state.** `insufficient_answer`
  (`answers.py:193`) returns low confidence with all citation lists empty; that
  renders as "no durable knowledge covers this" with the considered-but-not-cited
  list promoted to the primary content, not as a normal answer with blank
  sections.

### 4.3 Backend shape

| Endpoint | Backed by |
| --- | --- |
| `POST /api/ask/context` | `build_context_packet()` → `context_payload()` (`ui/ask_payloads.py`) |
| `POST /api/ask/answer` | the same packet, then `answerer().answer(packet)` → `with_citations()` |
| `POST /api/ask/save` | `render_saved_answer()` over an answer already recorded this session |
| `GET /api/ask/scopes`, `POST /api/ask/scopes` | project scope definitions ([§4.4](#44-project-scoped-qa)) — not built |

**Retrieval and answering are split into two calls on purpose.** Retrieval is
deterministic and fast; the model call takes seconds and has a 120s timeout
(`cli.py:580`). The UI renders the packet — object count, selected objects,
omissions, scope receipt — the moment retrieval returns, then fills in the answer
when the model responds. A slow or failing model still leaves the user with the
evidence, which is most of the value. Both routes build the packet and its
payload inside one `read_cache()` scope and call the provider outside it, so the
excerpts, the omissions, and the bytes sent to the model come from a single
consistent read.

The answer response is `answer_payload(answer)` joined with resolved citation
payloads (object cards and evidence excerpts) so the frontend never re-fetches
per chip. Each response also carries `packet_sha256`, so the client can tell
whether the context it displayed after retrieval is the context the answer was
produced from — the two calls are separate and the repository can change between
them. An empty packet never reaches a provider: the route returns
`insufficient_answer()` exactly as the CLI does.

**This tab writes nothing.** The one exception is the optional "save answer",
and it saves *by token*, not by question: the answer route records what it
returned, and save replays that record. Re-running the provider would be a
different sample, and the file has to say what the screen said. The destination
is derived server-side (`outputs/Knowledge-Answers/answer-<run-id>.md`) and
never accepted from the client, so no request can name a path inside
`knowledge/` or `knowledge-review/` in the first place.

### 4.4 Project-scoped QA

Today `ask` searches all durable knowledge. When the question is about one
project, unrelated objects compete for the character budget and the answer mixes
contexts. A **project scope** bounds retrieval to the meetings and Slack channels
that belong to that project.

**A project is a saved scope, not a new knowledge type.** It holds a name, a set
of source selectors (date ranges, meeting-file paths, Slack channel IDs), and
optional category/owner/status filters. Stored one JSON file per project under
`.knowledge-state/projects/<slug>.json`. That path is neither canonical
knowledge nor review state, so writing it directly does not cross the
[§5](#5-invariants-the-implementation-must-hold) boundary — but the exemption is
narrow and worth naming rather than assuming.

**Where the filter has to apply — all four places, or the scope leaks:**

1. **Search.** Pre-filter the `SearchDocument` sequence handed to
   `build_context_packet`: keep an object if any entry in its `evidence_sources`
   matches the scope.
2. **One-hop relation expansion.** `select_context_documents` pulls in related
   objects through `by_id = {item.id: item for item in documents}`
   (`context.py:130`) — built from the same sequence, so pre-filtering at step 1
   covers this for free. Worth verifying with a test, because a related object
   entering the packet from outside the scope is a silent violation with no
   visible symptom.
3. **Connected review items.** `connected_reviews` (`context.py:166`) reads
   `repository.load_reviews("pending")` directly, *not* the document sequence,
   and admits reviews on a text match against the query
   (`context.py:181-182`) — so an out-of-scope review can enter a scoped packet.
   Fixing this properly means threading an optional source predicate into
   `build_context_packet`. **Until that exists, a scope forces
   `include_review_items=False`** rather than showing material the scope
   excluded. The UI states this in the scope receipt.
4. **The answer validator.** `_validate_answer` derives its allow-list from
   `packet.selected` (`answers.py:130`), so it tightens automatically. Nothing to
   change; worth an assertion in tests.

**Objects that straddle the scope.** An object may cite evidence from both
in-scope and out-of-scope sources. The rule: admit it, render only in-scope
evidence, and badge it `3 of 7 evidence items in scope`. The honest caveat, shown
in the UI and not buried here: the object's *statement* was synthesized from all
of its evidence, so a scope bounds what is retrieved and what can be inspected —
not the provenance of every clause in the sentence. A hard guarantee would need
statement-level provenance the repository does not record ([§7](#7-known-gaps)).

**Every scoped answer carries a scope receipt** — project name, resolved source
count, objects in scope versus total, and whether review items were suppressed —
displayed above the answer and included in any saved output. Without it, a scoped
answer and an unscoped one are indistinguishable after the fact, and a
narrow-scope answer read as global is worse than no answer.

**An empty scope refuses to call the model.** If no object survives the filter,
the UI shows "no knowledge objects in scope" with the scope definition and an
edit affordance. **It never falls back to unscoped retrieval.** Silent widening
is the one failure this feature cannot have.

**Building a scope from what exists, not from typed globs.** The scope editor
lists the actual source universe — every distinct `evidence_sources` entry across
the index, grouped by date and by kind (`meetings/<date>/*.md` versus
`meetings/<date>/slack-<channel>.md`, `slack.py:376`) — with Slack channel IDs
resolved to names where configuration provides them. Selecting sources updates a
live count: `42 sources → 17 knowledge objects → est. 12k chars`. A project
assembled from observed sources cannot reference a file that does not exist.

Scopes also apply to Tab 1 and Tab 2 as a filter once they exist, but that is a
follow-on; Ask is where the concept pays for itself.

## 5. Invariants the implementation must hold

These follow from the existing code and are what a UI most easily erodes:

1. Raw meeting and Slack notes are never editable or writable from the UI.
2. Every mutation goes through `ReviewResolver`, merge, or removal — never a
   direct file write to `knowledge/` or `knowledge-review/`.
3. Dry-run precedes apply, always, with the preview visibly rendered.
4. `suggestion_id` is transmitted on override as well as accept, so the audit
   records suggested-versus-final.
5. Suggestion artifacts stay append-only; the UI never edits or deletes one.
6. Pending review files are never deleted — dropping a candidate is
   `keep-existing`.
7. All writes hold the repository mutation lock; long LLM calls
   (`review suggest`, `ask`) happen outside it, as they do today.
8. The Ask tab is a read path. Its only write is an explicit "save answer",
   through `_answer_output_path`'s protected-directory guard.
9. A project scope only ever narrows the candidate set. No code path may widen
   it, and no scoped request may fall back to unscoped retrieval — an empty
   scope is an error state, not a reason to search everything.
10. Every answer is displayed with the scope it ran under and the omissions the
    budget forced. An answer shown without its retrieval context is a claim
    without a receipt.

## 6. Phasing

| Phase | Deliverable |
| --- | --- |
| 1 | FastAPI skeleton, `meeting-memory ui` command, read-only Tab 1 over run manifests plus object/evidence detail. Zero write paths. |
| 2 | Read-only Tab 2: queue, filters, side-by-side diff, evidence excerpts, suggestion rendering. Still zero writes. |
| 3 | Write path: accept/override form → dry-run preview → apply, plus defer. Ships the actual value. |
| 4 | Blocked-state handling: refresh flow, stale-evidence override, duplicate linking, batch suggestion generation. |
| 5 | Tab 1 actions: merge, removal basket. |
| 6 | **Shipped.** Tab 3 read path: question → packet → answer, citation drill-down to evidence excerpts, considered-but-not-cited, omissions callout, raw-packet toggle, save-by-token. |
| 7 | Project scopes: scope editor over the observed source universe, scoped retrieval, scope receipt, empty-scope refusal, review-item suppression. |
| 8 | Scope predicate threaded into `build_context_packet` so scoped packets can include review items honestly; scopes reused as a filter on Tabs 1 and 2. |

Phases 1–3 are the usable product; 4–5 remove the remaining reasons to drop back
to the CLI. Phase 6 is independently shippable — it depends on nothing in 1–5
beyond the shared evidence renderer — and can be pulled forward if grounded
answers matter more than queue throughput.

**Testing.** The backend is thin enough to test at the route level against a
temporary repository, asserting that each route calls the resolver with the exact
argument set the equivalent CLI invocation would produce. That property is what
keeps the UI and CLI from diverging. For Tab 3 the equivalent property is
retrieval-side: given a fixed document set and scope, the route must produce the
byte-identical packet `ask` produces with the same options, with a `FakeAnswerer`
standing in for the model.

## 7. Known gaps

- **Refined-object diffs are best-effort.** The prior statement is not stored
  structurally, only in the `history` prose. Extending the run manifest to record
  before/after summaries in the pipeline would make this exact.
- **Tab 1 has no notion of "reviewed".** Tracking that a human looked at
  yesterday's insertions needs new state — a per-run acknowledgement file.
- **No per-claim attribution.** Citations are document-level. Adding
  `claims: [{text, knowledge_objects, meeting_evidence}]` to the answer contract
  would let the UI underline each sentence with its own support, but it breaks
  the exact-key equality check in `_decode_json` (`answers.py:92`) and needs a
  versioned contract plus revalidation of per-claim citations against the packet.
  Worth doing only after the document-level view is in daily use.
- **No statement-level provenance**, so project scoping bounds retrieval and
  inspection but cannot guarantee a cited statement was authored solely from
  in-scope sources. Recording which evidence items contributed which clause is a
  pipeline change, not a UI one.
- **`connected_reviews` is not scope-aware** (`context.py:166`); phase 7 works
  around it by suppressing review items under a scope, phase 8 fixes it.
- **Answers are not retained.** There is no ask history, so a scoped answer
  cannot be re-examined later unless it was explicitly saved. A local
  `.knowledge-state/answers/` log is the obvious fix; it is deliberately left out
  until someone wants it, since it is new state with a retention question
  attached.

---

## 8. Design system (Notion-referenced)

Notion is the reference for **surface treatment, density, and navigation
patterns**. The values below are a Notion-*like* token set — a faithful
approximation of its web app, not an official brand export. Treat them as this
project's tokens.

### 8.1 Tokens

Defined once as CSS custom properties, declared under both a
`prefers-color-scheme` media query and a `[data-theme]` scope so an explicit
toggle wins over the OS setting.

| Role | Light | Dark |
| --- | --- | --- |
| Page surface | `#FFFFFF` | `#191919` |
| Secondary surface (sidebar, peek) | `#F7F7F5` | `#202020` |
| Hover wash | `#F1F1EF` | `#2C2C2C` |
| Primary ink | `#37352F` | `#D4D4D4` |
| Secondary ink | `#787774` | `#9B9B9B` |
| Muted ink (labels, placeholders) | `#9B9A97` | `#7F7F7F` |
| Divider (hairline) | `rgba(55,53,47,0.09)` | `rgba(255,255,255,0.094)` |
| Accent | `#2383E2` | `#529CCA` |
| Callout tint — neutral | `#F1F1EF` | `#2C2C2C` |
| Callout tint — info | `#E7F3F8` | `#143A4E` |
| Callout tint — warning | `#FBF3DB` | `#402C1B` |
| Callout tint — danger | `#FDEBEC` | `#522E2A` |

**Status roles** (from the data-viz status palette, fixed and never themed):
good `#0CA30C`, warning `#FAB219`, critical `#D03B3B`.

Validated with `validate_palette.js` against these surfaces:

- Light (`#FFFFFF`): CVD separation PASS (worst adjacent ΔE 11.3, protan),
  normal-vision PASS (ΔE 27.6). Warning yellow measures 1.83:1 contrast —
  below 3:1. This is the documented status-palette case, and the **mitigation is
  mandatory**: every status cue ships as icon + label + color, never color alone.
- Dark (`#191919`): all three clear 3:1; separation results identical.
- Accent blue clears 3:1 on its surface in both modes (`#2383E2` on `#FFFFFF`,
  `#529CCA` on `#191919`).

The categorical-palette gate does not apply to these — they are status roles, not
series colors — but the separation checks were run anyway, since the stat row
places them side by side.

**Typography.** Notion's system sans stack throughout:
`ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial,
sans-serif`. Body 16px/1.5, secondary and dense UI 14px, section headings 20px
semibold, page title 30px bold. Evidence excerpts, IDs, and file paths use
`SFMono-Regular, Menlo, Consolas, monospace` at 13px. Corner radius 3px on
controls and rows, 6px on cards and the side peek — small, in Notion's register.

**Stat tiles** (the Tab 1 header row) follow the data-viz stat-tile contract:
sentence-case label, value in the same sans at semibold, proportional figures
(`tabular-nums` reserved for table columns and axis ticks). No hero number — five
peer metrics have no single headline. `→ review` wears warning and `errors` wears
critical, both with an icon and the word, so the count never depends on hue.
Optional 12-point sparkline of objects created per run: one series, so no legend.

### 8.2 Notion patterns mapped to this workflow

| Notion pattern | Used for |
| --- | --- |
| Collapsible left sidebar (240px, secondary surface) | Workspace nav: runs, queue, resolved history, settings |
| Inline database view tabs (Table / Board / Calendar) | The two tabs — text labels, active marked by weight plus a 2px underline, counts as muted trailing numbers |
| Filter / sort chip bar under the view tabs | Exactly the filters `review list` exposes |
| Database rows with hover-reveal handle and OPEN | Queue rows and Tab 1 change lists |
| **Side peek** (right drawer on row click, expandable to full page) | Review detail and knowledge-object detail — the single most valuable borrowed pattern |
| **Properties panel** (muted label left, value right) | Status, owner, confidence, effective date, target object |
| **Callout blocks** (tinted, icon + text) | `⚠ DRIFTED`, canonical drift, `requires_human` |
| **Toggle blocks** | Collapsible evidence excerpts, risks, material differences |
| **Comment thread** styling | AI suggestion rationale as a comment from an AI author chip; the human note as the reply being composed |
| **Inline mention chips** (`@page` pills) | Citation chips in the Ask answer — an object ID renders as a chip, hover peeks, click opens the side peek |
| Notion AI answer panel (question box above, sourced answer below) | Tab 3's shell, minus the streaming-typewriter affectation — the answer arrives whole, since it is validated before display |
| Database filter chip labelled with the active view | The project-scope selector, always visible, never collapsed into a menu |
| `⌘K` quick find | Jump to a review or knowledge object by ID or title |

### 8.3 Density and motion

Notion's restraint is the part worth copying: hairline dividers instead of boxes,
generous vertical rhythm, no drop shadows except on the side peek and popovers,
hover states as a flat wash rather than a border change, and transitions capped
around 120ms. Icons are line-based and monochrome, inheriting ink color.

### 8.4 Where we deliberately diverge from Notion

Notion's editing model is optimistic, instantly saved, and undoable. **This
domain is none of those.** Resolutions are audited, gated by a dry-run, and not
reversible by an undo. So:

- **No inline auto-save.** Fields in the properties panel stage into a form and
  are written only via Preview → Apply.
- **No optimistic UI.** Rows update after the server confirms the commit.
- **No undo toast.** The confirmation toast reports what was written and links to
  the resulting audit record. Reversing a decision means a new audited action, not
  an undo.

The visual language is Notion's; the transaction model stays the repository's.
