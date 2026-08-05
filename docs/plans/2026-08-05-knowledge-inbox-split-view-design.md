# Knowledge Inbox Split-View Design

## Goal

Make the daily run page an end-user knowledge browser. Raw meeting notes and
Slack snapshots are noisy; this page should make the knowledge synthesized from
those sources easy to scan, easy to read, and — most importantly — easy to
verify against the source text it was derived from.

The page is not a reading-compliance workflow. It will not record read status,
completion progress, or acknowledgements.

## Current problem

The current page behaves primarily like an operational run report:

1. run identity and timestamps;
2. five outcome totals;
3. source-processing counts;
4. a large twelve-run chart; and
5. collapsed knowledge groups below the chart.

This puts pipeline history above the content the user opens the page to see.
The extracted knowledge is below the fold, collapsed, and represented only by
short rows until each object is opened separately.

Layout is only half the problem. Even once an object is opened, the existing
object view presents evidence fifth, after the statement, a ten-row property
grid, and related objects. When the underlying notes are noisy, "which source
sentence produced this claim, and does that sentence still say this?" is the
question the reader actually has, and it is currently the hardest one to answer.

## Design decisions

Two decisions drive the rest of this document.

**Evidence is the reader's primary supporting content, not a trailing
section.** The run payload already carries a per-excerpt trust label computed by
`_freshness_label`: `current`, `re-verified`, `drifted`, or `unavailable`. A
`drifted` excerpt means the synthesized statement no longer matches the source
text it cites — exactly the failure a reader of noisy notes needs to catch. That
label is promoted into the reader rather than buried in an evidence list.

**Reconfirmed objects are demoted by default.** A reconfirmed object carries
almost no new information: the pipeline saw an existing fact again and changed
nothing. It is also typically the largest bucket, so treating it equally with
Created and Refined would bury the run's actual news in its own restatements.

## Recommended layout

Use a knowledge-inbox split view as the main page.

### Compact run header

Keep the selected run date, completion status, time range, meeting-date range,
and the number of knowledge objects in the list in a compact header, along with
the existing run selector so historical runs remain directly accessible.

The header also carries two persistent links that must not be hidden by
collapsing operational detail:

- when the run produced review candidates, a link such as **3 sent to review →**
  that opens the Review queue; and
- when the run recorded errors, a visible warning that expands **Run details**.

Review candidates are excluded from the knowledge list because they have their
own decision workflow, but they are the items most likely to need a human, so
the run must not hide the fact that they exist.

The count shown in the header is the number of objects in the list. The sidebar
and tab badge produced by `runsView.count()` must be changed to the same number,
so the two places a user sees a count for this run agree.

### Knowledge list

The left pane lists every ordinary knowledge object in the selected run, grouped
by manifest outcome in the order the run payload already emits:

- **Created** — expanded by default;
- **Refined** — expanded by default; and
- **Reconfirmed** — collapsed by default, with its count visible on the group
  header.

Created-first ordering is deliberate. It matches how the page is now read —
newest knowledge first, then what changed, then what merely held — rather than
the scrutiny-first ordering an operational verification report would use. The
header comment in `runs.js` currently states the opposite rationale while the
payload emits created-first; that comment must be corrected in the same change
so the stated reason and the observed order agree.

Each list row shows:

- category;
- title;
- a short statement preview; and
- the outcome type when the surrounding group header is not visible.

The pane provides text search over title and statement plus a category filter.
Both are client side over data already loaded, and neither triggers a refetch
(see **Data flow**). The first present matching object is selected
automatically. Selecting a different run resets selection to the first present
object in that run.

### Knowledge reader

The right pane displays the selected object, ordered so the claim and its
support sit together:

1. title, category, and outcome;
2. the complete knowledge statement;
3. **Evidence** — the source references with their excerpts, each labeled
   `current`, `re-verified`, `drifted`, or `unavailable`;
4. status, effective date, and last confirmed; and
5. remaining metadata and history.

An excerpt labeled `drifted` is called out rather than styled like the others:
the reader states that the cited source text has changed since extraction and
shows the excerpt as it reads now. An `unavailable` excerpt states that the
source could not be read and identifies the reference, and never silently
renders as an absent or empty excerpt. Neither case is treated as an error
condition for the page, and neither modifies the knowledge object.

For refined objects, show the existing before/after comparison when the prior
statement is available. If it is unavailable, explain that limitation and show
the current statement without fabricating a prior value.

Previous and Next controls support sequential browsing without imposing a review
or acknowledgement state.

