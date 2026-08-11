import test from "node:test";
import assert from "node:assert/strict";

import {
  chartSeries,
  fullDateLabel,
  pointX,
  runOptionLabel,
  yTicks,
} from "../../src/meeting_memory/ui/static/js/runs_chart.js";

function run(day, count, sourcesProcessed = 0) {
  return {
    started_at: `2026-07-${String(day).padStart(2, "0")}T08:00:00Z`,
    counts: { objects_created: count, sources_processed: sourcesProcessed },
  };
}

test("chartSeries limits newest-first API runs and returns them oldest-first", () => {
  const runs = Array.from({ length: 13 }, (_, index) => run(31 - index, index));

  const series = chartSeries(runs);

  assert.equal(series.length, 12);
  assert.equal(series[0].date, "2026-07-20");
  assert.equal(series.at(-1).date, "2026-07-31");
});

test("chartSeries exposes compact UTC dates, created-object counts, and sources processed", () => {
  const [entry] = chartSeries([run(29, 7, 5)]);

  assert.deepEqual(entry, {
    date: "2026-07-29",
    dateLabel: "Jul 29",
    count: 7,
    sourcesProcessed: 5,
  });
});

test("fullDateLabel renders an English UTC calendar date", () => {
  assert.equal(fullDateLabel("2026-08-04T12:50:00Z"), "August 4, 2026");
});

test("fullDateLabel preserves malformed values", () => {
  assert.equal(fullDateLabel("not-a-date"), "not-a-date");
  assert.equal(fullDateLabel(""), "—");
});

function manifest(runId, startedAt, status = "success") {
  return { run_id: runId, started_at: startedAt, status };
}

test("runOptionLabel leaves a date bare when it holds one run", () => {
  const runs = [
    manifest("20260810T125024Z", "2026-08-10T12:50:24Z"),
    manifest("20260806T125026Z", "2026-08-06T12:50:26Z"),
  ];

  assert.equal(runOptionLabel(runs[0], runs), "2026-08-10");
  assert.equal(runOptionLabel(runs[1], runs), "2026-08-06");
});

test("runOptionLabel separates reruns of the same date by UTC time and status", () => {
  const nightly = manifest(
    "20260807T125025Z",
    "2026-08-07T12:50:25Z",
    "partial_failure"
  );
  const recovery = manifest("20260807T131453Z", "2026-08-07T13:14:53Z");
  const runs = [recovery, nightly, manifest("20260806T125026Z", "2026-08-06T12:50:26Z")];

  assert.equal(runOptionLabel(nightly, runs), "2026-08-07 12:50 · partial failure");
  assert.equal(runOptionLabel(recovery, runs), "2026-08-07 13:14 · success");
  assert.notEqual(runOptionLabel(nightly, runs), runOptionLabel(recovery, runs));
});

test("runOptionLabel keeps every same-date option distinct across a busy day", () => {
  const runs = [
    manifest("20260723T143810Z", "2026-07-23T14:38:10Z"),
    manifest("20260723T143247Z", "2026-07-23T14:32:47Z"),
    manifest("20260723T125026Z", "2026-07-23T12:50:26Z", "partial_failure"),
    manifest("20260723T021512Z", "2026-07-23T02:15:12Z"),
    manifest("20260723T021044Z", "2026-07-23T02:10:44Z", "failed"),
  ];

  const labels = runs.map((run) => runOptionLabel(run, runs));

  assert.equal(new Set(labels).size, runs.length);
  assert.equal(labels.at(-1), "2026-07-23 02:10 · failed");
});

test("runOptionLabel falls back to the run id when the timestamp is missing", () => {
  const broken = { run_id: "20260807T131453Z", started_at: "", status: "success" };

  assert.equal(runOptionLabel(broken, [broken]), "20260807T131453Z");
  assert.equal(runOptionLabel({}, []), "—");
});

test("yTicks returns an integer scale above the largest count", () => {
  const ticks = yTicks([0, 3, 7]);

  assert.deepEqual(ticks, [0, 2, 4, 6, 8]);
  assert.ok(ticks.every(Number.isInteger));
});

test("yTicks gives an all-zero series a visible scale", () => {
  assert.deepEqual(yTicks([0, 0]), [0, 1]);
});

test("pointX centers a single run and spans the plot for multiple runs", () => {
  assert.equal(pointX(0, 1, 48, 600), 348);
  assert.equal(pointX(0, 3, 48, 600), 48);
  assert.equal(pointX(1, 3, 48, 600), 348);
  assert.equal(pointX(2, 3, 48, 600), 648);
});
