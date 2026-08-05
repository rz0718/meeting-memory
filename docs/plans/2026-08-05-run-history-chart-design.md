# Run History Chart Design

## Goal

Make the run summary easier to understand by clarifying source processing and
turning the small fixed-width sparkline into a full-width history chart. The
chart continues to measure only knowledge objects created per run.

## Source processing summary

Present the source counts in one explicit sentence:

> 13 sources examined · 9 processed · 4 unchanged

Add a short explanation that unchanged sources required no new processing. The
values come from the selected run's existing summary counts.

## Knowledge objects chart

Replace the 220-by-34-pixel sparkline with a responsive SVG chart in a
full-width card titled **Knowledge objects created**.

The chart will:

- plot up to the last 12 runs, ordered oldest to newest;
- show run dates on the x-axis;
- show integer knowledge-object counts on the y-axis;
- label each point with its exact created-object count;
- include subtle horizontal grid lines; and
- expose the full run date and count to hover, keyboard focus, and assistive
  technology.

The chart will fill its container while retaining a useful fixed visual height.
Date labels will use a compact format and adapt on narrow screens so they stay
legible.

## Edge cases

A single run will still render as one labeled point instead of hiding history.
No run-history state will render an explanatory empty message. A maximum y-axis
value of zero will still produce a valid scale and visible zero-value points.

## Architecture and data flow

This is a frontend-only change. The existing `/api/runs` response already
contains each run's `started_at` value and `counts.objects_created`. The run
view will transform those values into chart geometry without changing the API
or persisted manifests.

The chart will use the repository's existing DOM and SVG construction style and
will not add a charting dependency.

## Verification

Add focused frontend tests for chronological ordering, rendered dates and
counts, and the single-run case where practical in the current test setup. Run
the existing UI route and static-asset tests, and verify the responsive layout
in the served UI.
