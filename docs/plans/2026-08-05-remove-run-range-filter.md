# Remove the Run Range Filter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Run date the Knowledge Updates page's only date control.

**Architecture:** Keep the runs API and its optional range support unchanged. Simplify the browser view so it always requests all manifests, then uses the existing run selector for navigation and the complete response for chart history.

**Tech Stack:** Browser ES modules, Python/FastAPI static asset tests, pytest

---

### Task 1: Specify the simplified filter bar

**Files:**
- Modify: `tests/test_ui_routes.py`

**Step 1: Write the failing test**

Add a static asset regression test that loads `js/runs.js` and verifies it:

```python
def test_runs_view_has_only_the_run_date_control(self):
    source = self.client.get("/static/js/runs.js").text

    self.assertIn('text: "Run date"', source)
    self.assertNotIn("Inserted from", source)
    self.assertNotIn("Clear range", source)
    self.assertIn("state.runs = await api.runs()", source)
```

**Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_runs_view_has_only_the_run_date_control -q`

Expected: FAIL because the range controls and query parameters still exist.

### Task 2: Remove the redundant controls

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js`

**Step 1: Implement the minimal UI change**

- Remove `start` and `end` from view state.
- Change the manifest request to `api.runs()`.
- Remove the two date inputs and the **Clear range** button.
- Keep the **Run date** selector and its UTC explanatory tooltip.

**Step 2: Run the focused test**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_runs_view_has_only_the_run_date_control -q`

Expected: PASS.

### Task 3: Verify regressions

**Files:**
- No additional changes expected.

**Step 1: Run UI route tests**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py -q`

Expected: all tests PASS.

**Step 2: Run JavaScript chart tests**

Run: `node --test tests/js/test_runs_chart.mjs`

Expected: all tests PASS.

**Step 3: Review and commit**

Confirm the backend range API remains intact and the unrelated `setup.cfg`
change is untouched. Commit only the implementation plan, view, and test.
