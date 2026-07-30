"""Materiality tests for the reconciler's measurement and identity rules.

These cover the two paths that decide whether a restated fact costs a human a
review: the measurement payload that defines a material change, and the title
containment rule that resolves identity inside the similarity dead band.
"""

import unittest

from meeting_memory.knowledge.models import Evidence, KnowledgeCandidate, KnowledgeObject
from meeting_memory.knowledge.reconcile import KnowledgeReconciler, _measurements


EVIDENCE = Evidence(
    source="meetings/2026-07-29/recap.md",
    source_sha256="a" * 64,
    anchor="recap",
    line_start=1,
    line_end=1,
    observed_at="2026-07-29",
)

LATER_EVIDENCE = Evidence(
    source="meetings/2026-07-30/recap.md",
    source_sha256="b" * 64,
    anchor="recap",
    line_start=2,
    line_end=2,
    observed_at="2026-07-30",
)


def obj(statement, title="Gold exposure targets", category="metrics", status="approved"):
    return KnowledgeObject(
        id="metric-gold-exposure-targets",
        title=title,
        category=category,
        status=status,
        effective_date="2026-07-01",
        last_confirmed="2026-07-01",
        owner="Treasury",
        confidence="high",
        created_at="2026-07-01T08:00:00Z",
        updated_at="2026-07-01T08:00:00Z",
        evidence=[EVIDENCE],
        related_objects=[],
        statement=statement,
        history=[],
    )


def candidate(statement, title="Gold exposure targets", category="metrics", status="approved"):
    return KnowledgeCandidate(
        category=category,
        title=title,
        statement=statement,
        status=status,
        effective_date=None,
        owner=None,
        confidence="high",
        reason_for_durability="standing threshold",
        evidence=[LATER_EVIDENCE],
    )


class MeasurementTest(unittest.TestCase):
    def assert_same(self, left, right):
        self.assertEqual(_measurements(left), _measurements(right))

    def assert_differs(self, left, right):
        self.assertNotEqual(_measurements(left), _measurements(right))

    def test_typographic_minus_folds_to_hyphen(self):
        self.assert_same("market exposure of −5 to +5 kg", "market exposure -5 to +5 kg")

    def test_symmetric_tolerance_forms_agree(self):
        self.assert_same("threshold is +/- $500K", "threshold is ±$500K")

    def test_range_may_elide_the_unit_on_its_first_bound(self):
        self.assert_same("physical +2 to +8 kg", "physical +2 kg to +8 kg")

    def test_thousands_separator_and_trailing_zero_are_normalized(self):
        self.assert_same("value 1,000", "value 1000")
        self.assert_same("rate 2.50", "rate 2.5")

    def test_digits_inside_an_identifier_are_not_a_threshold(self):
        self.assert_same("approver U01UFMJ86SV", "approver ZHAO RUI")

    def test_prose_noun_after_a_number_is_not_a_unit(self):
        self.assert_same("5-category framework", "5 categories")

    def test_sign_flip_is_material(self):
        self.assert_differs("target +2 to +8 kg", "target -2 to -8 kg")

    def test_negative_currency_amount_is_material(self):
        self.assert_differs("loss of -$777K", "gain of $777K")

    def test_unit_change_is_material(self):
        self.assert_differs("threshold 2 kg", "threshold 2 tonnes")

    def test_percent_against_bare_number_is_material(self):
        self.assert_differs("LTV 70%", "LTV 70")

    def test_hyphen_between_digits_is_a_range_not_a_sign(self):
        self.assert_same("band 2-3 kg", "band 2 to 3 kg")


