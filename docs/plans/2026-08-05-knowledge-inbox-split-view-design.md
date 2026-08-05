# Knowledge Inbox Split-View Design

## Goal

Make the daily run page an end-user knowledge browser. Raw meeting notes and
Slack snapshots are noisy; this page should make the knowledge synthesized from
those sources easy to scan, read, and verify.

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

## Recommended layout

Use a knowledge-inbox split view as the main page.

### Compact run header

Keep the selected run date, completion status, time range, meeting-date range,
and total number of knowledge objects in a compact header. Keep the existing run
selector so historical extraction runs remain directly accessible.

### Knowledge list

The left pane lists every ordinary knowledge object in the selected run. Objects
remain grouped by their manifest outcome:

- Created;
- Refined; and
- Reconfirmed.

Review-queue candidates are not mixed into this list because they have their
own decision workflow in the Review queue.

Each list row shows:

- category;
- title;
- a short statement preview; and
- the outcome type when the surrounding group is not visible.

The pane shows the total count and provides text search plus a category filter.
The first matching object is selected automatically. Selecting a different run
resets selection to the first object in that run.

### Knowledge reader

The right pane displays the selected object with the synthesized knowledge as
the primary content:

- title and category;
- complete knowledge statement;
- status and effective date;
- evidence count and source references; and
- evidence excerpts or links where available.

Previous and Next controls support sequential browsing without imposing a
review or acknowledgement state. Keyboard navigation may mirror those controls.

For refined objects, show the existing before/after comparison when the prior
statement is available. If it is unavailable, explain that limitation and show
the current statement without fabricating a prior value.

### Operational run details

Move secondary pipeline information into a collapsed **Run details** section:

- Created, Reconfirmed, Refined, Sent to review, and Error totals;
- sources examined, processed, and unchanged;
- rejected-candidate count;
- run errors; and
- the full-width twelve-run knowledge-object chart.

The collapsed section keeps operational traceability available without making
it the dominant morning-reading experience. If the run contains errors, show a
visible warning in the compact header even while details are collapsed.

## Responsive behavior

On wide screens, the knowledge list and reader appear side by side. The list has
a stable readable width and the reader consumes the remaining space. Each pane
scrolls independently so the selected object remains in context.

On narrow screens, show the list first. Selecting an object opens the reader as
a full-page panel with a clear **Back to knowledge list** action. Run details
remain accessible below the list.

## Data flow

The design is stateless. It adds no read receipts, completion records, or new
repository mutations.

The existing run-detail payload already provides grouped rows and statements.
Selecting an object can use the existing knowledge-object detail route to load
complete metadata and evidence. Search and category filtering remain client
side because a run contains a bounded set of object rows.

If an object listed by a historical manifest no longer exists in the canonical
repository, keep it visible as **No longer present** and do not select it as the
default readable object.

## Loading and error behavior

- Show a skeleton or busy state while the run loads.
- If object detail fails, keep the list usable and show a retry action in the
  reader pane.
- If no ordinary knowledge objects exist, show a clear empty state and retain
  access to Run details and the Review queue.
- If filters produce no matches, explain that the run still contains objects
  and provide a Clear filters action.
- Never treat a failed object-detail load as a review decision or modify the
  knowledge object.

## Accessibility

- Use a labeled list or tree structure for grouped knowledge objects.
- Preserve visible keyboard focus and support keyboard selection.
- Announce the selected object to assistive technology.
- Do not encode Created, Refined, or Reconfirmed by color alone.
- Keep Previous, Next, Back, filters, and Run details available by keyboard.

## Verification

Tests should cover:

- grouping and ordering of run objects;
- search and category filtering;
- default selection and selection reset after changing runs;
- removed-object behavior;
- reader loading and retry behavior;
- separation of review-queue candidates from ordinary knowledge;
- Run details expansion;
- desktop split view and narrow-screen reader navigation; and
- regression coverage for existing run routes and object-detail behavior.