The reader and the existing side peek render the same object and must share one
renderer. The peek remains reachable from quick find and from cross-tab links,
so `objectView` is factored to serve both surfaces rather than duplicated; only
the actions and chrome differ.

### Operational run details

Move secondary pipeline information into a collapsed **Run details** section:

- Created, Reconfirmed, Refined, Sent to review, and Error totals;
- sources examined, processed, and unchanged;
- rejected-candidate count;
- run errors; and
- the full-width twelve-run knowledge-object chart.

The chart must be rendered on first expansion, not on page render. It sizes
itself from a `ResizeObserver` measurement of its own SVG; inside a hidden
container that measurement is zero, so an eagerly rendered chart would keep its
fallback width and stay wrong until an unrelated window resize corrected it.

The collapsed section keeps operational traceability available without making it
the dominant morning-reading experience.

## Responsive behavior

On wide screens, the knowledge list and reader appear side by side. The list has
a stable readable width and the reader consumes the remaining space. Each pane
scrolls independently so the selected object remains in context.

On narrow screens, show the list first. Selecting an object opens the reader as
a full-page panel with a clear **Back to knowledge list** action. Run details
remain accessible below the list.

## Data flow

The design is stateless. It adds no read receipts, completion records, or new
repository mutations, and no new API routes.

**Filtering never refetches.** The view currently re-renders by calling
`runsView.render`, which reloads both the run index and the run detail and
remounts the filter bar. Search and category filtering must not use that path:
they re-render the list pane only, from the already-loaded run detail. Routing a
search box through the existing render path would issue two requests per
keystroke and would remount — and therefore blur — the input the user is typing
into.

**The reader paints before it hydrates.** The run-detail row already carries
`title`, `category`, `status`, `statement`, `updated_at`, and `evidence_count`.
The reader renders the statement and header immediately from the row, then loads
evidence, history, and remaining metadata from the existing knowledge-object
detail route and fills them in. Only the sections still loading show a busy
state. A whole-pane skeleton on every selection is not acceptable here, because
Previous and Next make selection a rapid, repeated action.

If an object listed by a historical manifest no longer exists in the canonical
repository, keep it visible as **No longer present** and never select it as the
default readable object.

## Keyboard

Selection uses `j` and `k`, matching the Review queue, dispatched from the
application shell's key map alongside the queue's handler and guarded by the
existing typing check so the search input does not swallow them. The existing
`1`/`2`/`3` view switches continue to work whenever focus is not in a field.

Previous, Next, Back, filters, search, group headers, and Run details are all
reachable and operable by keyboard.

## Loading and error behavior

- Show a busy state while the run loads.
- If object detail fails, keep the list usable and show a retry action in the
  reader pane; the statement already rendered from the row stays visible.
- If no ordinary knowledge objects exist, show a clear empty state and retain
  access to Run details and the Review queue.
- If filters produce no matches, explain that the run still contains objects and
  provide a Clear filters action.
- If every object in a run is no longer present, show a reader empty state
  explaining that, rather than an error.
- Never treat a failed object-detail load, a drifted excerpt, or an unreadable
  excerpt as a review decision or modify the knowledge object.

## Accessibility

- Use a labeled list or tree structure for grouped knowledge objects.
- Preserve visible keyboard focus and support keyboard selection.
- Announce the selected object to assistive technology.
- Do not encode Created, Refined, or Reconfirmed by color alone.
- Do not encode evidence freshness by color alone; each label carries text.

## Verification

The repository has no browser DOM test harness: JavaScript is verified with
dependency-free `node --test` files over exported pure functions, and route and
static-asset behavior is verified with pytest. The list and reader logic must
therefore be factored into exported pure functions — grouping, search and
category matching, default-selection choice, and freshness-label presentation —
so the following are reachable as unit tests rather than assertions about a
rendered page.

Tests should cover:

- grouping and ordering of run objects, and Reconfirmed collapsed by default;
- search and category filtering, including that filtering derives from loaded
  run detail rather than a request;
- default selection, skipping objects that are no longer present, and selection
  reset after changing runs;
- the empty case where no object in a run is present;
- freshness-label presentation for current, re-verified, drifted, and
  unavailable excerpts;
- reader paint from row data followed by detail hydration, and retry behavior
  when hydration fails;
- separation of review-queue candidates from ordinary knowledge, and the
  header's link to the Review queue;
- Run details expansion and first-expansion chart rendering;
- keyboard selection and that view-switch keys still work; and
- regression coverage for existing run routes and object-detail behavior.
