# Removal Basket “Put Back” Label Design

**Date:** 2026-08-06

## Goal

Describe the basket-clearing action in the user's terms.

## Design

Change the removal basket footer label from `Clear` to `Put back`. Keep
`Preview` and `Delete` unchanged.

This is a copy-only change. Selecting `Put back` continues to remove every item
from the temporary removal basket, close the dialog, and refresh the basket. It
does not restore previously deleted knowledge and does not change the permanent
removal workflow, API calls, or safety checks.

## Verification

Update the existing static-asset regression test to expect `Put back` and reject
the superseded `Clear` label. Run the focused label test, removal basket route
tests, removal workflow tests, and JavaScript syntax check.
