import test from "node:test";
import assert from "node:assert/strict";

import {
  chartSeries,
  fullDateLabel,
  pointX,
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
