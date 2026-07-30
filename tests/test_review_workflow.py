import contextlib
import copy
import datetime as dt
import io
import tempfile
import threading
import unittest
from pathlib import Path

from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.errors import (
    ReviewResolutionError,
    SchemaError,
    StaleReviewError,
)
from meeting_memory.knowledge.extractors import FakeExtractor
from meeting_memory.knowledge.machine_index import machine_index_status
from meeting_memory.knowledge.models import (
    Evidence,
    KnowledgeCandidate,
    KnowledgeObject,
    ReviewItem,
)
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.review import (
    ReviewRefresher,
    ReviewResolver,
    list_reviews,
)
from meeting_memory.knowledge.pipeline import KnowledgePipeline
from meeting_memory.knowledge.util import (
    dump_frontmatter,
    parse_frontmatter,
    sha256_bytes,
    sha256_file,
)


FIXED_NOW = dt.datetime(2026, 7, 29, 8, 0, tzinfo=dt.timezone.utc)


class ReviewWorkflowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.source = (
            self.repository.meetings_dir / "2026-07-29" / "project-update.md"
        )
        self.source.parent.mkdir(parents=True)
        self.source.write_text(
            "# Project update\n\nThe framework is complete.\n",
            encoding="utf-8",
        )
        self.source_ref = "meetings/2026-07-29/project-update.md"
        self.evidence = Evidence(
            source=self.source_ref,
            source_sha256=sha256_file(self.source),
            anchor="framework is complete",
            line_start=3,
            line_end=3,
            observed_at="2026-07-29",
        )

    def make_object(self):
        obj = KnowledgeObject(
            id="project-framework",
            title="Framework",
            category="projects",
            status="proposed",
            effective_date="2026-07-28",
            last_confirmed="2026-07-28",
            owner="Team",
            confidence="medium",
            created_at="2026-07-28T08:00:00Z",
            updated_at="2026-07-28T08:00:00Z",
            evidence=[
                Evidence(
                    source=self.source_ref,
                    source_sha256=sha256_file(self.source),
                    anchor="project update",
                    line_start=1,
                    line_end=1,
                    observed_at="2026-07-28",
                )
            ],
            related_objects=[],
            statement="The framework is being planned.",
            history=["2026-07-28: Initially recorded as proposed."],
            path=(
                self.repository.knowledge_dir
                / "projects"
                / "project-framework.md"
            ),
        )
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        return obj

    def make_review(
        self,
        *,
        identifier="review-framework",
        structured=True,
        existing=None,
        statement="The framework is complete.",
    ):
        candidate = KnowledgeCandidate(
            category="projects",
            title="Framework",
            statement=statement,
            status="approved",
            effective_date="2026-07-29",
            owner="Team",
            confidence="high",
            reason_for_durability="Project lifecycle changed.",
            evidence=[copy.deepcopy(self.evidence)],
            existing_object_id=existing.id if existing else None,
            relationship="conflict" if existing else None,
        )
        item = ReviewItem(
            id=identifier,
            created_at="2026-07-29T07:00:00Z",
            status="pending",
            reason="conflicting_evidence" if existing else "ambiguous_match",
            candidate_category="projects",
            possible_existing_ids=[existing.id] if existing else [],
            sources=[self.source_ref],
            title="Framework conflict" if existing else "Framework review",
            existing_statement=existing.statement if existing else None,
            candidate_statement=statement,
            explanation="A human must select the lifecycle state.",
            existing_evidence=copy.deepcopy(existing.evidence if existing else []),
            candidate_evidence=[copy.deepcopy(self.evidence)],
            candidate=candidate if structured else None,
            existing_updated_at=existing.updated_at if structured and existing else None,
            existing_statement_sha256=(
                sha256_bytes(existing.statement.encode("utf-8"))
                if structured and existing
                else None
            ),
        )
        path = self.repository.review_dir / "pending" / ("%s.md" % item.id)
        path.write_bytes(self.repository.render_review(item))
        return item, path

    def resolver(self):
        return ReviewResolver(self.repository, now_fn=lambda: FIXED_NOW)

    def test_structured_review_round_trip_preserves_candidate_and_evidence(self):
        existing = self.make_object()
        _, path = self.make_review(existing=existing)

        loaded = self.repository.load_review_file(path)

        self.assertEqual("approved", loaded.candidate.status)
        self.assertEqual("The framework is complete.", loaded.candidate_statement)
        self.assertEqual(1, len(loaded.candidate_evidence))
        self.assertEqual("framework is complete", loaded.candidate_evidence[0].anchor)
        self.assertEqual(existing.updated_at, loaded.existing_updated_at)

    def test_pipeline_persists_structured_candidate_and_canonical_snapshot(self):
        existing = self.make_object()
        candidate = KnowledgeCandidate(
            category="projects",
            title="Framework",
            statement="The framework is complete.",
            status="approved",
            effective_date="2026-07-29",
            owner="Team",
            confidence="high",
            reason_for_durability="Project lifecycle changed.",
            evidence=[
                Evidence(
                    source=self.source_ref,
                    source_sha256=None,
                    anchor="framework is complete",
                    line_start=3,
                    line_end=3,
                    observed_at=None,
                )
            ],
            existing_object_id=existing.id,
            relationship="conflict",
        )

        result = KnowledgePipeline(
            self.repository,
            FakeExtractor([candidate]),
            now_fn=lambda: FIXED_NOW,
        ).process_dates(["2026-07-29"])

        self.assertEqual("success", result.manifest["status"])
        reviews = self.repository.load_reviews("pending")
        self.assertEqual(1, len(reviews))
        self.assertEqual("approved", reviews[0].candidate.status)
        self.assertEqual(existing.updated_at, reviews[0].existing_updated_at)
        self.assertEqual(sha256_file(self.source), reviews[0].candidate_evidence[0].source_sha256)

    def test_legacy_review_parser_recovers_backticks_in_evidence_anchor(self):
        existing = self.make_object()
        self.evidence.anchor = "remove the raw `log` field"
        _, path = self.make_review(existing=existing, structured=False)

        loaded = self.repository.load_review_file(path)

        self.assertIsNone(loaded.candidate)
        self.assertEqual("remove the raw `log` field", loaded.candidate_evidence[0].anchor)

    def test_list_prioritizes_conflicts_before_unlinked_reviews(self):
        existing = self.make_object()
        self.make_review(identifier="review-unlinked", existing=None)
        self.make_review(identifier="review-conflict", existing=existing)

        values = list_reviews(self.repository)

        self.assertEqual(
            ["review-conflict", "review-unlinked"],
            [item.id for item in values],
        )

    def test_refine_dry_run_does_not_change_repository(self):
        existing = self.make_object()
        _, pending_path = self.make_review(existing=existing)
        before = existing.path.read_bytes()

        result = self.resolver().resolve(
            "review-framework",
            "refine",
            "Rui",
            "The completion statement was confirmed.",
            dry_run=True,
        )

        self.assertTrue(result.dry_run)
        self.assertEqual(before, existing.path.read_bytes())
        self.assertTrue(pending_path.is_file())
        self.assertFalse(
            (self.repository.review_dir / "resolved" / pending_path.name).exists()
        )

    def test_refine_updates_canonical_closes_review_and_refreshes_indexes(self):
        existing = self.make_object()
        _, pending_path = self.make_review(existing=existing)

        result = self.resolver().resolve(
            "review-framework",
            "refine",
            "Rui",
            "The project owner confirmed completion.",
        )

        self.assertFalse(result.dry_run)
        self.assertFalse(pending_path.exists())
        resolved_path = self.repository.review_dir / "resolved" / pending_path.name
        self.assertTrue(resolved_path.is_file())
        updated = self.repository.load_knowledge_file(existing.path)
        self.assertEqual("The framework is complete.", updated.statement)
        self.assertEqual("approved", updated.status)
        self.assertEqual("high", updated.confidence)
        self.assertEqual(2, len(updated.evidence))
        self.assertIn("Human review refine by Rui", updated.history[-1])
        resolved = self.repository.load_review_file(resolved_path)
        self.assertEqual("resolved", resolved.status)
        self.assertEqual("refine", resolved.resolution_action)
        self.assertEqual(["project-framework"], resolved.affected_object_ids)
        self.assertEqual("current", machine_index_status(self.repository))

    def test_reconfirm_keeps_statement_and_appends_candidate_evidence(self):
        existing = self.make_object()
        self.make_review(existing=existing)

        self.resolver().resolve(
            "review-framework",
            "reconfirm",
            "Rui",
            "The new source reconfirms the existing planning state.",
        )

        updated = self.repository.load_knowledge_file(existing.path)
        self.assertEqual("The framework is being planned.", updated.statement)
        self.assertEqual("proposed", updated.status)
        self.assertEqual(2, len(updated.evidence))
        self.assertIn("Human review reconfirmed by Rui", updated.history[-1])

    def test_replace_uses_structured_candidate_metadata(self):
        existing = self.make_object()
        self.make_review(existing=existing)

        self.resolver().resolve(
            "review-framework",
            "replace",
            "Rui",
            "The completed state supersedes the proposal.",
        )

        updated = self.repository.load_knowledge_file(existing.path)
        self.assertEqual("The framework is complete.", updated.statement)
        self.assertEqual("approved", updated.status)
        self.assertEqual("high", updated.confidence)
        self.assertEqual("2026-07-29", updated.effective_date)
        self.assertIn("Human review replace by Rui", updated.history[-1])

    def test_reviewer_can_select_existing_object_for_unlinked_case(self):
        existing = self.make_object()
        self.make_review(existing=None)

        result = self.resolver().resolve(
            "review-framework",
            "refine",
            "Rui",
            "The reviewer matched this candidate to the existing project.",
            existing_id=existing.id,
        )

        self.assertEqual((existing.id,), result.affected_object_ids)
        updated = self.repository.load_knowledge_file(existing.path)
        self.assertEqual("The framework is complete.", updated.statement)

    def test_stale_canonical_snapshot_blocks_resolution(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        existing.statement = "The framework changed after review creation."
        existing.updated_at = "2026-07-29T07:30:00Z"
        existing.path.write_bytes(self.repository.render_knowledge(existing))

        with self.assertRaises(StaleReviewError):
            self.resolver().resolve(
                "review-framework",
                "refine",
                "Rui",
                "Attempting an outdated decision.",
            )

    def test_refresh_rebases_snapshot_then_keep_existing_can_resolve(self):
        existing = self.make_object()
        _, pending_path = self.make_review(existing=existing)
        candidate_statement = "The framework is complete."
        existing.statement = "The framework remains in planning."
        existing.updated_at = "2026-07-29T07:30:00Z"
        existing.path.write_bytes(self.repository.render_knowledge(existing))
        before_refresh = pending_path.read_bytes()

        dry_run = ReviewRefresher(self.repository).refresh(
            "review-framework", dry_run=True
        )

        self.assertTrue(dry_run.dry_run)
        self.assertEqual(before_refresh, pending_path.read_bytes())
        self.assertEqual(
            "The framework remains in planning.", dry_run.current_statement
        )

        applied = ReviewRefresher(self.repository).refresh("review-framework")

        self.assertFalse(applied.dry_run)
        refreshed = self.repository.load_review_file(pending_path)
        self.assertEqual(existing.statement, refreshed.existing_statement)
        self.assertEqual(existing.updated_at, refreshed.existing_updated_at)
        self.assertEqual(
            sha256_bytes(existing.statement.encode("utf-8")),
            refreshed.existing_statement_sha256,
        )
        self.assertEqual(candidate_statement, refreshed.candidate_statement)

        result = self.resolver().resolve(
            "review-framework",
            "keep-existing",
            "Rui",
            "The candidate does not supersede the refreshed canonical state.",
        )
        self.assertEqual("rejected", result.destination_status)
        current = self.repository.load_knowledge_file(existing.path)
        self.assertEqual(existing.statement, current.statement)

    def test_refresh_refuses_to_change_review_identity(self):
        existing = self.make_object()
        self.make_review(existing=existing)

        with self.assertRaises(ReviewResolutionError):
            ReviewRefresher(self.repository).refresh(
                "review-framework", existing_id="another-object"
            )

    def test_commit_precondition_fails_before_overwriting_changed_file(self):
        path = self.repository.root / "precondition.txt"
        path.write_text("loaded", encoding="utf-8")
        expected = sha256_file(path)
        path.write_text("changed concurrently", encoding="utf-8")

        with self.assertRaises(StaleReviewError):
            self.repository.commit(
                {path: b"replacement"},
                preconditions={path: expected},
            )

        self.assertEqual("changed concurrently", path.read_text(encoding="utf-8"))

    def test_expected_absent_precondition_blocks_created_target(self):
        target = self.repository.root / "unexpected-create.txt"
        target.write_text("created concurrently", encoding="utf-8")

        with self.assertRaises(StaleReviewError):
            self.repository.commit(
                {target: b"replacement"},
                preconditions={target: None},
            )

        self.assertEqual(
            "created concurrently", target.read_text(encoding="utf-8")
        )

    def test_read_only_external_evidence_precondition_blocks_every_write(self):
        target = self.repository.root / "precondition-target.txt"
        expected = sha256_file(self.source)
        self.source.write_text("changed outside the repository", encoding="utf-8")

        with self.assertRaises(StaleReviewError):
            self.repository.commit(
                {target: b"must not be written"},
                preconditions={self.source: expected},
            )

        self.assertFalse(target.exists())

    def test_repository_mutation_lock_serializes_repository_instances(self):
        other = KnowledgeRepository(
            self.repository.root,
            meetings_dir=self.repository.meetings_dir,
        )
        attempted = threading.Event()
        acquired = threading.Event()

        def contender():
            attempted.set()
            with other.mutation_lock():
                acquired.set()

        with self.repository.mutation_lock():
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(acquired.wait(0.05))
        thread.join(1)
        self.assertTrue(acquired.is_set())

    def test_unrelated_source_edit_does_not_block_resolution(self):
        # Synced day-files are rewritten whenever any later message lands, so a
        # changed digest alone must not strand a citation that is still exact.
        existing = self.make_object()
        self.make_review(existing=existing)
        self.source.write_text(
            "# Project update\n\nThe framework is complete.\n\nA later note arrived.\n",
            encoding="utf-8",
        )

        result = self.resolver().resolve(
            "review-framework",
            "refine",
            "Rui",
            "The anchor still reads at the recorded lines.",
        )

        self.assertFalse(result.dry_run)
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertFalse(resolved.allowed_stale_evidence)

    def test_shifted_anchor_requires_explicit_override(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        # The anchor text survives, but no longer at the recorded line.
        self.source.write_text(
            "# Project update\n\nAn inserted line.\nThe framework is complete.\n",
            encoding="utf-8",
        )

        with self.assertRaises(StaleReviewError):
            self.resolver().resolve(
                "review-framework",
                "refine",
                "Rui",
                "A moved anchor is real drift.",
            )

    def test_stale_candidate_evidence_requires_explicit_override(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        self.source.write_text(
            "# Project update\n\nThe source changed after extraction.\n",
            encoding="utf-8",
        )

        with self.assertRaises(StaleReviewError):
            self.resolver().resolve(
                "review-framework",
                "refine",
                "Rui",
                "Candidate evidence must still match.",
            )

    def test_stale_candidate_evidence_can_be_explicitly_overridden(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        self.source.write_text(
            "# Project update\n\nThe wording changed but the reviewer checked it.\n",
            encoding="utf-8",
        )

        result = self.resolver().resolve(
            "review-framework",
            "refine",
            "Rui",
            "I inspected the changed source and accept the historical locator.",
            allow_stale_evidence=True,
        )

        self.assertFalse(result.dry_run)
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertTrue(resolved.allowed_stale_evidence)

    def test_stale_override_still_rejects_out_of_range_locator(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        self.source.write_text("# Short source\n", encoding="utf-8")

        with self.assertRaises(StaleReviewError):
            self.resolver().resolve(
                "review-framework",
                "refine",
                "Rui",
                "The evidence locator must remain valid.",
                allow_stale_evidence=True,
            )

    def test_keep_existing_rejects_candidate_without_changing_canonical(self):
        existing = self.make_object()
        _, pending_path = self.make_review(existing=existing)
        before = existing.path.read_bytes()

        result = self.resolver().resolve(
            "review-framework",
            "keep-existing",
            "Rui",
            "The checkmark covered planning only.",
        )

        self.assertEqual("rejected", result.destination_status)
        self.assertEqual(before, existing.path.read_bytes())
        self.assertFalse(pending_path.exists())
        rejected = self.repository.load_review_file(
            self.repository.review_dir / "rejected" / pending_path.name
        )
        self.assertEqual("keep-existing", rejected.resolution_action)
        self.assertEqual("human", rejected.resolution_mode)
        self.assertEqual("not_used", rejected.suggestion_disposition)
        self.assertIsNone(rejected.suggestion_id)

    def test_hybrid_resolution_audit_round_trip(self):
        existing = self.make_object()
        item, _ = self.make_review(existing=existing)
        item.status = "rejected"
        item.resolved_at = "2026-07-29T08:00:00Z"
        item.reviewer = "Rui"
        item.resolution_action = "keep-existing"
        item.resolution_note = "The human accepted the AI recommendation."
        item.affected_object_ids = [existing.id]
        item.suggestion_id = "suggestion-test"
        item.suggested_action = "keep-existing"
        item.suggestion_disposition = "accepted"
        item.resolution_mode = "hybrid"
        path = self.repository.review_dir / "rejected" / ("%s.md" % item.id)
        path.write_bytes(self.repository.render_review(item))

        loaded = self.repository.load_review_file(path)
        text = path.read_text(encoding="utf-8")

        self.assertEqual("suggestion-test", loaded.suggestion_id)
        self.assertEqual("keep-existing", loaded.suggested_action)
        self.assertEqual("accepted", loaded.suggestion_disposition)
        self.assertEqual("hybrid", loaded.resolution_mode)
        self.assertIn("- Suggestion ID: `suggestion-test`", text)
        self.assertIn("- Suggestion disposition: accepted", text)

    def test_legacy_resolved_review_without_ai_audit_fields_still_loads(self):
        existing = self.make_object()
        _, pending_path = self.make_review(existing=existing)
        self.resolver().resolve(
            "review-framework",
            "keep-existing",
            "Rui",
            "Legacy human decision.",
        )
        path = self.repository.review_dir / "rejected" / pending_path.name
        raw, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        for key in (
            "suggestion_id",
            "suggested_action",
            "suggestion_disposition",
            "resolution_mode",
            "automation_policy",
            "verifier_suggestion_id",
            "verifier_model",
        ):
            raw["resolution"].pop(key, None)
        path.write_text(
            "---\n%s\n---\n\n%s"
            % (dump_frontmatter(raw), body.lstrip("\n")),
            encoding="utf-8",
        )

        loaded = self.repository.load_review_file(path)

        self.assertEqual("human", loaded.resolution_mode)
        self.assertEqual("not_used", loaded.suggestion_disposition)
        self.assertIsNone(loaded.suggestion_id)

    def test_repository_rejects_resolution_with_missing_suggestion(self):
        existing = self.make_object()
        _, pending_path = self.make_review(existing=existing)
        self.resolver().resolve(
            "review-framework",
            "keep-existing",
            "Rui",
            "Create a valid human resolution first.",
        )
        path = self.repository.review_dir / "rejected" / pending_path.name
        raw, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        raw["resolution"].update(
            {
                "suggestion_id": "suggestion-missing",
                "suggested_action": "keep-existing",
                "suggestion_disposition": "accepted",
                "resolution_mode": "hybrid",
            }
        )
        path.write_text(
            "---\n%s\n---\n\n%s"
            % (dump_frontmatter(raw), body.lstrip("\n")),
            encoding="utf-8",
        )

        with self.assertRaises(SchemaError):
            self.repository.load_reviews()

    def test_legacy_create_separate_requires_explicit_status_and_confidence(self):
        self.make_review(existing=None, structured=False)

        with self.assertRaises(ReviewResolutionError):
            self.resolver().resolve(
                "review-framework",
                "create-separate",
                "Rui",
                "This is a separate project.",
            )

        result = self.resolver().resolve(
            "review-framework",
            "create-separate",
            "Rui",
            "This is a separate project.",
            status="approved",
            confidence="high",
        )
        self.assertEqual(("project-framework",), result.affected_object_ids)
        created = self.repository.load_knowledge()[0]
        self.assertEqual("approved", created.status)

    def test_merge_duplicate_closes_only_the_duplicate_case(self):
        self.make_review(identifier="review-primary", existing=None)
        _, duplicate_path = self.make_review(
            identifier="review-duplicate", existing=None
        )

        self.resolver().resolve(
            "review-duplicate",
            "merge-duplicate",
            "Rui",
            "Same source and topic as the retained case.",
            duplicate_of="review-primary",
        )

        self.assertTrue(
            (
                self.repository.review_dir
                / "pending"
                / "review-primary.md"
            ).is_file()
        )
        self.assertFalse(duplicate_path.exists())
        merged = self.repository.load_review_file(
            self.repository.review_dir
            / "rejected"
            / "review-duplicate.md"
        )
        self.assertEqual("review-primary", merged.duplicate_of)

    def test_cli_list_show_and_resolve_dry_run(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        common = [
            "--output-dir",
            str(self.repository.root),
            "--meetings-dir",
            str(self.repository.meetings_dir),
            "review",
        ]

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            list_code = main(common + ["list", "--priority", "conflict"])
            show_code = main(common + ["show", "review-framework"])
            resolve_code = main(
                common
                + [
                    "resolve",
                    "review-framework",
                    "--action",
                    "refine",
                    "--reviewer",
                    "Rui",
                    "--note",
                    "Confirmed.",
                    "--dry-run",
                ]
            )

        self.assertEqual((0, 0, 0), (list_code, show_code, resolve_code))
        self.assertIn("review-framework", output.getvalue())
        self.assertIn("Dry run review-framework", output.getvalue())

    def test_cli_refresh_dry_run(self):
        existing = self.make_object()
        self.make_review(existing=existing)
        existing.statement = "The framework remains in planning."
        existing.updated_at = "2026-07-29T07:30:00Z"
        existing.path.write_bytes(self.repository.render_knowledge(existing))
        common = [
            "--output-dir",
            str(self.repository.root),
            "--meetings-dir",
            str(self.repository.meetings_dir),
            "review",
        ]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = main(
                common
                + [
                    "refresh",
                    "review-framework",
                    "--dry-run",
                ]
            )

        self.assertEqual(0, code)
        self.assertIn("Dry run review-framework", output.getvalue())


if __name__ == "__main__":
    unittest.main()
