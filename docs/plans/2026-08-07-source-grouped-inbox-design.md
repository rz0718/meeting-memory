# Source-Grouped Knowledge Inbox Design

## Problem

The Knowledge Updates page groups a run's objects by what happened to them —
Created, Refined, Reconfirmed. That answers "what changed today". It does not
answer the other question users bring to the page: **"what came out of this
meeting?"** or **"what did that Slack channel produce today?"**

Nothing in the list pane names a source at all. Source appears in two places,
neither of them usable for this:

- the reader's **Evidence** blocks, which show a path per excerpt
  (`evidence.js:56`) — one object at a time, after it has been selected; and
- `sourceSummary` under **Run details** (`runs.js:654`), which shows only
  counts: examined, processed, unchanged.

So a user who wants the cluster from one meeting has to open each object in
turn and read its evidence paths. On a seventeen-object run that is seventeen
selections to answer one question.

## Chosen behavior

Add a **Group by: Change | Source** control. It changes how the left pane is
grouped and nothing else. The reader, Previous/Next, `j`/`k`, search, and the
category filter all keep working exactly as they do now.

In Source grouping, each group header names one source file — one meeting or one
Slack channel snapshot, which are already per-day artifacts — and the rows
underneath carry their outcome (`CREATED`, `REFINED`, `RECONFIRMED`) as the row
badge instead of the group heading. Groups are expanded by default.

```
Group by:  [ Change ]  [ Source ]

⌄ ICEx Model Discussion · 2026-08-04                    7
    decisions  CREATED
    Conservative market-making revenue model
    The team adopts a conservative modeling approach for…

    systems  REFINED
    GitHub Page for Production Code

⌄ Slack C0194TGL94H · 2026-08-04                        4
    projects  CREATED
    USDT Wallet Launch

› Weekly Risk Sync · 2026-08-03                         6
```

A grouping toggle is preferred over a fourth filter chip. A source dropdown
would make the user name the source before seeing it, and would surface one
source at a time; regrouping shows every source in the run at once and needs no
prior knowledge of what the run contained. The two are not equivalent in effort
either — the grouping key is a swap in the same list renderer.

Two supporting changes keep the feature from being a toggle nobody finds:

- **In Change grouping, each row gains a source line** beneath its statement
  preview. Attribution then never requires switching modes, and the toggle
  becomes an amplifier rather than the only way to learn where a row came from.
- **The sources processed in Run details become clickable.** `sourceSummary`
  currently prints counts only; it gains the list of processed sources, and
  activating one switches to Source grouping, expands that group, and selects
  its first present object. That is the path a user actually walks — they are
  looking at the run's sources when the question occurs to them.

## Attribution rule

A row is attributed to **the source of its latest evidence among the sources
this run processed**, falling back to the latest evidence overall.

The "latest among this run's sources" qualifier matters. A run covers a range of
meeting dates — the current one spans 2026-08-03 to 2026-08-04 — and an object
touched by an 08-03 meeting may already carry 08-04 evidence from an earlier
run. Unqualified "latest evidence" would file that object under a meeting that
had nothing to do with this run's outcome. The manifest already lists
`sources_processed`, so the qualifier costs one set membership test.

Latest means the greatest `observed_at`, with ties broken by position: evidence
is appended, never reordered (`merge.py:58`), so the last entry is the most
recently added.

This rule is sound because every outcome path appends the processing source's
evidence before recording the bucket — `objects_created` at `pipeline.py:283`,
`objects_reconfirmed` at `pipeline.py:308` (which is only reached when
`_append_evidence` actually added something), and `objects_refined` at
`pipeline.py:338`. An object in a run's manifest therefore carries evidence from
the source that put it there.

**Residual imprecision, accepted.** Two sources processed in the same run can
both touch one object; the row is attributed to one of them, and if a refinement
appends evidence whose anchor is already present the attribution can fall back
to older evidence from another file. The manifest records no object-to-source
attribution, so no display rule can recover the true edge. The honest fix is a
future `objects_by_source` map written by the pipeline; this design deliberately
does not add one, because reading it from evidence is correct for the common
case and requires no change to stored data. Rows that cannot be attributed at
all — removed objects, which keep only an ID, and objects with no evidence —
collect in a trailing **Unattributed** group rather than being hidden or guessed
at.

## Source labels

The row payload carries the portable `meetings/...` identifier; the run payload
carries a display label per processed source. Labels are derived, never stored:

