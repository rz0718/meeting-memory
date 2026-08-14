import json
import unittest
from pathlib import Path

from meeting_memory.knowledge.errors import TransientExtractionError
from meeting_memory.knowledge.extractors import OpenRouterExtractor
from meeting_memory.knowledge.models import MeetingSource


CANDIDATE = {
    "category": "decisions",
    "title": "Ship the launch",
    "statement": "The team approved the launch.",
    "status": "approved",
    "effective_date": "2026-08-06",
    "owner": "Rui",
    "confidence": "high",
    "reason_for_durability": "A recorded approval.",
    "evidence": [
        {
            "source": "meetings/2026-08-06/notes.md",
            "anchor": "The team approved the launch.",
            "line_start": 1,
            "line_end": 1,
        }
    ],
}

VALID_RESPONSE = json.dumps({"candidates": [CANDIDATE]})


class FakeChatClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, response_format=None):
        self.calls += 1
        return self.responses.pop(0)


def build_extractor(responses, max_attempts=3):
    extractor = OpenRouterExtractor(
        api_key="test-key", model="test-model", max_attempts=max_attempts
    )
    extractor.client = FakeChatClient(responses)
    return extractor


def build_source():
    return MeetingSource(
        path=Path("meetings/2026-08-06/notes.md"),
        relative_path="meetings/2026-08-06/notes.md",
        source_date="2026-08-06",
        sha256="0" * 64,
        content="The team approved the launch.\n",
    )


class DecodeModelJsonTest(unittest.TestCase):
    def test_accepts_fenced_json(self):
        raw = OpenRouterExtractor._decode_model_json(
            "```json\n{\"candidates\": []}\n```"
        )
        self.assertEqual(raw, {"candidates": []})

    def test_accepts_prose_wrapped_json(self):
        raw = OpenRouterExtractor._decode_model_json(
            'Here is the result:\n{"candidates": []}\nLet me know.'
        )
        self.assertEqual(raw, {"candidates": []})

    def test_ignores_extra_top_level_keys(self):
        raw = OpenRouterExtractor._decode_model_json(
            '{"candidates": [], "notes": "none found"}'
        )
        self.assertEqual(raw["candidates"], [])

    def test_malformed_json_is_retryable(self):
        with self.assertRaises(TransientExtractionError) as caught:
            OpenRouterExtractor._decode_model_json('{"candidates": [')
        self.assertIn("was not valid JSON", str(caught.exception))

    def test_missing_candidates_array_is_retryable(self):
        with self.assertRaises(TransientExtractionError):
            OpenRouterExtractor._decode_model_json('{"items": []}')

    def test_error_includes_a_bounded_response_preview(self):
        with self.assertRaises(TransientExtractionError) as caught:
            OpenRouterExtractor._decode_model_json("not json " * 200)
        message = str(caught.exception)
        self.assertIn("response began: not json", message)
        self.assertLess(len(message), 400)


class ExtractionPromptTest(unittest.TestCase):
    def test_omits_inline_image_data_without_changing_line_numbers(self):
        source = MeetingSource(
            path=Path("meetings/2026-08-06/notes.md"),
            relative_path="meetings/2026-08-06/notes.md",
            source_date="2026-08-06",
            sha256="0" * 64,
            content=(
                "The team approved the launch.\n"
                "[image1]: <data:image/png;base64,aGVsbG8=>\n"
                "Rui owns the rollout.\n"
            ),
        )

        prompt = OpenRouterExtractor._prompt(source)

        self.assertIn("1: The team approved the launch.", prompt)
        self.assertIn(
            "2: [image1]: <[inline image data omitted from extraction]>", prompt
        )
        self.assertIn("3: Rui owns the rollout.", prompt)
        self.assertNotIn("aGVsbG8=", prompt)
        self.assertIn("aGVsbG8=", source.content)


class ExtractRetryTest(unittest.TestCase):
    def test_retries_a_malformed_response(self):
        extractor = build_extractor(["oops, thinking out loud", VALID_RESPONSE])
        candidates = extractor.extract(build_source())
        self.assertEqual(extractor.client.calls, 2)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "Ship the launch")

    def test_raises_retryable_error_after_exhausting_attempts(self):
        extractor = build_extractor(["bad", "worse", "worst"])
        with self.assertRaises(TransientExtractionError):
            extractor.extract(build_source())
        self.assertEqual(extractor.client.calls, 3)

    def test_single_attempt_does_not_retry(self):
        extractor = build_extractor(["bad"], max_attempts=1)
        with self.assertRaises(TransientExtractionError):
            extractor.extract(build_source())
        self.assertEqual(extractor.client.calls, 1)

    def test_rejects_a_non_positive_attempt_budget(self):
        with self.assertRaises(ValueError):
            OpenRouterExtractor(api_key="k", model="m", max_attempts=0)


if __name__ == "__main__":
    unittest.main()
