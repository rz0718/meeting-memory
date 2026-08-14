"""One conversation synced twice must be ingested once.

A meeting that carries two calendar entries syncs to two files whose bodies are
byte-identical and whose front matter differs on calendar_event_id, organizer,
and start time. That is enough to change the file hash, so both copies used to
be treated as independent sources: the same fact was extracted twice, creating
an object on the first pass and then refining or reconfirming it on the second.
The result looked like corroboration from a second meeting and inflated the
object's evidence with a duplicate citation of a conversation that only
happened once. These tests pin that the second copy is withheld, that the
withholding is visible rather than silent, and that a genuinely different
source is never folded away with it.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.extractors import FakeExtractor
from meeting_memory.knowledge.models import (
    Evidence,
    KnowledgeCandidate,
    validate_run_manifest,
)
from meeting_memory.knowledge.pipeline import KnowledgePipeline
from meeting_memory.knowledge.repository import KnowledgeRepository


FIXED_NOW = dt.datetime(2026, 8, 12, 12, 50, tzinfo=dt.timezone.utc)
DATE = "2026-08-12"

BODY = """# Tata <> Rui

## Decisions

* **JPM auto-hedging disabled for PGB** Auto-hedging for JPM on PGB is disabled
  to prioritize daily netting and manual hedging capabilities.
