# Compact Merge Survivor Selector Design

## Problem

The merge dialog uses a native eight-row `select` to choose the surviving
knowledge object. The permanently expanded list consumes much of the dialog,
clips long title-and-ID labels, and makes exact knowledge IDs difficult to
scan and select.

## Chosen interaction

Replace the expanded list with a compact searchable combobox. The search field
opens its results only while the reviewer is searching or changing the
selection. The results panel is capped at roughly five rows and scrolls
independently, so it does not consume the rest of the merge form.

Each result presents the title as its primary label and the full knowledge ID
in monospace as a separate secondary line. Filtering remains case-insensitive
and matches either the title or ID.

Selecting a result closes the panel and replaces the search state with a clear
summary of the selected survivor. A **Change** action returns focus to the
search field and reopens the filtered candidates.

## Keyboard and accessibility behavior

The search field and results use combobox/listbox semantics. Arrow keys move
through visible matches, Enter chooses the active match, and Escape closes the
results without changing the current survivor. Mouse and touch selection use
the same selection path.

The active result is visually distinct and exposed through
`aria-activedescendant`. Empty and unavailable states appear inside the compact
results panel rather than expanding the dialog.

## Data flow

The existing knowledge collection remains the source of candidates, excluding
the retiring object. Search updates the filtered results and active row without
changing the chosen survivor. Committing a result updates the survivor ID,
selected-survivor statement, and final-statement default through the existing
merge draft flow.

Changing the selected survivor invalidates any completed preview and disables
Apply until a new deterministic preview succeeds. Merely opening, closing, or
navigating the result list does not invalidate the preview.

## Error and edge states

- No text match shows a compact “No matching knowledge objects” state.
- A repository with no other objects shows “No other knowledge objects
  available” and leaves preview unavailable.
- Escape and focus departure close the panel without clearing a committed
  selection.
- Long titles and IDs wrap or truncate within their own lines without widening
  the dialog.

## Testing

Extract small deterministic selector helpers where useful and cover
case-insensitive filtering, title and ID matching, result limits, active-index
movement, and selection preservation. DOM-level coverage should verify the
collapsed selected state, Change behavior, keyboard selection, empty results,
and merge-preview invalidation when the survivor actually changes.

The merge request shape and server behavior remain unchanged.
