# Remove the Run Range Filter

## Context

The Knowledge Updates page already selects one manifest with the **Run date**
control. The adjacent **Inserted from (UTC)** and **to** inputs filter only the
available run manifests and chart history; they do not filter the objects shown
for the selected run. When the selected run remains inside the range, the main
content does not change, making the controls appear broken.

## Considered approaches

1. Remove the range controls. This keeps the page focused on selecting one run
   and eliminates an ambiguous, redundant interaction.
2. Relabel them as **Show runs from/to** and move them beside the chart. This is
   clearer but adds controls for a history-navigation need that is not currently
   important.
3. Make them filter knowledge objects by insertion time. This would change the
   page from a run-manifest view into a mixed object-history view and would need
   new data semantics.

## Design

Use approach 1. Keep **Run date** as the only date control. Remove the range
state, date inputs, clear action, and `start`/`end` query parameters from the
page's runs request. Continue loading all available manifests so the run
selector and history chart retain their existing data.

The backend range-query support remains available for API consumers; only the
redundant page controls are removed.

## Verification

Add or update the JavaScript view test to verify that the page requests the run
list without range parameters and renders only the **Run date** control. Run the
relevant UI tests and the existing run route tests.
