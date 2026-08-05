# Knowledge Updates Labels Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename the runs view to Knowledge Updates, use Updates in compact navigation, and display its selected run as a readable English date.

**Architecture:** Reuse the view metadata split already provided by `label` and `title`: the application shell uses `label` in the sidebar and tabs and `title` in the page header. Add one pure UTC calendar-date formatter to the existing run chart helper module and consume it in the runs view, leaving API data and time-zone behavior unchanged.

**Tech Stack:** Browser ES modules, Node built-in test runner, Python/FastAPI static asset tests, pytest

---

### Task 1: Specify readable run-date formatting

**Files:**
- Modify: `tests/js/test_runs_chart.mjs`
- Modify: `src/meeting_memory/ui/static/js/runs_chart.js`

**Step 1: Write the failing tests**

Import `fullDateLabel` and add:

```js
test("fullDateLabel renders an English UTC calendar date", () => {
  assert.equal(fullDateLabel("2026-08-04T12:50:00Z"), "August 4, 2026");
});

test("fullDateLabel preserves malformed values", () => {
  assert.equal(fullDateLabel("not-a-date"), "not-a-date");
  assert.equal(fullDateLabel(""), "—");
});
```

**Step 2: Run the test to verify it fails**

Run: `node --test tests/js/test_runs_chart.mjs`

Expected: FAIL because `fullDateLabel` is not exported.

**Step 3: Implement the pure formatter**

Add full English month names and export a formatter that extracts the first
`YYYY-MM-DD` segment without converting the timestamp through the browser's
local time zone:

```js
export function fullDateLabel(value) {
  const date = String(value || "").slice(0, 10);
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match) return value ? String(value) : "—";
  const [, year, month, day] = match.map(Number);
  if (!FULL_MONTHS[month - 1] || day < 1 || day > 31) return String(value);
  return `${FULL_MONTHS[month - 1]} ${day}, ${year}`;
}
```

**Step 4: Run the helper tests**

Run: `node --test tests/js/test_runs_chart.mjs`

Expected: all tests PASS.

### Task 2: Apply the approved labels

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js`
- Modify: `tests/test_ui_routes.py`
- Modify: `README.md`

**Step 1: Write the failing static asset assertions**

Extend `test_runs_view_has_only_the_run_date_control` to assert:

```python
self.assertIn('label: "Updates"', source)
self.assertIn('title: "Knowledge Updates"', source)
self.assertNotIn("Today's Knowledge", source)
self.assertIn("fullDateLabel(summary.started_at)", source)
```

**Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_runs_view_has_only_the_run_date_control -q`

Expected: FAIL because the old labels and ISO heading remain.

**Step 3: Implement the view changes**

- Change `runsView.label` to `Updates`.
- Change `runsView.title` to `Knowledge Updates`.
- Import `fullDateLabel` from `runs_chart.js`.
- Render the run page heading with `fullDateLabel(summary.started_at)`.
- Update the file header comment to use the new name.
- Update the README's user-facing tab name to **Knowledge Updates**.

**Step 4: Run the focused test**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_runs_view_has_only_the_run_date_control -q`

Expected: PASS.

### Task 3: Verify and commit

**Files:**
- No additional changes expected.

**Step 1: Run the UI route tests**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py -q`

Expected: all tests PASS.

**Step 2: Run JavaScript tests and syntax checks**

Run:

```bash
node --test tests/js/test_runs_chart.mjs
node --check src/meeting_memory/ui/static/js/runs.js
git diff --check
```

Expected: all checks PASS.

**Step 3: Commit**

Stage only the plan, README terminology, run view, run helper, and tests. Leave
the existing `setup.cfg` modification untouched.
