import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.consumption import load_documents
from meeting_memory.knowledge.errors import EvidenceError
from meeting_memory.knowledge.models import Evidence, KnowledgeObject
from meeting_memory.knowledge.repository import KnowledgeRepository


class DetachedKnowledgeTest(unittest.TestCase):
    def make_repository(self, require_evidence_sources=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        repository = KnowledgeRepository(
            base / "output",
            meetings_dir=base / "missing-meetings",
            require_evidence_sources=require_evidence_sources,
        )
        path = repository.knowledge_dir / "policies" / "policy-test.md"
        path.parent.mkdir(parents=True)
        obj = KnowledgeObject(
            id="policy-test",
            title="Test policy",
            category="policies",
            status="approved",
            effective_date="2026-07-22",
            last_confirmed="2026-07-22",
            owner="Treasury",
            confidence="high",
            created_at="2026-07-22T00:00:00Z",
            updated_at="2026-07-22T00:00:00Z",
            evidence=[
                Evidence(
                    source="meetings/2026-07-22/test.md",
                    source_sha256="0" * 64,
                    anchor="Decision",
                    line_start=1,
                    line_end=1,
                    observed_at="2026-07-22",
                )
            ],
            related_objects=[],
            statement="The test policy is active.",
        )
        path.write_bytes(repository.render_knowledge(obj))
        return repository

    def test_strict_mode_requires_evidence_files(self):
        repository = self.make_repository(require_evidence_sources=True)

        with self.assertRaises(EvidenceError):
            load_documents(repository)

    def test_detached_mode_validates_metadata_without_evidence_files(self):
        repository = self.make_repository(require_evidence_sources=False)

        documents = load_documents(repository)
        counts = repository.validate_all()

        self.assertEqual(["policy-test"], [item.id for item in documents])
        self.assertEqual(1, counts["knowledge_objects"])

    def test_cli_allow_missing_evidence_validates_detached_mirror(self):
        repository = self.make_repository(require_evidence_sources=False)

        result = main(
            [
                "--output-dir",
                str(repository.root),
                "--meetings-dir",
                str(repository.meetings_dir),
                "--allow-missing-evidence",
                "validate",
            ]
        )

        self.assertEqual(0, result)

    def test_cli_rejects_detached_mode_for_extraction(self):
        repository = self.make_repository(require_evidence_sources=False)

        result = main(
            [
                "--output-dir",
                str(repository.root),
                "--meetings-dir",
                str(repository.meetings_dir),
                "--allow-missing-evidence",
                "process-pending",
            ]
        )

        self.assertEqual(2, result)


if __name__ == "__main__":
    unittest.main()
