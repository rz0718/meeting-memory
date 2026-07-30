"""Duplicate detection across the dated files of one recurring document."""

import unittest

from meeting_memory.knowledge.models import Evidence, ReviewItem
from meeting_memory.knowledge.review import (
    reviews_look_duplicate,
    review_series,
    source_series,
)


def evidence(source):
    return Evidence(
        source=source,
        source_sha256="a" * 64,
        anchor="recap",
        line_start=1,
        line_end=1,
        observed_at=source.split("/")[1],
    )


def review(review_id, source, topic="Gold A/L exposure targets", target="metric-gold-l-exposure-targets"):
    return ReviewItem(
        id=review_id,
        created_at="2026-07-29T08:00:00Z",
        status="pending",
        reason="ambiguous_match",
        candidate_category="metrics",
        possible_existing_ids=[target] if target else [],
        sources=[source],
        title="%s review" % topic,
        existing_statement="Targets are -5 to +5 kg.",
        candidate_statement="Targets are -5 kg to +5 kg.",
        explanation="ambiguous",
        candidate_evidence=[evidence(source)],
    )


class SourceSeriesTest(unittest.TestCase):
    def test_dated_paths_of_one_channel_share_a_series(self):
        self.assertEqual(
            source_series("meetings/2026-07-02/slack-c076e4a9q7l.md"),
            source_series("meetings/2026-07-06/slack-c076e4a9q7l.md"),
        )

    def test_different_documents_do_not_share_a_series(self):
        self.assertNotEqual(
            source_series("meetings/2026-07-02/slack-c076e4a9q7l.md"),
            source_series("meetings/2026-07-02/slack-c0194tgl94h.md"),
        )

    def test_series_of_a_review_covers_every_source(self):
        item = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        item.sources = [
            "meetings/2026-07-02/slack-c076e4a9q7l.md",
            "meetings/2026-07-03/Finance-Weekly.md",
        ]
        self.assertEqual(review_series(item), {"slack-c076e4a9q7l", "finance-weekly"})


class DuplicateAcrossDaysTest(unittest.TestCase):
    def test_same_threshold_restated_on_a_later_day_is_a_duplicate(self):
        left = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        right = review("review-b", "meetings/2026-07-06/slack-c076e4a9q7l.md")
        self.assertTrue(reviews_look_duplicate(left, right))

    def test_same_day_duplicates_still_match(self):
        left = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        right = review("review-b", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        self.assertTrue(reviews_look_duplicate(left, right))

    def test_a_different_channel_is_not_a_duplicate(self):
        left = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        right = review("review-b", "meetings/2026-07-02/slack-c0194tgl94h.md")
        self.assertFalse(reviews_look_duplicate(left, right))

    def test_a_different_topic_is_not_a_duplicate(self):
        left = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        right = review(
            "review-b", "meetings/2026-07-06/slack-c076e4a9q7l.md", topic="FX excess inventory threshold"
        )
        self.assertFalse(reviews_look_duplicate(left, right))

    def test_a_different_target_object_is_not_a_duplicate(self):
        left = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        right = review(
            "review-b", "meetings/2026-07-06/slack-c076e4a9q7l.md", target="metric-fx-excess-threshold"
        )
        self.assertFalse(reviews_look_duplicate(left, right))

    def test_a_review_never_duplicates_itself(self):
        item = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md")
        self.assertFalse(reviews_look_duplicate(item, item))

    def test_two_unlinked_reviews_on_one_topic_are_duplicates(self):
        left = review("review-a", "meetings/2026-07-02/slack-c076e4a9q7l.md", target=None)
        right = review("review-b", "meetings/2026-07-06/slack-c076e4a9q7l.md", target=None)
        self.assertTrue(reviews_look_duplicate(left, right))


if __name__ == "__main__":
    unittest.main()
