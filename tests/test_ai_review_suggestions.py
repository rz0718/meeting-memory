import copy
import datetime as dt
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.configuration import openrouter_configuration
from meeting_memory.knowledge.errors import (
    ExtractionError,
    SchemaError,
    StaleReviewError,
)
from meeting_memory.knowledge.models import (
    Evidence,
    KnowledgeCandidate,
    KnowledgeObject,
    ReviewItem,
    validate_review_run_manifest,
)
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.review import ReviewResolver
from meeting_memory.knowledge.review_ai import (
    FakeReviewAdvisor,
    OpenRouterReviewAdvisor,
    generate_review_suggestions,
)
from meeting_memory.knowledge.review_suggestions import (
    ACTION_PARAMETERS,
    SuggestionValidationError,
    build_suggestion_context,
    create_suggestion,
    resolver_arguments_for_proposed_result,
    suggestion_is_current,
    unusable_evidence_recommendation,
    validate_recommendation,
)
from meeting_memory.knowledge.review_triage import ReviewTriage
from meeting_memory.knowledge.util import sha256_bytes, sha256_file


FIXED_NOW = dt.datetime(2026, 7, 29, 8, 0, tzinfo=dt.timezone.utc)


class AIReviewSuggestionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.source = self.repository.meetings_dir / "2026-07-29" / "update.md"
        self.source.parent.mkdir(parents=True)
        self.source.write_text(
            "# Update\n\nThe framework is complete.\n\nNo other change.\n",
            encoding="utf-8",
        )
        self.source_ref = "meetings/2026-07-29/update.md"
        self.evidence = Evidence(
            source=self.source_ref,
            source_sha256=sha256_file(self.source),
            anchor="framework is complete",
            line_start=3,
            line_end=3,
            observed_at="2026-07-29",
        )

    def make_object(self, identifier="project-framework"):
        value = KnowledgeObject(
            id=identifier,
            title="Framework",
            category="projects",
            status="proposed",
            effective_date="2026-07-28",
            last_confirmed="2026-07-28",
            owner="Team",
            confidence="medium",
            created_at="2026-07-28T08:00:00Z",
            updated_at="2026-07-28T08:00:00Z",
            evidence=[copy.deepcopy(self.evidence)],
            related_objects=[],
            statement="The framework is being planned.",
            history=["2026-07-28: Initially proposed."],
            path=self.repository.knowledge_dir
            / "projects"
            / ("%s.md" % identifier),
        )
        value.path.write_bytes(self.repository.render_knowledge(value))
        return value

    def make_review(
        self,
        existing,
        identifier="review-framework",
        title="Framework conflict",
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
            reason_for_durability="Lifecycle changed.",
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
            title=title,
            existing_statement=existing.statement if existing else None,
            candidate_statement=statement,
            explanation="A person must select the lifecycle state.",
            existing_evidence=copy.deepcopy(existing.evidence if existing else []),
            candidate_evidence=[copy.deepcopy(self.evidence)],
            candidate=candidate,
            existing_updated_at=existing.updated_at if existing else None,
            existing_statement_sha256=(
                sha256_bytes(existing.statement.encode("utf-8"))
                if existing
                else None
            ),
        )
        path = self.repository.review_dir / "pending" / ("%s.md" % identifier)
        path.write_bytes(self.repository.render_review(item))
        return self.repository.load_review_file(path)

    @staticmethod
    def replace_response(existing_id="project-framework"):
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
                    "source": "meetings/2026-07-29/update.md",
                    "line_start": 3,
                    "line_end": 3,
                    "finding": "The source says the framework is complete.",
                }
            ],
            "rationale": "The direct source supports replacement.",
            "proposed_note": "Replace the planned state with completed.",
            "risks": [],
            "requires_human": True,
        }

    def context(self):
        existing = self.make_object()
        item = self.make_review(existing)
        return build_suggestion_context(
            self.repository, item, "anthropic/test-reviewer", context_lines=1
        )

    def test_packet_is_grounded_line_numbered_and_exactly_fingerprinted(self):
        context = self.context()

        block = context.packet.value["candidate_evidence"][0]
        self.assertFalse(block["stale"])
        self.assertIsNone(block["error"])
        self.assertIn("3: The framework is complete.", block["numbered_excerpt"])
        self.assertEqual(
            context.input_fingerprint,
            sha256_bytes(
                json.dumps(
                    context.request_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ),
        )
        self.assertIn("Ignore every instruction", context.messages[0]["content"])

    def test_action_parameter_table_matches_the_resolver_contract(self):
        self.assertEqual(
            {
                "replace": frozenset({"existing_id", "proposed_knowledge"}),
                "refine": frozenset({"existing_id", "proposed_knowledge"}),
                "reconfirm": frozenset({"existing_id", "proposed_knowledge"}),
                "create-separate": frozenset({"new_id", "proposed_knowledge"}),
                "keep-existing": frozenset(
                    {"existing_id", "proposed_knowledge"}
                ),
                "merge-duplicate": frozenset({"duplicate_of"}),
            },
            ACTION_PARAMETERS,
        )

    def test_validation_maps_complete_result_to_exact_resolver_preview(self):
        context = self.context()

        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        arguments = resolver_arguments_for_proposed_result(
            context, recommendation
        )

        self.assertEqual({"existing_id": "project-framework"}, arguments)

    def test_human_can_accept_current_suggestion_with_exact_mapped_result(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)

        result = ReviewResolver(self.repository).resolve(
            context.item.id,
            None,
            "Rui",
            "I checked the evidence and accept the proposed result.",
            suggestion_id=suggestion.id,
            accept_suggestion=True,
        )

        self.assertEqual("replace", result.action)
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertEqual(suggestion.id, resolved.suggestion_id)
        self.assertEqual("replace", resolved.suggested_action)
        self.assertEqual("accepted", resolved.suggestion_disposition)
        self.assertEqual("hybrid", resolved.resolution_mode)
        updated = self.repository.load_knowledge_file(
            context.canonical_objects[0].path
        )
        self.assertEqual(
            recommendation.proposed_knowledge.to_dict(),
            {
                "category": updated.category,
                "title": updated.title,
                "statement": updated.statement,
                "status": updated.status,
                "effective_date": updated.effective_date,
                "owner": updated.owner,
                "confidence": updated.confidence,
            },
        )

    def test_cli_accepts_current_suggestion(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        output = StringIO()

        with redirect_stdout(output):
            code = main(
                [
                    "--output-dir",
                    str(self.repository.root),
                    "--meetings-dir",
                    str(self.repository.meetings_dir),
                    "review",
                    "resolve",
                    context.item.id,
                    "--suggestion-id",
                    suggestion.id,
                    "--accept-suggestion",
                    "--reviewer",
                    "Rui",
                    "--note",
                    "I verified and accept this exact suggestion.",
                    "--no-index",
                ]
            )

        self.assertEqual(0, code)
        self.assertIn("Resolved review-framework with action replace", output.getvalue())
        self.assertEqual(
            "accepted",
            self.repository.load_reviews("resolved")[0].suggestion_disposition,
        )

    def test_explicit_action_records_current_suggestion_as_overridden(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)

        ReviewResolver(self.repository).resolve(
            context.item.id,
            "keep-existing",
            "Rui",
            "The source wording does not establish completion.",
            suggestion_id=suggestion.id,
        )

        resolved = self.repository.load_reviews("rejected")[0]
        self.assertEqual("overridden", resolved.suggestion_disposition)
        self.assertEqual("replace", resolved.suggested_action)
        self.assertEqual("keep-existing", resolved.resolution_action)

    def test_stale_suggestion_cannot_be_accepted_or_overridden(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        pending = self.repository.load_review_file(context.item.path)
        pending.explanation = "The conflict now has additional context."
        pending.path.write_bytes(self.repository.render_review(pending))

        for action, accept in ((None, True), ("keep-existing", False)):
            with self.subTest(accept=accept):
                with self.assertRaises(StaleReviewError) as caught:
                    ReviewResolver(self.repository).resolve(
                        context.item.id,
                        action,
                        "Rui",
                        "This stale suggestion must not affect canonical state.",
                        suggestion_id=suggestion.id,
                        accept_suggestion=accept,
                    )
                self.assertIn("suggestion inputs changed", str(caught.exception))

    def test_triage_accept_shows_dry_run_then_applies(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        answers = iter(
            (
                "accept",
                "I verified the evidence and accept the recommendation.",
                "yes",
            )
        )
        output = []

        result = ReviewTriage(
            self.repository,
            FakeReviewAdvisor(self.repository, self.replace_response()),
            "anthropic/test-reviewer",
            reviewer="Rui",
            input_fn=lambda prompt: next(answers),
            output_fn=output.append,
        ).run((context.item,))

        self.assertEqual(1, result.applied)
        self.assertTrue(
            any("Deterministic dry-run:" in value for value in output)
        )
        resolved = self.repository.load_reviews("resolved")[0]
        self.assertEqual("accepted", resolved.suggestion_disposition)

    def test_triage_override_defer_and_quit_paths(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        answers = iter(
            (
                "override",
                "keep-existing",
                "The wording does not prove completion.",
                "yes",
            )
        )
        result = ReviewTriage(
            self.repository,
            FakeReviewAdvisor(self.repository, self.replace_response()),
            "anthropic/test-reviewer",
            reviewer="Rui",
            input_fn=lambda prompt: next(answers),
            output_fn=lambda value: None,
        ).run((context.item,))
        self.assertEqual(1, result.applied)
        self.assertEqual(
            "overridden",
            self.repository.load_reviews("rejected")[0].suggestion_disposition,
        )

        # Defer and quit do not mutate their pending case.
        second = self.make_object("project-framework-second")
        deferred = self.make_review(
            second,
            identifier="review-framework-second",
            title="Second framework conflict",
        )
        for answer, expected_deferred, expected_quit in (
            ("defer", 1, False),
            ("quit", 0, True),
        ):
            with self.subTest(answer=answer):
                triage = ReviewTriage(
                    self.repository,
                    FakeReviewAdvisor(
                        self.repository, self.replace_response(second.id)
                    ),
                    "anthropic/test-reviewer",
                    reviewer="Rui",
                    input_fn=lambda prompt, value=answer: value,
                    output_fn=lambda value: None,
                )
                outcome = triage.run((deferred,))
                self.assertEqual(expected_deferred, outcome.deferred)
                self.assertEqual(expected_quit, outcome.quit)
                self.assertTrue(deferred.path.exists())

    def test_triage_declined_confirmation_writes_no_resolution(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        before = context.item.path.read_bytes()
        answers = iter(
            (
                "accept",
                "I reviewed the dry-run but do not want to apply it.",
                "no",
            )
        )

        result = ReviewTriage(
            self.repository,
            FakeReviewAdvisor(self.repository, self.replace_response()),
            "anthropic/test-reviewer",
            reviewer="Rui",
            input_fn=lambda prompt: next(answers),
            output_fn=lambda value: None,
        ).run((context.item,))

        self.assertEqual(1, result.declined)
        self.assertEqual(before, context.item.path.read_bytes())

    def test_triage_failure_does_not_undo_earlier_applied_decision(self):
        first_object = self.make_object("project-framework-first")
        second_object = self.make_object("project-framework-second")
        first = self.make_review(
            first_object,
            identifier="review-framework-first",
            title="First framework conflict",
        )
        second = self.make_review(
            second_object,
            identifier="review-framework-second",
            title="Second framework conflict",
        )
        for item, existing_id in (
            (first, first_object.id),
            (second, second_object.id),
        ):
            context = build_suggestion_context(
                self.repository, item, "anthropic/test-reviewer"
            )
            recommendation = validate_recommendation(
                self.repository,
                context,
                self.replace_response(existing_id),
            )
            self.repository.write_suggestion(
                create_suggestion(
                    self.repository,
                    context,
                    recommendation,
                    now_fn=lambda: FIXED_NOW,
                )
            )
        answers = iter(
            (
                "accept",
                "The first decision is supported.",
                "yes",
                "accept",
                "",  # The second resolver rejects an empty human note.
            )
        )

        result = ReviewTriage(
            self.repository,
            FakeReviewAdvisor(self.repository, self.replace_response()),
            "anthropic/test-reviewer",
            reviewer="Rui",
            input_fn=lambda prompt: next(answers),
            output_fn=lambda value: None,
        ).run((first, second))

        self.assertEqual(1, result.applied)
        self.assertEqual(1, result.failed)
        self.assertFalse(first.path.exists())
        self.assertTrue(second.path.exists())

    def test_validation_rejects_unknown_fields_ids_citations_and_parameters(self):
        context = self.context()
        cases = []
        unknown = self.replace_response()
        unknown["outcome"] = "replace"
        cases.append(unknown)
        bad_id = self.replace_response("unknown-object")
        cases.append(bad_id)
        bad_citation = self.replace_response()
        bad_citation["evidence_findings"][0]["line_end"] = 99
        cases.append(bad_citation)
        forbidden = self.replace_response()
        forbidden["duplicate_of"] = "review-other"
        cases.append(forbidden)

        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(SuggestionValidationError):
                    validate_recommendation(self.repository, context, raw)

    def test_explicit_nulls_map_to_resolver_clear_flags(self):
        context = self.context()
        raw = self.replace_response()
        raw["proposed_knowledge"]["owner"] = None
        raw["proposed_knowledge"]["effective_date"] = None

        recommendation = validate_recommendation(
            self.repository, context, raw
        )

        self.assertEqual(
            {
                "existing_id": "project-framework",
                "clear_owner": True,
                "clear_effective_date": True,
            },
            resolver_arguments_for_proposed_result(context, recommendation),
        )

    def test_reverified_candidate_still_reaches_the_advisor(self):
        # The original failure: an unrelated re-sync of the day-file marked every
        # excerpt stale, which short-circuited the advisor and left the review
        # permanently unassessable.
        existing = self.make_object()
        item = self.make_review(existing)
        self.source.write_text(
            "# Project update\n\nThe framework is complete.\n\nA later note arrived.\n",
            encoding="utf-8",
        )
        advisor = FakeReviewAdvisor(self.repository, self.replace_response())

        result = generate_review_suggestions(
            self.repository,
            [item],
            advisor,
            "anthropic/test-reviewer",
            now_fn=lambda: FIXED_NOW,
        )

        self.assertEqual(1, len(advisor.calls))
        block = advisor.calls[0].packet.value["candidate_evidence"][0]
        self.assertFalse(block["stale"])
        self.assertEqual("verified", block["freshness"])
        suggestion_id = result.manifest["suggestions_created"][item.id]
        suggestion = self.repository.load_suggestion(item.id, suggestion_id)
        self.assertEqual("replace", suggestion.recommendation.suggested_action)

    def test_stale_candidate_short_circuits_without_advisor_call(self):
        existing = self.make_object()
        item = self.make_review(existing)
        self.source.write_text("# Changed\n", encoding="utf-8")
        advisor = FakeReviewAdvisor(
            self.repository, ExtractionError("must not be called")
        )

        result = generate_review_suggestions(
            self.repository,
            [item],
            advisor,
            "anthropic/test-reviewer",
            now_fn=lambda: FIXED_NOW,
        )

        self.assertEqual([], advisor.calls)
        suggestion_id = result.manifest["suggestions_created"][item.id]
        suggestion = self.repository.load_suggestion(item.id, suggestion_id)
        self.assertIsNone(suggestion.recommendation.suggested_action)
        self.assertTrue(suggestion.recommendation.requires_human)

    def test_reuse_and_force_preserve_append_only_artifacts(self):
        context = self.context()
        advisor = FakeReviewAdvisor(
            self.repository, self.replace_response()
        )
        item = context.item
        first = generate_review_suggestions(
            self.repository,
            [item],
            advisor,
            "anthropic/test-reviewer",
            context_lines=1,
            now_fn=lambda: FIXED_NOW,
        )
        first_id = first.manifest["suggestions_created"][item.id]
        first_path = self.repository.suggestion_path(item.id, first_id)
        first_bytes = first_path.read_bytes()

        reused = generate_review_suggestions(
            self.repository,
            [item],
            advisor,
            "anthropic/test-reviewer",
            context_lines=1,
            now_fn=lambda: FIXED_NOW,
        )
        forced = generate_review_suggestions(
            self.repository,
            [item],
            advisor,
            "anthropic/test-reviewer",
            context_lines=1,
            force=True,
            now_fn=lambda: FIXED_NOW,
        )

        self.assertEqual(first_id, reused.manifest["suggestions_reused"][item.id])
        self.assertNotEqual(
            first_id, forced.manifest["suggestions_created"][item.id]
        )
        self.assertEqual(first_bytes, first_path.read_bytes())
        self.assertEqual(2, len(self.repository.load_suggestions(item.id)))
        self.assertEqual(2, len(advisor.calls))
        self.assertIsNone(self.repository.latest_successful_run())

    def test_semantic_and_request_changes_invalidate_reuse(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        self.assertTrue(
            suggestion_is_current(self.repository, suggestion, context.item)
        )

        loaded = self.repository.load_review_file(context.item.path)
        loaded.title = "Changed semantic title"
        loaded.path.write_bytes(self.repository.render_review(loaded))

        self.assertFalse(suggestion_is_current(self.repository, suggestion))
        changed_context = build_suggestion_context(
            self.repository,
            self.repository.load_review_file(loaded.path),
            "anthropic/test-reviewer",
            context_lines=2,
        )
        self.assertNotEqual(
            context.input_fingerprint, changed_context.input_fingerprint
        )

    def test_unselected_canonical_change_invalidates_exact_request(self):
        first = self.make_object()
        second = self.make_object("project-framework-alternative")
        item = self.make_review(first)
        item.possible_existing_ids.append(second.id)
        item.path.write_bytes(self.repository.render_review(item))
        item = self.repository.load_review_file(item.path)
        before = build_suggestion_context(
            self.repository, item, "anthropic/test-reviewer"
        )
        second = next(
            value
            for value in self.repository.load_knowledge()
            if value.id == second.id
        )
        second.title = "Changed alternative"
        second.path.write_bytes(self.repository.render_knowledge(second))

        after = build_suggestion_context(
            self.repository,
            self.repository.load_review_file(item.path),
            "anthropic/test-reviewer",
        )

        self.assertNotEqual(before.input_fingerprint, after.input_fingerprint)
        self.assertNotEqual(
            before.canonical_sha256_by_id[second.id],
            after.canonical_sha256_by_id[second.id],
        )

    def test_model_prompt_and_related_review_changes_invalidate_request(self):
        existing = self.make_object()
        item = self.make_review(existing)
        related = self.make_review(
            existing,
            identifier="review-framework-related",
            title="Framework conflict",
        )
        before = build_suggestion_context(
            self.repository, item, "anthropic/test-reviewer", context_lines=1
        )
        model_changed = build_suggestion_context(
            self.repository, item, "anthropic/other-reviewer", context_lines=1
        )
        context_changed = build_suggestion_context(
            self.repository, item, "anthropic/test-reviewer", context_lines=2
        )
        with mock.patch(
            "meeting_memory.knowledge.review_suggestions.SYSTEM_PROMPT",
            "Changed reviewer contract.",
        ):
            prompt_changed = build_suggestion_context(
                self.repository, item, "anthropic/test-reviewer", context_lines=1
            )
        related.explanation = "The duplicate review explanation changed."
        related.path.write_bytes(self.repository.render_review(related))
        related_changed = build_suggestion_context(
            self.repository,
            self.repository.load_review_file(item.path),
            "anthropic/test-reviewer",
            context_lines=1,
        )

        for changed in (
            model_changed,
            context_changed,
            prompt_changed,
            related_changed,
        ):
            self.assertNotEqual(
                before.input_fingerprint, changed.input_fingerprint
            )
        self.assertNotEqual(
            before.related_review_sha256_by_id[related.id],
            related_changed.related_review_sha256_by_id[related.id],
        )

    def test_canonical_only_evidence_digest_is_part_of_request(self):
        existing = self.make_object()
        item = self.make_review(existing)
        item.existing_evidence = []
        item.path.write_bytes(self.repository.render_review(item))
        item = self.repository.load_review_file(item.path)
        before = build_suggestion_context(
            self.repository, item, "anthropic/test-reviewer"
        )

        self.source.write_text(
            "# Update\n\nThe framework is complete.\n\nChanged context.\n",
            encoding="utf-8",
        )
        after = build_suggestion_context(
            self.repository, item, "anthropic/test-reviewer"
        )

        self.assertNotEqual(
            before.evidence_sha256_by_source[self.source_ref],
            after.evidence_sha256_by_source[self.source_ref],
        )
        self.assertNotEqual(before.input_fingerprint, after.input_fingerprint)

    def test_nonsemantic_review_markdown_change_does_not_invalidate_request(self):
        context = self.context()
        path = context.item.path
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n<!-- presentation-only formatting change -->\n",
            encoding="utf-8",
        )
        loaded = self.repository.load_review_file(path)

        after = build_suggestion_context(
            self.repository,
            loaded,
            "anthropic/test-reviewer",
            context_lines=1,
        )

        self.assertEqual(context.review_sha256, after.review_sha256)
        self.assertEqual(context.input_fingerprint, after.input_fingerprint)

    def test_openrouter_advisor_retries_invalid_structured_responses(self):
        context = self.context()

        class Client:
            model = "anthropic/test-reviewer"

            def __init__(self):
                self.calls = 0

            def complete(self, messages, response_format=None):
                self.calls += 1
                if self.calls < 3:
                    return "{}"
                return json.dumps(AIReviewSuggestionTest.replace_response())

        client = Client()
        recommendation = OpenRouterReviewAdvisor(
            self.repository, client
        ).suggest(context)

        self.assertEqual(3, client.calls)
        self.assertEqual("replace", recommendation.suggested_action)

    def test_partial_failure_continues_and_status_is_derived(self):
        existing = self.make_object()
        first = self.make_review(existing)
        second = self.make_review(
            existing,
            identifier="review-framework-second",
            title="Framework second conflict",
        )
        advisor = FakeReviewAdvisor(
            self.repository,
            [self.replace_response(), ExtractionError("provider failed")],
        )

        result = generate_review_suggestions(
            self.repository,
            [first, second],
            advisor,
            "anthropic/test-reviewer",
            now_fn=lambda: FIXED_NOW,
        )

        self.assertEqual("partial_failure", result.manifest["status"])
        self.assertEqual([first.id], list(result.manifest["suggestions_created"]))
        self.assertEqual(second.id, result.manifest["failures"][0]["review_id"])
        validate_review_run_manifest(result.manifest)

    def test_artifact_tampering_fails_repository_validation(self):
        context = self.context()
        recommendation = validate_recommendation(
            self.repository, context, self.replace_response()
        )
        suggestion = create_suggestion(
            self.repository,
            context,
            recommendation,
            now_fn=lambda: FIXED_NOW,
        )
        self.repository.write_suggestion(suggestion)
        path = self.repository.suggestion_path(context.item.id, suggestion.id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["request_payload"]["temperature"] = 0.5
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(SchemaError):
            self.repository.validate_all()

    def test_review_configuration_loads_reviewer_keys(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "meeting-memory.ini"
        path.write_text(
            "[openrouter]\n"
            "review_model = anthropic/reviewer\n"
            "review_critic_model = anthropic/critic\n",
            encoding="utf-8",
        )

        configured = openrouter_configuration(str(path))

        self.assertEqual("anthropic/reviewer", configured["review_model"])
        self.assertEqual(
            "anthropic/critic", configured["review_critic_model"]
        )

    def test_manifest_rejects_overlap_and_untrue_status(self):
        manifest = {
            "schema_version": "1",
            "run_type": "review_suggestions",
            "run_id": "review-run-test",
            "started_at": "2026-07-29T00:00:00Z",
            "completed_at": "2026-07-29T00:01:00Z",
            "status": "success",
            "model": "anthropic/test",
            "prompt_version": "1",
            "filters": {},
            "requested_review_ids": ["review-a"],
            "suggestions_created": {"review-a": "suggestion-a"},
            "suggestions_reused": {"review-a": "suggestion-a"},
            "failures": [],
        }
        with self.assertRaises(SchemaError):
            validate_review_run_manifest(manifest)
        manifest["suggestions_reused"] = {}
        manifest["status"] = "failed"
        with self.assertRaises(SchemaError):
            validate_review_run_manifest(manifest)

    def test_cli_suggest_and_show_do_not_change_review_or_canonical_files(self):
        existing = self.make_object()
        item = self.make_review(existing)
        before_object = existing.path.read_bytes()
        before_review = item.path.read_bytes()
        stdout = StringIO()
        argv = [
            "--meetings-dir",
            str(self.repository.meetings_dir),
            "--output-dir",
            str(self.repository.root),
            "review",
            "suggest",
            item.id,
            "--model",
            "anthropic/test-reviewer",
            "--json",
        ]
        with mock.patch.dict(
            "os.environ", {"OPENROUTER_API_KEY": "test-key"}, clear=False
        ), mock.patch(
            "meeting_memory.knowledge.openrouter.OpenRouterChatClient.complete",
            return_value=json.dumps(self.replace_response()),
        ), redirect_stdout(stdout):
            code = main(argv)

        self.assertEqual(0, code)
        self.assertEqual(before_object, existing.path.read_bytes())
        self.assertEqual(before_review, item.path.read_bytes())
        run = json.loads(stdout.getvalue())
        suggestion_id = run["suggestions_created"][item.id]

        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "--meetings-dir",
                    str(self.repository.meetings_dir),
                    "--output-dir",
                    str(self.repository.root),
                    "review",
                    "show",
                    item.id,
                    "--suggestion-id",
                    suggestion_id,
                    "--json",
                ]
            )
        shown = json.loads(stdout.getvalue())
        self.assertEqual(0, code)
        self.assertEqual(suggestion_id, shown["suggestion"]["id"])
        self.assertTrue(shown["suggestion"]["current"])


if __name__ == "__main__":
    unittest.main()
