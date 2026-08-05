# Run History Chart Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the fixed sparkline with a full-width, labeled chart of knowledge objects created per run and make the source-processing summary self-explanatory.

**Architecture:** Keep the runs API unchanged. Add a dependency-free module of pure chart-data helpers, use it from `runs.js` to construct an accessible responsive SVG, and add chart-specific styles to the existing stylesheet.

**Tech Stack:** Browser ES modules, SVG, CSS, Node built-in test runner, Python/FastAPI UI route tests

---

### Task 1: Specify chart data preparation

**Files:**
- Create: `tests/js/test_runs_chart.mjs`
- Create: `src/meeting_memory/ui/static/js/runs_chart.js`

**Step 1: Write the failing tests**

Cover these behaviors with `node:test` and `node:assert/strict`:

- the newest-first API response is limited to 12 and returned oldest-first;
- each chart entry carries its ISO date, compact display date, and created count;
- the y-axis uses integer ticks and has a non-zero ceiling when every count is zero;
- a one-run series gets a centered x coordinate rather than dividing by zero; and
- a multi-run series uses the full plot width.

**Step 2: Run the tests to verify failure**

Run: `node --test tests/js/test_runs_chart.mjs`

Expected: FAIL because `runs_chart.js` does not exist yet.

**Step 3: Implement the pure helpers**

Create and export:

```js
export function chartSeries(runs, limit = 12) { /* limit, reverse, normalize */ }
export function yTicks(values, targetTickCount = 4) { /* integer nice scale */ }
export function pointX(index, count, left, width) { /* centered singleton */ }
```

Use a stable UTC date formatter so labels match the manifest run date rather
than changing with the browser timezone.

**Step 4: Run the helper tests**

Run: `node --test tests/js/test_runs_chart.mjs`

Expected: all tests PASS.

### Task 2: Build the explanatory chart UI

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js:137-211`
- Modify: `src/meeting_memory/ui/static/styles.css:430-490`

**Step 1: Replace the sparkline renderer**

Import the pure helpers and construct a `knowledge-chart` card containing:

- heading “Knowledge objects created”;
- subtitle “Per run · last 12 runs · run dates in UTC”;
- a responsive SVG with y-axis grid lines and integer tick labels;
- an oldest-to-newest line and light area fill;
- one focusable point per run with a `<title>` containing the full date and
  count;
- visible count labels above points; and
- compact date labels beneath points.

Render a useful one-point chart and an explanatory empty state instead of
returning `null` for fewer than two runs.

**Step 2: Clarify the source summary**

Replace “9 of 13 sources processed (4 unchanged)” with a `source-summary`
block containing:

```text
13 sources examined · 9 processed · 4 unchanged
Unchanged sources required no new processing.
```

Keep the rejected-candidate count visible when present.

**Step 3: Add responsive styling**

Make the card and SVG `width: 100%`, set a useful chart height, style grid,
axes, line, area, points, and labels using existing theme variables, and reduce
label density/font size on narrow screens without reducing the data points.

### Task 3: Verify integration and regressions

**Files:**
- Modify: `tests/test_ui_routes.py:1855-1870`

**Step 1: Verify the new static module is served**

Add `js/runs_chart.js` to the static-asset route test.

**Step 2: Run focused tests**

Run:

```bash
node --test tests/js/test_runs_chart.mjs
.venv/bin/python -m pytest tests/test_ui_routes.py -q
```

Expected: all tests PASS.

**Step 3: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests PASS.

**Step 4: Review the diff**

Confirm no API contract changed, no chart dependency was added, the unrelated
`setup.cfg` modification remains untouched, and the chart uses the full content
width.
