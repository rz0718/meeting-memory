# Removal Basket “Put Back” Label Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename the removal basket's `Clear` action to `Put back` without changing what the action does.

**Architecture:** Update the existing static-asset regression test first, then change the single button label in `objects.js`. Keep the event handler, basket API, modal behavior, and permanent-removal safeguards untouched.

**Tech Stack:** Browser ES modules, Python/FastAPI static asset tests, pytest

---

### Task 1: Update the label regression test

**Files:**
- Modify: `tests/test_ui_routes.py:1905-1915`

**Step 1: Change the expected basket label**

In `StaticAssetTest.test_removal_basket_uses_concise_action_labels`, replace the
`Clear` assertion with `Put back` and explicitly reject the superseded label:

```python
self.assertIn('text: "Put back"', source)
self.assertNotIn('text: "Clear"', source)
```

Keep the existing `Preview`, `Delete`, and old-label assertions.

**Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_removal_basket_uses_concise_action_labels -q
```

Expected: FAIL because `objects.js` still contains `text: "Clear"` and does not
contain `text: "Put back"`.

**Step 3: Commit the failing test**

```bash
git add tests/test_ui_routes.py
git commit -m "test: expect put-back basket label"
```

### Task 2: Rename the basket action

**Files:**
- Modify: `src/meeting_memory/ui/static/js/objects.js:390-399`

**Step 1: Make the minimal UI change**

Keep the button and its existing handler unchanged. Replace only its label:

```javascript
text: "Put back",
```

**Step 2: Run the focused test**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_removal_basket_uses_concise_action_labels -q
```

Expected: PASS.

**Step 3: Check JavaScript syntax**

Run:

```bash
node --check src/meeting_memory/ui/static/js/objects.js
```

Expected: exits successfully with no output.

**Step 4: Commit the UI change**

```bash
git add src/meeting_memory/ui/static/js/objects.js
git commit -m "ui: rename clear basket action to put back"
```

### Task 3: Verify the unchanged basket workflow

**Files:**
- No additional changes expected.

**Step 1: Run the relevant UI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_routes.py::RemovalBasketTest tests/test_ui_routes.py::StaticAssetTest -q
```

Expected: all tests PASS.

**Step 2: Run the removal workflow tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_removal_workflow.py -q
```

Expected: all tests PASS.

**Step 3: Check repository cleanliness**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and no uncommitted changes.
