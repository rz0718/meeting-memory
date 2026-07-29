import datetime as dt
import unittest

from meeting_memory.knowledge.consumption import SearchDocument, stem, stemmed
from meeting_memory.knowledge.context import _strong_text_match
from meeting_memory.knowledge.search import search_documents


def make_document(
    identifier="policy-withdrawal-approval",
    title="Withdrawal approval policy",
    statement="Withdrawals over $50k require dual approval.",
    category="policies",
    status="approved",
    owner="Treasury",
):
    moment = dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc)
    return SearchDocument(
        id=identifier,
        title=title,
        category=category,
        status=status,
        statement=statement,
        owner=owner,
        confidence="high",
        effective_date=dt.date(2026, 7, 22),
        last_confirmed=dt.date(2026, 7, 22),
        created_at=moment,
        updated_at=moment,
        related_objects=(),
        evidence=(),
        history_text="",
        manual_notes="",
        file_path="knowledge/policies/%s.md" % identifier,
    )


class StemTest(unittest.TestCase):
    def test_folds_regular_plurals(self):
        for plural, singular in (
            ("withdrawals", "withdrawal"),
            ("metrics", "metric"),
            ("systems", "system"),
            ("decisions", "decision"),
            ("fees", "fee"),
        ):
            self.assertEqual(stem(plural), singular)

    def test_folds_ies_and_es_plurals(self):
        self.assertEqual(stem("policies"), "policy")
        self.assertEqual(stem("processes"), "process")
        self.assertEqual(stem("batches"), "batch")
        self.assertEqual(stem("boxes"), "box")

    def test_leaves_guarded_tokens_alone(self):
        # -ss / -us / -is words are not plurals; short and non-alphabetic
        # tokens are abbreviations or figures.
        for token in ("status", "business", "process", "analysis", "sla", "ops", "50k"):
            self.assertEqual(stem(token), token)

    def test_is_idempotent(self):
        for token in ("withdrawals", "policies", "processes", "status", "sla"):
            self.assertEqual(stem(stem(token)), stem(token))

    def test_stemmed_can_drop_stop_words(self):
        self.assertEqual(stemmed("the withdrawals", remove_stop_words=True), ("withdrawal",))


class SearchStemmingTest(unittest.TestCase):
    def test_plural_query_matches_singular_title(self):
        documents = (make_document(),)
        results = search_documents(documents, "withdrawals")
        self.assertEqual(len(results), 1)
        self.assertIn("title", results[0].matched_fields)

    def test_singular_query_matches_plural_statement(self):
        documents = (
            make_document(
                identifier="policy-payout",
                title="Payout limits",
                statement="Payouts are capped at $10k per day.",
            ),
        )
        results = search_documents(documents, "payout")
        self.assertEqual(len(results), 1)
        self.assertIn("statement", results[0].matched_fields)

    def test_reason_reports_the_users_wording_not_the_stem(self):
        documents = (make_document(),)
        results = search_documents(documents, "withdrawals")
        self.assertIn("title tokens withdrawals (+12)", results[0].reasons)

    def test_terms_sharing_a_stem_score_once(self):
        # Both query terms fold to "withdrawal", so the title is worth one
        # match (+12) rather than two (+24).
        documents = (make_document(),)
        results = search_documents(documents, "withdrawal withdrawals")
        self.assertIn("title tokens withdrawal (+12)", results[0].reasons)

    def test_guarded_singular_matches_its_es_plural(self):
        # "process" is protected from the -s rule by the -ss guard, and
        # "processes" is folded by the -sses rule; both land on "process".
        documents = (
            make_document(
                identifier="process-payment-review",
                title="Payment process review",
                statement="Each payment process is reviewed weekly.",
                category="processes",
            ),
        )
        results = search_documents(documents, "processes")
        self.assertEqual(len(results), 1)
        self.assertIn("title", results[0].matched_fields)

    def test_unrelated_query_still_misses(self):
        documents = (make_document(),)
        self.assertEqual(search_documents(documents, "fireblocks custody"), ())

    def test_exact_title_phrase_bonus_is_unchanged(self):
        documents = (make_document(),)
        results = search_documents(documents, "Withdrawal approval policy")
        self.assertIn("exact title (+80)", results[0].reasons)


class StrongTextMatchTest(unittest.TestCase):
    def test_matches_across_plural_forms(self):
        self.assertTrue(_strong_text_match("withdrawal limits", "Withdrawals limit"))

    def test_still_rejects_unrelated_text(self):
        self.assertFalse(_strong_text_match("withdrawal limits", "Vendor onboarding"))


if __name__ == "__main__":
    unittest.main()
