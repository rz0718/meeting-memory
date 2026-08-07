# Source-Grouped Knowledge Inbox Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a reader see everything one meeting or one Slack channel produced in a run, by regrouping the knowledge inbox's list pane by source instead of by outcome.

**Architecture:** Attribute each run-detail row to a source in `payloads.py` — the latest evidence among the run's processed sources — and add a per-source display label derived from front matter. Add pure regrouping helpers to `knowledge_inbox.js` so grouping stays unit-testable without a DOM, then swap the grouping key inside the existing `runs.js` list renderer. No new API route, no manifest change, no stored attribution.

**Tech Stack:** Python/FastAPI payload builders, browser ES modules, CSS, Node built-in test runner, pytest

Design reference: `docs/plans/2026-08-07-source-grouped-inbox-design.md`.

---

### Task 1: Attribute run-detail rows to a source

**Files:**
- Modify: `tests/test_ui_routes.py:158-245`
- Modify: `src/meeting_memory/ui/payloads.py:208-284`

**Step 1: Write the failing payload tests**

Add tests to the run-detail group in `tests/test_ui_routes.py`, near
`test_run_detail_groups_by_manifest_bucket` (`tests/test_ui_routes.py:459`).
`UiTestCase` already writes one source at `meetings/2026-07-29/project-update.md`
with **no front matter**, which is the stem-fallback case; add a second source
directory and a Slack snapshot where a test needs them.

Cover:

- a row carries `source` equal to the evidence source; labels live only in the
  payload's `sources` array, so the row stays small and one label is derived per
  distinct source rather than per row;
- with evidence from two sources, the row is attributed to the one with the
  greater `observed_at`;
- when the later-dated evidence names a source **not** in the manifest's
  `sources_processed`, the row is attributed to the processed source instead;
- when no evidence names a processed source, the row falls back to the latest
  evidence overall;
- a row for an object that is no longer present, and a row for an object with
  no evidence, both carry `source: None`;
- a source with no front matter is labeled from its file stem with `-` and `_`
  as spaces (`project-update.md` → `project update`);
- a source whose file has been deleted still produces a label and does not raise;
- a source with `source_type: slack` and `slack_channel_id: C0194TGL94H` is
  labeled `Slack C0194TGL94H` with `kind: "slack"`;
- a source with a front-matter `title` uses that title with `kind: "meeting"`;
- the payload's `sources` array describes every processed source in manifest
  order and includes a `date` read from the `meetings/<date>/` path segment; and
- `sources_processed` still lists the same plain strings it does today.

**Step 2: Run the tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py -q -k "run_detail or source"`

Expected: FAIL — rows have no `source` key.

**Step 3: Describe a source**

In `payloads.py`, import `PurePosixPath` from `pathlib`, `EvidenceError` from
`..knowledge.errors`, and `parse_frontmatter` from `..knowledge.util`
(`util.py:178`). Add:

```python
_SOURCE_DATE = re.compile(r"^meetings/(?P<date>\d{4}-\d{2}-\d{2})/")


def _describe_source(repository: KnowledgeRepository, source: str) -> Dict[str, Any]:
    """Name a source for display. Never raises: an unreadable source still
    has to render, the same way an unavailable excerpt does."""
