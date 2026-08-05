import test from "node:test";
import assert from "node:assert/strict";

import {
  ORDINARY_BUCKETS,
  COLLAPSED_BY_DEFAULT,
  inboxGroups,
  objectCount,
  reviewCandidateCount,
  matchesFilter,
  filterGroups,
  defaultSelectionId,
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
