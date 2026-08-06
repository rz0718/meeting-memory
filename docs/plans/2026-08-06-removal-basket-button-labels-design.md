# Removal Basket Button Labels Design

**Date:** 2026-08-06

## Goal

Make the removal basket actions immediately understandable to ordinary business
users without changing the existing safety workflow.

## Design

Use concise, task-based labels for the three footer actions:

| Current label | New label |
| --- | --- |
| `Empty basket` | `Clear` |
| `Write inventory and preview` | `Preview` |
| `Remove permanently` | `Delete` |

After a successful preview, keep the final action concise by showing `Delete`
rather than replacing it with an object-count label.

The existing order remains unchanged: `Cancel`, `Clear`, `Preview`, `Delete`.
Button styling and enabled/disabled states continue to distinguish the neutral,
preview, and destructive actions. The permanent-removal warning and preview
continue to explain the inventory, count, and SHA-256 safeguards.

## Behavior

This is a copy-only change. `Clear` still clears the basket without deleting
knowledge, `Preview` still writes the approved ID inventory and performs the dry
run, and `Delete` remains disabled until a successful preview. Applying still
asserts the previewed inventory count and digest before permanent removal.

## Verification

Update or add focused UI tests for the rendered labels where the current test
setup supports DOM assertions. Run the existing UI route and removal-workflow
tests to confirm the safety behavior is unchanged.
