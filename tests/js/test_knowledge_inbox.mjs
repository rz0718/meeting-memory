import test from "node:test";
import assert from "node:assert/strict";

import {
  ORDINARY_BUCKETS,
  COLLAPSED_BY_DEFAULT,
  UNATTRIBUTED,
  attributedSourceCount,
  inboxGroups,
  groupsFor,
  objectCount,
  openReviewIds,
  reviewCandidateCount,
  matchesFilter,
  filterGroups,
  defaultSelectionId,
  sourceGroups,
} from "../../src/meeting_memory/ui/static/js/knowledge_inbox.js";

function row(id, overrides = {}) {
  return {
    id,
    present: true,
    title: `Title ${id}`,
    category: "roadmap",
    statement: `Statement for ${id}`,
    ...overrides,
  };
}

function group(bucket, label, rows) {
  return { bucket, label, count: rows.length, rows };
}

function detailFixture(overrides = {}) {
  return {
    summary: { run_id: "run-1" },
    groups: [
      group("objects_created", "Created", [row("c1"), row("c2")]),
      group("objects_refined", "Refined", [row("r1")]),
      group("objects_reconfirmed", "Reconfirmed", [row("k1")]),
      group("review_items_created", "Sent to review", [row("v1"), row("v2")]),
    ],
    ...overrides,
  };
}

test("inboxGroups returns only the three ordinary buckets in payload order", () => {
  const groups = inboxGroups(detailFixture());

  assert.deepEqual(
    groups.map((entry) => entry.bucket),
    ["objects_created", "objects_refined", "objects_reconfirmed"],
  );
  assert.ok(!groups.some((entry) => entry.bucket === "review_items_created"));
});

test("inboxGroups marks reconfirmed collapsed by default and the others expanded", () => {
  const groups = inboxGroups(detailFixture());
  const byBucket = Object.fromEntries(groups.map((entry) => [entry.bucket, entry]));

  assert.equal(byBucket.objects_created.collapsed, false);
  assert.equal(byBucket.objects_refined.collapsed, false);
  assert.equal(byBucket.objects_reconfirmed.collapsed, true);
  assert.ok(COLLAPSED_BY_DEFAULT.has("objects_reconfirmed"));
  assert.deepEqual(ORDINARY_BUCKETS, [
    "objects_created",
    "objects_refined",
    "objects_reconfirmed",
  ]);
});

test("objectCount totals the ordinary rows only", () => {
  const groups = inboxGroups(detailFixture());

  assert.equal(objectCount(groups), 4);
});

test("reviewCandidateCount reports the excluded review rows", () => {
  assert.equal(reviewCandidateCount(detailFixture()), 2);
});

test("reviewCandidateCount is zero when there is no review bucket", () => {
  const detail = detailFixture({
    groups: [group("objects_created", "Created", [row("c1")])],
  });

  assert.equal(reviewCandidateCount(detail), 0);
});

test("openReviewIds keeps only cases still pending in the queue", () => {
  const detail = detailFixture({
    groups: [
      group("review_items_created", "Sent to review", [
        row("v1", { status: "pending" }),
        row("v2", { status: "resolved" }),
        row("v3", { status: "rejected" }),
        row("v4", { present: false, status: null }),
        row("v5", { status: "pending" }),
      ]),
    ],
  });

  assert.deepEqual(openReviewIds(detail), ["v1", "v5"]);
  assert.equal(reviewCandidateCount(detail), 5);
});

test("openReviewIds is empty when every case the run queued has gone", () => {
  const detail = detailFixture({
    groups: [
      group("review_items_created", "Sent to review", [
        row("v1", { present: false, status: null }),
        row("v2", { status: "resolved" }),
      ]),
    ],
  });

  assert.deepEqual(openReviewIds(detail), []);
  assert.equal(reviewCandidateCount(detail), 2);
});

test("openReviewIds is empty when there is no review bucket", () => {
  const detail = detailFixture({
    groups: [group("objects_created", "Created", [row("c1")])],
  });

  assert.deepEqual(openReviewIds(detail), []);
});

test("matchesFilter matches case-insensitively on title and statement", () => {
  const target = row("x1", { title: "Pricing Model", statement: "The plan raises Fees" });

  assert.ok(matchesFilter(target, { search: "pricing" }));
  assert.ok(matchesFilter(target, { search: "FEES" }));
  assert.ok(!matchesFilter(target, { search: "unrelated" }));
});

