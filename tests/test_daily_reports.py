"""A day's report must survive the rerun that recovers a failed source.

A report summarizes one run, but readers treat it as the record of what a date
yielded. Recovering a single failed extraction means rerunning the whole date,
and by then every other source is already in "success" state and is skipped --
so the recovering run legitimately reports no changes. These tests pin that
such a run leaves the earlier report alone, while a run that does change
knowledge still rewrites it.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.errors import ExtractionError
from meeting_memory.knowledge.extractors import FakeExtractor
from meeting_memory.knowledge.models import Evidence, KnowledgeCandidate
from meeting_memory.knowledge.pipeline import KnowledgePipeline
from meeting_memory.knowledge.repository import KnowledgeRepository


FIXED_NOW = dt.datetime(2026, 8, 7, 12, 50, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 8, 7, 13, 14, tzinfo=dt.timezone.utc)

DATE = "2026-08-07"
NOTES = "meetings/%s/recon-revamp.md" % DATE
SLACK = "meetings/%s/slack-c076e4a9q7l.md" % DATE


class DailyReportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.write_source(
            "recon-revamp.md", "# Recon\n\nPosition-level reconciliation is the target.\n"
        )
        self.write_source(
            "slack-c076e4a9q7l.md", "# Slack\n\nRefunds move to one daily batch.\n"
        )

    def write_source(self, name: str, text: str) -> None:
        path = self.repository.meetings_dir / DATE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def candidate(self, title: str, statement: str, source: str, anchor: str):
        return KnowledgeCandidate(
            category="decisions",
            title=title,
            statement=statement,
            status="approved",
            effective_date=None,
            owner=None,
            confidence="high",
            reason_for_durability="A recorded decision.",
            evidence=[
                Evidence(
                    source=source,
                    source_sha256=None,
                    anchor=anchor,
                    line_start=3,
                    line_end=3,
                    observed_at=None,
                )
            ],
        )

    def run_pipeline(self, responses, now=FIXED_NOW, force=False):
        return KnowledgePipeline(
            self.repository, FakeExtractor(responses), now_fn=lambda: now
        ).process_dates([DATE], force=force)

    @property
    def report_path(self) -> Path:
        return self.repository.outputs_dir / ("durable-knowledge-%s.md" % DATE)

    def report_text(self) -> str:
        return self.report_path.read_text(encoding="utf-8")

    def nightly_run_with_one_failed_source(self):
        """The notes extract cleanly; the Slack source returns unusable JSON."""
        result = self.run_pipeline(
            {
                NOTES: [
                    self.candidate(
                        "Position-level reconciliation",
                        "Position-level reconciliation is the target.",
                        NOTES,
                        "Position-level reconciliation",
                    )
                ],
                SLACK: ExtractionError("model response was not valid JSON"),
            }
        )
        self.assertEqual("partial_failure", result.manifest["status"])
        return result

    def test_recovering_rerun_leaves_the_earlier_report_intact(self):
        self.nightly_run_with_one_failed_source()
        first = self.report_text()
        self.assertIn("- 1 objects created", first)
        self.assertIn("Added Position-level reconciliation.", first)

        # The recovering rerun: the Slack source now extracts nothing durable,
        # and the notes are unchanged, so the run reports no changes at all.
        rerun = self.run_pipeline({NOTES: [], SLACK: []}, now=LATER)

        self.assertEqual("success", rerun.manifest["status"])
        self.assertEqual([SLACK], rerun.manifest["sources_processed"])
        self.assertEqual([NOTES], rerun.manifest["sources_skipped"])
        self.assertEqual(first, self.report_text())
        self.assertNotIn(
            self.report_path.resolve().as_posix(),
            [Path(item).resolve().as_posix() for item in rerun.wrote_files],
        )

    def test_recovering_rerun_that_adds_knowledge_still_rewrites_the_report(self):
        self.nightly_run_with_one_failed_source()
        first = self.report_text()

        rerun = self.run_pipeline(
            {
                NOTES: [],
                SLACK: [
                    self.candidate(
                        "Refunds batched daily",
                        "Refunds move to one daily batch.",
                        SLACK,
                        "one daily batch",
                    )
                ],
            },
            now=LATER,
        )

        self.assertEqual("success", rerun.manifest["status"])
        rewritten = self.report_text()
        self.assertNotEqual(first, rewritten)
        self.assertIn("Added Refunds batched daily.", rewritten)

    def test_first_run_for_a_date_always_writes_a_report(self):
        result = self.run_pipeline({NOTES: [], SLACK: []})

        self.assertEqual("success", result.manifest["status"])
        self.assertTrue(self.report_path.is_file())
        self.assertIn("- 0 objects created", self.report_text())


if __name__ == "__main__":
    unittest.main()