```

Behavior, in order:

1. build the fallback label from `PurePosixPath(source).stem` with `-` and `_`
   replaced by spaces, falling back to `source` itself if that is empty;
2. read the file through `repository.evidence_path(source)` and
   `parse_frontmatter`, catching `EvidenceError`, `OSError`, `UnicodeError`, and
   `SchemaError` and returning the fallback on any of them — sources without
   front matter are ordinary, not exceptional;
3. when `source_type` is `"slack"`, return `kind: "slack"` labeled
   `"Slack %s" % slack_channel_id` when that ID is a non-empty string, else the
   fallback. Do not invent a channel name — the collector resolves user names
   only (`slack.py:236`) and no channel name is stored anywhere;
4. otherwise use a non-empty string front-matter `title`, else the fallback,
   with `kind: "meeting"`.

Return `{"source", "label", "date", "kind"}`, where `date` comes from
`_SOURCE_DATE` or is `None`.

**Step 4: Attribute rows**

Add the attributor and use it from `run_detail_payload`:

```python
class _SourceAttribution:
    """Which of a run's processed sources an object came from.

    Every outcome path appends the processing source's evidence before it
    records the bucket (pipeline.py:283,308,338), so an object in a manifest
    carries evidence from the source that put it there. Evidence is appended
    and never reordered (merge.py:58), so position breaks observed_at ties.
    """

    def __init__(self, repository, sources_processed):
        self._repository = repository
        self._processed = set(sources_processed)
        self._described: Dict[str, Dict[str, Any]] = {}

    def source_for(self, obj: Optional[KnowledgeObject]) -> Optional[str]:
        if obj is None or not obj.evidence:
            return None
        pool = [item for item in obj.evidence if item.source in self._processed]
        if not pool:
            pool = list(obj.evidence)
        latest = max(
            range(len(pool)), key=lambda index: (pool[index].observed_at or "", index)
        )
        return pool[latest].source

    def describe(self, source: str) -> Dict[str, Any]:
        if source not in self._described:
            self._described[source] = _describe_source(self._repository, source)
        return self._described[source]
```

The cache keeps this to one read per distinct source per request.

Give `_object_row` an `attribution` parameter, and set `row["source"]` from
`attribution.source_for(obj)`. Keep every existing key exactly as it is.

In `run_detail_payload`, construct the attributor from
`manifest["sources_processed"]`, pass it into `_object_row`, and add one new
top-level key:

```python
"sources": [attribution.describe(value) for value in ordered_sources],
```

where `ordered_sources` is the manifest's `sources_processed` in order, followed
by any row source not already in that list, sorted. The second part matters
because a fallback attribution can name a source this run did not process, and
the list pane needs a label for it. Leave `sources_processed` as the plain
string list it is today — `sourceSummary` and the chart read counts from it.

**Step 5: Run the payload tests**

Run: `.venv/bin/python -m pytest tests/test_ui_routes.py -q`

Expected: all tests PASS.

---

### Task 2: Add pure source-grouping helpers

**Files:**
- Modify: `tests/js/test_knowledge_inbox.mjs`
- Modify: `src/meeting_memory/ui/static/js/knowledge_inbox.js`

**Step 1: Write the failing helper tests**

Extend `tests/js/test_knowledge_inbox.mjs` with fixtures shaped like the Task 1
payload — `groups` of `{bucket, label, count, rows}` where rows carry `source`,
plus a top-level `sources` array of `{source, label, date, kind}`. Cover:

- `sourceGroups` produces one group per distinct row source, keyed by the source
  identifier, labeled from the `sources` entry;
- rows land in their group in payload bucket order — created, then refined, then
  reconfirmed;
- each row carries `outcomeLabel` equal to its bucket group's label, so the row
  badge in source mode says what the heading says in change mode;
- groups sort by `date` descending, then by label case-insensitively;
- rows with a null `source` collect in a single trailing **Unattributed** group,
  after every dated group;
- a row whose source has no `sources` entry still forms a group, labeled with
  the raw identifier rather than being dropped;
- `attributedSourceCount` counts distinct non-unattributed sources and is `0`
  for a run whose rows are all unattributed;
- `groupsFor(detail, "change")` equals `inboxGroups(detail)` and
  `groupsFor(detail, "source")` equals `sourceGroups(detail)`;
- every group from either mode exposes a `key`; and
- `filterGroups` and `defaultSelectionId` behave the same over source groups as
  over bucket groups, including dropping emptied source groups.

**Step 2: Run the tests to verify failure**

Run: `node --test tests/js/test_knowledge_inbox.mjs`

Expected: FAIL — the new exports do not exist.

**Step 3: Implement the helpers**

Add to `knowledge_inbox.js`, with no DOM access:

```js
export const UNATTRIBUTED = "__unattributed__";

