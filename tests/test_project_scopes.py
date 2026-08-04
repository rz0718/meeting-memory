import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meeting_memory.knowledge.answers import AnswerValidationError, _validate_answer
from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.consumption import EvidenceReference, SearchDocument
from meeting_memory.knowledge.context import (
    ContextPacket,
    SelectedDocument,
    build_context_packet,
    connected_reviews,
    select_context_documents,
)
from meeting_memory.knowledge.models import Evidence, KnowledgeObject, ReviewItem
from meeting_memory.knowledge.projects import (
    ProjectScope,
    load_project,
    load_projects,
    save_project,
    scope_documents,
)
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.search import search_documents
from meeting_memory.knowledge.util import sha256_file


NOW = dt.datetime(2026, 8, 4, tzinfo=dt.timezone.utc)


def evidence(source):
    return EvidenceReference(
        source=source,
        source_sha256="0" * 64,
        anchor="Decision",
        line_start=1,
        line_end=1,
        observed_at=dt.date(2026, 8, 4),
    )


def document(
    identifier,
    title,
    sources,
    statement="The policy is active.",
    confirmed=dt.date(2026, 8, 4),
    related=(),
):
    return SearchDocument(
        id=identifier,
        title=title,
        category="policies",
        status="approved",
        statement=statement,
        owner="Treasury",
        confidence="high",
        effective_date=confirmed,
        last_confirmed=confirmed,
        created_at=NOW,
        updated_at=NOW,
        related_objects=tuple(related),
        evidence=tuple(evidence(source) for source in sources),
        history_text="",
        manual_notes="",
        file_path="knowledge/policies/%s.md" % identifier,
    )


def review(identifier, sources, possible=()):
    return ReviewItem(
        id=identifier,
        created_at="2026-08-04T00:00:00Z",
        status="pending",
        reason="ambiguous_match",
        candidate_category="policies",
        possible_existing_ids=list(possible),
        sources=list(sources),
        title="Policy review",
        existing_statement=None,
        candidate_statement="The policy candidate is active.",
        explanation="Needs review.",
    )


class ProjectScopeTest(unittest.TestCase):
    def setUp(self):
        self.scope = ProjectScope(
            name="CCI",
            meeting_names=("CCI",),
            slack_names=("C123",),
        )

    def test_meeting_names_are_fuzzy_and_slack_names_are_exact(self):
        self.assertTrue(
            self.scope.matches_source("meetings/2026-08-01/CCI Weekly Planning.md")
        )
        self.assertTrue(
            self.scope.matches_source("meetings/2026-08-02/vendor-cci-review.md")
        )
        self.assertFalse(
            self.scope.matches_source("meetings/2026-08-02/vendor-review.md")
        )
        self.assertTrue(
            self.scope.matches_source("meetings/2026-08-02/slack-c123.md")
        )
        self.assertFalse(
            self.scope.matches_source("meetings/2026-08-02/slack-c123-updates.md")
        )

    def test_scope_prunes_evidence_on_straddling_objects(self):
        in_scope = "meetings/2026-08-01/CCI Weekly.md"
        outside = "meetings/2026-08-01/Other Weekly.md"
        documents = (
            document("policy-cci", "CCI policy", (in_scope, outside)),
            document("policy-other", "Other policy", (outside,)),
        )

        scoped = scope_documents(self.scope, documents)

        self.assertEqual(("policy-cci",), tuple(item.id for item in scoped))
        self.assertEqual((in_scope,), scoped[0].evidence_sources)

    def test_one_hop_relation_cannot_enter_from_outside_scope(self):
        in_scope = document(
            "policy-cci",
            "CCI policy",
            ("meetings/2026-08-01/CCI Weekly.md",),
            related=("policy-other",),
        )
        outside = document(
            "policy-other",
            "Unrelated title",
            ("meetings/2026-08-01/Other Weekly.md",),
            statement="Unrelated statement.",
        )

        selected = select_context_documents(
            scope_documents(self.scope, (in_scope, outside)), "CCI policy", 8
        )

        self.assertEqual(
            ("policy-cci",), tuple(item.document.id for item in selected)
        )

    def test_connected_reviews_are_filtered_and_their_sources_pruned(self):
        selected = (
            SelectedDocument(
                document=document(
                    "policy-cci",
                    "CCI policy",
                    ("meetings/2026-08-01/CCI Weekly.md",),
                ),
                selection_reason="test",
                score=1,
                matched_fields=("title",),
                evidence_limit=1,
            ),
        )
        in_scope = "meetings/2026-08-01/CCI Weekly.md"
        outside = "meetings/2026-08-01/Other Weekly.md"
        reviews = (
            review("review-in", (in_scope, outside), possible=("policy-cci",)),
            review("review-out", (outside,), possible=("policy-cci",)),
        )

        connected = connected_reviews(
            reviews, selected, "policy", source_predicate=self.scope.matches_source
        )

        self.assertEqual(("review-in",), tuple(item.id for item in connected))
        self.assertEqual([in_scope], connected[0].sources)

    def test_context_builder_threads_the_scope_into_pending_reviews(self):
        in_scope = "meetings/2026-08-01/CCI Weekly.md"
        outside = "meetings/2026-08-01/Other Weekly.md"
        repository = mock.Mock()
        repository.load_reviews.return_value = [
            review("review-out", (outside,), possible=("policy-cci",))
        ]
        documents = (
            document("policy-cci", "CCI policy", (in_scope,)),
        )

        packet = build_context_packet(
            repository,
            documents,
            "CCI policy",
            source_predicate=self.scope.matches_source,
        )

        self.assertEqual((), packet.reviews)

    def test_scoped_search_uses_project_relative_recency_baseline(self):
        old_date = dt.date(2024, 1, 1)
        old = document(
            "policy-cci",
            "Shared policy",
            ("meetings/2024-01-01/CCI Planning.md",),
            confirmed=old_date,
        )
        newer_outside = document(
            "policy-other",
            "Shared policy elsewhere",
            ("meetings/2026-08-01/Other Planning.md",),
            confirmed=dt.date(2026, 8, 1),
        )

        unscoped = next(
            item for item in search_documents((old, newer_outside), "shared policy")
            if item.document.id == old.id
        )
        scoped = search_documents(
            scope_documents(self.scope, (old, newer_outside)), "shared policy"
        )[0]

        self.assertNotIn("recent confirmation (+3)", unscoped.reasons)
        self.assertIn("recent confirmation (+3)", scoped.reasons)
        self.assertEqual(unscoped.score + 3, scoped.score)

    def test_answer_validation_only_allows_pruned_evidence(self):
        scoped_document = scope_documents(
            self.scope,
            (
                document(
                    "policy-cci",
                    "CCI policy",
                    (
                        "meetings/2026-08-01/CCI Weekly.md",
                        "meetings/2026-08-01/Other Weekly.md",
                    ),
                ),
            ),
        )[0]
        packet = ContextPacket(
            query="policy",
            selected=(
                SelectedDocument(
                    document=scoped_document,
                    selection_reason="test",
                    score=1,
                    matched_fields=("title",),
                    evidence_limit=1,
                ),
            ),
            reviews=(),
            omissions=(),
            markdown="context",
        )
        raw = {
            "answer": "Answer",
            "confidence": "high",
            "knowledge_objects_used": ["policy-cci"],
            "meeting_evidence_used": ["meetings/2026-08-01/Other Weekly.md"],
            "open_conflicts": [],
        }

        with self.assertRaises(AnswerValidationError):
            _validate_answer(packet, raw, "test-model")


class ProjectPersistenceAndCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(
            base / "output", meetings_dir=base / "meetings"
        )
        self.repository.ensure_layout()

    def test_json_save_load_and_replace(self):
        scope = ProjectScope("CCI", ("CCI",), ("C123",))
        path = save_project(self.repository, scope)

        self.assertEqual(".knowledge-state/projects/cci.json", path)
        self.assertEqual((scope,), load_projects(self.repository))
        self.assertEqual(scope, load_project(self.repository, "cci"))
        with self.assertRaises(ValueError):
            save_project(self.repository, scope)

        updated = ProjectScope("CCI", ("CCI", "Custody"), ("C123",))
        save_project(self.repository, updated, replace_existing=True)
        self.assertEqual(updated, load_project(self.repository, "CCI"))

    def _write_object(self, identifier, title, source_name, statement):
        source_path = self.repository.meetings_dir / "2026-08-04" / source_name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("Decision\n", encoding="utf-8")
        source = self.repository.source_reference(source_path)
        obj = KnowledgeObject(
            id=identifier,
            title=title,
            category="policies",
            status="approved",
            effective_date="2026-08-04",
            last_confirmed="2026-08-04",
            owner="Treasury",
            confidence="high",
            created_at="2026-08-04T00:00:00Z",
            updated_at="2026-08-04T00:00:00Z",
            evidence=[
                Evidence(
                    source=source,
                    source_sha256=sha256_file(source_path),
                    anchor="Decision",
                    line_start=1,
                    line_end=1,
                    observed_at="2026-08-04",
                )
            ],
            related_objects=[],
            statement=statement,
        )
        path = self.repository.knowledge_dir / "policies" / (identifier + ".md")
        path.write_bytes(self.repository.render_knowledge(obj))

    def test_cli_create_list_search_context_and_empty_ask(self):
        self._write_object(
            "policy-cci", "CCI policy", "CCI Weekly.md", "Shared policy for CCI."
        )
        self._write_object(
            "policy-other",
            "Other policy",
            "Other Weekly.md",
            "Shared policy with other-only-term.",
        )
        common = [
            "--output-dir",
            str(self.repository.root),
            "--meetings-dir",
            str(self.repository.meetings_dir),
        ]

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                common + ["project", "create", "CCI", "--meeting-name", "CCI"]
            )
        self.assertEqual(0, result)
        self.assertTrue(
            (self.repository.state_dir / "projects" / "cci.json").is_file()
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(common + ["project", "list", "--json"])
        self.assertEqual(0, result)
        self.assertIn('"name": "CCI"', output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(common + ["search", "shared policy", "--project", "CCI"])
        self.assertEqual(0, result)
        self.assertIn("CCI policy", output.getvalue())
        self.assertNotIn("Other policy", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                common
                + [
                    "context",
                    "shared policy",
                    "--project",
                    "CCI",
                    "--no-review-items",
                    "--output",
                    "scoped-context.md",
                ]
            )
        self.assertEqual(0, result)
        self.assertIn("CCI policy", output.getvalue())
        self.assertNotIn("Other policy", output.getvalue())

        output = io.StringIO()
        with mock.patch(
            "meeting_memory.knowledge.cli.OpenRouterChatClient",
            side_effect=AssertionError("empty scoped ask must not call the model"),
        ), contextlib.redirect_stdout(output):
            result = main(
                common + ["ask", "other-only-term", "--project", "CCI"]
            )
        self.assertEqual(0, result)
        self.assertIn("insufficient", output.getvalue().casefold())


if __name__ == "__main__":
    unittest.main()
