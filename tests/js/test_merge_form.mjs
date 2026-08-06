import test from "node:test";
import assert from "node:assert/strict";

import {
  filterSurvivorCandidates,
  mergeRequestBody,
  moveActiveIndex,
  objectStatement,
  shouldRestoreApply,
} from "../../src/meeting_memory/ui/static/js/merge_form.js";

const objects = [
  { id: "retiring", statement: "Retiring source statement." },
  { id: "survivor", statement: "Surviving source statement." },
];

test("filterSurvivorCandidates matches title and exact-ID fragments", () => {
  const candidates = [
    { id: "decision-buying-power", title: "Buying power will be capped" },
    { id: "project-cloud-cleanup", title: "Cloud Skill automatic data cleanup" },
    { id: "decision-fallback", title: "Fallback" },
  ];

  assert.deepEqual(
    filterSurvivorCandidates(candidates, "BUYING").map((value) => value.id),
    ["decision-buying-power"],
  );
  assert.deepEqual(
    filterSurvivorCandidates(candidates, "cloud-cleanup").map((value) => value.id),
    ["project-cloud-cleanup"],
  );
});

test("filterSurvivorCandidates keeps order, tolerates missing titles, and limits results", () => {
  const candidates = Array.from({ length: 305 }, (_, index) => ({
    id: `knowledge-${index}`,
    ...(index === 0 ? {} : { title: `Knowledge ${index}` }),
  }));

  const result = filterSurvivorCandidates(candidates, "knowledge", 300);

  assert.equal(result.length, 300);
  assert.equal(result[0].id, "knowledge-0");
  assert.equal(result[299].id, "knowledge-299");
});

test("moveActiveIndex enters and wraps the survivor results", () => {
  assert.equal(moveActiveIndex(-1, 3, 1), 0);
  assert.equal(moveActiveIndex(-1, 3, -1), 2);
  assert.equal(moveActiveIndex(2, 3, 1), 0);
  assert.equal(moveActiveIndex(0, 3, -1), 2);
  assert.equal(moveActiveIndex(0, 0, 1), -1);
});

test("objectStatement provides the selected survivor's initial final statement", () => {
  assert.equal(objectStatement(objects, "survivor"), "Surviving source statement.");
});

test("objectStatement can select either source statement for editing", () => {
  assert.equal(objectStatement(objects, "retiring"), "Retiring source statement.");
  assert.equal(objectStatement(objects, "survivor"), "Surviving source statement.");
  assert.equal(objectStatement(objects, "missing"), "");
});

test("mergeRequestBody trims the final statement and note", () => {
  const request = mergeRequestBody({
    loserId: "retiring",
    survivorId: "survivor",
    note: " Same fact. ",
    statement: " Combined wording. ",
    allowCrossCategory: true,
    allowConflictingNumbers: false,
  });

  assert.equal(request.note, "Same fact.");
  assert.equal(request.statement, "Combined wording.");
});

test("mergeRequestBody transmits IDs and both explicit override booleans", () => {
  assert.deepEqual(
    mergeRequestBody({
      loserId: "retiring",
      survivorId: "survivor",
      note: " Same fact. ",
      statement: " Combined wording. ",
      allowCrossCategory: true,
      allowConflictingNumbers: false,
    }),
    {
      loser_id: "retiring",
      survivor_id: "survivor",
      note: "Same fact.",
      statement: "Combined wording.",
      allow_cross_category: true,
      allow_conflicting_numbers: false,
    },
  );
});

test("a failed apply is restored only while its merge draft is still current", () => {
  assert.equal(shouldRestoreApply(3, 3), true);
  assert.equal(shouldRestoreApply(3, 4), false);
});
