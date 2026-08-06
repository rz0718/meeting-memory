import contextlib
import copy
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.constants import (
    GENERATED_BEGIN,
    GENERATED_END,
    MANUAL_BEGIN,
    MANUAL_END,
)
from meeting_memory.knowledge.errors import MergeError, SchemaError
from meeting_memory.knowledge.merge import KnowledgeMerger
from meeting_memory.knowledge.models import Evidence, KnowledgeObject, ReviewItem
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.util import sha256_bytes, sha256_file


FIXED_NOW = dt.datetime(2026, 7, 31, 9, 0, tzinfo=dt.timezone.utc)


class MergeWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.source = self.repository.meetings_dir / "2026-07-07" / "recap.md"
        self.source.parent.mkdir(parents=True)
        self.source.write_text(
            "# Recap\n\nExcess inventory -$604,042 (threshold +/-$500K)\n",
            encoding="utf-8",
        )
        self.source_ref = "meetings/2026-07-07/recap.md"

    def make_object(
        self,
        *,
        identifier,
        category="metrics",
        title,
        statement,
        observed_at,
        last_confirmed=None,
        related_objects=None,
    ):
        obj = KnowledgeObject(
            id=identifier,
            title=title,
            category=category,
            status="approved",
            effective_date=None,
            last_confirmed=last_confirmed or observed_at,
            owner=None,
            confidence="medium",
            created_at="2026-07-01T00:00:00Z",
            updated_at="2026-07-01T00:00:00Z",
            evidence=[
                Evidence(
                    source=self.source_ref,
                    source_sha256=sha256_file(self.source),
                    anchor="threshold +/-$500K",
                    line_start=3,
                    line_end=3,
                    observed_at=observed_at,
                )
            ],
            related_objects=related_objects or [],
            statement=statement,
            history=["%s: Initially recorded as approved." % observed_at],
            path=self.repository.knowledge_dir / category / ("%s.md" % identifier),
        )
        obj.path.parent.mkdir(parents=True, exist_ok=True)
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        return obj

    def make_review(
        self,
        *,
        identifier,
        status="pending",
        possible_existing_ids=(),
        affected_object_ids=(),
    ):
        item = ReviewItem(
            id=identifier,
            created_at="2026-07-02T00:00:00Z",
            status=status,
            reason="ambiguous_match",
            candidate_category="metrics",
            possible_existing_ids=list(possible_existing_ids),
            sources=[self.source_ref],
            title="Some review",
            existing_statement=None,
            candidate_statement="Excess inventory threshold is +/-$500K.",
            explanation="matching identity is uncertain",
            existing_evidence=[],
            candidate_evidence=[],
            affected_object_ids=list(affected_object_ids),
            resolved_at="2026-07-03T00:00:00Z" if status != "pending" else None,
            reviewer="automated" if status != "pending" else None,
            resolution_action="reconfirm" if status != "pending" else None,
            resolution_note="note" if status != "pending" else None,
        )
        path = self.repository.review_dir / status / ("%s.md" % item.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.repository.render_review(item))
        item = self.repository.load_review_file(path)
        return item, path

    def merger(self):
        return KnowledgeMerger(self.repository, now_fn=lambda: FIXED_NOW)

    def write_dangling_suggestion_manifest(self):
        manifest = {
            "schema_version": "1",
            "run_type": "review_suggestions",
            "run_id": "review-run-dangling",
            "started_at": "2026-07-29T12:40:57Z",
            "completed_at": "2026-07-29T12:41:04Z",
            "status": "success",
            "model": "test/model",
            "prompt_version": "1",
            "filters": {},
            "requested_review_ids": ["review-dangling"],
            "suggestions_created": {
                "review-dangling": "suggestion-missing",
            },
            "suggestions_reused": {},
            "failures": [],
        }
        path = self.repository.review_run_dir / "review-run-dangling.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_merge_unions_evidence_and_bumps_last_confirmed_to_the_newer_date(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        loser = self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )

        result = self.merger().merge(
            "metric-loser", "metric-survivor", "rui", "Consolidating duplicates."
        )

        self.assertEqual(1, result.evidence_added)
        self.assertFalse(loser.path.exists())
        reloaded = self.repository.load_knowledge_file(survivor.path)
        self.assertEqual(2, len(reloaded.evidence))
        self.assertEqual("2026-07-07", reloaded.last_confirmed)
        self.assertIn("Merged metric-loser", reloaded.history[-1])

    def test_dry_run_changes_nothing_on_disk(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        loser = self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        before_survivor = survivor.path.read_bytes()
        before_loser = loser.path.read_bytes()

        result = self.merger().merge(
            "metric-loser",
            "metric-survivor",
            "rui",
            "Consolidating duplicates.",
            dry_run=True,
        )

        self.assertTrue(result.dry_run)
        self.assertEqual(before_survivor, survivor.path.read_bytes())
        self.assertEqual(before_loser, loser.path.read_bytes())
        self.assertTrue(loser.path.exists())

    def test_merge_persists_a_custom_statement_and_the_review_note(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        final_statement = "The final statement combines both canonical records."
        review_note = "Consolidating the two supported descriptions."

        result = self.merger().merge(
            "metric-loser",
            "metric-survivor",
            "rui",
            review_note,
            statement=final_statement,
        )

        self.assertEqual(final_statement, result.after["statement"])
        reloaded = self.repository.load_knowledge_file(survivor.path)
        self.assertEqual(final_statement, reloaded.statement)
        self.assertIn(review_note, reloaded.history[-1])

    def test_merge_rejects_a_whitespace_only_statement_without_mutation(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        loser = self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        before_survivor = survivor.path.read_bytes()
        before_loser = loser.path.read_bytes()

        with self.assertRaisesRegex(MergeError, "final statement may not be empty"):
            self.merger().merge(
                "metric-loser",
                "metric-survivor",
                "rui",
                "Consolidating duplicates.",
                statement="   ",
            )

        self.assertEqual(before_survivor, survivor.path.read_bytes())
        self.assertEqual(before_loser, loser.path.read_bytes())
        self.assertTrue(loser.path.exists())

    def test_merge_rejects_a_multiline_note_before_validation_or_mutation(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        loser = self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        before_survivor = survivor.path.read_bytes()
        before_loser = loser.path.read_bytes()

        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with mock.patch.object(
                    self.repository, "validate_all", wraps=self.repository.validate_all
                ) as validate_all:
                    with self.assertRaisesRegex(MergeError, "exactly one line"):
                        self.merger().merge(
                            "metric-loser",
                            "metric-survivor",
                            "rui",
                            "Consolidating duplicates.\nInjected history entry.",
                            dry_run=dry_run,
                        )
                    validate_all.assert_not_called()
                self.assertEqual(before_survivor, survivor.path.read_bytes())
                self.assertEqual(before_loser, loser.path.read_bytes())
                self.assertTrue(loser.path.exists())

    def test_merge_rejects_protected_markers_in_a_custom_statement_without_mutation(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        loser = self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        before_survivor = survivor.path.read_bytes()
        before_loser = loser.path.read_bytes()

        for marker in (GENERATED_BEGIN, GENERATED_END, MANUAL_BEGIN, MANUAL_END):
            for dry_run in (True, False):
                with self.subTest(marker=marker, dry_run=dry_run):
                    with mock.patch.object(
                        self.repository,
                        "validate_all",
                        wraps=self.repository.validate_all,
                    ) as validate_all:
                        with self.assertRaisesRegex(
                            MergeError, "protected Markdown marker"
                        ):
                            self.merger().merge(
                                "metric-loser",
                                "metric-survivor",
                                "rui",
                                "Consolidating duplicates.",
                                statement="Combined statement %s" % marker,
                                dry_run=dry_run,
                            )
                        validate_all.assert_not_called()
                    self.assertEqual(before_survivor, survivor.path.read_bytes())
                    self.assertEqual(before_loser, loser.path.read_bytes())
                    self.assertTrue(loser.path.exists())

    def test_merge_preflight_blocks_dangling_suggestion_before_mutation(self):
        survivor = self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        loser = self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        before_survivor = survivor.path.read_bytes()
        before_loser = loser.path.read_bytes()
        self.write_dangling_suggestion_manifest()

        with self.assertRaises(SchemaError):
            self.merger().merge(
                "metric-loser",
                "metric-survivor",
                "rui",
                "Consolidating duplicates.",
                dry_run=True,
            )
        self.assertEqual(before_survivor, survivor.path.read_bytes())
        self.assertEqual(before_loser, loser.path.read_bytes())

        with self.assertRaises(SchemaError):
            self.merger().merge(
                "metric-loser",
                "metric-survivor",
                "rui",
                "Consolidating duplicates.",
            )
        self.assertEqual(before_survivor, survivor.path.read_bytes())
        self.assertEqual(before_loser, loser.path.read_bytes())

    def test_cross_category_merge_requires_explicit_override(self):
        self.make_object(
            identifier="metric-survivor",
            category="metrics",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        self.make_object(
            identifier="policy-loser",
            category="policies",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )

        with self.assertRaises(MergeError):
            self.merger().merge(
                "policy-loser", "metric-survivor", "rui", "Consolidating."
            )

        result = self.merger().merge(
            "policy-loser",
            "metric-survivor",
            "rui",
            "Consolidating.",
            allow_cross_category=True,
        )
        self.assertEqual("metric-survivor", result.survivor_id)

    def test_merge_refuses_statements_with_different_numbers_by_default(self):
        self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$600K.",
            observed_at="2026-07-07",
        )

        with self.assertRaises(MergeError):
            self.merger().merge(
                "metric-loser", "metric-survivor", "rui", "Consolidating."
            )

        result = self.merger().merge(
            "metric-loser",
            "metric-survivor",
            "rui",
            "Consolidating.",
            allow_conflicting_numbers=True,
        )
        self.assertEqual("metric-survivor", result.survivor_id)

    def test_merge_refuses_to_merge_object_into_itself(self):
        self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )

        with self.assertRaises(MergeError):
            self.merger().merge(
                "metric-survivor", "metric-survivor", "rui", "Consolidating."
            )

    def test_merge_retargets_pending_and_resolved_review_references(self):
        self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        pending, pending_path = self.make_review(
            identifier="review-pending",
            status="pending",
            possible_existing_ids=["metric-loser"],
        )
        resolved, resolved_path = self.make_review(
            identifier="review-resolved",
            status="resolved",
            possible_existing_ids=["metric-loser"],
            affected_object_ids=["metric-loser"],
        )

        result = self.merger().merge(
            "metric-loser", "metric-survivor", "rui", "Consolidating."
        )

        self.assertEqual(
            {"review-pending", "review-resolved"},
            set(result.retargeted_review_ids),
        )
        reloaded_pending = self.repository.load_review_file(pending_path)
        self.assertEqual(["metric-survivor"], reloaded_pending.possible_existing_ids)
        reloaded_resolved = self.repository.load_review_file(resolved_path)
        self.assertEqual(
            ["metric-survivor"], reloaded_resolved.possible_existing_ids
        )
        self.assertEqual(
            ["metric-survivor"], reloaded_resolved.affected_object_ids
        )
        # Validation would otherwise fail with a dangling reference.
        self.repository.validate_all()

    def test_merge_retargets_related_objects_on_other_canonical_objects(self):
        self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        bystander = self.make_object(
            identifier="process-fx-review",
            category="processes",
            title="FX review process",
            statement="The FX desk reviews excess inventory daily.",
            observed_at="2026-06-15",
            related_objects=["metric-loser"],
        )

        result = self.merger().merge(
            "metric-loser", "metric-survivor", "rui", "Consolidating."
        )

        self.assertEqual(("process-fx-review",), result.retargeted_object_ids)
        reloaded = self.repository.load_knowledge_file(bystander.path)
        self.assertEqual(["metric-survivor"], reloaded.related_objects)
        self.repository.validate_all()

    def test_cli_merge_dry_run_then_apply(self):
        self.make_object(
            identifier="metric-survivor",
            title="Survivor",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-06-01",
        )
        self.make_object(
            identifier="metric-loser",
            title="Loser",
            statement="Excess inventory threshold is +/-$500K.",
            observed_at="2026-07-07",
        )
        common = [
            "--output-dir",
            str(self.repository.root),
            "--meetings-dir",
            str(self.repository.meetings_dir),
            "merge",
            "metric-loser",
            "--into",
            "metric-survivor",
            "--reviewer",
            "rui",
            "--note",
            "Consolidating duplicates.",
        ]

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            dry_code = main(common + ["--dry-run"])
            apply_code = main(common)

        self.assertEqual((0, 0), (dry_code, apply_code))
        self.assertIn("Dry run metric-loser into metric-survivor", output.getvalue())
        self.assertIn("Merged metric-loser into metric-survivor", output.getvalue())
        self.assertEqual(
            set(),
            {
                path.name
                for path in (self.repository.knowledge_dir / "metrics").iterdir()
                if path.stem == "metric-loser"
            },
        )