export function sourceGroups(detail) { /* one group per row source, ordered */ }
export function attributedSourceCount(detail) { /* distinct non-null row sources */ }
export function groupsFor(detail, groupBy) { /* "source" or change grouping */ }
```

Have `inboxGroups` add `key: group.bucket` to each group so both modes expose the
same identity field; `collapsed` keeps its current meaning and is `false` for
every source group — collapsing one by default would hide part of the answer the
mode exists to give.

Build source groups by iterating `inboxGroups(detail)` outermost, so bucket order
is preserved inside each group and each row can be tagged
`{ ...row, outcomeLabel: group.label }` from the payload's own label. Do not
duplicate `BUCKET_LABELS` in JavaScript; carrying the label across is what keeps
the two languages from drifting.

Sort with unattributed forced last, then `date` descending treating a missing
date as empty, then label compared case-insensitively.

**Step 4: Run the helper tests**

Run: `node --test tests/js/test_knowledge_inbox.mjs`

Expected: all tests PASS.

---

### Task 3: Swap the grouping key in the list pane

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js:13-40,89-117,142-209,337-434,469-492,940-965`

**Step 1: Replace the seeded expansion state**

Add `groupBy: "change"` to `state` and change `expanded` to a plain `{}`. Delete
the `ORDINARY_BUCKETS` / `COLLAPSED_BY_DEFAULT` seed at `runs.js:35-37` and drop
both from the import at `runs.js:13-22` if nothing else uses them. Group
expansion becomes:

```js
const key = `${effectiveGroupBy()}:${group.key}`;
const open = state.expanded[key] ?? !group.collapsed;
```

which reproduces today's behavior — reconfirmed collapsed, the rest open —
while keeping the two modes' expansion state in separate key spaces. Update
`subsectionNode` (`runs.js:410`) to key on `group.key` instead of `group.bucket`
for the same reason.

**Step 2: Add one grouping accessor and route every reader through it**

```js
function effectiveGroupBy() {
  return state.groupBy === "source" && attributedSourceCount(state.detail) >= 2
    ? "source"
    : "change";
}

function currentGroups() {
  return filterGroups(groupsFor(state.detail, effectiveGroupBy()), state.filters);
}
```

Replace every `filterGroups(inboxGroups(state.detail), state.filters)` with
`currentGroups()` — `refreshRunDetail` (`runs.js:106`), `renderList`
(`runs.js:340`), `renderReader` (`runs.js:484`), and `handleRunsKey`
(`runs.js:951`). `readerEmptyState` (`runs.js:494`) keeps using `inboxGroups`
directly: its first two messages are about what the run contains, not about how
the list is arranged.

The fallback in `effectiveGroupBy` reads `state.groupBy` without writing it, so a
user's choice survives a single-source run.

**Step 3: Add the Group by control**

In `filterBar` (`runs.js:142`), append a chip when
`attributedSourceCount(state.detail) >= 2`, before the return at `runs.js:208`:

```js
const groupChip = el("span", { class: "chip" }, [
  el("span", { class: "chip__label", text: "Group by" }),
  el("span", { class: "segmented" }, [
    segmentButton("change", "Change"),
    segmentButton("source", "Source"),
  ]),
]);
```

Each button carries `aria-pressed` and, on click, sets `state.groupBy`, re-mounts
the filter bar so the pressed state updates, and calls `onFilterChange()`. It
must not call `api.run`, `api.runs`, or `runsView.render` — regrouping reads the
run detail already in memory, exactly as filtering does.

`clearFilters` (`runs.js:136`) must not reset `groupBy`. Grouping is not a
filter, and clearing a search should not rearrange the list.

**Step 4: Show the outcome on the row in either mode**

In `listItemNode` (`runs.js:436`), the outcome span becomes
`row.outcomeLabel ?? group.label`. In change mode nothing changes; in source mode
the badge says `CREATED` / `REFINED` / `RECONFIRMED` while the heading names the
source.

Apply the same fix in the reader header at `runs.js:580`, which currently prints
`row.groupLabel` from `flattenRows`: use `row.outcomeLabel ?? row.groupLabel`, so
the reader keeps naming the outcome rather than the source.

**Step 5: Render the source group header**

