import datetime as dt
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.errors import SchemaError
from meeting_memory.knowledge.models import Evidence, KnowledgeObject, ReviewItem
from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.removal import KnowledgeRemover
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.util import json_bytes, sha256_file


FIXED_NOW = dt.datetime(2026, 7, 31, 9, 0, tzinfo=dt.timezone.utc)


class RemovalWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.source = self.repository.meetings_dir / "2026-07-07" / "slack-c123.md"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("# Slack\n\nA durable statement.\n", encoding="utf-8")
        self.source_ref = "meetings/2026-07-07/slack-c123.md"

    def make_object(self, identifier, related=()):
        obj = KnowledgeObject(
            id=identifier,
            title=identifier,
            category="systems",
            status="approved",
            effective_date=None,
            last_confirmed="2026-07-07",
            owner=None,
            confidence="medium",
            created_at="2026-07-07T00:00:00Z",
            updated_at="2026-07-07T00:00:00Z",
            evidence=[
                Evidence(
                    source=self.source_ref,
                    source_sha256=sha256_file(self.source),
                    anchor="A durable statement.",
                    line_start=3,
                    line_end=3,
                    observed_at="2026-07-07",
                )
            ],
            related_objects=list(related),
            statement="A durable statement.",
            history=["2026-07-07: Initially recorded as approved."],
            path=self.repository.knowledge_dir / "systems" / ("%s.md" % identifier),
        )
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        return obj

    def make_review(self, identifier, object_id):
        item = ReviewItem(
            id=identifier,
            created_at="2026-07-08T00:00:00Z",
            status="pending",
            reason="ambiguous_match",
            candidate_category="systems",
            possible_existing_ids=[object_id],
            sources=[self.source_ref],
            title="Review",
            existing_statement="A durable statement.",
            candidate_statement="A candidate statement.",
            explanation="matching identity is uncertain",
            existing_evidence=[],
            candidate_evidence=[],
            existing_updated_at="2026-07-07T00:00:00Z",
            existing_statement_sha256="a" * 64,
        )
        path = self.repository.review_dir / "pending" / ("%s.md" % identifier)
        path.write_bytes(self.repository.render_review(item))
        return path

    def remover(self):
        return KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW)

    def test_dry_run_changes_nothing(self):
        removed = self.make_object("system-remove")
        result = self.remover().remove(
            [removed.id], "Rui", "TreasuryBot cleanup.", dry_run=True
        )
        self.assertTrue(removed.path.exists())
        self.assertEqual(("system-remove",), result.object_ids)
        self.assertFalse(
            (self.repository.state_dir / "cleanup-runs").exists()
        )

    def test_remove_repairs_related_reviews_and_source_states(self):
        removed = self.make_object("system-remove")
        retained = self.make_object("system-retained", related=[removed.id])
        review_path = self.make_review("review-remove", removed.id)
        state_path = self.repository.state_path(self.source_ref)
        state = {
            "source_path": self.source_ref,
            "source_sha256": sha256_file(self.source),
            "source_date": "2026-07-07",
            "processed_at": "2026-07-08T00:00:00Z",
            "extractor_version": "1",
            "schema_version": "1",
            "run_id": "run-1",
            "result": "success",
            "knowledge_object_ids": [removed.id, retained.id],
            "review_item_ids": ["review-remove"],
            "error": None,
        }
        state_path.write_bytes(json_bytes(state))

        result = self.remover().remove(
            [removed.id],
            "Rui",
            "TreasuryBot cleanup.",
            inventory_sha256="b" * 64,
            refresh_indexes=False,
        )

        self.assertFalse(removed.path.exists())
        reloaded = self.repository.load_knowledge_file(retained.path)
        self.assertEqual([], reloaded.related_objects)
        review = self.repository.load_review_file(review_path)
        self.assertEqual([], review.possible_existing_ids)
        self.assertIsNone(review.existing_statement)
        updated_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual([retained.id], updated_state["knowledge_object_ids"])
        manifest = self.repository.root / result.manifest_path
        self.assertTrue(manifest.is_file())
        self.assertEqual(
            "b" * 64,
            json.loads(manifest.read_text(encoding="utf-8"))["inventory_sha256"],
        )
        self.repository.validate_all()

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
            "suggestions_created": {"review-dangling": "suggestion-missing"},
            "suggestions_reused": {},
            "failures": [],
        }
        path = self.repository.review_run_dir / "review-run-dangling.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_removal_preflight_blocks_dangling_suggestion_before_mutation(self):
        removed = self.make_object("system-remove")
        before = removed.path.read_bytes()
        self.write_dangling_suggestion_manifest()

        with self.assertRaises(SchemaError):
            self.remover().remove(
                [removed.id], "Rui", "TreasuryBot cleanup.", dry_run=True
            )
        self.assertEqual(before, removed.path.read_bytes())

        with self.assertRaises(SchemaError):
            self.remover().remove([removed.id], "Rui", "TreasuryBot cleanup.")
        self.assertEqual(before, removed.path.read_bytes())
        self.assertFalse((self.repository.state_dir / "cleanup-runs").exists())

    def test_cli_requires_matching_count_and_inventory_digest(self):
        removed = self.make_object("system-remove")
        inventory = self.repository.root / "approved-removal-ids.txt"
        inventory.write_text(removed.id + "\n", encoding="utf-8")
        arguments = [
            "--output-dir",
            str(self.repository.root),
            "--meetings-dir",
            str(self.repository.meetings_dir),
            "remove",
            "--object-id-file",
            str(inventory),
            "--confirm-count",
            "1",
            "--inventory-sha256",
            sha256_file(inventory),
            "--reviewer",
            "Rui",
            "--note",
            "TreasuryBot cleanup.",
            "--dry-run",
            "--json",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(arguments))
        self.assertEqual(1, json.loads(output.getvalue())["objects_removed"])
        self.assertTrue(removed.path.exists())


if __name__ == "__main__":
    unittest.main()
