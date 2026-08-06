# Safe Merge and Custom Final Statement Design

## Problem

Canonical-object merge commits its filesystem changes before running full
repository validation. A pre-existing, unrelated schema error can therefore
make a successful merge return an error. Retrying then reports that the retired
object no longer exists, even though the first request completed the merge.

The merge domain and API already accept a statement override, but the web
dialog does not expose it. Reviewers can only keep the survivor's statement,
even when the best final wording combines or improves both source statements.

## Chosen behavior

Every merge preview and apply performs full repository validation before it
constructs or commits the merge. A pre-existing repository error therefore
blocks the operation before any canonical, review, or index file is changed.
The existing post-merge validation remains as a defensive check that the merge
itself preserved repository invariants.

The merge dialog shows the retiring and survivor statements and provides an
editable **Final statement** textarea. It is prefilled with the survivor's
statement. Reviewers may keep that text, copy the retiring statement through a
shortcut, or write new wording. The final statement must contain non-whitespace
text.

## Audit model

The merge note remains a required audit rationale, separate from canonical
content. The survivor's `## History` records the date, retired object ID,
reviewer, evidence count, and note. The note does not become part of the final
statement.

The final statement and note are both part of the existing preview decision
fingerprint. Changing either field, the target, or an override after a preview
invalidates the UI's Apply state and requires another preview. The server's
fingerprint check remains the authoritative protection against applying a
decision different from the one reviewed.

## UI flow

1. The reviewer opens **Merge into...** on the object being retired.
2. The dialog shows that object's statement and lets the reviewer select the
   survivor.
3. The dialog shows the selected survivor's statement and prefills **Final
   statement** from it.
4. **Use survivor** and **Use retiring** copy either source statement into the
   editable field without preventing further edits.
5. Preview displays the statement transition and the existing deterministic
   merge summary.
6. Editing any decision field disables Apply until a new preview succeeds.
7. Apply commits exactly the previewed decision, refreshes the knowledge list,
   and links to the survivor.

## Error handling

- Blank or whitespace-only final statements are rejected before repository
  mutation.
- Repository-wide validation errors appear during preview and again protect
  apply if the repository changes after preview.
- Failed previews keep Apply disabled.
- Server-side preview fingerprints still reject stale or altered decisions.

## Testing

- A merge against a repository with a dangling historical suggestion reference
  fails before changing either canonical object.
- A custom final statement is present in dry-run output and persists on apply.
- Blank final statements are rejected without mutation.
- The merge route preserves parity with the CLI statement override.
- UI tests cover survivor-prefill, source-statement shortcuts, preview
  invalidation after edits, and transmission of the final statement.