In `listGroupNode` (`runs.js:357`), when the group carries a `date`, render it
after the label as secondary text within the same disclosure button, keeping the
existing `group-toggle__count`. The Live/Removed subsection split is unchanged
and continues to apply per group.

**Step 6: Show the source on the row in change mode**

When `effectiveGroupBy() === "change"` and `row.source` is set, add a fourth line
to `listItemNode` with the source's label from the detail's `sources` array, so
attribution is visible without switching modes. Skip it entirely when the row is
unattributed rather than rendering an em dash.

---

### Task 4: Make Run details' processed sources open their group

**Files:**
- Modify: `src/meeting_memory/ui/static/js/runs.js:654-695`

**Step 1: List the processed sources**

`sourceSummary` (`runs.js:654`) currently prints counts only. Below the existing
counts line and its note, render `state.detail.sources_processed` as a list of
buttons, each labeled from the matching `sources` entry with its date, and each
titled with the raw `meetings/...` identifier so the path is still recoverable.
Leave the counts, the unchanged note, and the rejected- and
suppressed-candidate notes exactly as they are.

**Step 2: Open the group from the list**

Activating one calls:

```js
function openSourceGroup(source) {
  state.groupBy = "source";
  state.expanded[`source:${source}`] = true;
  setRunDetailsOpen(false);
  if (currentCtx) mount(currentCtx.filters, filterBar(currentCtx));
  renderList();
  const first = currentGroups()
    .find((group) => group.key === source)
    ?.rows.find((row) => row.present);
  if (first) selectItem(first.id);
  document.querySelector(`[data-source-group="${CSS.escape(source)}"]`)
    ?.scrollIntoView({ block: "start" });
}
```

Closing Run details is deliberate: the section sits above the split
(`runs.js:213-229`), so leaving it open pushes the group the user just asked for
off screen. Tag each source group's node with `data-source-group` in
`listGroupNode` for the scroll target.

If the run has fewer than two attributed sources, `effectiveGroupBy` still
returns `"change"` and the list simply does not regroup; the selection and scroll
must not throw in that case.

---

### Task 5: Style the grouping control and source rows

**Files:**
- Modify: `src/meeting_memory/ui/static/styles.css`

**Step 1: Style the segmented control**

Add a `.segmented` pair of buttons sized to sit inside the existing `.chip`
alongside the run-date `select`, with the pressed button distinguished by
background and weight, not by color alone. Use existing theme variables only.

**Step 2: Style the source affordances**

Add the group header's date as secondary text, the row's source line in the same
muted register as `.inbox__item-preview` but on one clamped line, and the Run
details source buttons as a plain text list — they are navigation, not primary
actions, and must not read as a second stat row.

---

### Task 6: Verify integration and regressions

**Files:**
- Modify: `tests/test_ui_routes.py:1931-1958`

**Step 1: Pin the view contract**

Extend the static-source assertions on `runs.js`:

- `text: "Group by"` is present;
- `state.groupBy` and `effectiveGroupBy()` are present;
- the `onFilterChange` body still contains no `api.runs()`, no `api.run(`, and no
  `runsView.render` — the existing
  `test_runs_view_filtering_does_not_reenter_the_render_path` covers this and
  must keep passing; and
- add the equivalent assertion for the grouping handler, so a later change cannot
  make regrouping refetch.

Keep every existing assertion in
`test_runs_view_has_only_the_run_date_control` passing: `text: "Run date"`,
`state.runs = await api.runs()`, `label: "Updates"`,
`title: "Knowledge Updates"`, and `fullDateLabel(summary.started_at)`.

**Step 2: Run focused tests**

Run:

```bash
node --test tests/js/test_knowledge_inbox.mjs tests/js/test_runs_chart.mjs
.venv/bin/python -m pytest tests/test_ui_routes.py -q
```

Expected: all tests PASS.

**Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests PASS.

**Step 4: Review the diff**

Confirm that no API route was added, that the run manifest schema is unchanged,
that `sources_processed` still carries plain strings, that no dependency was
added, that regrouping issues no network request, that grouping state survives
`clearFilters`, and that source labels are derived at read time rather than
written anywhere.