test("matchesFilter matches every row when the search string is empty", () => {
  const target = row("x1", { title: "Anything", statement: "Whatever" });

  assert.ok(matchesFilter(target, { search: "" }));
  assert.ok(matchesFilter(target, {}));
});

test("matchesFilter applies the category filter, and an empty category matches all", () => {
  const target = row("x1", { category: "pricing" });

  assert.ok(matchesFilter(target, { category: "pricing" }));
  assert.ok(!matchesFilter(target, { category: "roadmap" }));
  assert.ok(matchesFilter(target, { category: "" }));
});

test("filterGroups preserves group order and drops emptied groups", () => {
  const groups = inboxGroups(detailFixture());

  const filtered = filterGroups(groups, { search: "r1", category: "" });

  assert.deepEqual(
    filtered.map((entry) => entry.bucket),
    ["objects_refined"],
  );
  assert.equal(filtered[0].rows.length, 1);
  assert.equal(filtered[0].rows[0].id, "r1");
});

test("defaultSelectionId returns the first present row across the filtered groups", () => {
  const groups = inboxGroups(detailFixture());

  assert.equal(defaultSelectionId(groups), "c1");
});

test("defaultSelectionId skips rows whose present is false, including a missing first row", () => {
  const detail = detailFixture({
    groups: [
      group("objects_created", "Created", [
        row("c1", { present: false }),
        row("c2"),
      ]),
      group("objects_refined", "Refined", [row("r1")]),
      group("objects_reconfirmed", "Reconfirmed", [row("k1")]),
    ],
  });

  assert.equal(defaultSelectionId(inboxGroups(detail)), "c2");
});

test("defaultSelectionId returns null when a run has no present rows at all", () => {
  const detail = detailFixture({
    groups: [
      group("objects_created", "Created", [row("c1", { present: false })]),
      group("objects_refined", "Refined", [row("r1", { present: false })]),
      group("objects_reconfirmed", "Reconfirmed", [row("k1", { present: false })]),
    ],
  });

  assert.equal(defaultSelectionId(inboxGroups(detail)), null);
});

test("defaultSelectionId returns null when filters match nothing", () => {
  const groups = inboxGroups(detailFixture());
  const filtered = filterGroups(groups, { search: "no-such-match", category: "" });

  assert.equal(defaultSelectionId(filtered), null);
});

// -- source grouping ---------------------------------------------------------

const ICEX = "meetings/2026-08-04/icex-model-discussion.md";
const SLACK = "meetings/2026-08-04/slack-c0194tgl94h.md";
const RISK = "meetings/2026-08-03/weekly-risk-sync.md";

function sourced(overrides = {}) {
  return detailFixture({
    sources: [
      { source: ICEX, label: "ICEx Model Discussion", date: "2026-08-04", kind: "meeting" },
      { source: SLACK, label: "Slack C0194TGL94H", date: "2026-08-04", kind: "slack" },
      { source: RISK, label: "Weekly Risk Sync", date: "2026-08-03", kind: "meeting" },
    ],
    groups: [
      group("objects_created", "Created", [
        row("c1", { source: ICEX }),
        row("c2", { source: SLACK }),
      ]),
      group("objects_refined", "Refined", [row("r1", { source: ICEX })]),
      group("objects_reconfirmed", "Reconfirmed", [row("k1", { source: RISK })]),
      group("review_items_created", "Sent to review", [row("v1")]),
    ],
    ...overrides,
  });
}

test("sourceGroups produces one group per distinct row source, labeled from sources", () => {
  const groups = sourceGroups(sourced());

  assert.deepEqual(
    groups.map((entry) => entry.key),
    [ICEX, SLACK, RISK],
  );
  assert.deepEqual(
    groups.map((entry) => entry.label),
    ["ICEx Model Discussion", "Slack C0194TGL94H", "Weekly Risk Sync"],
  );
  assert.deepEqual(
    groups.map((entry) => entry.date),
    ["2026-08-04", "2026-08-04", "2026-08-03"],
  );
  assert.equal(groups[1].kind, "slack");
});

test("sourceGroups never includes review candidates", () => {
  const ids = sourceGroups(sourced()).flatMap((entry) => entry.rows.map((item) => item.id));

  assert.ok(!ids.includes("v1"));
});

