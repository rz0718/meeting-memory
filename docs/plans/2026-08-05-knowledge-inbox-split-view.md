# Knowledge Inbox Split-View Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the Knowledge Updates page into a split-view knowledge inbox whose reader presents each synthesized statement next to the source evidence it was derived from, and move pipeline metrics below the fold.

**Architecture:** No API or stored-data changes. Add one dependency-free module of pure inbox helpers (grouping, filtering, default selection) so the logic is unit-testable without a DOM, then rebuild `runs.js` around the `.split` / `.queue` / `.detail` layout the Review queue already uses. The reader paints from the run-detail row that is already loaded and hydrates from the existing knowledge-object route; `objects.js` is factored so the side peek and the inline reader share one renderer instead of drifting apart.

**Tech Stack:** Browser ES modules, CSS, Node built-in test runner, Python/FastAPI UI route tests

Design reference: `docs/plans/2026-08-05-knowledge-inbox-split-view-design.md`.

---

### Task 1: Specify the pure inbox helpers

**Files:**
- Create: `tests/js/test_knowledge_inbox.mjs`
- Create: `src/meeting_memory/ui/static/js/knowledge_inbox.js`

**Step 1: Write the failing tests**

Cover these behaviors with `node:test` and `node:assert/strict`, building fixture
payloads shaped like `run_detail_payload` output (`groups` of
`{bucket, label, count, rows}` where each row has `id`, `present`, `title`,
`category`, `statement`):

- `inboxGroups` returns only the three ordinary buckets in payload order —
  created, refined, reconfirmed — and never `review_items_created`;
- `inboxGroups` marks reconfirmed as collapsed by default and the other two as
  expanded;
- `objectCount` totals the ordinary rows only, so the header and the tab badge
  cannot disagree with the list;
- `reviewCandidateCount` reports the excluded review rows so the header can link
  to them;
- `matchesFilter` matches case-insensitively on title and on statement, and
  matches every row when the search string is empty;
- `matchesFilter` applies the category filter, and an empty category matches all;
- `filterGroups` preserves group order and drops emptied groups;
- `defaultSelectionId` returns the first present row across the filtered groups;
- `defaultSelectionId` skips rows whose `present` is false, including when the
  first row of the first group is missing; and
- `defaultSelectionId` returns `null` when a run has no present rows at all, and
  when filters match nothing.

**Step 2: Run the tests to verify failure**

Run: `node --test tests/js/test_knowledge_inbox.mjs`

Expected: FAIL because `knowledge_inbox.js` does not exist yet.

**Step 3: Implement the pure helpers**

Create and export, with no DOM access in this module:

```js
export const ORDINARY_BUCKETS = ["objects_created", "objects_refined", "objects_reconfirmed"];
export const COLLAPSED_BY_DEFAULT = new Set(["objects_reconfirmed"]);

export function inboxGroups(detail) { /* ordinary buckets, payload order, + collapsed flag */ }
export function objectCount(groups) { /* rows across ordinary groups */ }
export function reviewCandidateCount(detail) { /* review_items_created rows */ }
export function matchesFilter(row, { search = "", category = "" } = {}) { /* title + statement */ }
export function filterGroups(groups, filters) { /* preserve order, drop empty */ }
export function defaultSelectionId(groups) { /* first present row, else null */ }
```

**Step 4: Run the helper tests**

Run: `node --test tests/js/test_knowledge_inbox.mjs`

Expected: all tests PASS.

---

### Task 2: Factor one shared object renderer

**Files:**
- Modify: `src/meeting_memory/ui/static/js/objects.js:25-103`

**Step 1: Reorder the object view around evidence**

Rewrite `objectView(object, { actions = true } = {})` so the sections read
claim-then-support:

1. the pending-review callout, unchanged;
2. **Statement**;
3. **Evidence**, via the existing `evidenceList` from `evidence.js`;
4. **Status** — status, effective date, last confirmed; and
5. the remaining property grid and history.

Export it so the inline reader can consume the same function. Render the action
bar (merge, flag for removal) only when `actions` is true; the side peek keeps
it, the inline reader does not add a second copy.

**Step 2: Call out untrustworthy excerpts**