class ImmaterialRewordTest(unittest.TestCase):
    def setUp(self):
        self.reconciler = KnowledgeReconciler()

    def test_reworded_statement_reconfirms_without_a_review(self):
        existing = obj("Gold physical exposure target range is −2 to +8 kg.")
        decision = self.reconciler.reconcile(
            candidate("Gold physical exposure target range is -2 kg to +8 kg."),
            [existing],
        )
        self.assertEqual(decision.outcome, "reconfirmation")
        self.assertEqual(decision.existing_id, existing.id)

    def test_reconfirmation_leaves_the_curated_statement_authoritative(self):
        # The decision carries no replacement text, so a reword can never
        # overwrite curated wording the way a refinement does.
        existing = obj("The Chainalysis threshold has been reduced in order to cut costs.")
        decision = self.reconciler.reconcile(
            candidate("The Chainalysis monitoring threshold has been reduced to cut costs."),
            [existing],
        )
        self.assertEqual(decision.outcome, "reconfirmation")

    def test_changed_threshold_still_reaches_a_human(self):
        existing = obj("FX excess inventory threshold is ±$500K.")
        decision = self.reconciler.reconcile(
            candidate("FX excess inventory threshold is ±$750K."),
            [existing],
        )
        self.assertEqual(decision.outcome, "conflict")

    def test_reversed_threshold_is_not_treated_as_a_reword(self):
        # Identical wording apart from the signs, which normalize_text drops,
        # so only the measurement payload distinguishes these two.
        existing = obj("Gold physical exposure target range is +2 to +8 kg.")
        decision = self.reconciler.reconcile(
            candidate("Gold physical exposure target range is -2 to -8 kg."),
            [existing],
        )
        self.assertEqual(decision.outcome, "conflict")
        self.assertEqual(
            decision.reason, "same wording carries a different threshold, sign, or unit"
        )

    def test_dropping_a_threshold_entirely_is_material(self):
        existing = obj("Gold physical exposure target range is +2 to +8 kg.")
        decision = self.reconciler.reconcile(
            candidate("Custody moved to a new vault operator entirely."),
            [existing],
        )
        self.assertEqual(decision.outcome, "conflict")

    def test_unrelated_statement_below_the_floor_still_reaches_a_human(self):
        existing = obj("Gold custody is handled by the incumbent vault operator.")
        decision = self.reconciler.reconcile(
            candidate("Quarterly audit cadence was introduced for reporting."),
            [existing],
        )
        self.assertEqual(decision.outcome, "needs_review")
        self.assertEqual(decision.reason, "semantic relationship is not safe to determine")


class TitleContainmentTest(unittest.TestCase):
    def setUp(self):
        self.reconciler = KnowledgeReconciler()

    def test_longer_title_containing_the_curated_one_resolves_identity(self):
        existing = obj("Gold A/L exposure targets are -5 to +5 kg.", title="Gold A/L exposure targets")
        decision = self.reconciler.reconcile(
            candidate(
                "Gold A/L exposure targets are -5 to +5 kg.",
                title="Treasury Gold A/L exposure targets",
            ),
            [existing],
        )
        self.assertEqual(decision.existing_id, existing.id)
        self.assertIn(decision.outcome, ("duplicate", "reconfirmation"))

    def test_containment_by_two_objects_still_reaches_a_human(self):
        # Both curated titles contain the candidate's tokens and both clear the
        # length guard, so identity is genuinely ambiguous.
        first = obj("Targets are -5 to +5 kg.", title="Treasury gold exposure targets")
        first.id = "metric-treasury-gold-exposure-targets"
        second = obj("Targets are -5 to +5 kg.", title="Daily gold exposure targets")
        second.id = "metric-daily-gold-exposure-targets"
        decision = self.reconciler.reconcile(
            candidate("Targets are -5 to +5 kg.", title="Gold exposure targets"),
            [first, second],
        )
        self.assertEqual(decision.outcome, "needs_review")
        self.assertEqual(decision.reason, "matching identity is uncertain")

    def test_single_token_title_does_not_get_swallowed(self):
        existing = obj("Gold custody sits with BRINKS.", title="Gold custody arrangement")
        decision = self.reconciler.reconcile(
            candidate("Something unrelated entirely.", title="Gold"),
            [existing],
        )
        self.assertEqual(decision.outcome, "new")

    def test_containment_requires_comparable_length(self):
        # Two shared tokens inside a much longer curated title fall below
        # uncertain_threshold, so containment must not claim a match.
        existing = obj(
            "The treasury gold physical exposure target range for daily hedging is +2 to +8 kg.",
            title="Treasury gold physical exposure target range for daily hedging operations",
        )
        decision = self.reconciler.reconcile(
            candidate("An unrelated new fact.", title="hedging operations"),
            [existing],
        )
        self.assertEqual(decision.outcome, "new")


if __name__ == "__main__":
    unittest.main()