test("sourceGroups keeps payload bucket order inside each group", () => {
  const groups = sourceGroups(sourced());

  assert.deepEqual(
    groups[0].rows.map((entry) => entry.id),
    ["c1", "r1"],
  );
});

test("sourceGroups carries the outcome label onto each row", () => {
  const groups = sourceGroups(sourced());

  assert.deepEqual(
    groups[0].rows.map((entry) => entry.outcomeLabel),
    ["Created", "Refined"],
  );
  assert.equal(groups[2].rows[0].outcomeLabel, "Reconfirmed");
});

test("sourceGroups orders by date descending, then label case-insensitively", () => {
  const detail = sourced({
    sources: [
      { source: ICEX, label: "zulu review", date: "2026-08-04", kind: "meeting" },
      { source: SLACK, label: "Alpha sync", date: "2026-08-04", kind: "slack" },
      { source: RISK, label: "Weekly Risk Sync", date: "2026-08-05", kind: "meeting" },
    ],
  });

  assert.deepEqual(
    sourceGroups(detail).map((entry) => entry.label),
    ["Weekly Risk Sync", "Alpha sync", "zulu review"],
  );
});

test("sourceGroups collects unattributed rows in one trailing group", () => {
  const detail = sourced({
    groups: [
      group("objects_created", "Created", [
        row("c1", { source: ICEX }),
        row("gone", { present: false, source: null }),
        row("c3", { source: null }),
      ]),
      group("objects_refined", "Refined", []),
      group("objects_reconfirmed", "Reconfirmed", []),
    ],
  });

  const groups = sourceGroups(detail);
  const last = groups[groups.length - 1];

  assert.equal(last.key, UNATTRIBUTED);
  assert.equal(last.label, "Unattributed");
  assert.deepEqual(
    last.rows.map((entry) => entry.id),
    ["gone", "c3"],
  );
});

test("sourceGroups labels a source with no sources entry from its identifier", () => {
  const orphan = "meetings/2026-08-02/orphan.md";
  const detail = sourced({
    groups: [
      group("objects_created", "Created", [row("c1", { source: orphan })]),
      group("objects_refined", "Refined", []),
      group("objects_reconfirmed", "Reconfirmed", []),
    ],
  });

  const groups = sourceGroups(detail);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].label, orphan);
});

test("sourceGroups tolerates a payload with no sources array", () => {
  const detail = detailFixture({
    sources: undefined,
    groups: [
      group("objects_created", "Created", [row("c1", { source: ICEX })]),
      group("objects_refined", "Refined", []),
      group("objects_reconfirmed", "Reconfirmed", []),
    ],
  });

  assert.deepEqual(
    sourceGroups(detail).map((entry) => entry.label),
    [ICEX],
  );
});

test("no source group is collapsed by default", () => {
  assert.ok(sourceGroups(sourced()).every((entry) => entry.collapsed === false));
});

test("attributedSourceCount counts distinct attributed sources only", () => {
  assert.equal(attributedSourceCount(sourced()), 3);

  const unattributed = sourced({
    groups: [
      group("objects_created", "Created", [row("c1", { source: null })]),
      group("objects_refined", "Refined", []),
      group("objects_reconfirmed", "Reconfirmed", []),
    ],
  });

  assert.equal(attributedSourceCount(unattributed), 0);
});

test("groupsFor selects the grouping and every group exposes a key", () => {
  const detail = sourced();

  assert.deepEqual(groupsFor(detail, "change"), inboxGroups(detail));
  assert.deepEqual(groupsFor(detail, "source"), sourceGroups(detail));
  assert.deepEqual(
    groupsFor(detail, "change").map((entry) => entry.key),
    ["objects_created", "objects_refined", "objects_reconfirmed"],
  );
  assert.ok(groupsFor(detail, "source").every((entry) => entry.key));
});

test("filterGroups and defaultSelectionId behave the same over source groups", () => {
  const groups = sourceGroups(sourced());

  const filtered = filterGroups(groups, { search: "k1", category: "" });
  assert.deepEqual(
    filtered.map((entry) => entry.key),
    [RISK],
  );

  assert.equal(defaultSelectionId(groups), "c1");
  assert.equal(defaultSelectionId(filtered), "k1");
  assert.equal(
    defaultSelectionId(filterGroups(groups, { search: "no-such-match", category: "" })),
    null,
  );
});
