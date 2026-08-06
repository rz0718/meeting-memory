import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeRequestBody,
  objectStatement,
  shouldRestoreApply,
} from "../../src/meeting_memory/ui/static/js/merge_form.js";

const objects = [
  { id: "retiring", statement: "Retiring source statement." },
  { id: "survivor", statement: "Surviving source statement." },
];

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