- **Meetings** use the source's front-matter `title` when it is a non-empty
  string, otherwise the file stem with `-` and `_` turned into spaces. Many
  meeting notes have no front matter at all, so the stem fallback is the normal
  path, not an error path.
- **Slack snapshots** are recognized by `source_type: slack` in front matter
  (`slack.py:334-341`) and labeled from `slack_channel_id` — `Slack
  C0194TGL94H`. The collector never resolves channel names (it resolves user
  names only, `slack.py:236`), so the repository does not know that channel's
  human name and the UI must not invent one. If the collector later records a
  channel name, the label picks it up with no UI change.
- **Unreadable or missing sources** fall back to the stem of their path. A
  source that has been deleted since the run must not fail the run payload —
  the same posture the reader takes with an `unavailable` excerpt.

The date shown beside a label comes from the `meetings/<date>/...` path segment,
which is the source's own calendar day. It is shown because a run can span
several meeting dates and two days of the same recurring meeting would otherwise
render as two identically named groups.

## Grouping and ordering

Source groups are ordered by source date descending, then by label
case-insensitively, with **Unattributed** always last. Within a group, rows keep
the payload's bucket order — created, then refined, then reconfirmed — so the
run's news still reads first inside each source.

Unlike Reconfirmed in Change grouping, no source group is collapsed by default.
Collapsing one would hide part of the answer to the question the mode exists to
answer.

The **Group by** control renders only when the run has at least two distinct
attributed sources. On a single-source run the toggle is a no-op and would only
add chrome. When it is hidden the list renders as Change grouping, but
`state.groupBy` is not modified, so a user who chose Source grouping still gets
it back on the next multi-source run.

## Interaction with existing behavior

- **Filters compose and never refetch.** Search and category filtering run over
  the already-loaded run detail, and so does regrouping. Changing the grouping
  must not enter `runsView.render` or issue a request, for the same reason
  filtering must not (`runs.js:131-134`).
- **Selection survives regrouping** when the selected object is still in the
  filtered list; the row moves between groups, it does not disappear. If it is
  filtered out, selection falls back to `defaultSelectionId` over the new
  grouping, as it does today.
- **Removed rows keep their tombstone treatment.** The Live/Removed split
  (`runs.js:357-402`) is keyed by group and continues to apply in either mode;
  in Source grouping removed rows sit in Unattributed, since a tombstone carries
  no evidence to attribute.
- **Group expansion state is per mode.** Change-mode bucket state and
  source-mode group state occupy separate keys, so collapsing Reconfirmed does
  not silently collapse a source group.
- **`runsView.count()` is unchanged.** The header count is the number of objects
  in the list, and regrouping does not change that number.

## Non-goals

- No new API route, no manifest schema change, no stored attribution.
- No cross-run source view. This is scoped to the selected run, like the rest of
  the page.
- No source-level actions: no marking a source read, no per-source export.
- No filtering by source. The grouping shows every source at once, which is the
  point; a filter that hides the others can be added later if asked for.

## Accessibility

- The **Group by** control is a pair of buttons carrying `aria-pressed`, not a
  color-only segmented graphic.
- Source group headers reuse the existing `group-toggle` disclosure with its
  `aria-expanded` handling, so keyboard operation is unchanged.
- Source is conveyed as text on the row and in the heading; nothing about
  attribution is encoded in color.
- Outcome badges continue to carry text in both modes.

## Verification

The repository verifies JavaScript with dependency-free `node --test` files over
exported pure functions, and route and static-asset behavior with pytest. The
grouping logic must therefore land in `knowledge_inbox.js` as pure functions —
source grouping, group ordering, and the unattributed bucket — rather than
inside the renderer.

Tests should cover:

- attribution picking the latest evidence among the run's processed sources,
  including the case where a later-dated evidence entry from a source this run
  did not process must not win;
- attribution falling back to the overall latest evidence when no evidence
  matches the run's processed sources;
- a source with no front matter, a source whose file is missing, and a Slack
  source, all producing labels without raising;
- source-group ordering by date descending then label, with Unattributed last;
- removed rows and evidence-free rows landing in Unattributed;
- rows carrying their outcome label through regrouping, so the badge in Source
  grouping matches the group heading in Change grouping;
- search and category filters composing with either grouping, and regrouping
  issuing no request;
- selection surviving a regroup, and falling back when the selection is filtered
  out;
- the **Group by** control being withheld on a single-source run without
  discarding the stored preference; and
- the Run details source list switching modes and selecting into the chosen
  group.
