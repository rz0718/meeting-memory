# Compact Merge Survivor Selector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the merge dialog's permanently expanded survivor list with a compact, accessible autocomplete that makes titles and exact knowledge IDs easy to scan.

**Architecture:** Keep merge selection state in `openMergeDialog`, but move deterministic filtering and keyboard-index calculations into the existing pure `merge_form.js` module. Render the search state and committed-selection state as two views of one selector, and route mouse, touch, and keyboard choice through one `selectSurvivor` function so preview invalidation remains consistent.

**Tech Stack:** Browser-native JavaScript ES modules, semantic ARIA combobox/listbox markup, existing DOM helpers and CSS token system, Node's built-in test runner.

---

### Task 1: Add deterministic survivor-search helpers

**Files:**
- Modify: `src/meeting_memory/ui/static/js/merge_form.js:1-22`
- Test: `tests/js/test_merge_form.mjs:1-61`

**Step 1: Write the failing filtering tests**

Import `filterSurvivorCandidates` and add tests that prove matching is
case-insensitive across both ID and title, keeps source order, tolerates a
missing title, and caps rendered results:

```js
test("filterSurvivorCandidates matches title and exact-ID fragments", () => {
  const candidates = [
    { id: "decision-buying-power", title: "Buying power will be capped" },
    { id: "project-cloud-cleanup", title: "Cloud Skill automatic data cleanup" },
    { id: "decision-fallback", title: "Fallback" },
  ];

  assert.deepEqual(
    filterSurvivorCandidates(candidates, "BUYING").map((value) => value.id),
    ["decision-buying-power"],
  );
  assert.deepEqual(
    filterSurvivorCandidates(candidates, "cloud-cleanup").map((value) => value.id),
    ["project-cloud-cleanup"],
  );
});

test("filterSurvivorCandidates keeps order, tolerates missing titles, and limits results", () => {
  const candidates = Array.from({ length: 305 }, (_, index) => ({
    id: `knowledge-${index}`,
    ...(index === 0 ? {} : { title: `Knowledge ${index}` }),
  }));

  const result = filterSurvivorCandidates(candidates, "knowledge", 300);

  assert.equal(result.length, 300);
  assert.equal(result[0].id, "knowledge-0");
  assert.equal(result[299].id, "knowledge-299");
});
```

**Step 2: Run the tests to verify they fail**

Run: `node --test tests/js/test_merge_form.mjs`

Expected: FAIL because `filterSurvivorCandidates` is not exported.

**Step 3: Implement the minimal filtering helper**

Add this export to `merge_form.js`:

```js
export function filterSurvivorCandidates(candidates, query, limit = 300) {
  const needle = query.trim().toLowerCase();
  return candidates
    .filter((candidate) => {
      const id = String(candidate.id || "").toLowerCase();
      const title = String(candidate.title || "").toLowerCase();
      return !needle || id.includes(needle) || title.includes(needle);
    })
    .slice(0, limit);
}
```

**Step 4: Run the tests to verify they pass**

Run: `node --test tests/js/test_merge_form.mjs`

Expected: all merge-form tests PASS.

**Step 5: Commit the helper**

```bash
git add src/meeting_memory/ui/static/js/merge_form.js tests/js/test_merge_form.mjs
git commit -m "test: cover merge survivor filtering"
```

### Task 2: Add wraparound keyboard navigation

**Files:**
- Modify: `src/meeting_memory/ui/static/js/merge_form.js`
- Test: `tests/js/test_merge_form.mjs`

**Step 1: Write the failing active-index tests**

Import `moveActiveIndex` and add:

```js
test("moveActiveIndex enters and wraps the survivor results", () => {
  assert.equal(moveActiveIndex(-1, 3, 1), 0);
  assert.equal(moveActiveIndex(-1, 3, -1), 2);
  assert.equal(moveActiveIndex(2, 3, 1), 0);
  assert.equal(moveActiveIndex(0, 3, -1), 2);
  assert.equal(moveActiveIndex(0, 0, 1), -1);
});
```

**Step 2: Run the tests to verify they fail**

Run: `node --test tests/js/test_merge_form.mjs`

Expected: FAIL because `moveActiveIndex` is not exported.

**Step 3: Implement the minimal index helper**

```js
export function moveActiveIndex(activeIndex, resultCount, direction) {
  if (!resultCount) return -1;
  if (activeIndex < 0) return direction < 0 ? resultCount - 1 : 0;
  return (activeIndex + direction + resultCount) % resultCount;
}
```

**Step 4: Run the tests to verify they pass**

Run: `node --test tests/js/test_merge_form.mjs`

Expected: all merge-form tests PASS.

**Step 5: Commit the navigation helper**

```bash
git add src/meeting_memory/ui/static/js/merge_form.js tests/js/test_merge_form.mjs
git commit -m "feat: add survivor result navigation"
```

### Task 3: Replace the expanded select with the compact combobox

**Files:**
- Modify: `src/meeting_memory/ui/static/js/objects.js:10,142-370`
- Modify: `src/meeting_memory/ui/static/styles.css:1500-1520`

**Step 1: Build the selector DOM and local state**

Import the new helpers. Replace `search` and `select` with:

