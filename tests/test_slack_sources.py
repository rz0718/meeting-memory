import datetime as dt
import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.configuration import slack_configuration
from meeting_memory.knowledge.errors import ConfigurationError, StorageError
from meeting_memory.knowledge.extractors import FakeExtractor
from meeting_memory.knowledge.pipeline import KnowledgePipeline
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.slack import SlackCollector


def slack_ts(day, hour, minute=0):
    value = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.timezone.utc)
    return "%.6f" % value.timestamp()


class FakeSlackClient:
    def __init__(self, day):
        self.day = day
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "conversations.history":
            if not params.get("cursor"):
                parent_ts = slack_ts(self.day, 9)
                return {
                    "ok": True,
                    "messages": [
                        {
                            "type": "message",
                            "user": "U1",
                            "text": "We approved the launch policy.",
                            "ts": parent_ts,
                            "thread_ts": parent_ts,
                            "reply_count": 1,
                        }
                    ],
                    "response_metadata": {"next_cursor": "next-page"},
                }
            return {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": "U2",
                        "text": "",
                        "ts": slack_ts(self.day, 11),
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": "Treasury owns the daily check."},
                            }
                        ],
                        "files": [{"title": "Runbook", "permalink": "https://slack.example/runbook"}],
                        "reactions": [{"name": "white_check_mark", "count": 2}],
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            }
        if method == "conversations.replies":
            parent_ts = slack_ts(self.day, 9)
            return {
                "ok": True,
                "messages": [
                    {
                        "type": "message",
                        "user": "U1",
                        "text": "We approved the launch policy.",
                        "ts": parent_ts,
                        "thread_ts": parent_ts,
                        "reply_count": 1,
                    },
                    {
                        "type": "message",
                        "user": "U2",
                        "text": "Effective next Monday.",
                        "ts": slack_ts(self.day, 10),
                        "thread_ts": parent_ts,
                    },
                ],
                "response_metadata": {"next_cursor": ""},
            }
        if method == "users.info":
            user_id = params["user"]
            return {
                "ok": True,
                "user": {"id": user_id, "profile": {"display_name": "Alice" if user_id == "U1" else "Bob"}},
            }
        raise AssertionError("unexpected Slack method %s" % method)


class SlackConfigurationTest(unittest.TestCase):
    def test_channel_ids_are_configurable_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "meeting-memory.ini"
            path.write_text(
                "[paths]\nmeetings_dir = notes\noutput_dir = data\n"
                "[slack]\nbot_token = local-token\nchannel_ids = C123, G456\n  C123\n",
                encoding="utf-8",
            )
            configured = slack_configuration(str(path))
            self.assertEqual(["C123", "G456"], configured["channel_ids"])
            self.assertEqual("local-token", configured["bot_token"])

    def test_invalid_channel_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "meeting-memory.ini"
            path.write_text("[slack]\nchannel_ids = C123;rm\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                slack_configuration(str(path))


class SlackCollectorTest(unittest.TestCase):
    def test_collects_paginated_messages_threads_and_files_as_a_source(self):
        day = dt.date(2026, 7, 21)
        with tempfile.TemporaryDirectory() as temporary:
            meetings = Path(temporary) / "meetings"
            client = FakeSlackClient(day)
            collector = SlackCollector(meetings, ["C123"], client=client)

            result = collector.sync(day, day + dt.timedelta(days=1))

            self.assertEqual(3, result.messages)
            self.assertEqual(["2026-07-21/slack-c123.md"], result.files_changed)
            note = meetings / "2026-07-21" / "slack-c123.md"
            text = note.read_text(encoding="utf-8")
            self.assertIn("source_type: slack", text)
            self.assertIn("We approved the launch policy.", text)
            self.assertIn("Effective next Monday.", text)
            self.assertIn("Treasury owns the daily check.", text)
            self.assertIn("File: Runbook (https://slack.example/runbook)", text)
            self.assertIn("Reactions: :white_check_mark: ×2", text)
            self.assertIn("Alice (U1)", text)
            self.assertIn("Thread parent:", text)

            repository = KnowledgeRepository(Path(temporary) / "output", meetings)
            sources = repository.qualifying_sources("2026-07-21")
            self.assertEqual(["meetings/2026-07-21/slack-c123.md"], [item.relative_path for item in sources])

            meet_note = meetings / "2026-07-21" / "gmeet-planning.md"
            meet_note.write_text("# Google Meet planning\n\nA durable meeting decision.\n", encoding="utf-8")
            extractor = FakeExtractor([])
            pipeline_result = KnowledgePipeline(repository, extractor).process_dates(["2026-07-21"])
            self.assertEqual("success", pipeline_result.manifest["status"])
            self.assertEqual(
                [
                    "meetings/2026-07-21/gmeet-planning.md",
                    "meetings/2026-07-21/slack-c123.md",
                ],
                extractor.calls,
            )

            unchanged = collector.sync(day, day + dt.timedelta(days=1))
            self.assertEqual([], unchanged.files_changed)
            self.assertEqual(["2026-07-21/slack-c123.md"], unchanged.files_unchanged)

    def test_empty_channel_day_is_written_but_opted_out_of_extraction(self):
        class EmptyClient:
            def call(self, method, params):
                return {"ok": True, "messages": [], "response_metadata": {"next_cursor": ""}}

        day = dt.date(2026, 7, 21)
        with tempfile.TemporaryDirectory() as temporary:
            meetings = Path(temporary) / "meetings"
            SlackCollector(meetings, ["C123"], client=EmptyClient()).sync(
                day, day + dt.timedelta(days=1)
            )
            note = meetings / "2026-07-21" / "slack-c123.md"
            self.assertIn("durable_knowledge: false", note.read_text(encoding="utf-8"))
            repository = KnowledgeRepository(Path(temporary) / "output", meetings)
            self.assertEqual([], repository.qualifying_sources("2026-07-21"))

    def test_dry_run_does_not_write(self):
        day = dt.date(2026, 7, 21)
        with tempfile.TemporaryDirectory() as temporary:
            meetings = Path(temporary) / "meetings"
            result = SlackCollector(meetings, ["C123"], client=FakeSlackClient(day)).sync(
                day, day + dt.timedelta(days=1), dry_run=True
            )
            self.assertEqual(["2026-07-21/slack-c123.md"], result.files_changed)
            self.assertFalse(meetings.exists())

    def test_refuses_to_overwrite_a_human_note_at_reserved_path(self):
        day = dt.date(2026, 7, 21)
        with tempfile.TemporaryDirectory() as temporary:
            meetings = Path(temporary) / "meetings"
            note = meetings / "2026-07-21" / "slack-c123.md"
            note.parent.mkdir(parents=True)
            note.write_text("# Human-authored note\n", encoding="utf-8")
            with self.assertRaises(StorageError):
                SlackCollector(meetings, ["C123"], client=FakeSlackClient(day)).sync(
                    day, day + dt.timedelta(days=1)
                )


if __name__ == "__main__":
    unittest.main()
