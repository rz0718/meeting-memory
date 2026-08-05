"""Route-level tests for the local review UI.

The property that keeps the UI and CLI from diverging is that each route calls
the library with the exact argument set the equivalent CLI invocation produces,
so several of these tests parse the CLI's own argv and compare the two argument
dictionaries directly.
"""

import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from meeting_memory.knowledge.answers import FakeAnswerer, KnowledgeAnswer
from meeting_memory.knowledge.cli import build_parser
from meeting_memory.knowledge.errors import ExtractionError
from meeting_memory.knowledge.models import (
    Evidence,
    KnowledgeCandidate,
    KnowledgeObject,
    ReviewItem,
)
from meeting_memory.knowledge.context import build_context_packet
from meeting_memory.knowledge.projects import ProjectScope, scoped_documents
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.review import ReviewResolver
from meeting_memory.knowledge.review_ai import FakeReviewAdvisor
from meeting_memory.knowledge.util import (
    json_bytes,
    local_timezone_label,
    local_timezone_offset_minutes,
    sha256_bytes,
    sha256_file,
)
from meeting_memory.ui.app import create_app
from meeting_memory.ui.arguments import (
    AskRequest,
    MergeRequest,
    ProjectRequest,
    ResolveRequest,
    context_arguments,
    merge_arguments,
    project_scope,
    resolver_arguments,
)
from meeting_memory.ui.payloads import canonical_drift, review_detail_payload, word_diff


FIXED_NOW = dt.datetime(2026, 7, 29, 8, 0, tzinfo=dt.timezone.utc)


def cli_resolve_arguments(argv):
    """The kwargs cli.py builds for `review resolve`, straight from its argv."""
    args = build_parser().parse_args(argv)
    return {
        "action": args.action,
        "reviewer": args.reviewer,
        "note": args.note,
        "suggestion_id": args.suggestion_id,
        "accept_suggestion": args.accept_suggestion,
        "existing_id": args.existing_id,
        "duplicate_of": args.duplicate_of,
        "new_id": args.new_id,
        "title": args.title,
        "status": args.status,
        "owner": args.owner,
        "clear_owner": args.clear_owner,
        "confidence": args.confidence,
        "effective_date": (
            args.effective_date.isoformat() if args.effective_date is not None else None
        ),
        "clear_effective_date": args.clear_effective_date,
        "allow_stale_evidence": args.allow_stale_evidence,
        "resolution_mode": args.resolution_mode,
        "dry_run": args.dry_run,
        "refresh_indexes": not args.no_index,
    }


def cli_context_arguments(argv):
    """The kwargs cli.py builds for `ask`, straight from its argv."""
    args = build_parser().parse_args(argv)
    return {
        "limit": args.limit,
        "max_chars": args.max_chars,
        "max_evidence_per_object": args.max_evidence_per_object,
        "include_review_items": args.include_review_items,
        "include_manual_notes": args.include_manual_notes,
        "include_evidence_excerpts": args.include_evidence_excerpts,
    }


def cli_project_scope(argv):
    """The scope cli.py builds for `project create`, straight from its argv."""
    args = build_parser().parse_args(argv)
    return ProjectScope(
        name=args.name,
        meeting_names=tuple(args.meeting_names),
        slack_names=tuple(args.slack_names),
    )


def cli_merge_arguments(argv):
    args = build_parser().parse_args(argv)
    return {
        "loser_id": args.loser_id,
        "survivor_id": args.survivor_id,
        "reviewer": args.reviewer,
        "note": args.note,
        "statement": args.statement,
        "title": args.title,
        "status": args.status,
        "owner": args.owner,
        "clear_owner": args.clear_owner,
        "confidence": args.confidence,
        "effective_date": (
            args.effective_date.isoformat() if args.effective_date is not None else None
        ),
        "clear_effective_date": args.clear_effective_date,
        "allow_cross_category": args.allow_cross_category,
        "allow_conflicting_numbers": args.allow_conflicting_numbers,
        "dry_run": args.dry_run,
        "refresh_indexes": not args.no_index,
    }


class UiTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.source = self.repository.meetings_dir / "2026-07-29" / "project-update.md"
        self.source.parent.mkdir(parents=True)
        self.source.write_text(
            "# Project update\n\nThe framework is complete.\n", encoding="utf-8"
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
        self.client = TestClient(create_app(self.repository, reviewer="rui"))

    # -- fixtures ----------------------------------------------------------

    def make_object(self, object_id="project-framework", statement=None):
        obj = KnowledgeObject(
            id=object_id,
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
            statement=statement or "The framework is being planned.",
            history=["2026-07-28: Initially recorded as proposed."],
            path=self.repository.knowledge_dir / "projects" / ("%s.md" % object_id),
        )
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        return obj

    def make_review(self, existing=None, identifier="review-framework"):
        candidate = KnowledgeCandidate(
            category="projects",
            title="Framework",
            statement="The framework is complete.",
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
            candidate_statement="The framework is complete.",
            explanation="A human must select the lifecycle state.",
            existing_evidence=copy.deepcopy(existing.evidence if existing else []),
            candidate_evidence=[copy.deepcopy(self.evidence)],
            candidate=candidate,
            existing_updated_at=existing.updated_at if existing else None,
            existing_statement_sha256=(
                sha256_bytes(existing.statement.encode("utf-8")) if existing else None
            ),
        )
        path = self.repository.review_dir / "pending" / ("%s.md" % item.id)
        path.write_bytes(self.repository.render_review(item))
        return item, path

    def make_run_manifest(self, run_id="20260729T080000Z", **overrides):
        manifest = {
            "run_id": run_id,
            "target_dates": ["2026-07-29"],
            "started_at": "2026-07-29T08:00:00Z",
            "completed_at": "2026-07-29T08:05:00Z",
            "status": "success",
            "sources_examined": [self.source_ref],
            "sources_processed": [self.source_ref],
            "sources_skipped": [],
            "objects_created": [],
            "objects_reconfirmed": [],
            "objects_refined": [],
            "review_items_created": [],
            "candidates_rejected": [],
            "errors": [],
        }
        manifest.update(overrides)
        path = self.repository.state_dir / "runs" / ("%s.json" % manifest["run_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json_bytes(manifest))
        return manifest


class ArgumentEquivalenceTest(unittest.TestCase):
    """The UI form and the CLI flag set must produce identical library calls."""

    def test_override_form_matches_cli_resolve_invocation(self):
        body = ResolveRequest(
            action="refine",
            suggestion_id="suggestion-1",
            note="Confirmed with the owner.",
            existing_id="project-framework",
            status="approved",
            owner="Team",
            confidence="high",
            effective_date="2026-07-29",
            allow_stale_evidence=True,
            dry_run=True,
        )

        self.assertEqual(
            cli_resolve_arguments(
                [
                    "review",
                    "resolve",
                    "review-framework",
                    "--action",
                    "refine",
                    "--suggestion-id",
                    "suggestion-1",
                    "--reviewer",
                    "rui",
                    "--note",
                    "Confirmed with the owner.",
                    "--existing-id",
                    "project-framework",
                    "--status",
                    "approved",
                    "--owner",
                    "Team",
                    "--confidence",
                    "high",
                    "--effective-date",
                    "2026-07-29",
                    "--allow-stale-evidence",
                    "--dry-run",
                ]
            ),
            resolver_arguments(body, "rui"),
        )

    def test_accept_form_matches_cli_accept_suggestion_invocation(self):
        body = ResolveRequest(
            accept_suggestion=True,
            suggestion_id="suggestion-1",
            note="Accepted as suggested.",
            dry_run=False,
        )

        self.assertEqual(
            cli_resolve_arguments(
                [
                    "review",
                    "resolve",
                    "review-framework",
                    "--accept-suggestion",
                    "--suggestion-id",
                    "suggestion-1",
                    "--reviewer",
                    "rui",
                    "--note",
                    "Accepted as suggested.",
                ]
            ),
            resolver_arguments(body, "rui"),
        )

    def test_clear_flags_match_cli_clear_invocation(self):
        body = ResolveRequest(
            action="replace",
            note="Owner is no longer assigned.",
            clear_owner=True,
            clear_effective_date=True,
            no_index=True,
            dry_run=False,
        )

        self.assertEqual(
            cli_resolve_arguments(
                [
                    "review",
                    "resolve",
                    "review-framework",
                    "--action",
                    "replace",
                    "--reviewer",
                    "rui",
                    "--note",
                    "Owner is no longer assigned.",
                    "--clear-owner",
                    "--clear-effective-date",
                    "--no-index",
                ]
            ),
            resolver_arguments(body, "rui"),
        )

    def test_merge_form_matches_cli_merge_invocation(self):
        body = MergeRequest(
            loser_id="metric-threshold",
            survivor_id="policy-threshold",
            note="The same standing threshold recorded twice.",
            allow_cross_category=True,
            allow_conflicting_numbers=True,
            dry_run=True,
        )

        self.assertEqual(
            cli_merge_arguments(
                [
                    "merge",
                    "metric-threshold",
                    "--into",
                    "policy-threshold",
                    "--reviewer",
                    "rui",
                    "--note",
                    "The same standing threshold recorded twice.",
                    "--allow-cross-category",
                    "--allow-conflicting-numbers",
                    "--dry-run",
                ]
            ),
            merge_arguments(body, "rui"),
        )

    def test_blank_fields_are_omitted_rather_than_sent_as_empty_strings(self):
        arguments = resolver_arguments(
            ResolveRequest(action="reconfirm", note="Still true.", owner="", title=""),
            "rui",
        )

        self.assertIsNone(arguments["owner"])
        self.assertIsNone(arguments["title"])

    def test_invalid_effective_date_is_rejected_like_argparse(self):
        with self.assertRaises(ValueError):
            resolver_arguments(
                ResolveRequest(action="refine", note="n", effective_date="29-07-2026"),
                "rui",
            )


class RunRoutesTest(UiTestCase):
    def test_runs_list_reports_manifest_counts_and_ordering(self):
        self.make_run_manifest("20260728T080000Z", started_at="2026-07-28T08:00:00Z",
                               completed_at="2026-07-28T08:04:00Z")
        self.make_run_manifest("20260729T080000Z", objects_created=["project-framework"])

        payload = self.client.get("/api/runs").json()

        self.assertEqual(2, payload["count"])
        self.assertEqual("20260729T080000Z", payload["runs"][0]["run_id"])
        self.assertEqual(1, payload["runs"][0]["counts"]["objects_created"])
        self.assertEqual(["2026-07-29", "2026-07-28"], payload["dates"])

    def test_runs_list_filters_by_run_start_date(self):
        self.make_run_manifest("20260728T080000Z", started_at="2026-07-28T08:00:00Z",
                               completed_at="2026-07-28T08:04:00Z")
        self.make_run_manifest("20260729T080000Z")

        payload = self.client.get("/api/runs", params={"date": "2026-07-28"}).json()

        self.assertEqual(["20260728T080000Z"], [run["run_id"] for run in payload["runs"]])

    def test_malformed_date_filter_is_a_client_error(self):
        response = self.client.get("/api/runs", params={"date": "29-07-2026"})

        self.assertEqual(400, response.status_code)
        self.assertIn("YYYY-MM-DD", response.json()["error"])

    def test_run_detail_groups_by_manifest_bucket(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        self.make_run_manifest(
            objects_created=[obj.id],
            review_items_created=[item.id],
        )

        payload = self.client.get("/api/runs/20260729T080000Z").json()

        buckets = {group["bucket"]: group for group in payload["groups"]}
        self.assertEqual(
            ["objects_created", "objects_refined", "objects_reconfirmed", "review_items_created"],
            [group["bucket"] for group in payload["groups"]],
        )
        self.assertEqual(obj.id, buckets["objects_created"]["rows"][0]["id"])
        self.assertTrue(buckets["objects_created"]["rows"][0]["present"])
        self.assertEqual("conflict", buckets["review_items_created"]["rows"][0]["priority"])

    def test_refined_row_reports_unavailable_prior_text_instead_of_inventing_one(self):
        obj = self.make_object()
        self.make_run_manifest(objects_refined=[obj.id])

        payload = self.client.get("/api/runs/20260729T080000Z").json()
        row = payload["groups"][1]["rows"][0]

        self.assertFalse(row["refinement"]["available"])
        self.assertIsNone(row["refinement"]["before"])
        self.assertIn("not recorded structurally", row["refinement"]["reason"])

    def test_row_for_a_removed_object_is_marked_absent(self):
        self.make_run_manifest(objects_created=["object-that-was-removed"])

        payload = self.client.get("/api/runs/20260729T080000Z").json()

        self.assertFalse(payload["groups"][0]["rows"][0]["present"])

    def test_unknown_run_is_not_found(self):
        self.assertEqual(404, self.client.get("/api/runs/nope").status_code)

    def test_knowledge_detail_includes_evidence_excerpts_with_line_numbers(self):
        obj = self.make_object()

        payload = self.client.get("/api/knowledge/%s" % obj.id).json()

        excerpt = payload["evidence"][0]
        self.assertEqual(1, excerpt["line_start"])
        self.assertEqual("# Project update", excerpt["excerpt"]["text"])
        self.assertEqual("current", excerpt["freshness_label"])

    def test_knowledge_detail_lists_pending_reviews_that_name_the_object(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)

        payload = self.client.get("/api/knowledge/%s" % obj.id).json()

        self.assertEqual([item.id], payload["pending_review_ids"])


class ReviewReadRoutesTest(UiTestCase):
    def test_queue_is_sorted_conflict_before_unlinked(self):
        obj = self.make_object()
        self.make_review(existing=None, identifier="review-unlinked")
        self.make_review(existing=obj, identifier="review-conflict")

        payload = self.client.get("/api/reviews").json()

        self.assertEqual(
            ["review-conflict", "review-unlinked"],
            [row["id"] for row in payload["reviews"]],
        )
        self.assertEqual({"conflict": 1, "linked": 0, "unlinked": 1},
                         payload["counts_by_priority"])

    def test_queue_filters_match_review_list_filters(self):
        obj = self.make_object()
        self.make_review(existing=obj, identifier="review-conflict")
        self.make_review(existing=None, identifier="review-unlinked")

        payload = self.client.get("/api/reviews", params={"priority": "conflict"}).json()

        self.assertEqual(["review-conflict"], [row["id"] for row in payload["reviews"]])

    def test_review_detail_carries_diff_evidence_and_blocked_state(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)

        payload = self.client.get("/api/reviews/%s" % item.id).json()

        self.assertEqual("conflict", payload["priority"])
        self.assertTrue(payload["statement_diff"]["left"])
        self.assertEqual("current", payload["candidate_evidence"][0]["freshness_label"])
        self.assertIsNone(payload["blocked"]["canonical_drift"])
        self.assertTrue(payload["blocked"]["accept_disabled"])
        self.assertEqual(
            "no current AI suggestion is available",
            payload["blocked"]["accept_disabled_reason"],
        )
        self.assertFalse(payload["blocked"]["target_choice_required"])

    def test_review_detail_reports_canonical_drift_when_the_object_moved(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        obj.statement = "The framework was cancelled."
        obj.path.write_bytes(self.repository.render_knowledge(obj))

        payload = self.client.get("/api/reviews/%s" % item.id).json()

        drift = payload["blocked"]["canonical_drift"]
        self.assertIsNotNone(drift)
        self.assertEqual(obj.id, drift["existing_id"])
        self.assertEqual("The framework was cancelled.", drift["current_statement"])

    def test_drift_detection_matches_the_resolver_refusal(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        obj.statement = "The framework was cancelled."
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        reloaded = self.repository.load_reviews("pending")[0]

        self.assertIsNotNone(
            canonical_drift(reloaded, self.repository.load_knowledge())
        )
        response = self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={"action": "refine", "note": "Trying anyway.", "dry_run": True},
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("StaleReviewError", response.json()["type"])

    def test_unknown_review_is_not_found(self):
        self.assertEqual(404, self.client.get("/api/reviews/nope").status_code)


class ResolveRouteTest(UiTestCase):
    def test_dry_run_changes_nothing_and_reports_the_recorded_disposition(self):
        obj = self.make_object()
        item, pending = self.make_review(existing=obj)
        before = obj.path.read_bytes()

        payload = self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={"action": "refine", "note": "Confirmed.", "dry_run": True},
        ).json()

        self.assertTrue(payload["preview"]["dry_run"])
        self.assertEqual("resolved", payload["preview"]["destination_status"])
        self.assertEqual(
            {"disposition": "not_used", "mode": "human"}, payload["will_record"]
        )
        self.assertEqual(before, obj.path.read_bytes())
        self.assertTrue(pending.is_file())

    def test_apply_without_a_preview_is_refused(self):
        obj = self.make_object()
        item, pending = self.make_review(existing=obj)

        response = self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={"action": "refine", "note": "Confirmed.", "dry_run": False},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("preview this exact decision", response.json()["error"])
        self.assertTrue(pending.is_file())

    def test_apply_with_arguments_that_differ_from_the_preview_is_refused(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={"action": "refine", "note": "Confirmed.", "dry_run": True},
        )

        response = self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={"action": "replace", "note": "Confirmed.", "dry_run": False},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("preview this exact decision", response.json()["error"])

    def test_previewed_decision_applies_and_writes_the_audit_record(self):
        obj = self.make_object()
        item, pending = self.make_review(existing=obj)
        body = {"action": "refine", "note": "The owner confirmed completion."}

        self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": True}
        )
        payload = self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False}
        ).json()

        self.assertFalse(payload["applied"]["dry_run"])
        self.assertFalse(pending.exists())
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertEqual("rui", resolved.reviewer)
        self.assertEqual("refine", resolved.resolution_action)
        self.assertEqual("human", resolved.resolution_mode)
        self.assertEqual(
            "The framework is complete.",
            self.repository.load_knowledge_file(obj.path).statement,
        )

    def test_replaying_the_same_apply_fails_loudly_instead_of_repeating(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        body = {"action": "refine", "note": "Confirmed."}
        self.client.post("/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": True})
        self.client.post("/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False})

        response = self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False}
        )

        self.assertEqual(400, response.status_code)

    def test_empty_note_is_refused_by_the_resolver(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)

        response = self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={"action": "refine", "note": "   ", "dry_run": True},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("note may not be empty", response.json()["error"])

    def test_reviewer_change_is_recorded_on_the_next_resolution(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        self.client.post("/api/session/reviewer", json={"reviewer": "someone-else"})
        body = {"action": "keep-existing", "note": "Existing text still holds."}

        self.client.post("/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": True})
        self.client.post("/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False})

        self.assertEqual(
            "someone-else", self.repository.load_reviews("rejected")[0].reviewer
        )

    def test_raw_notes_are_never_written_by_a_resolution(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        before = self.source.read_bytes()
        body = {"action": "refine", "note": "Confirmed."}

        self.client.post("/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": True})
        self.client.post("/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False})

        self.assertEqual(before, self.source.read_bytes())


class RefreshRouteTest(UiTestCase):
    def test_refresh_rebases_a_drifted_review_after_its_own_dry_run(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        obj.statement = "The framework was cancelled."
        obj.updated_at = "2026-07-29T09:00:00Z"
        obj.path.write_bytes(self.repository.render_knowledge(obj))

        preview = self.client.post(
            "/api/reviews/%s/refresh" % item.id, json={"dry_run": True}
        ).json()
        applied = self.client.post(
            "/api/reviews/%s/refresh" % item.id, json={"dry_run": False}
        ).json()

        self.assertEqual(
            "The framework is being planned.", preview["preview"]["previous_statement"]
        )
        self.assertEqual(
            "The framework was cancelled.", applied["applied"]["current_statement"]
        )
        reloaded = self.repository.load_reviews("pending")[0]
        self.assertEqual("The framework was cancelled.", reloaded.existing_statement)
        self.assertIsNone(canonical_drift(reloaded, self.repository.load_knowledge()))

    def test_refresh_apply_without_a_preview_is_refused(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        obj.statement = "The framework was cancelled."
        obj.path.write_bytes(self.repository.render_knowledge(obj))

        response = self.client.post(
            "/api/reviews/%s/refresh" % item.id, json={"dry_run": False}
        )

        self.assertEqual(400, response.status_code)


class MergeRouteTest(UiTestCase):
    def test_merge_requires_a_preview_and_then_folds_the_loser_in(self):
        survivor = self.make_object("project-framework")
        loser = self.make_object("project-framework-duplicate")
        body = {
            "loser_id": loser.id,
            "survivor_id": survivor.id,
            "note": "The same project recorded twice.",
        }

        refused = self.client.post("/api/merge", json={**body, "dry_run": False})
        self.client.post("/api/merge", json={**body, "dry_run": True})
        applied = self.client.post("/api/merge", json={**body, "dry_run": False})

        self.assertEqual(400, refused.status_code)
        self.assertEqual(200, applied.status_code)
        self.assertFalse(loser.path.exists())
        self.assertTrue(survivor.path.is_file())

    def test_cross_category_merge_is_refused_until_the_override_is_ticked(self):
        survivor = self.make_object("project-framework")
        other = KnowledgeObject(
            id="metric-framework",
            title="Framework metric",
            category="metrics",
            status="proposed",
            effective_date=None,
            last_confirmed=None,
            owner=None,
            confidence="medium",
            created_at="2026-07-28T08:00:00Z",
            updated_at="2026-07-28T08:00:00Z",
            evidence=[copy.deepcopy(self.evidence)],
            related_objects=[],
            statement="The framework is being planned.",
            history=[],
            path=self.repository.knowledge_dir / "metrics" / "metric-framework.md",
        )
        other.path.write_bytes(self.repository.render_knowledge(other))
        body = {
            "loser_id": other.id,
            "survivor_id": survivor.id,
            "note": "One fact in two categories.",
            "dry_run": True,
        }

        refused = self.client.post("/api/merge", json=body)
        allowed = self.client.post(
            "/api/merge", json={**body, "allow_cross_category": True}
        )

        self.assertEqual(400, refused.status_code)
        self.assertIn("--allow-cross-category", refused.json()["error"])
        self.assertEqual(200, allowed.status_code)


class RemovalBasketTest(UiTestCase):
    def test_basket_is_assembled_then_executed_against_an_asserted_inventory(self):
        obj = self.make_object()

        self.client.post("/api/removal-basket/items", json={"object_id": obj.id})
        preview = self.client.post(
            "/api/removal-basket/preview", json={"note": "Superseded by a merge."}
        ).json()
        basket = preview["basket"]
        self.assertTrue(obj.path.is_file(), "preview must not remove anything")
        applied = self.client.post(
            "/api/removal-basket/apply",
            json={
                "note": "Superseded by a merge.",
                "confirm_count": basket["inventory_count"],
                "inventory_sha256": basket["inventory_sha256"],
            },
        ).json()

        self.assertEqual(1, basket["inventory_count"])
        self.assertEqual(64, len(basket["inventory_sha256"]))
        self.assertTrue(preview["preview"]["dry_run"])
        self.assertEqual([obj.id], applied["applied"]["object_ids"])
        self.assertFalse(obj.path.exists())
        self.assertEqual(0, applied["basket"]["count"])

    def test_apply_without_a_preview_is_refused(self):
        obj = self.make_object()
        self.client.post("/api/removal-basket/items", json={"object_id": obj.id})

        response = self.client.post(
            "/api/removal-basket/apply",
            json={"note": "n", "confirm_count": 1, "inventory_sha256": "0" * 64},
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("preview the removal basket", response.json()["error"])
        self.assertTrue(obj.path.is_file())

    def test_apply_with_a_mismatched_digest_is_refused(self):
        obj = self.make_object()
        self.client.post("/api/removal-basket/items", json={"object_id": obj.id})
        preview = self.client.post("/api/removal-basket/preview", json={"note": "n"}).json()

        response = self.client.post(
            "/api/removal-basket/apply",
            json={
                "note": "n",
                "confirm_count": preview["basket"]["inventory_count"],
                "inventory_sha256": "0" * 64,
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("SHA-256 does not match", response.json()["error"])
        self.assertTrue(obj.path.is_file())

    def test_apply_with_a_mismatched_count_is_refused(self):
        obj = self.make_object()
        self.client.post("/api/removal-basket/items", json={"object_id": obj.id})
        preview = self.client.post("/api/removal-basket/preview", json={"note": "n"}).json()

        response = self.client.post(
            "/api/removal-basket/apply",
            json={
                "note": "n",
                "confirm_count": 5,
                "inventory_sha256": preview["basket"]["inventory_sha256"],
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("expected 5", response.json()["error"])
        self.assertTrue(obj.path.is_file())

    def test_adding_an_unknown_object_is_refused(self):
        response = self.client.post(
            "/api/removal-basket/items", json={"object_id": "no-such-object"}
        )

        self.assertEqual(400, response.status_code)

    def test_changing_the_basket_invalidates_the_previous_preview(self):
        first = self.make_object("project-framework")
        second = self.make_object("project-second")
        self.client.post("/api/removal-basket/items", json={"object_id": first.id})
        preview = self.client.post("/api/removal-basket/preview", json={"note": "n"}).json()
        self.client.post("/api/removal-basket/items", json={"object_id": second.id})

        state = self.client.get("/api/removal-basket").json()

        self.assertFalse(state["inventory_matches_basket"])
        self.assertNotEqual(state["pending_sha256"], preview["basket"]["inventory_sha256"])


class SessionAndDiffTest(UiTestCase):
    def test_session_reports_reviewer_and_model_configuration(self):
        payload = self.client.get("/api/session").json()

        self.assertEqual("rui", payload["reviewer"])
        self.assertFalse(payload["review_model_configured"])

    def test_session_reports_the_operating_calendar_timezone(self):
        payload = self.client.get("/api/session").json()

        self.assertEqual(
            {"label": "SGT", "offset_minutes": 480}, payload["timezone"]
        )

    def test_timezone_label_follows_the_configured_offset(self):
        cases = {"8": "SGT", "0": "UTC", "-5": "UTC-5", "9": "UTC+9"}
        for offset, expected in cases.items():
            with mock.patch.dict(
                os.environ, {"MEETING_TZ_UTC_OFFSET_HOURS": offset}, clear=False
            ):
                self.assertEqual(expected, local_timezone_label(), offset)

    def test_timezone_label_can_be_named_explicitly(self):
        with mock.patch.dict(
            os.environ,
            {"MEETING_TZ_UTC_OFFSET_HOURS": "9", "MEETING_TZ_LABEL": "JST"},
            clear=False,
        ):
            self.assertEqual("JST", local_timezone_label())
            self.assertEqual(540, local_timezone_offset_minutes())

    def test_suggestion_generation_without_a_model_is_a_service_error(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)

        response = self.client.post("/api/reviews/%s/suggest" % item.id, json={})

        self.assertEqual(503, response.status_code)
        self.assertIn("review model is not configured", response.json()["error"])

    def test_empty_reviewer_is_refused(self):
        self.assertEqual(
            422, self.client.post("/api/session/reviewer", json={"reviewer": ""}).status_code
        )

    def test_word_diff_marks_only_the_changed_words_on_each_side(self):
        result = word_diff("The framework is being planned.", "The framework is complete.")

        self.assertEqual(
            "The framework is ",
            "".join(part["text"] for part in result["left"] if part["kind"] == "same"),
        )
        self.assertEqual(
            ["being planned."],
            [part["text"] for part in result["left"] if part["kind"] == "removed"],
        )
        self.assertEqual(
            ["complete."],
            [part["text"] for part in result["right"] if part["kind"] == "added"],
        )

    def test_word_diff_handles_an_absent_existing_statement(self):
        result = word_diff(None, "The framework is complete.")

        self.assertEqual([], result["left"])
        self.assertEqual(["added"], [part["kind"] for part in result["right"]])


class SuggestionFlowTest(UiTestCase):
    """The accept/override distinction is the point of the tab, so it is tested
    against a real suggestion artifact rather than a stubbed payload."""

    MODEL = "test/reviewer"

    def setUp(self):
        super().setUp()
        self.client = TestClient(
            create_app(
                self.repository,
                reviewer="rui",
                review_model=self.MODEL,
                advisor_factory=lambda service: FakeReviewAdvisor(
                    self.repository, self.replace_response()
                ),
            )
        )

    def replace_response(self, existing_id="project-framework"):
        return {
            "suggested_action": "replace",
            "confidence": "high",
            "existing_id": existing_id,
            "duplicate_of": None,
            "new_id": None,
            "proposed_knowledge": {
                "category": "projects",
                "title": "Framework",
                "statement": "The framework is complete.",
                "status": "approved",
                "effective_date": "2026-07-29",
                "owner": "Team",
                "confidence": "high",
            },
            "material_differences": ["The lifecycle status changed."],
            "evidence_findings": [
                {
                    "source": self.source_ref,
                    "line_start": 3,
                    "line_end": 3,
                    "finding": "The source says the framework is complete.",
                }
            ],
            "rationale": "The direct source supports replacement.",
            "proposed_note": "Replace the planned state with completed.",
            "risks": [],
            "requires_human": False,
        }

    def with_suggestion(self):
        obj = self.make_object()
        item, pending = self.make_review(existing=obj)
        payload = self.client.post("/api/reviews/%s/suggest" % item.id, json={}).json()
        return obj, item, pending, payload["suggestion"]

    def test_generating_a_suggestion_enables_accept_and_prefills_the_note(self):
        _, item, _, suggestion = self.with_suggestion()

        detail = self.client.get("/api/reviews/%s" % item.id).json()

        self.assertEqual(suggestion["id"], detail["suggestion"]["id"])
        self.assertTrue(detail["suggestion"]["current"])
        self.assertFalse(detail["blocked"]["accept_disabled"])
        self.assertEqual(
            "Replace the planned state with completed.",
            detail["suggestion"]["recommendation"]["proposed_note"],
        )

    def test_accepting_records_accepted_and_hybrid(self):
        obj, item, pending, suggestion = self.with_suggestion()
        body = {
            "accept_suggestion": True,
            "suggestion_id": suggestion["id"],
            "note": "I read the rationale and agree.",
        }

        preview = self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": True}
        ).json()
        self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False}
        )

        self.assertEqual(
            {"disposition": "accepted", "mode": "hybrid"}, preview["will_record"]
        )
        self.assertFalse(pending.exists())
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertEqual("accepted", resolved.suggestion_disposition)
        self.assertEqual("hybrid", resolved.resolution_mode)
        self.assertEqual(suggestion["id"], resolved.suggestion_id)
        self.assertEqual("replace", resolved.suggested_action)
        self.assertEqual("replace", resolved.resolution_action)

    def test_overriding_still_transmits_the_suggestion_id(self):
        obj, item, _, suggestion = self.with_suggestion()
        body = {
            "action": "reconfirm",
            "suggestion_id": suggestion["id"],
            "existing_id": obj.id,
            "note": "The existing text is right; only re-date it.",
        }

        preview = self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": True}
        ).json()
        self.client.post(
            "/api/reviews/%s/resolve" % item.id, json={**body, "dry_run": False}
        )

        self.assertEqual(
            {"disposition": "overridden", "mode": "hybrid"}, preview["will_record"]
        )
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertEqual("overridden", resolved.suggestion_disposition)
        self.assertEqual("hybrid", resolved.resolution_mode)
        self.assertEqual(suggestion["id"], resolved.suggestion_id)
        self.assertEqual("replace", resolved.suggested_action)
        self.assertEqual("reconfirm", resolved.resolution_action)

    def test_accepting_with_an_override_is_refused_by_the_resolver(self):
        obj, item, _, suggestion = self.with_suggestion()

        response = self.client.post(
            "/api/reviews/%s/resolve" % item.id,
            json={
                "accept_suggestion": True,
                "suggestion_id": suggestion["id"],
                "existing_id": obj.id,
                "note": "Accepting but also steering.",
                "dry_run": True,
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("cannot be combined with resolution overrides", response.json()["error"])

    def test_suggestion_artifacts_are_append_only_across_regeneration(self):
        _, item, _, first = self.with_suggestion()

        self.client.post("/api/reviews/%s/suggest" % item.id, json={"force": True})
        detail = self.client.get("/api/reviews/%s" % item.id).json()

        self.assertEqual(2, len(detail["suggestions"]))
        self.assertIn(first["id"], [value["id"] for value in detail["suggestions"]])

    def test_batch_generation_streams_one_line_per_review(self):
        self.make_object()
        obj = self.repository.load_knowledge()[0]
        self.make_review(existing=obj, identifier="review-framework")

        with self.client.stream("POST", "/api/reviews/suggest", json={}) as response:
            events = [
                json.loads(line) for line in response.iter_lines() if line.strip()
            ]

        kinds = [event["event"] for event in events]
        self.assertEqual("batch", kinds[0])
        self.assertIn("start", kinds)
        self.assertIn("item", kinds)
        self.assertEqual("complete", kinds[-1])
        completion = events[-1]
        self.assertEqual("success", completion["manifest"]["status"])
        self.assertEqual(
            ["review-framework"], list(completion["manifest"]["suggestions_created"])
        )

    def test_batch_generation_isolates_a_single_failing_review(self):
        obj = self.make_object()
        self.make_review(existing=obj, identifier="review-framework")
        service = self.client.app.state.service
        service.advisor_factory = lambda _service: FakeReviewAdvisor(
            self.repository, ExtractionError("provider exploded")
        )

        with self.client.stream("POST", "/api/reviews/suggest", json={}) as response:
            events = [
                json.loads(line) for line in response.iter_lines() if line.strip()
            ]

        failure = next(
            event for event in events if event.get("outcome") == "failed"
        )
        self.assertIn("provider exploded", failure["error"])
        self.assertEqual("failed", events[-1]["manifest"]["status"])

    def test_refresh_then_regenerate_clears_the_drift_block(self):
        obj, item, _, _ = self.with_suggestion()
        obj.statement = "The framework was cancelled."
        obj.updated_at = "2026-07-29T09:00:00Z"
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        self.assertIsNotNone(
            self.client.get("/api/reviews/%s" % item.id).json()["blocked"]["canonical_drift"]
        )

        self.client.post("/api/reviews/%s/refresh" % item.id, json={"dry_run": True})
        self.client.post("/api/reviews/%s/refresh" % item.id, json={"dry_run": False})
        self.client.post("/api/reviews/%s/suggest" % item.id, json={})
        detail = self.client.get("/api/reviews/%s" % item.id).json()

        self.assertIsNone(detail["blocked"]["canonical_drift"])
        self.assertIsNotNone(detail["suggestion"])
        self.assertTrue(detail["suggestion"]["current"])


class ReadCacheTest(UiTestCase):
    """The read cache exists so one rendered page parses the corpus once. It is
    only ever safe around reads, which the mutation lock is responsible for."""

    def test_cache_serves_one_corpus_parse_within_its_scope(self):
        self.make_object()

        with self.repository.read_cache():
            first = self.repository.load_knowledge()
            second = self.repository.load_knowledge()
        outside = self.repository.load_knowledge()

        self.assertIs(first, second)
        self.assertIsNot(first, outside)

    def test_a_writer_neither_reads_from_nor_poisons_the_cache(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)

        with self.repository.read_cache():
            cached = self.repository.load_knowledge()
            ReviewResolver(self.repository).resolve(
                item.id, "refine", "rui", "The owner confirmed completion."
            )
            # The scope is one request: it keeps serving what it already read.
            self.assertIs(cached, self.repository.load_knowledge())

        self.assertEqual(
            "The framework is being planned.",
            next(value for value in cached if value.id == obj.id).statement,
        )
        self.assertEqual(
            "The framework is complete.",
            self.repository.load_knowledge_file(obj.path).statement,
        )
        self.assertEqual(
            "The framework is complete.",
            next(
                value
                for value in self.repository.load_knowledge()
                if value.id == obj.id
            ).statement,
        )

    def test_nested_scopes_share_one_cache(self):
        self.make_object()

        with self.repository.read_cache():
            outer = self.repository.load_knowledge()
            with self.repository.read_cache():
                inner = self.repository.load_knowledge()
            still_cached = self.repository.load_knowledge()

        self.assertIs(outer, inner)
        self.assertIs(outer, still_cached)

    def test_review_detail_route_is_unchanged_by_caching(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)

        cached = self.client.get("/api/reviews/%s" % item.id).json()
        uncached = review_detail_payload(self.repository, item.id)

        self.assertEqual(uncached["statement_diff"], cached["statement_diff"])
        self.assertEqual(uncached["blocked"], cached["blocked"])
        self.assertEqual(uncached["candidate_evidence"], cached["candidate_evidence"])


class AskRoutesTest(UiTestCase):
    """Tab 3 renders an answer next to the retrieval it rests on.

    The value of the tab is entirely in what surrounds the answer -- which
    objects were supplied, which of those the model cited, which it passed over,
    and what the character budget dropped -- so that is what these assert.
    """

    MODEL = "test/ask"

    def setUp(self):
        super().setUp()
        self.answers = []
        self.answerer = None
        self.client = TestClient(
            create_app(
                self.repository,
                reviewer="rui",
                ask_model=self.MODEL,
                answerer_factory=lambda service: self.answerer,
            )
        )

    def use_answer(self, **overrides):
        values = {
            "question": "What is the state of the framework?",
            "answer": "The framework is being planned.",
            "confidence": "high",
            "knowledge_objects_used": ("project-framework",),
            "meeting_evidence_used": (self.source_ref,),
            "open_conflicts": (),
            "model": self.MODEL,
        }
        values.update(overrides)
        self.answerer = FakeAnswerer(KnowledgeAnswer(**values))
        return self.answerer

    def ask(self, path="answer", **body):
        payload = {"query": "framework", "include_review_items": False}
        payload.update(body)
        return self.client.post("/api/ask/%s" % path, json=payload)

    def test_context_route_retrieves_without_calling_a_provider(self):
        self.make_object()
        self.answerer = None  # any provider call would raise AttributeError

        payload = self.ask("context").json()

        self.assertEqual(1, payload["retrieval"]["objects_selected"])
        selected = payload["objects"][0]
        self.assertEqual("project-framework", selected["id"])
        self.assertIn("direct deterministic search match", selected["selection_reason"])
        self.assertEqual(["id", "title", "statement"], selected["matched_fields"])
        self.assertEqual("current", selected["evidence"][0]["freshness_label"])
        self.assertIn("The framework is being planned.", payload["markdown"])

    def test_answer_marks_what_was_cited_and_what_was_only_considered(self):
        self.make_object()
        self.make_object(
            object_id="project-framework-rollout",
            statement="The framework rollout starts in August.",
        )
        self.use_answer()

        payload = self.ask().json()

        self.assertEqual("high", payload["answer"]["confidence"])
        self.assertEqual(
            [{"id": "project-framework", "title": "Framework",
              "category": "projects", "present": True}],
            payload["cited_objects"],
        )
        self.assertEqual(["project-framework-rollout"], payload["uncited_object_ids"])
        cited = next(
            value for value in payload["context"]["objects"]
            if value["id"] == "project-framework"
        )
        self.assertTrue(cited["cited"])
        self.assertTrue(cited["evidence"][0]["cited"])
        considered = next(
            value for value in payload["context"]["objects"]
            if value["id"] == "project-framework-rollout"
        )
        self.assertFalse(considered["cited"])
        # Retrieval detail is what separates a retrieval failure from a
        # reasoning failure, so it survives onto the uncited object too.
        self.assertTrue(considered["selection_reason"])

    def test_cited_evidence_resolves_to_the_objects_that_carry_it(self):
        self.make_object()
        self.use_answer()

        payload = self.ask().json()

        self.assertEqual(
            [{"source": self.source_ref,
              "object_ids": ["project-framework"],
              "present": True}],
            payload["cited_evidence"],
        )

    def test_a_citation_outside_the_packet_is_reported_not_hidden(self):
        """The validator makes this impossible for a real answer; if it ever
        happens it is a retrieval bug and must be visible as one."""
        self.make_object()
        self.use_answer(knowledge_objects_used=("project-ghost",))

        payload = self.ask().json()

        self.assertEqual(
            [{"id": "project-ghost", "title": None,
              "category": None, "present": False}],
            payload["cited_objects"],
        )

    def test_an_empty_packet_answers_insufficient_without_a_provider_call(self):
        self.make_object()
        self.answerer = None

        payload = self.ask(query="nothing here matches this").json()

        self.assertEqual(0, payload["context"]["retrieval"]["objects_selected"])
        self.assertEqual("low", payload["answer"]["confidence"])
        self.assertIsNone(payload["answer"]["model"])
        self.assertIn("insufficient", payload["answer"]["answer"].lower())
        self.assertEqual([], payload["cited_objects"])

    def test_budget_omissions_are_reported_rather_than_swallowed(self):
        self.make_object()
        self.make_object(
            object_id="project-framework-rollout",
            statement="The framework rollout starts in August.",
        )
        self.use_answer()

        payload = self.ask(max_chars=1400).json()

        omissions = payload["context"]["omissions"]
        self.assertTrue(omissions)
        self.assertEqual(len(omissions), payload["context"]["retrieval"]["omissions"])

    def test_pending_review_items_are_carried_as_proposed_material(self):
        obj = self.make_object()
        item, _ = self.make_review(existing=obj)
        self.use_answer()

        payload = self.ask(include_review_items=True).json()

        reviews = payload["context"]["reviews"]
        self.assertEqual([item.id], [value["id"] for value in reviews])
        self.assertEqual("conflicting_evidence", reviews[0]["reason"])

    def test_evidence_trimmed_by_the_budget_is_labelled_as_partial(self):
        self.make_object()
        self.use_answer()

        payload = self.ask("context", max_evidence_per_object=0).json()

        selected = payload["objects"][0]
        self.assertEqual([], selected["evidence"])
        self.assertEqual(0, selected["evidence_in_context"])
        self.assertEqual(1, selected["evidence_total"])

    def test_saving_writes_the_answer_that_was_shown_not_a_fresh_one(self):
        self.make_object()
        self.use_answer()
        answered = self.ask().json()

        saved = self.client.post(
            "/api/ask/save", json={"answer_token": answered["answer_token"]}
        ).json()

        # One provider call: saving replays the recorded answer.
        self.assertEqual(1, len(self.answerer.calls))
        path = self.repository.root / saved["saved_path"]
        text = path.read_text(encoding="utf-8")
        self.assertIn("The framework is being planned.", text)
        self.assertIn("`project-framework`", text)
        self.assertIn("Context SHA-256", text)
        self.assertNotIn("knowledge/", saved["saved_path"])
        self.assertNotIn("knowledge-review", saved["saved_path"])

    def test_saving_an_answer_this_session_never_saw_is_refused(self):
        response = self.client.post(
            "/api/ask/save", json={"answer_token": "0" * 64}
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("ask the question again", response.json()["error"])

    def test_the_packet_digest_lets_the_client_detect_a_changed_repository(self):
        self.make_object()
        self.use_answer()

        first = self.ask("context").json()
        self.make_object(
            object_id="project-framework-rollout",
            statement="The framework rollout starts in August.",
        )
        second = self.ask("context").json()

        self.assertNotEqual(first["packet_sha256"], second["packet_sha256"])

    def test_ask_without_a_configured_model_reports_the_configuration(self):
        self.make_object()
        client = TestClient(create_app(self.repository, reviewer="rui"))

        response = client.post("/api/ask/answer", json={"query": "framework"})

        self.assertEqual(503, response.status_code)
        self.assertIn("ask model is not configured", response.json()["error"])

    def test_the_form_and_the_cli_build_the_same_context_packet_call(self):
        body = AskRequest(
            query="framework",
            limit=4,
            max_chars=12000,
            max_evidence_per_object=2,
            include_review_items=False,
            include_manual_notes=True,
            include_evidence_excerpts=True,
        )

        self.assertEqual(
            cli_context_arguments(
                [
                    "ask",
                    "framework",
                    "--limit",
                    "4",
                    "--max-chars",
                    "12000",
                    "--max-evidence-per-object",
                    "2",
                    "--no-review-items",
                    "--include-manual-notes",
                    "--include-evidence-excerpts",
                ]
            ),
            context_arguments(body),
        )


class ProjectScopeRoutesTest(UiTestCase):
    """A project bounds retrieval to one set of meetings and Slack channels.

    The two properties worth asserting are that the scope only ever narrows --
    no object, related object, or review item may enter from outside it -- and
    that what it narrowed to is reported, since a scoped answer read as a global
    one is worse than no answer at all.
    """

    MODEL = "test/ask"

    def setUp(self):
        super().setUp()
        self.answerer = None
        self.client = TestClient(
            create_app(
                self.repository,
                reviewer="rui",
                ask_model=self.MODEL,
                answerer_factory=lambda service: self.answerer,
            )
        )
        self.cci = self.write_source(
            "2026-07-29", "CCI Weekly.md", "# CCI Weekly\n\nThe framework is live.\n"
        )
        self.other = self.write_source(
            "2026-07-29", "Vendor Weekly.md", "# Vendor Weekly\n\nThe framework waits.\n"
        )
        self.slack = self.write_source(
            "2026-07-29", "slack-c123.md", "# Slack channel C123\n\nFramework shipped.\n"
        )

    # -- fixtures ----------------------------------------------------------

    def write_source(self, date, filename, body):
        path = self.repository.meetings_dir / date / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def make_object_on(self, object_id, sources, statement, related=()):
        obj = KnowledgeObject(
            id=object_id,
            title="Framework",
            category="projects",
            status="approved",
            effective_date="2026-07-29",
            last_confirmed="2026-07-29",
            owner="Team",
            confidence="high",
            created_at="2026-07-29T08:00:00Z",
            updated_at="2026-07-29T08:00:00Z",
            evidence=[
                Evidence(
                    source=self.repository.source_reference(path),
                    source_sha256=sha256_file(path),
                    anchor="framework",
                    line_start=1,
                    line_end=1,
                    observed_at="2026-07-29",
                )
                for path in sources
            ],
            related_objects=list(related),
            statement=statement,
            path=self.repository.knowledge_dir / "projects" / ("%s.md" % object_id),
        )
        obj.path.write_bytes(self.repository.render_knowledge(obj))
        return obj

    def save_scope(self, name="CCI", meetings=("CCI",), slack=()):
        response = self.client.post(
            "/api/projects",
            json={
                "name": name,
                "meeting_names": list(meetings),
                "slack_names": list(slack),
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def ask(self, path="answer", **body):
        payload = {"query": "framework", "include_review_items": False}
        payload.update(body)
        return self.client.post("/api/ask/%s" % path, json=payload).json()

    # -- the source universe -----------------------------------------------

    def test_the_universe_offers_only_sources_the_index_actually_cites(self):
        """A project assembled from observed sources cannot name a file that
        does not exist, and a source backing no object could only ever resolve
        to an empty scope."""
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")
        self.write_source("2026-07-29", "Unused.md", "# Unused\n\nNothing cites this.\n")

        universe = self.client.get("/api/projects").json()["universe"]

        self.assertEqual(
            ["meetings/2026-07-29/CCI Weekly.md"],
            [entry["source"] for entry in universe["sources"]],
        )
        entry = universe["sources"][0]
        self.assertEqual("meeting", entry["kind"])
        self.assertEqual("2026-07-29", entry["date"])
        self.assertEqual("CCI Weekly", entry["selector"])
        self.assertEqual(1, entry["objects"])

    def test_slack_sources_are_offered_by_channel_and_selected_exactly(self):
        self.make_object_on("project-slack", [self.slack], "The framework shipped.")

        universe = self.client.get("/api/projects").json()["universe"]
        entry = universe["sources"][0]

        self.assertEqual("slack", entry["kind"])
        self.assertEqual("c123", entry["selector"])
        self.assertEqual(1, universe["slack"])
        resolution = self.client.post(
            "/api/projects/preview", json={"slack_names": ["c123-ops"]}
        ).json()
        self.assertEqual(0, resolution["sources_matched"])
        self.assertEqual(["c123-ops"], resolution["unmatched_selectors"]["slack_names"])

    def test_preview_reports_sources_a_fuzzy_selector_pulled_in_unclicked(self):
        """Meeting selectors match by substring, so the sources a selection
        resolves to are not always the sources that were clicked."""
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")
        self.make_object_on("project-vendor", [self.other], "The vendor waits.")

        resolution = self.client.post(
            "/api/projects/preview", json={"meeting_names": ["Weekly"]}
        ).json()

        self.assertEqual(2, resolution["sources_matched"])
        self.assertEqual(2, resolution["sources_by_fuzzy_match"])
        self.assertEqual([False, False], [e["direct"] for e in resolution["sources"]])
        self.assertEqual(2, resolution["objects"])

        exact = self.client.post(
            "/api/projects/preview", json={"meeting_names": ["CCI Weekly"]}
        ).json()
        self.assertEqual([True], [e["direct"] for e in exact["sources"]])
        self.assertEqual(0, exact["sources_by_fuzzy_match"])

    def test_an_empty_selection_resolves_to_nothing_rather_than_everything(self):
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")

        resolution = self.client.post("/api/projects/preview", json={}).json()

        self.assertEqual(0, resolution["objects"])
        self.assertEqual(0, resolution["sources_matched"])
        self.assertEqual(1, resolution["objects_total"])

    # -- saving --------------------------------------------------------------

    def test_the_editor_and_the_cli_build_the_same_scope(self):
        body = ProjectRequest(
            name="CCI", meeting_names=["CCI", ""], slack_names=["C123"]
        )

        self.assertEqual(cli_project_scope(
            ["project", "create", "CCI", "--meeting-name", "CCI", "--slack-name", "C123"]
        ), project_scope(body))

    def test_saving_a_project_writes_the_file_the_cli_writes(self):
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")

        saved = self.save_scope(slack=("C123",))

        self.assertEqual(".knowledge-state/projects/cci.json", saved["path"])
        stored = json.loads(
            (self.repository.root / saved["path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(["CCI"], stored["meeting_names"])
        self.assertEqual(1, saved["resolution"]["objects"])
        listed = self.client.get("/api/projects").json()["projects"]
        self.assertEqual(["CCI"], [value["name"] for value in listed])
        self.assertEqual(1, listed[0]["objects"])

    def test_redefining_a_saved_project_requires_the_replace_flag(self):
        """A scope that could be silently redefined would change what an
        already-answered question meant."""
        self.save_scope()

        clash = self.client.post(
            "/api/projects", json={"name": "CCI", "meeting_names": ["Vendor"]}
        )
        self.assertEqual(400, clash.status_code)
        self.assertIn("already exists", clash.json()["error"])

        replaced = self.client.post(
            "/api/projects",
            json={"name": "CCI", "meeting_names": ["Vendor"], "replace": True},
        )
        self.assertEqual(200, replaced.status_code)
        self.assertEqual(["Vendor"], replaced.json()["project"]["meeting_names"])

    def test_a_project_with_no_selectors_is_refused(self):
        response = self.client.post("/api/projects", json={"name": "CCI"})

        self.assertEqual(400, response.status_code)
        self.assertIn("at least one", response.json()["error"])

    # -- scoped retrieval ----------------------------------------------------

    def test_retrieval_admits_only_objects_evidenced_inside_the_scope(self):
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")
        self.make_object_on("project-vendor", [self.other], "The framework waits.")
        self.save_scope()

        payload = self.ask("context", project="CCI")

        self.assertEqual(
            ["project-cci"], [value["id"] for value in payload["objects"]]
        )
        self.assertEqual("CCI", payload["scope"]["name"])
        self.assertEqual(1, payload["scope"]["objects"])
        self.assertEqual(2, payload["scope"]["objects_total"])
        self.assertEqual(1, payload["scope"]["sources_matched"])
        self.assertEqual(2, payload["scope"]["sources_total"])
        self.assertNotIn("vendor", payload["markdown"].lower())

    def test_a_related_object_may_not_enter_the_packet_from_outside_the_scope(self):
        """One-hop expansion reads the same document sequence, so pre-filtering
        covers it -- silently, which is why it is asserted."""
        self.make_object_on(
            "project-cci",
            [self.cci],
            "The CCI framework is live.",
            related=("project-vendor",),
        )
        self.make_object_on("project-vendor", [self.other], "Unrelated wording.")
        self.save_scope()

        payload = self.ask("context", project="CCI")

        self.assertEqual(
            ["project-cci"], [value["id"] for value in payload["objects"]]
        )

    def test_an_out_of_scope_review_item_never_enters_a_scoped_packet(self):
        obj = self.make_object_on("project-framework", [self.cci], "The framework is live.")
        self.make_review(existing=obj)
        self.save_scope()

        unscoped = self.ask("context", include_review_items=True)
        scoped = self.ask("context", project="CCI", include_review_items=True)

        # The review's evidence is the out-of-scope default source, so it is
        # connected to the question but not to the project.
        self.assertEqual(1, unscoped["retrieval"]["reviews_selected"])
        self.assertEqual(0, scoped["retrieval"]["reviews_selected"])
        self.assertTrue(scoped["scope"]["review_items_scoped"])

    def test_a_straddling_object_shows_only_its_in_scope_evidence(self):
        """The scope bounds what is retrieved and inspectable, not the
        provenance of a statement synthesized from all of an object's
        evidence -- so the shortfall is reported rather than implied."""
        self.make_object_on(
            "project-cci", [self.cci, self.other], "The CCI framework is live."
        )
        self.save_scope()

        payload = self.ask("context", project="CCI")

        selected = payload["objects"][0]
        self.assertEqual(
            ["meetings/2026-07-29/CCI Weekly.md"],
            [entry["source"] for entry in selected["evidence"]],
        )
        self.assertEqual(1, selected["evidence_total"])
        self.assertEqual(2, selected["evidence_all_sources"])
        self.assertEqual(1, selected["evidence_out_of_scope"])
        self.assertEqual(
            [{"id": "project-cci", "title": "Framework", "in_scope": 1, "total": 2}],
            payload["scope"]["straddling"],
        )

    def test_an_empty_scope_stays_empty_and_never_reaches_a_provider(self):
        """Silent widening is the one failure this feature cannot have."""
        self.make_object_on("project-vendor", [self.other], "The framework waits.")
        self.save_scope()
        self.answerer = None  # any provider call would raise AttributeError

        payload = self.ask(project="CCI")

        self.assertEqual(0, payload["context"]["retrieval"]["objects_selected"])
        self.assertEqual(0, payload["context"]["scope"]["objects"])
        self.assertEqual(1, payload["context"]["scope"]["objects_total"])
        self.assertIsNone(payload["answer"]["model"])
        self.assertIn("insufficient", payload["answer"]["answer"].lower())

    def test_an_unknown_project_is_refused_rather_than_ignored(self):
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")

        response = self.client.post(
            "/api/ask/context", json={"query": "framework", "project": "Ghost"}
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("project scope not found", response.json()["error"])

    def test_the_saved_answer_states_the_scope_it_ran_under(self):
        self.make_object_on(
            "project-cci", [self.cci, self.other], "The CCI framework is live."
        )
        self.save_scope()
        self.answerer = FakeAnswerer(
            KnowledgeAnswer(
                question="framework",
                answer="The CCI framework is live.",
                confidence="high",
                knowledge_objects_used=("project-cci",),
                meeting_evidence_used=("meetings/2026-07-29/CCI Weekly.md",),
                open_conflicts=(),
                model=self.MODEL,
            )
        )

        answered = self.ask(project="CCI")
        saved = self.client.post(
            "/api/ask/save", json={"answer_token": answered["answer_token"]}
        ).json()
        text = (self.repository.root / saved["saved_path"]).read_text(encoding="utf-8")

        self.assertIn("- Project: CCI", text)
        # This question ran with review items switched off; the file says which
        # of the two reasons no proposal appears in it.
        self.assertIn("- Pending review items: excluded from this question", text)
        self.assertIn("- Knowledge objects in scope: 1 of 1", text)
        self.assertIn("- Sources in scope: 1 of 2", text)
        self.assertIn("outside the scope: 1", text)

    def test_an_unscoped_saved_answer_says_so_explicitly(self):
        self.make_object_on("project-cci", [self.cci], "The CCI framework is live.")
        self.answerer = FakeAnswerer(
            KnowledgeAnswer(
                question="framework",
                answer="The CCI framework is live.",
                confidence="high",
                knowledge_objects_used=("project-cci",),
                meeting_evidence_used=("meetings/2026-07-29/CCI Weekly.md",),
                open_conflicts=(),
                model=self.MODEL,
            )
        )

        answered = self.ask()
        saved = self.client.post(
            "/api/ask/save", json={"answer_token": answered["answer_token"]}
        ).json()

        self.assertIn(
            "All durable knowledge (no project scope).",
            (self.repository.root / saved["saved_path"]).read_text(encoding="utf-8"),
        )

    def test_the_route_and_the_cli_build_the_same_scoped_packet(self):
        """Byte-identical packets are what keep a scoped answer in the UI and a
        scoped answer on the command line from being two different things."""
        self.make_object_on(
            "project-cci", [self.cci, self.other], "The CCI framework is live."
        )
        self.make_object_on("project-vendor", [self.other], "The framework waits.")
        self.save_scope()
        # Excerpts are named explicitly because the form defaults them on and
        # ``ask`` defaults them off. This test is about scoping, so both sides
        # ask for the same material and the packets differ only if scope does.
        args = build_parser().parse_args(
            [
                "ask",
                "framework",
                "--project",
                "CCI",
                "--no-review-items",
                "--include-evidence-excerpts",
            ]
        )

        documents, scope = scoped_documents(self.repository, args.project)
        expected = build_context_packet(
            self.repository,
            documents,
            args.query,
            limit=args.limit,
            max_chars=args.max_chars,
            max_evidence_per_object=args.max_evidence_per_object,
            include_review_items=args.include_review_items,
            include_manual_notes=args.include_manual_notes,
            include_evidence_excerpts=args.include_evidence_excerpts,
            source_predicate=scope.matches_source,
        )

        self.assertEqual(expected.markdown, self.ask("context", project="CCI")["markdown"])


class StaticAssetTest(UiTestCase):
    def test_index_and_modules_are_served(self):
        self.assertEqual(200, self.client.get("/").status_code)
        for name in (
            "styles.css",
            "js/app.js",
            "js/queue.js",
            "js/runs.js",
            "js/ask.js",
            "js/projects.js",
        ):
            self.assertEqual(200, self.client.get("/static/%s" % name).status_code, name)


if __name__ == "__main__":
    unittest.main()