```js
const selectorId = `merge-survivor-${loserId}`;
const search = el("input", {
  role: "combobox",
  placeholder: "Search by knowledge ID or title…",
  autocomplete: "off",
  "aria-autocomplete": "list",
  "aria-controls": `${selectorId}-results`,
  "aria-expanded": "false",
});
const results = el("div", {
  id: `${selectorId}-results`,
  class: "merge-survivor__results",
  role: "listbox",
});
const searchView = el("div", { class: "merge-survivor__search" }, [search, results]);
const selectedView = el("div", { class: "merge-survivor__selected", hidden: true });
const selector = el("div", { class: "merge-survivor" }, [searchView, selectedView]);
let survivorId = "";
let matches = [];
let activeIndex = -1;
let resultsOpen = false;
```

Use DOM-safe IDs derived from the result index (not the knowledge ID). Add
`setResultsOpen`, `setActiveIndex`, `renderResults`, `selectSurvivor`, and
`showSearch` closures. `renderResults` must create button-like option rows with
the title and ID in separate spans:

```js
el("button", {
  id: `${selectorId}-option-${index}`,
  class: `merge-survivor__option${index === activeIndex ? " is-active" : ""}`,
  type: "button",
  role: "option",
  "aria-selected": candidate.id === survivorId,
  onMouseDown: (event) => event.preventDefault(),
  onClick: () => selectSurvivor(candidate.id),
}, [
  el("span", { class: "merge-survivor__title", text: candidate.title || "Untitled" }),
  el("span", { class: "merge-survivor__id", text: candidate.id }),
]);
```

When there are no matches, render one `merge-survivor__empty` status using the
existing two messages. Keep at most 300 rendered matches, while CSS shows about
five rows and scrolls the rest.

**Step 2: Route all selection-dependent behavior through `survivorId`**

Replace every `select.value` read in statement initialization, the two source
shortcut buttons, preview guards, and merge request construction with
`survivorId`. `selectSurvivor(nextId)` must:

1. Return immediately when `nextId` is empty.
2. Record whether it differs from the committed `survivorId`.
3. Update the selected card with title, full monospace ID, and a **Change** button.
4. Close the results and swap from search view to selected view.
5. Call `updateSelectedSurvivor({ invalidate: changed })` only after assigning
   the new ID.

Do not auto-select the first candidate on dialog open. Preview and **Use
survivor** stay disabled until the reviewer explicitly commits a result.

**Step 3: Add keyboard, focus, and pointer behavior**

On input/focus, filter and open results. On `ArrowDown`/`ArrowUp`, prevent the
default cursor movement, call `moveActiveIndex`, update
`aria-activedescendant`, and scroll the active row into view. On Enter, select
the active match; if none is active and exactly one match exists, select that
match. On Escape, close results without clearing `survivorId`.

Close results on `focusout` only when the new focus is outside `selector`, so
the Change button and option clicks remain reliable. **Change** clears the
query, swaps back to search view, renders all candidates, focuses the input,
and opens results without invalidating the current preview until a different
candidate is committed.

Replace the property body with:

```js
...property("Keep (survivor)", selector),
```

**Step 4: Add compact, token-based styling**

Add styles beside the existing merge styles:

```css
.merge-survivor {
  position: relative;
}

.merge-survivor__search[hidden],
.merge-survivor__selected[hidden],
.merge-survivor__results[hidden] {
  display: none;
}

.merge-survivor__results {
  position: absolute;
  z-index: 2;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 250px;
  overflow-y: auto;
  border: 1px solid var(--divider);
  border-radius: var(--radius-card);
  background: var(--surface);
  box-shadow: 0 8px 24px rgba(15, 15, 15, 0.14);
}

.merge-survivor__option {
  display: block;
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--divider);
  background: var(--surface);
  padding: 8px 10px;
  color: var(--ink);
  text-align: left;
  cursor: pointer;
}

.merge-survivor__option:last-child {
  border-bottom: 0;
}

.merge-survivor__option:hover,
.merge-survivor__option.is-active {
  background: var(--hover);
}

.merge-survivor__title,
.merge-survivor__id {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.merge-survivor__id {
  margin-top: 2px;
  color: var(--ink-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}

.merge-survivor__empty {
  padding: 10px;
  color: var(--ink-muted);
}

.merge-survivor__selected {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--divider);
  border-radius: var(--radius-control);
  background: var(--surface-secondary);
}

.merge-survivor__selection {
  flex: 1;
  min-width: 0;
}
```

Keep the user's unrelated edits already present in `styles.css`; add only the
new merge-selector block.

**Step 5: Run the focused and full test suites**

Run:

```bash
node --test tests/js/test_merge_form.mjs
node --test tests/js/*.mjs
python3 -m pytest -q
```

Expected: all tests PASS.

**Step 6: Manually verify the interaction**

Run: `meeting-memory ui`

Open a merge dialog and verify:

1. The form initially occupies one input row rather than eight list rows.
2. Title and ID queries show a dropdown capped near five visible rows.
3. Each match exposes its full ID on a separate monospace line.
4. Arrow keys, Enter, Escape, click, and Change behave as designed.
5. Selecting a survivor collapses to the committed card.
6. Choosing a different survivor updates source/final statements and disables
   Apply until another preview.
7. Narrow and dark-mode layouts remain legible.

**Step 7: Commit the UI change**

```bash
git add src/meeting_memory/ui/static/js/objects.js src/meeting_memory/ui/static/styles.css
git commit -m "feat: compact merge survivor selector"
```
