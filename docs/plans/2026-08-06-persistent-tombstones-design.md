# Persistent Removal and Merge Tombstones Design

## Problem

Removal and merge delete a canonical file, but nothing records that the
decision was made. `KnowledgeReconciler.reconcile` compares a candidate only
against objects loaded from disk, so a retired object is simply absent, and
absence reads as "new knowledge".

The deletion looks durable only because `source_needs_processing` skips a
source whose recorded `source_sha256`, `extractor_version`, `schema_version`,
and `result` all still match. Removal strips the object ID out of that source
state but leaves those four fields intact, so an ordinary re-run re-extracts
nothing.

The decision is lost the moment any of these holds:

- the run passes `--force`
- `EXTRACTOR_VERSION` or `SCHEMA_VERSION` changes
- the source note is edited, changing its digest
- a later meeting simply re-states the fact from a source never processed before

In each case the candidate reaches `reconcile`, finds no match, and is created
as new. `knowledge_id` is deterministic on category and slugified title, so a
removed object returns under the exact ID that was deleted. A merged loser
returns as a standalone object, silently undoing the merge.

Merge additionally leaves no structured record at all. Removal at least writes
`.knowledge-state/cleanup-runs/permanent-removal-<run>.json`; a merge's only
trace is a prose line in the survivor's `## History`.

## Chosen behavior

A **tombstone** is a durable record that a canonical ID was retired
deliberately. One JSON file per retired ID lives at
`.knowledge-state/tombstones/<object-id>.json`, written inside the same atomic
commit as the removal or merge that created it. A tombstone therefore cannot
exist without its deletion, and a deletion cannot land without its tombstone.

Reconciliation consults tombstones only where it would otherwise return `new`.
A live object always wins; a tombstone can never shadow curated knowledge.

- A candidate matching a **removed** tombstone is **suppressed**: dropped, and
  recorded in the run manifest with the source, the candidate title, and the
  tombstone that blocked it.
- A candidate matching a **merged** tombstone is **redirected** to the
  survivor and then classified normally against it, so a clean restatement
  reconfirms the survivor and a conflicting one still reaches review.
- A candidate that matches **two or more** tombstones ambiguously reaches a
  human as `needs_review`, mirroring how `_find_match` treats an ambiguous
  live match.

## Why identity, not just the ID

Tombstoning by ID alone is insufficient. `knowledge_id` derives the ID from
category and `slugify(title)`, so a later note stating the same fact under
drifted wording produces a different ID and walks past an ID-only check. The
removal would appear to hold for weeks and then fail on the one restatement
whose title moved.

A tombstone therefore records `category` and `title`, and matching reuses the
same identity rules `_find_match` already applies to live objects: the
generated ID, exact normalized title, the two-token containment rule, and
`likely_match_threshold` on token overlap. Tombstones behave as ghost objects
during matching — invisible in output, still able to catch.

## Suppression, not review items

Permanent removal is already deliberately expensive: a reviewer, a mandatory
note, a preview, an inventory file, and an apply step that must echo back the
confirmed count and the inventory digest. Re-opening that decision as a review
item on every restatement would charge the reviewer repeatedly for one
considered judgement. Recurring recaps restate standing facts constantly — the
case `_classify_change` has an "immaterial rewording" branch for — so a removed
standing fact would regenerate the same review item indefinitely.

Suppression is instead recorded in the run manifest under
`candidates_suppressed`, alongside the existing `candidates_rejected`. Counting
suppressions per tombstone is a scan over run manifests, the same way
`latest_successful_run` and `status()` already read run state. This keeps the
ingestion pipeline from writing to tombstone files, which removal and merge
also write.

The signal that matters — a removed fact that many independent sources keep
asserting — is then a read over existing state rather than a review queue.

## Why merge redirects

A merge asserts the two objects are the same fact; it already folds the loser's
evidence onto the survivor and refreshes `last_confirmed` from the pair. A
later restatement in the loser's wording carries evidence that belongs on the
survivor, and dropping it would make the survivor read staler than it is.

Redirect changes only *which* object the candidate is judged against, never
whether it is judged. `_classify_change` still runs, so a restatement carrying
a different threshold, sign, polarity, or status becomes `conflict` and reaches
review.

Two cases follow from the model:

- **Chains.** A survivor later merged into a third object leaves
  `L -> S1 -> S2`. Resolution follows `redirect_to` transitively, with a cycle
  guard.
- **Survivor later removed.** The chain terminates at a `removed` tombstone and
  the candidate is suppressed.

## Tombstone record

```json
{
  "object_id": "policy-fx-pnl-excess-threshold",
  "kind": "merged",
  "redirect_to": "metric-fx-excess-threshold",
  "category": "policies",
  "title": "FX P&L excess threshold",
  "statement": "…",
  "created_at": "2026-08-06T09:00:00Z",
  "reviewer": "Rui",
  "note": "Duplicate of the metric object.",
  "manifest_path": ".knowledge-state/cleanup-runs/permanent-removal-….json"
}
```

`redirect_to` is required for `merged` and forbidden for `removed`.
`manifest_path` is present for removals, which write a cleanup manifest, and
absent for merges, which do not.

## Repository invariants

`validate_all` gains a tombstone pass:

- a tombstoned ID may not also exist as a live canonical object
- every `merged` tombstone's `redirect_to` must resolve to a live object or to
  another tombstone
- redirect chains may not cycle

The result dictionary gains a `tombstones` count.

`validate_run_manifest` treats `candidates_suppressed` as optional and
validates its shape only when present, so run manifests written before this
change stay valid.

## Lifting a tombstone

`knowledge tombstone list` and
`knowledge tombstone lift <id> --reviewer --note`.

Lifting does **not** restore deleted content — that is gone, and reconstructing
evidence digests against sources that may have changed would be a fabrication.
Lifting only stops the blocking, so the next run re-extracts the fact from
whatever evidence exists now.

Lifting is refused when another tombstone redirects through the target, which
would leave that chain dangling. Each lift writes
`.knowledge-state/cleanup-runs/tombstone-lift-<run>.json` recording the lifted
record, the reviewer, and the note.

There is no UI affordance. Reversal should cost what the removal cost.

## Backfill

Removals already on disk can be reconstructed from
`.knowledge-state/cleanup-runs/*.json`, which records each removed object's ID,
title, category, and digest. Merges cannot: no structured record exists, only
survivor history prose. Merges performed before this change remain vulnerable
unless re-asserted by hand.

Backfill is not automatic — an operator runs it deliberately, because a
cleanup manifest may describe an object that has since been legitimately
re-created.

## Testing

- A removed object is not re-created by a forced re-run of its source.
- A removed object is not re-created by a later source restating it under
  drifted wording.
- Suppression appears in the run manifest with its blocking tombstone.
- A merged loser's restatement reconfirms the survivor rather than creating a
  duplicate.
- A conflicting restatement of a merged loser still reaches review.
- Redirect chains resolve transitively; cycles are rejected by validation.
- A merged tombstone whose survivor was later removed suppresses.
- Two ambiguously matching tombstones produce `needs_review`.
- Lifting a tombstone allows the fact to return on the next run.
- Lifting is refused while another tombstone redirects through it.
- `validate_all` rejects a tombstone that is also a live object.
- Run manifests written before `candidates_suppressed` still validate.
