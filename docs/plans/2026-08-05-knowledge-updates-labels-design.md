# Knowledge Updates Labels Design

## Goal

Replace the time-sensitive **Today's Knowledge** wording with labels that remain
accurate when a reviewer selects any historical run, and make the selected run
date easier to read.

## Considered approaches

1. Use **Updates** in compact navigation and **Knowledge Updates** as the page
   title. This preserves context in the main heading without crowding the
   sidebar or tab.
2. Use **Knowledge Updates** everywhere. This is consistent but unnecessarily
   long in both navigation locations.
3. Keep **Today's Knowledge** and append the selected date. This still conflicts
   with historical selections and duplicates information.

## Design

Use approach 1. The runs view's compact label becomes **Updates**, which the
existing application shell renders in both the sidebar and top tab alongside
the current count (for example, **Updates 17**). Its page title becomes
**Knowledge Updates**. The existing filter remains **Run date**.

Render the selected manifest heading as an English calendar date such as
**August 4, 2026**, derived from the manifest's UTC `started_at` date. Formatting
must not depend on the browser's local time zone, because run selection and the
manifest date are defined in UTC.

## Data flow and edge cases

No API or stored-data changes are required. The view continues to receive an
ISO timestamp, extracts its `YYYY-MM-DD` date, and passes it through a small pure
formatter. If a date is missing or malformed, the formatter returns the
original value or an em dash rather than inventing a date.

## Verification

Extend the dependency-free JavaScript tests to cover full English run-date
formatting, including a malformed value. Update the static asset regression
test to assert the new compact label and page title and to ensure the old label
is gone. Run the UI route tests, JavaScript tests, and syntax checks.
