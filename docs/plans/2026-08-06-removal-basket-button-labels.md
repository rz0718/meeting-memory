# Removal Basket Button Labels Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the removal basket's technical action labels with the approved concise labels `Clear`, `Preview`, and `Delete`.

**Architecture:** Keep the existing removal workflow, API calls, approval state, and safety checks unchanged. Update only the button copy in `objects.js`, including keeping the destructive action labeled `Delete` after preview, and protect the wording with the project's existing static-asset regression test pattern.

**Tech Stack:** Browser ES modules, Python/FastAPI static asset tests, pytest

---

### Task 1: Protect the concise removal labels

**Files:**
- Modify: `tests/test_ui_routes.py`

**Step 1: Write the failing test**

Add this method to `StaticAssetTest`:

```python
def test_removal_basket_uses_concise_action_labels(self):
    source = self.client.get("/static/js/objects.js").text

    self.assertIn('text: "Clear"', source)
    self.assertIn('text: "Preview"', source)
    self.assertIn('text: "Delete"', source)
    self.assertNotIn('text: "Empty basket"', source)
    self.assertNotIn('text: "Write inventory and preview"', source)
    self.assertNotIn('text: "Remove permanently"', source)
    self.assertNotIn('`Remove ${approved.inventory_count} objects`', source)
```

**Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_removal_basket_uses_concise_action_labels -q
```

Expected: FAIL because `objects.js` still contains the old labels and dynamic `Remove N objects` copy.

**Step 3: Commit the failing test**

```bash
git add tests/test_ui_routes.py
git commit -m "test: cover concise removal basket labels"
```

### Task 2: Apply the approved labels

**Files:**
- Modify: `src/meeting_memory/ui/static/js/objects.js:291-318`
- Modify: `src/meeting_memory/ui/static/js/objects.js:394-401`

**Step 1: Replace the destructive and preview labels**

Change the button creation to:

```javascript
const applyButton = el("button", { class: "btn btn--danger", disabled: true }, [
  icon("trash"),
  el("span", { text: "Delete" }),
]);

const previewButton = el("button", { class: "btn" }, [
  icon("doc"),
  el("span", { text: "Preview" }),
]);
```

After a successful preview, continue enabling `applyButton`, but remove the block that replaces its label with `Remove 1 object` or `Remove N objects`.

**Step 2: Replace the clear label**

Keep the existing `api.basketClear()` handler and change only its text:

```javascript
text: "Clear",
```

**Step 3: Run the focused test**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_routes.py::StaticAssetTest::test_removal_basket_uses_concise_action_labels -q
```

Expected: PASS.

**Step 4: Check JavaScript syntax**

Run:

```bash
node --check src/meeting_memory/ui/static/js/objects.js
```

Expected: exits successfully with no output.

**Step 5: Commit the UI copy change**

```bash
git add src/meeting_memory/ui/static/js/objects.js
git commit -m "ui: simplify removal basket action labels"
```

### Task 3: Verify the unchanged safety workflow

**Files:**
- No additional changes expected.

**Step 1: Run removal route tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_ui_routes.py::RemovalBasketTest tests/test_ui_routes.py::StaticAssetTest -q
```

Expected: all tests PASS, including preview-before-apply and inventory count/digest safeguards.

**Step 2: Run removal workflow tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_removal_workflow.py -q
```

Expected: all tests PASS.

**Step 3: Check the final diff**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and no uncommitted implementation changes.
