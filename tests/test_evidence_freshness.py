import unittest

from meeting_memory.knowledge.models import Evidence
from meeting_memory.knowledge.util import (
    EVIDENCE_CURRENT,
    EVIDENCE_DRIFTED,
    EVIDENCE_VERIFIED,
    anchor_in_text,
    evidence_freshness,
    sha256_bytes,
)


SLACK_RECAP_LINE = (
    "• *Hongxu* — User-level circuit breaker staging tested "
    ":white_check_mark:, going live today; Bank risk monitor design with "
    "Daniel; New Jira DM-1553: withdrawal monitoring and circuit breaker"
)


class AnchorMatchingTest(unittest.TestCase):
    def test_anchor_survives_slack_markup_and_emoji_shortcodes(self):
        # The extraction model quotes the rendered line, so the stored anchor
        # drops the bullet, the bold markers, and the emoji shortcode that the
        # raw source still carries.
        anchor = "Hongxu — User-level circuit breaker staging tested, going live today"

        self.assertTrue(anchor_in_text(anchor, SLACK_RECAP_LINE))

    def test_anchor_matches_mid_line_quotation(self):
        line = (
            "MLE Daily Recap — 2026-07-01 *Team-level* • *Decisions:* "
            "User-level circuit breaker approved for go-live today; low-volume "
            "token testing strategy for Crypto P&amp;L validation"
        )

        self.assertTrue(
            anchor_in_text("User-level circuit breaker approved for go-live today", line)
        )

    def test_anchor_survives_html_escaped_source(self):
        # Slack renders "&" escaped, but the model quotes what it reads.
        line = "• *Decisions:* Crypto P&amp;L closing shifted to ongoing process"

        self.assertTrue(anchor_in_text("Crypto P&L closing shifted to ongoing process", line))

    def test_anchor_requires_whole_token_boundaries(self):
        self.assertFalse(anchor_in_text("staging test", "staging testing continues"))

    def test_unrelated_anchor_does_not_match(self):
        self.assertFalse(anchor_in_text("withdrawal limit raised", SLACK_RECAP_LINE))

    def test_anchor_with_no_alphanumeric_content_never_matches(self):
        self.assertFalse(anchor_in_text(":white_check_mark:", SLACK_RECAP_LINE))


class EvidenceFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.lines = ["# Recap", "", SLACK_RECAP_LINE]
        self.data = ("\n".join(self.lines) + "\n").encode("utf-8")

    def evidence(self, digest, anchor="staging tested, going live today", start=3, end=3):
        return Evidence(
            source="meetings/2026-07-01/slack-c02db1w2k2r.md",
            source_sha256=digest,
            anchor=anchor,
            line_start=start,
            line_end=end,
            observed_at="2026-07-01",
        )

    def test_matching_digest_is_current(self):
        item = self.evidence(sha256_bytes(self.data))

        self.assertEqual(EVIDENCE_CURRENT, evidence_freshness(self.data, self.lines, item))

    def test_changed_file_with_intact_anchor_is_verified(self):
        # A later Slack message rewrote the day-file, but not this citation.
        item = self.evidence("0" * 64)

        self.assertEqual(EVIDENCE_VERIFIED, evidence_freshness(self.data, self.lines, item))

    def test_changed_file_with_moved_anchor_is_drifted(self):
        item = self.evidence("0" * 64, start=1, end=1)

        self.assertEqual(EVIDENCE_DRIFTED, evidence_freshness(self.data, self.lines, item))

    def test_locator_past_end_of_file_is_drifted(self):
        item = self.evidence("0" * 64, start=9, end=9)

        self.assertEqual(EVIDENCE_DRIFTED, evidence_freshness(self.data, self.lines, item))

    def test_evidence_without_a_recorded_digest_is_current(self):
        item = self.evidence(None)

        self.assertEqual(EVIDENCE_CURRENT, evidence_freshness(self.data, self.lines, item))


if __name__ == "__main__":
    unittest.main()