"""


def frontmatter(title: str, event_id: str, start: str, organizer: str) -> str:
    return (
        "---\n"
        'title: "%s"\n'
        'start_time: "%s"\n'
        'calendar_event_id: "%s"\n'
        'organizer: "%s"\n'
        "---\n\n" % (title, start, event_id, organizer)
    )


LOWER = "meetings/%s/tata-rui.md" % DATE
UPPER = "meetings/%s/Tata-Rui.md" % DATE


class DuplicateSourceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()

    def write_source(self, name: str, text: str) -> None:
        path = self.repository.meetings_dir / DATE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_twin_calendar_syncs(self) -> None:
        """The same notes under two calendar entries for one conversation."""
        self.write_source(
            "tata-rui.md",
            frontmatter("tata rui", "62obbr5t", "12:00", "sutera.darmawan@example.com")
            + BODY,
        )
        self.write_source(
            "Tata-Rui.md",
            frontmatter("Tata <> Rui", "0e0bsmcb", "12:15", "zhao.rui@example.com")
            + BODY,
        )

    def candidate(self, statement: str, source: str):
        return KnowledgeCandidate(
            category="decisions",
            title="JPM auto-hedging disabled for PGB",
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
                    anchor="Decisions",
                    line_start=8,
                    line_end=9,
                    observed_at=None,
                )
            ],
        )

    def run_pipeline(self, responses):
        extractor = FakeExtractor(responses)
        result = KnowledgePipeline(
            self.repository, extractor, now_fn=lambda: FIXED_NOW
        ).process_dates([DATE])
        return result, extractor

    # -- discovery -------------------------------------------------------

    def test_identical_bodies_under_different_front_matter_are_one_source(self):
        self.write_twin_calendar_syncs()

        kept, duplicates = self.repository.scan_sources(DATE)

        self.assertEqual([UPPER], [item.relative_path for item in kept])
        self.assertEqual(
            [{"source": LOWER, "duplicate_of": UPPER}],
            duplicates,
        )

    def test_survivor_does_not_depend_on_filesystem_listing_order(self):
        """Case-equal names must not leave the winner to the directory order."""
        for order in (("tata-rui.md", "Tata-Rui.md"), ("Tata-Rui.md", "tata-rui.md")):
            with self.subTest(order=order):
                directory = self.repository.meetings_dir / DATE
                if directory.is_dir():
                    for path in directory.glob("*.md"):
                        path.unlink()
                for index, name in enumerate(order):
                    self.write_source(
                        name, frontmatter(name, "id%d" % index, "12:00", "a@b.com") + BODY
                    )
                kept, duplicates = self.repository.scan_sources(DATE)
                self.assertEqual([UPPER], [item.relative_path for item in kept])
                self.assertEqual([LOWER], [item["source"] for item in duplicates])

    def test_different_bodies_are_both_kept(self):
        self.write_source(
            "tata-rui.md", frontmatter("tata rui", "a", "12:00", "a@b.com") + BODY
        )
        self.write_source(
            "Tata-Rui.md",
            frontmatter("Tata <> Rui", "b", "12:15", "c@d.com")
            + BODY
            + "\n* **Netting** Daily netting continues.\n",
        )

        kept, duplicates = self.repository.scan_sources(DATE)

        self.assertEqual([UPPER, LOWER], [item.relative_path for item in kept])
        self.assertEqual([], duplicates)

    def test_sources_without_front_matter_still_pair(self):
        self.write_source("tata-rui.md", BODY)
        self.write_source("Tata-Rui.md", BODY)

        kept, duplicates = self.repository.scan_sources(DATE)

        self.assertEqual([UPPER], [item.relative_path for item in kept])
        self.assertEqual([LOWER], [item["source"] for item in duplicates])

    def test_trailing_whitespace_does_not_defeat_the_pairing(self):
        self.write_source(
            "tata-rui.md", frontmatter("tata rui", "a", "12:00", "a@b.com") + BODY
        )
        self.write_source(
            "Tata-Rui.md",
            frontmatter("Tata <> Rui", "b", "12:15", "c@d.com") + BODY + "\n\n",
        )

        _, duplicates = self.repository.scan_sources(DATE)

        self.assertEqual([LOWER], [item["source"] for item in duplicates])

    # -- ingestion -------------------------------------------------------

    def test_the_withheld_copy_is_never_extracted(self):
        self.write_twin_calendar_syncs()

        result, extractor = self.run_pipeline(
            {
                UPPER: [
                    self.candidate(
                        "Auto-hedging for JPM on PGB is disabled to prioritize daily "
                        "netting and manual hedging capabilities.",
                        UPPER,
                    )
                ]
            }
        )

        self.assertEqual([UPPER], extractor.calls)
        self.assertEqual([UPPER], result.manifest["sources_processed"])

    def test_withholding_is_reported_rather_than_silent(self):
        self.write_twin_calendar_syncs()

        result, _ = self.run_pipeline({UPPER: []})
        manifest = result.manifest

        self.assertEqual(
            [{"source": LOWER, "duplicate_of": UPPER}],
            manifest["sources_deduplicated"],
        )
        # Examined, because the run looked at the file and decided about it: a
        # reader chasing a missing meeting must find it accounted for.
        self.assertIn(LOWER, manifest["sources_examined"])
        self.assertNotIn(LOWER, manifest["sources_processed"])
        self.assertNotIn(LOWER, manifest["sources_skipped"])
        validate_run_manifest(manifest)

    def test_one_conversation_leaves_one_citation_and_no_reconfirmation(self):
        """The regression: a second sync used to look like a second meeting."""
        self.write_twin_calendar_syncs()
        statement = (
            "Auto-hedging for JPM on PGB is disabled to prioritize daily netting "
            "and manual hedging capabilities."
        )

        result, _ = self.run_pipeline(
            {
                UPPER: [self.candidate(statement, UPPER)],
                # Reached only if the withheld copy is extracted after all.
                LOWER: [self.candidate(statement, LOWER)],
            }
        )

        manifest = result.manifest
        self.assertEqual(1, len(manifest["objects_created"]))
        self.assertEqual([], manifest["objects_reconfirmed"])
        self.assertEqual([], manifest["objects_refined"])

        obj = self.repository.load_knowledge()[0]
        self.assertEqual([UPPER], [item.source for item in obj.evidence])
        self.assertEqual(
            [], [line for line in obj.history if "Reconfirmed" in line]
        )

    def test_a_second_real_meeting_still_reconfirms(self):
        """Dedupe must not blunt corroboration from a genuinely distinct source."""
        self.write_source(
            "tata-rui.md", frontmatter("tata rui", "a", "12:00", "a@b.com") + BODY
        )
        self.write_source(
            "treasury-sync.md",
            frontmatter("Treasury sync", "b", "15:00", "c@d.com")
            + "# Treasury sync\n\nJPM auto-hedging stays off for PGB.\n",
        )
        statement = (
            "Auto-hedging for JPM on PGB is disabled to prioritize daily netting "
            "and manual hedging capabilities."
        )
        other = "meetings/%s/treasury-sync.md" % DATE

        result, _ = self.run_pipeline(
            {
                LOWER: [self.candidate(statement, LOWER)],
                other: [self.candidate(statement, other)],
            }
        )

        manifest = result.manifest
        self.assertEqual([], manifest["sources_deduplicated"])
        self.assertEqual(1, len(manifest["objects_created"]))
        self.assertEqual(1, len(manifest["objects_reconfirmed"]))
        obj = self.repository.load_knowledge()[0]
        self.assertEqual([LOWER, other], [item.source for item in obj.evidence])

    def test_the_day_report_states_what_was_withheld(self):
        self.write_twin_calendar_syncs()

        self.run_pipeline(
            {UPPER: [self.candidate("JPM auto-hedging is off for PGB.", UPPER)]}
        )

        report = (
            self.repository.outputs_dir / ("durable-knowledge-%s.md" % DATE)
        ).read_text(encoding="utf-8")
        self.assertIn("- 1 withheld as a duplicate of another source", report)


if __name__ == "__main__":
    unittest.main()