`freshnessCue` in `evidence.js:7-12` already renders the four labels. Above the
evidence list, when any entry's `freshness_label` is `drifted` or `unavailable`,
add a warning callout stating that the cited source text has changed since
extraction, or could not be read, and that the excerpt below shows the source as
it reads now. Do not suppress, re-fetch, or modify anything — this is a display
state only.

**Step 3: Confirm the peek still renders**

`openObjectPeek` continues to call `objectView(object)` with actions enabled and
needs no other change.

---

### Task 3: Build the split-view shell

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js:16-26,61-85,299-449`

**Step 1: Extend view state**

Add `filters: { search: "", category: "" }` and `selectedId` to `state`, and keep
`expanded` for the group headers, seeding reconfirmed from
`COLLAPSED_BY_DEFAULT`. Keep `state.runs`, `state.runId`, and `state.detail`
as they are.

**Step 2: Replace `runPage` with the split layout**

Set `ctx.content.style.padding = "0"` as `queueView` does, then mount:

```js
el("div", { class: "split" }, [
  el("div", { class: "queue inbox__list" }),
  el("div", { class: "detail inbox__reader" }),
])
```

above which sits the compact header: run date via the existing
`fullDateLabel(summary.started_at)`, `instant` range, run status cue, meeting-date
range, the object count from `objectCount`, a **Sent to review →** link when
`reviewCandidateCount` is non-zero, and a run-error warning that expands
**Run details**.

The link crosses views through the handler the shell already registers with
`bindQueueNavigation` (`reviewpeek.js:17-20`, wired in `app.js:399-401`), called
with the run's first review-candidate id so the queue opens on that case. Do not
add a second view-switching mechanism.

Delete `statTile` usage from the main page; the totals move to Task 5.

**Step 3: Render the list without refetching**

Render groups from `filterGroups(inboxGroups(state.detail), state.filters)`,
reusing `queue__group`, `queue__group-label`, and `queue__item` classes. Each
item shows category, title, a truncated statement preview, and the outcome when
its group header is collapsed out of view. Rows with `present === false` render
the existing **no longer present** status cue and are not selectable.

Selecting an item sets `state.selectedId` and calls the list and reader
renderers directly. It must not call `runsView.render`.

**Step 4: Add search and category filtering to the filter bar**

Keep the existing **Run date** select exactly as it is, including its
`title` note and `text: "Run date"` label. Add a search input and a category
select whose handlers mutate `state.filters` and then re-render **only the list
pane** from `state.detail`:

```js
const onFilterChange = () => {
  renderList(listNode, readerNode);      // no api.runs(), no api.run()
};
```

The search input uses `oninput` and the filter bar is not remounted, so focus and
caret position survive typing. Re-derive the selection after filtering: if
`state.selectedId` is no longer in the filtered groups, fall back to
`defaultSelectionId`.

**Step 5: Reset selection when the run changes**

The run select keeps calling `runsView.render(ctx)`, which reloads the run. After
the load, set `state.selectedId = defaultSelectionId(...)` for the new run.

---

### Task 4: Paint the reader from row data, then hydrate

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js`

**Step 1: Render the header and statement synchronously**

The selected row already carries `title`, `category`, `status`, `statement`,
`updated_at`, and `evidence_count` from `_object_row`. Render those immediately
with no busy state, followed by a busy placeholder for the evidence and metadata
sections only.

**Step 2: Hydrate from the object route**

Call `api.knowledge(id)` and replace the placeholder with `objectView(object,
{ actions: false })`. Guard against out-of-order responses: when the resolved
object's id no longer equals `state.selectedId`, discard it. This matters because
Previous, Next, `j`, and `k` make selection a rapid repeated action.

**Step 3: Keep refined comparison and failure behavior**

For `objects_refined` rows, render the existing before/after comparison from
`row.refinement` using `diffSegments`, or the existing explanatory callout when
`refinement.available` is false. On a failed hydration, keep the statement
visible, show the error, and offer a **Retry** button that re-runs step 2. A
failed load must never be presented as a review decision.

**Step 4: Add Previous and Next**

Move selection through the flattened present rows of the filtered groups. Disable
the controls at the ends rather than wrapping.

