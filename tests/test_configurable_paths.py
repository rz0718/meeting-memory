import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from meeting_memory.knowledge.cli import (
    _ask_api_key,
    _ask_model,
    _openrouter_configuration,
    _processing_pipeline,
    _repository_paths,
)
from meeting_memory.knowledge.errors import EvidenceError
from meeting_memory.knowledge.extractors import FakeExtractor
from meeting_memory.knowledge.pipeline import KnowledgePipeline
from meeting_memory.knowledge.repository import KnowledgeRepository


class ConfigurablePathsTest(unittest.TestCase):
    def test_openrouter_configuration_is_loaded_from_ini(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "meeting-memory.ini"
            config.write_text(
                "[paths]\nmeetings_dir = notes\noutput_dir = data\n"
                "[openrouter]\napi_key = local-key\nmodel = provider/main\n"
                "ask_model = provider/ask\n",
                encoding="utf-8",
            )
            configured = _openrouter_configuration(str(config))
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual("local-key", _ask_api_key(configured))
                self.assertEqual("provider/ask", _ask_model(None, configured))

    def test_environment_and_explicit_ask_model_override_ini(self):
        configured = {
            "api_key": "ini-key",
            "model": "provider/main",
            "ask_model": "provider/ask",
        }
        environment = {
            "OPENROUTER_API_KEY": "env-key",
            "DAILY_KNOWLEDGE_ASK_MODEL": "provider/env-ask",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual("env-key", _ask_api_key(configured))
            self.assertEqual("provider/env-ask", _ask_model(None, configured))
            self.assertEqual("provider/explicit", _ask_model("provider/explicit", configured))

    def test_processing_pipeline_uses_ini_openrouter_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = KnowledgeRepository(
                Path(temporary) / "output", Path(temporary) / "meetings"
            )
            with patch.dict(os.environ, {}, clear=True):
                pipeline = _processing_pipeline(
                    repository,
                    {"api_key": "local-key", "model": "provider/extractor"},
                )
            self.assertEqual("local-key", pipeline.extractor.api_key)
            self.assertEqual("provider/extractor", pipeline.extractor.model)

    def test_ini_paths_are_resolved_relative_to_the_config_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "meeting-memory.ini"
            config.write_text(
                "[paths]\nmeetings_dir = notes\noutput_dir = data\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                output, meetings = _repository_paths(None, None, None, str(config))
            self.assertEqual((base / "data").resolve(), output)
            self.assertEqual((base / "notes").resolve(), meetings)

    def test_cli_paths_override_environment_and_ini(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = base / "meeting-memory.ini"
            config.write_text(
                "[paths]\nmeetings_dir = config-notes\noutput_dir = config-data\n",
                encoding="utf-8",
            )
            environment = {
                "MEETING_MEMORY_MEETINGS_DIR": str(base / "env-notes"),
                "MEETING_MEMORY_OUTPUT_DIR": str(base / "env-data"),
            }
            with patch.dict(os.environ, environment, clear=True):
                output, meetings = _repository_paths(
                    None, str(base / "cli-notes"), str(base / "cli-data"), str(config)
                )
            self.assertEqual(base / "cli-data", output)
            self.assertEqual(base / "cli-notes", meetings)

    def test_external_meetings_and_output_are_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            meetings = base / "company-notes"
            output = base / "memory-data"
            note = meetings / "2026-07-21" / "planning.md"
            note.parent.mkdir(parents=True)
            note.write_text("# Planning\n\nA durable note.\n", encoding="utf-8")

            repository = KnowledgeRepository(output, meetings_dir=meetings)
            sources = repository.qualifying_sources("2026-07-21")

            self.assertEqual(["meetings/2026-07-21/planning.md"], [s.relative_path for s in sources])
            self.assertEqual(note.resolve(), repository.evidence_path(sources[0].relative_path))

            result = KnowledgePipeline(repository, FakeExtractor([])).process_dates(
                ["2026-07-21"]
            )
            self.assertEqual("success", result.manifest["status"])
            self.assertTrue((output / ".knowledge-state" / "sources").is_dir())
            self.assertTrue(
                (output / "outputs" / "Durable-Knowledge" / "durable-knowledge-2026-07-21.md").is_file()
            )
            self.assertEqual("# Planning\n\nA durable note.\n", note.read_text(encoding="utf-8"))

    def test_evidence_cannot_escape_configured_meetings_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = KnowledgeRepository(base / "output", base / "meetings")
            with self.assertRaises(EvidenceError):
                repository.evidence_path("meetings/../secret.md")


if __name__ == "__main__":
    unittest.main()
