# Safe Merge and Custom Final Statement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent pre-existing repository errors from producing partially successful merges and let reviewers preview and apply an editable final statement.

**Architecture:** Keep `KnowledgeMerger` as the authoritative mutation boundary: validate the full repository before building either a dry run or an apply, normalize and validate the optional statement override, and retain the existing post-commit validation. Extend the existing merge dialog with source-statement context, a survivor-prefilled final-statement field, source-copy shortcuts, and client-side preview invalidation while relying on the server decision fingerprint as the final gate.

**Tech Stack:** Python 3, unittest/pytest, FastAPI/Pydantic, browser ES modules, Node built-in test runner

---

### Task 1: Block invalid repositories before merge mutation

**Files:**
- Modify: `tests/test_merge_workflow.py:1-166`
- Modify: `src/meeting_memory/knowledge/merge.py:153-165`

**Step 1: Write the failing preflight regression test**

Add `json` and `SchemaError` imports and a helper that writes a valid historical
review-run manifest whose suggestion file is deliberately absent:

```python
def write_dangling_suggestion_manifest(self):
    manifest = {
        "schema_version": "1",
        "run_type": "review_suggestions",
        "run_id": "review-run-dangling",
        "started_at": "2026-07-29T12:40:57Z",
        "completed_at": "2026-07-29T12:41:04Z",
        "status": "success",
        "model": "test/model",
        "prompt_version": "1",
        "filters": {},
        "requested_review_ids": ["review-dangling"],
        "suggestions_created": {
            "review-dangling": "suggestion-missing",
        },
        "suggestions_reused": {},
        "failures": [],
    }
    path = self.repository.review_run_dir / "review-run-dangling.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
```

Create survivor and loser objects, save both byte sequences, write the dangling
manifest, then assert both `dry_run=True` and apply raise `SchemaError`. Assert
the two canonical files still exist with the original bytes after each call.

**Step 2: Run the focused test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_merge_workflow.py -k preflight -q`

Expected: FAIL because dry run still completes and apply mutates before
post-commit validation raises.

**Step 3: Add repository-wide preflight validation**

After validating reviewer, note, self-merge, and override shapes—but before
loading or modifying canonical objects—add:

```python
self.repository.validate_all()
```

This must run for both preview and apply. Leave the existing post-commit
`validate_all()` in place as a defensive invariant check.

**Step 4: Run the focused merge tests**

Run: `.venv/bin/python -m pytest tests/test_merge_workflow.py -q`

Expected: all merge workflow tests PASS.

**Step 5: Commit**

```bash
git add tests/test_merge_workflow.py src/meeting_memory/knowledge/merge.py
git commit -m "Prevent partial merges on invalid repositories"
```

### Task 2: Validate and persist a human-authored final statement

**Files:**
- Modify: `tests/test_merge_workflow.py:113-166`
- Modify: `src/meeting_memory/knowledge/merge.py:153-208`
- Modify: `tests/test_ui_routes.py:354-381`
- Modify: `tests/test_ui_routes.py:721-739`

**Step 1: Write failing domain tests**

Add one test that calls merge with:

```python
statement="The final statement combines both canonical records."
```

Assert `result.after["statement"]` and the reloaded survivor both contain the
custom value, while the history entry contains the reviewer's note.

Add a second test that passes `statement="   "`, expects `MergeError`, and
asserts neither canonical file changed.

**Step 2: Run the tests to verify the blank-statement case fails**

Run: `.venv/bin/python -m pytest tests/test_merge_workflow.py -k statement -q`

Expected: the persistence test passes under the existing override support, but
the blank-statement test FAILS because whitespace is currently accepted.

**Step 3: Normalize the statement in the domain layer**

Add input normalization before repository validation:

```python
if statement is not None:
    statement = statement.strip()
    if not statement:
        raise MergeError("final statement may not be empty")
```

Keep `None` as “use the survivor statement,” preserving CLI compatibility.

**Step 4: Extend route/CLI parity coverage**

Add `statement="Combined final statement."` to `MergeRequest` in
`test_merge_form_matches_cli_merge_invocation` and add the matching
`--statement` arguments to the CLI invocation.

Extend the merge route test so preview and apply include a custom statement,
then reload the survivor and assert both the final statement and audit note.

**Step 5: Run focused Python tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_merge_workflow.py tests/test_ui_routes.py -q
```

Expected: all tests PASS.

**Step 6: Commit**

```bash
git add tests/test_merge_workflow.py src/meeting_memory/knowledge/merge.py tests/test_ui_routes.py
git commit -m "Validate custom merge statements"
```

### Task 3: Add testable merge-draft helpers

**Files:**
- Create: `src/meeting_memory/ui/static/js/merge_form.js`
- Create: `tests/js/test_merge_form.mjs`

**Step 1: Write failing pure-helper tests**

Use `node:test` and `node:assert/strict` to cover:

- the selected survivor statement is the initial final statement;
- either source object's statement can be selected as the editable value;
- whitespace is trimmed from the request's final statement; and
- the generated request includes loser ID, survivor ID, note, statement, and
  both explicit override booleans.

Example expectation:

```js
assert.deepEqual(
  mergeRequestBody({
    loserId: "loser",
    survivorId: "survivor",
    note: " Same fact. ",
    statement: " Combined wording. ",
    allowCrossCategory: true,
    allowConflictingNumbers: false,
  }),
  {
    loser_id: "loser",
    survivor_id: "survivor",
    note: "Same fact.",
    statement: "Combined wording.",
    allow_cross_category: true,
    allow_conflicting_numbers: false,
  },
);
```

**Step 2: Run the Node test to verify failure**

Run: `node --test tests/js/test_merge_form.mjs`

Expected: FAIL because `merge_form.js` does not exist.

**Step 3: Implement the dependency-free helpers**

Export small functions with no DOM dependency:

```js
export function objectStatement(objects, objectId) {
  return objects.find((value) => value.id === objectId)?.statement || "";
}

export function mergeRequestBody({
  loserId,
  survivorId,
  note,
  statement,
  allowCrossCategory,
  allowConflictingNumbers,
}) {
  return {
    loser_id: loserId,
    survivor_id: survivorId,
    note: note.trim(),
    statement: statement.trim(),
    allow_cross_category: Boolean(allowCrossCategory),
    allow_conflicting_numbers: Boolean(allowConflictingNumbers),
  };
}
```

**Step 4: Run helper tests**

Run: `node --test tests/js/test_merge_form.mjs`

Expected: all tests PASS.

**Step 5: Commit**

```bash
git add src/meeting_memory/ui/static/js/merge_form.js tests/js/test_merge_form.mjs
git commit -m "Add merge statement form helpers"
```

### Task 4: Build the editable final-statement merge dialog

**Files:**
- Modify: `src/meeting_memory/ui/static/js/objects.js:136-285`
- Modify: `src/meeting_memory/ui/static/styles.css`

**Step 1: Import and use the pure helpers**

Import `objectStatement` and `mergeRequestBody`. Add DOM nodes for:

- the retiring statement, rendered read-only;
- the selected survivor statement, rendered read-only;
- a required final-statement textarea;
- **Use survivor** and **Use retiring** shortcut buttons.

After initially filling the survivor selector, prefill the textarea with:

```js
finalStatement.value = objectStatement(store.knowledge, select.value);
```

When the survivor selection changes, update its read-only statement and reset
the final field to that survivor's statement. Each shortcut copies the relevant
source text into the textarea while leaving it editable.

**Step 2: Send the final statement in preview and apply**

Replace the local body builder with `mergeRequestBody(...)`, passing current
DOM values. Both requests continue to add only their own `dry_run` boolean.

**Step 3: Invalidate stale previews immediately**

Create a local `invalidatePreview()` that disables Apply and clears the old
preview. Attach it to `input`/`change` events for survivor, note, final
statement, and both override checkboxes. Call it after either source shortcut.
Do not invalidate merely because the candidate search text changes unless that
search changes the selected survivor.

Keep the server decision fingerprint as the authoritative apply check.

**Step 4: Improve the preview**

Above the raw deterministic JSON, render the survivor's prior statement and
`preview.after.statement` as an explicit before/final comparison. The note
remains visible in its labeled textarea as the separate audit rationale.

**Step 5: Add minimal responsive styles**

Reuse existing compare, property, textarea, and secondary text styles where
possible. Add only the layout rules needed for the two source statements and
shortcut row to remain readable in the existing modal at narrow widths.

**Step 6: Run JavaScript and route tests**

Run:

```bash
node --test tests/js/test_merge_form.mjs tests/js/test_knowledge_inbox.mjs tests/js/test_runs_chart.mjs
.venv/bin/python -m pytest tests/test_ui_routes.py -q
```

Expected: all tests PASS.

**Step 7: Commit**

```bash
git add src/meeting_memory/ui/static/js/objects.js src/meeting_memory/ui/static/styles.css
git commit -m "Add editable final statement to merge dialog"
```

### Task 5: Verify full behavior and regressions

**Files:**
- Verify only; no planned modifications

**Step 1: Run formatting and diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors and only intended files changed.

**Step 2: Run the full Python test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests PASS.

**Step 3: Run the full JavaScript test suite**

Run: `node --test tests/js/*.mjs`

Expected: all tests PASS.

**Step 4: Review the final merge contract**

Confirm that:

- invalid repositories fail preview and apply before either canonical file is
  changed;
- blank custom statements fail before mutation;
- the editable final statement appears in preview and persists on apply;
- the note remains separate and appears in the survivor history;
- editing a decision field disables Apply until preview succeeds again; and
- the existing cross-category and conflicting-number overrides remain explicit.