**Step 5: Handle the empty cases**

When `defaultSelectionId` returns `null`, render a reader empty state: one
message for a run with no ordinary objects, one for a run whose objects are all
no longer present, and one for filters that match nothing that offers a **Clear
filters** action.

---

### Task 5: Collapse run details and render the chart lazily

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js:208-264,299-357`

**Step 1: Build the collapsed section**

Add a **Run details** disclosure below the split view containing the five
`statTile` totals, the existing `sourceSummary`, the rejected-candidate count,
the run-error callout, and the knowledge chart. Reuse the `group-toggle` button
pattern and its `aria-expanded` handling from the current `groupBlock`.

**Step 2: Render the chart on first expansion only**

`drawKnowledgeChart` sizes itself from a `ResizeObserver` measurement of its own
SVG (`runs.js:221-238`). Inside a hidden container that measurement is zero, so
the chart would keep its 960px fallback until an unrelated window resize. Build
`knowledgeChart(state.runs.runs)` the first time the section is expanded, and
cache the node for later toggles.

**Step 3: Keep errors visible while collapsed**

The header warning added in Task 3 stays visible whenever `counts.errors` is
non-zero, and expands this section when activated.

---

### Task 6: Wire keyboard selection

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js`
- Modify: `src/meeting_memory/ui/static/js/app.js:352-355`

**Step 1: Export a key handler**

Add `handleRunsKey(event)` to `runs.js`, mirroring `handleQueueKey`
(`queue.js:1120-1138`): `j` selects the next present row, `k` the previous, and
both return `true` when handled so the caller can prevent the default.

**Step 2: Dispatch it from the shell**

In `bindKeys`, after the existing `isTyping` guard and the `1`/`2`/`3` view
switches, add the runs branch alongside the queue branch:

```js
if (activeView === runsView && handleRunsKey(event)) event.preventDefault();
```

The `isTyping` guard (`app.js:315-318`) already covers `input` and `select`, so
the new search box will not swallow `j`, `k`, or the view-switch digits.

---

### Task 7: Style the inbox

**Files:**
- Modify: `src/meeting_memory/ui/static/styles.css:781-860`

**Step 1: Extend the split styles**

Reuse `.split`, `.queue`, and `.detail`. Add `inbox__` modifiers for a wider,
readable list column, a statement preview clamped to two lines, a compact run
header, and the reader's section rhythm. Use existing theme variables only.

**Step 2: Add the narrow-screen behavior**

Below the split breakpoint, stack the panes: the list is full width, selecting an
object shows the reader as a full-page panel with a **Back to knowledge list**
action, and Run details stay reachable below the list.

---

### Task 8: Verify integration and regressions

**Files:**
- Modify: `tests/test_ui_routes.py:1853-1877`

**Step 1: Serve the new module**

Add `js/knowledge_inbox.js` to `test_index_and_modules_are_served`.

**Step 2: Assert the view's contract**

Keep the existing assertions in `test_runs_view_has_only_the_run_date_control`
passing — `text: "Run date"`, `state.runs = await api.runs()`, `label: "Updates"`,
`title: "Knowledge Updates"`, and `fullDateLabel(summary.started_at)` must all
still appear in `runs.js`. Add assertions that the split layout is present
(`class: "split"`) and that the filter handlers do not re-enter the view render.

**Step 3: Correct the stale ordering comment**

The header comment in `runs.js:1-6` states that refined and reconfirmed sit above
created, while `MANIFEST_BUCKETS` (`payloads.py:29-34`) emits created first.
Rewrite it to describe the created-first reading order and the reason reconfirmed
is collapsed, so the stated rationale matches the observed behavior.

**Step 4: Run focused tests**

Run:

```bash
node --test tests/js/test_knowledge_inbox.mjs tests/js/test_runs_chart.mjs
.venv/bin/python -m pytest tests/test_ui_routes.py -q
```

Expected: all tests PASS.

**Step 5: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests PASS.

**Step 6: Review the diff**

Confirm that no API contract changed, that no dependency was added, that the
unrelated `setup.cfg` modification remains untouched, that filtering issues no
network request, and that the object renderer exists in exactly one place.
