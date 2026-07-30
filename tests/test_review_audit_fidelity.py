"""Audit fidelity of a resolution and adoption of a target by an unlinked case."""

import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.errors import ReviewResolutionError
from meeting_memory.knowledge.models import Evidence, KnowledgeObject, ReviewItem
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.review import ReviewRefresher, ReviewResolver

FIXED_NOW = dt.datetime(2026, 7, 30, 8, 0, tzinfo=dt.timezone.utc)


class ReviewAuditTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        source = self.repository.meetings_dir / "2026-07-30" / "recap.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            "# Recap\n\nGold physical exposure target range is +2 to +8 kg.\n",
            encoding="utf-8",
        )
        self.source_ref = "meetings/2026-07-30/recap.md"
        from meeting_memory.knowledge.util import sha256_file

        self.evidence = Evidence(
            source=self.source_ref,
            source_sha256=sha256_file(source),
            anchor="Gold physical exposure target range",
            line_start=3,
            line_end=3,
            observed_at="2026-07-30",
        )
        self.target = KnowledgeObject(
            id="metric-gold-physical-exposure-target-range",
            title="Gold physical exposure target range",
            category="metrics",
            status="approved",
            effective_date="2026-07-01",
            last_confirmed="2026-07-01",
            owner="Treasury",
            confidence="high",
            created_at="2026-07-01T08:00:00Z",
            updated_at="2026-07-01T08:00:00Z",
            evidence=[self.evidence],
            related_objects=[],
            statement="Gold physical exposure target range is +2 to +8 kg.",
            history=[],
        )
        path = self.repository.knowledge_dir / "metrics" / ("%s.md" % self.target.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.repository.render_knowledge(self.target))

    def write_review(self, review_id, possible_ids, existing_statement):
        item = ReviewItem(
            id=review_id,
            created_at="2026-07-29T08:00:00Z",
            status="pending",
            reason="ambiguous_match",
            candidate_category="metrics",
            possible_existing_ids=list(possible_ids),
            sources=[self.source_ref],
            title="Gold physical exposure target range review",
            existing_statement=existing_statement,
            candidate_statement="Gold physical exposure target range is +2 kg to +8 kg.",
            explanation="ambiguous",
            existing_evidence=[self.evidence] if existing_statement else [],
            candidate_evidence=[self.evidence],
            existing_updated_at=self.target.updated_at if existing_statement else None,
        )
        path = self.repository.review_dir / "pending" / ("%s.md" % review_id)
        path.write_bytes(self.repository.render_review(item))
        return item

    def resolver(self):
        return ReviewResolver(self.repository, now_fn=lambda: FIXED_NOW)

    def test_scripted_resolution_records_automated_mode(self):
        self.write_review("review-a", [self.target.id], self.target.statement)
        self.resolver().resolve(
            "review-a",
            "reconfirm",
            "automated-rereconcile",
            "no material change",
            resolution_mode="automated",
            refresh_indexes=False,
        )
        resolved = self.repository.load_review_file(
            self.repository.review_dir / "resolved" / "review-a.md"
        )
        self.assertEqual(resolved.resolution_mode, "automated")

    def test_mode_defaults_to_human_when_unspecified(self):
        self.write_review("review-b", [self.target.id], self.target.statement)
        self.resolver().resolve(
            "review-b",
            "reconfirm",
            "someone",
            "checked by hand",
            refresh_indexes=False,
        )
        resolved = self.repository.load_review_file(
            self.repository.review_dir / "resolved" / "review-b.md"
        )
        self.assertEqual(resolved.resolution_mode, "human")

    def test_unsupported_mode_is_refused(self):
        self.write_review("review-c", [self.target.id], self.target.statement)
        with self.assertRaises(ReviewResolutionError):
            self.resolver().resolve(
                "review-c",
                "reconfirm",
                "someone",
                "note",
                resolution_mode="magic",
                refresh_indexes=False,
            )

    def test_unlinked_review_can_adopt_a_target(self):
        self.write_review("review-d", [], None)
        result = ReviewRefresher(self.repository).refresh(
            "review-d", existing_id=self.target.id, adopt_existing=True
        )
        self.assertEqual(result.existing_id, self.target.id)
        refreshed = self.repository.load_review_file(
            self.repository.review_dir / "pending" / "review-d.md"
        )
        self.assertEqual(refreshed.possible_existing_ids, [self.target.id])
        self.assertEqual(refreshed.existing_statement, self.target.statement)

    def test_adoption_is_refused_without_the_opt_in(self):
        self.write_review("review-e", [], None)
        with self.assertRaises(ReviewResolutionError):
            ReviewRefresher(self.repository).refresh(
                "review-e", existing_id=self.target.id
            )

    def test_adoption_never_rewrites_an_existing_linkage(self):
        # The review already names a different object; adoption must not be a
        # back door for changing an established identity.
        other = copy.deepcopy(self.target)
        other.id = "metric-gold-market-exposure-target-range"
        other.title = "Gold market exposure target range"
        other.statement = "Gold market exposure target range is -5 to +5 kg."
        path = self.repository.knowledge_dir / "metrics" / ("%s.md" % other.id)
        path.write_bytes(self.repository.render_knowledge(other))

        self.write_review("review-f", [other.id], other.statement)
        with self.assertRaises(ReviewResolutionError):
            ReviewRefresher(self.repository).refresh(
                "review-f", existing_id=self.target.id, adopt_existing=True
            )


if __name__ == "__main__":
    unittest.main()
