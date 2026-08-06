"""Deletion decisions must outlive the run that made them.

Every test here asks the same question in a different way: after a removal or a
merge, does the next extraction that actually reaches the source recreate what
was retired? An ordinary re-run does not, because unchanged sources are
skipped, so these drive the paths that do reach it -- force, a later source, a
changed survivor.
"""

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

from meeting_memory.knowledge.cli import main
from meeting_memory.knowledge.errors import RemovalError, SchemaError
from meeting_memory.knowledge.extractors import FakeExtractor
from meeting_memory.knowledge.merge import KnowledgeMerger
from meeting_memory.knowledge.models import Evidence, KnowledgeCandidate, Tombstone
from meeting_memory.knowledge.pipeline import KnowledgePipeline
from meeting_memory.knowledge.reconcile import KnowledgeReconciler
from meeting_memory.knowledge.removal import KnowledgeRemover
from meeting_memory.knowledge.repository import KnowledgeRepository
from meeting_memory.knowledge.tombstones import (
    backfill_tombstones,
    lift_tombstone,
    resolve_survivor,
)
from meeting_memory.knowledge.util import json_bytes


FIXED_NOW = dt.datetime(2026, 8, 6, 9, 0, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.timezone.utc)


class TombstoneTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.repository = KnowledgeRepository(base / "output", base / "meetings")
        self.repository.ensure_layout()
        self.source = self.write_source(
            "2026-08-01",
            "treasury.md",
            "# Treasury\n\nThe FX excess threshold is +/-$500K.\n",
        )
        self.source_ref = "meetings/2026-08-01/treasury.md"

    def write_source(self, date: str, name: str, text: str) -> Path:
        path = self.repository.meetings_dir / date / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def candidate(self, title, statement, source_ref=None, anchor=None):
        return KnowledgeCandidate(
            category="policies",
            title=title,
            statement=statement,
            status="approved",
            effective_date=None,
            owner=None,
            confidence="high",
            reason_for_durability="Standing treasury threshold.",
            evidence=[
                Evidence(
                    source=source_ref or self.source_ref,
                    source_sha256=None,
                    anchor=anchor or "FX excess threshold",
                    line_start=3,
                    line_end=3,
                    observed_at=None,
                )
            ],
        )

    def run_pipeline(self, candidates, dates, force=False, now=FIXED_NOW):
        return KnowledgePipeline(
            self.repository,
            FakeExtractor(candidates),
            now_fn=lambda: now,
        ).process_dates(dates, force=force)

    def object_ids(self):
        return sorted(item.id for item in self.repository.load_knowledge())


class RemovalTombstoneTest(TombstoneTestCase):
    def create_and_remove(self, title="FX excess threshold"):
        statement = "The FX excess threshold is +/-$500K."
        result = self.run_pipeline([self.candidate(title, statement)], ["2026-08-01"])
        self.assertEqual("success", result.manifest["status"])
        created = self.object_ids()
        self.assertEqual(1, len(created))
        KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            created,
            "Rui",
            "Superseded by the treasury handbook.",
            refresh_indexes=False,
        )
        return created[0], statement

    def test_removal_writes_a_tombstone_carrying_identity(self):
        object_id, statement = self.create_and_remove()

        tombstones = self.repository.load_tombstones()
        self.assertEqual(1, len(tombstones))
        tombstone = tombstones[0]
        self.assertEqual(object_id, tombstone.object_id)
        self.assertEqual("removed", tombstone.kind)
        self.assertEqual("policies", tombstone.category)
        self.assertEqual("FX excess threshold", tombstone.title)
        self.assertEqual(statement, tombstone.statement)
        self.assertIsNone(tombstone.redirect_to)
        self.assertIn("permanent-removal-", tombstone.manifest_path)
        self.repository.validate_all()

    def test_dry_run_removal_writes_no_tombstone(self):
        statement = "The FX excess threshold is +/-$500K."
        self.run_pipeline([self.candidate("FX excess threshold", statement)], ["2026-08-01"])
        result = KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            self.object_ids(), "Rui", "Superseded.", dry_run=True
        )

        self.assertEqual(1, len(result.tombstone_paths))
        self.assertEqual([], self.repository.load_tombstones())

    def test_forced_reprocessing_does_not_resurrect_a_removed_object(self):
        object_id, statement = self.create_and_remove()

        result = self.run_pipeline(
            [self.candidate("FX excess threshold", statement)],
            ["2026-08-01"],
            force=True,
        )

        self.assertEqual([], self.object_ids())
        suppressed = result.manifest["candidates_suppressed"]
        self.assertEqual(1, len(suppressed))
        self.assertEqual(object_id, suppressed[0]["tombstone_id"])
        self.assertEqual(self.source_ref, suppressed[0]["source"])
        self.assertIn("permanently removed on 2026-08-06 by Rui", suppressed[0]["reason"])
        self.assertEqual([], result.manifest["objects_created"])

    def test_a_later_source_restating_the_fact_is_suppressed(self):
        self.create_and_remove()
        later_ref = "meetings/2026-08-19/recap.md"
        self.write_source(
            "2026-08-19",
            "recap.md",
            "# Recap\n\nThe FX excess threshold is +/-$500K.\n",
        )

        result = self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold",
                    "The FX excess threshold is +/-$500K.",
                    source_ref=later_ref,
                )
            ],
            ["2026-08-19"],
            now=LATER,
        )

        self.assertEqual([], self.object_ids())
        self.assertEqual(1, len(result.manifest["candidates_suppressed"]))

    def test_drifted_wording_is_still_suppressed(self):
        """An ID-only tombstone would miss this: the slug no longer matches."""
        object_id, _ = self.create_and_remove()
        later_ref = "meetings/2026-08-19/recap.md"
        self.write_source(
            "2026-08-19",
            "recap.md",
            "# Recap\n\nThe FX excess threshold is +/-$500K.\n",
        )

        drifted = self.candidate(
            "Treasury FX excess threshold",
            "Treasury holds the FX excess threshold at +/-$500K.",
            source_ref=later_ref,
        )
        from meeting_memory.knowledge.reconcile import knowledge_id

        self.assertNotEqual(object_id, knowledge_id(drifted, []))

        result = self.run_pipeline([drifted], ["2026-08-19"], now=LATER)

        self.assertEqual([], self.object_ids())
        self.assertEqual(
            object_id, result.manifest["candidates_suppressed"][0]["tombstone_id"]
        )

    def test_an_unrelated_fact_still_becomes_new_knowledge(self):
        self.create_and_remove()
        later_ref = "meetings/2026-08-19/recap.md"
        self.write_source(
            "2026-08-19",
            "recap.md",
            "# Recap\n\nGold reserves are rebalanced monthly.\n",
        )

        result = self.run_pipeline(
            [
                self.candidate(
                    "Gold rebalancing cadence",
                    "Gold reserves are rebalanced monthly.",
                    source_ref=later_ref,
                    anchor="rebalanced monthly",
                )
            ],
            ["2026-08-19"],
            now=LATER,
        )

        self.assertEqual(1, len(result.manifest["objects_created"]))
        self.assertEqual([], result.manifest["candidates_suppressed"])

    def test_daily_report_states_the_suppression(self):
        _, statement = self.create_and_remove()
        self.run_pipeline(
            [self.candidate("FX excess threshold", statement)],
            ["2026-08-01"],
            force=True,
        )

        report = (
            self.repository.outputs_dir / "durable-knowledge-2026-08-01.md"
        ).read_text(encoding="utf-8")
        self.assertIn("1 candidates suppressed by a retired object", report)


class MergeTombstoneTest(TombstoneTestCase):
    def create_pair(self):
        self.write_source(
            "2026-08-02",
            "metrics.md",
            "# Metrics\n\nThe FX excess threshold is +/-$500K.\n",
        )
        result = self.run_pipeline(
            {
                self.source_ref: [
                    self.candidate(
                        "FX excess threshold",
                        "The FX excess threshold is +/-$500K.",
                    )
                ],
                "meetings/2026-08-02/metrics.md": [
                    self.candidate(
                        "Excess tolerance band",
                        "Desks hold the excess tolerance band at +/-$500K.",
                        source_ref="meetings/2026-08-02/metrics.md",
                        anchor="FX excess threshold",
                    )
                ],
            },
            ["2026-08-01", "2026-08-02"],
        )
        self.assertEqual("success", result.manifest["status"])
        ids = self.object_ids()
        self.assertEqual(2, len(ids))
        return ids

    def merge(self, loser, survivor, now=FIXED_NOW):
        return KnowledgeMerger(self.repository, now_fn=lambda: now).merge(
            loser,
            survivor,
            "Rui",
            "Same standing threshold under two titles.",
            refresh_indexes=False,
        )

    def test_merge_writes_a_redirecting_tombstone(self):
        loser, survivor = self.create_pair()

        result = self.merge(loser, survivor)

        tombstones = self.repository.load_tombstones()
        self.assertEqual(1, len(tombstones))
        self.assertEqual(loser, tombstones[0].object_id)
        self.assertEqual("merged", tombstones[0].kind)
        self.assertEqual(survivor, tombstones[0].redirect_to)
        self.assertEqual(
            self.repository._relative(self.repository.tombstone_path(loser)),
            result.tombstone_path,
        )
        self.repository.validate_all()

    def test_restating_the_loser_reconfirms_the_survivor(self):
        loser, survivor = self.create_pair()
        self.merge(loser, survivor)
        before = self.repository.load_knowledge_file(
            self.repository.knowledge_dir / "policies" / ("%s.md" % survivor)
        )
        later_ref = "meetings/2026-08-19/recap.md"
        self.write_source(
            "2026-08-19",
            "recap.md",
            "# Recap\n\nThe FX excess threshold is +/-$500K.\n",
        )

        result = self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold",
                    "The FX excess threshold is +/-$500K.",
                    source_ref=later_ref,
                )
            ],
            ["2026-08-19"],
            now=LATER,
        )

        self.assertEqual([survivor], self.object_ids())
        self.assertEqual([], result.manifest["objects_created"])
        self.assertEqual([survivor], result.manifest["objects_reconfirmed"])
        after = self.repository.load_knowledge_file(
            self.repository.knowledge_dir / "policies" / ("%s.md" % survivor)
        )
        self.assertEqual(len(before.evidence) + 1, len(after.evidence))

    def test_a_conflicting_restatement_of_the_loser_reaches_review(self):
        loser, survivor = self.create_pair()
        self.merge(loser, survivor)
        later_ref = "meetings/2026-08-19/recap.md"
        self.write_source(
            "2026-08-19",
            "recap.md",
            "# Recap\n\nThe FX excess threshold is +/-$900K.\n",
        )

        result = self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold",
                    "The FX excess threshold is +/-$900K.",
                    source_ref=later_ref,
                )
            ],
            ["2026-08-19"],
            now=LATER,
        )

        self.assertEqual([survivor], self.object_ids())
        self.assertEqual(1, len(result.manifest["review_items_created"]))
        self.assertEqual([], result.manifest["candidates_suppressed"])

    def test_a_merge_chain_resolves_to_the_final_survivor(self):
        loser, survivor = self.create_pair()
        self.merge(loser, survivor)
        self.write_source(
            "2026-08-03",
            "handbook.md",
            "# Handbook\n\nThe FX excess threshold is +/-$500K.\n",
        )
        self.run_pipeline(
            {
                "meetings/2026-08-03/handbook.md": [
                    self.candidate(
                        "Handbook excess rule",
                        "The handbook records the excess rule at +/-$500K.",
                        source_ref="meetings/2026-08-03/handbook.md",
                        anchor="FX excess threshold",
                    )
                ]
            },
            ["2026-08-03"],
        )
        final = next(value for value in self.object_ids() if value != survivor)
        self.merge(survivor, final)

        by_id = {value.object_id: value for value in self.repository.load_tombstones()}
        self.assertEqual((final, "merged"), resolve_survivor(loser, by_id))
        self.repository.validate_all()

    def test_a_removed_survivor_degrades_the_chain_to_suppression(self):
        loser, survivor = self.create_pair()
        self.merge(loser, survivor)
        KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            [survivor], "Rui", "Threshold retired entirely.", refresh_indexes=False
        )
        later_ref = "meetings/2026-08-19/recap.md"
        self.write_source(
            "2026-08-19",
            "recap.md",
            "# Recap\n\nThe FX excess threshold is +/-$500K.\n",
        )

        result = self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold",
                    "The FX excess threshold is +/-$500K.",
                    source_ref=later_ref,
                )
            ],
            ["2026-08-19"],
            now=LATER,
        )

        self.assertEqual([], self.object_ids())
        self.assertEqual(1, len(result.manifest["candidates_suppressed"]))
        self.repository.validate_all()


class TombstoneMatchingTest(TombstoneTestCase):
    def tombstone(self, object_id, title, kind="removed", redirect_to=None):
        return Tombstone.from_dict(
            {
                "object_id": object_id,
                "kind": kind,
                "redirect_to": redirect_to,
                "category": "policies",
                "title": title,
                "statement": "A retired statement.",
                "created_at": "2026-08-06T09:00:00Z",
                "reviewer": "Rui",
                "note": "Retired.",
                "manifest_path": None,
            }
        )

    def test_two_matching_tombstones_reach_a_human(self):
        # Neither ID is the generated base, so identity rests on the titles
        # alone and both claim the candidate equally.
        tombstones = [
            self.tombstone("policy-fx-excess-threshold-a", "FX excess threshold"),
            self.tombstone("policy-fx-excess-threshold-b", "FX excess threshold"),
        ]

        decision = KnowledgeReconciler().reconcile(
            self.candidate("FX excess threshold", "A statement."), [], tombstones
        )

        self.assertEqual("needs_review", decision.outcome)
        self.assertIn("more than one retired object", decision.reason)

    def test_an_exact_id_match_settles_an_otherwise_ambiguous_title(self):
        tombstones = [
            self.tombstone("policy-fx-excess-threshold", "FX excess threshold"),
            self.tombstone("policy-fx-excess-threshold-b", "FX excess threshold"),
        ]

        decision = KnowledgeReconciler().reconcile(
            self.candidate("FX excess threshold", "A statement."), [], tombstones
        )

        self.assertEqual("suppressed", decision.outcome)
        self.assertEqual("policy-fx-excess-threshold", decision.tombstone_id)

    def test_a_tombstone_in_another_category_does_not_match(self):
        tombstone = self.tombstone("policy-fx-excess-threshold", "FX excess threshold")
        tombstone.category = "metrics"

        decision = KnowledgeReconciler().reconcile(
            self.candidate("FX excess threshold", "A statement."), [], [tombstone]
        )

        self.assertEqual("new", decision.outcome)

    def test_a_weak_title_overlap_does_not_suppress(self):
        tombstones = [self.tombstone("policy-gold-cadence", "Gold rebalancing cadence")]

        decision = KnowledgeReconciler().reconcile(
            self.candidate("FX excess threshold", "A statement."), [], tombstones
        )

        self.assertEqual("new", decision.outcome)


class TombstoneValidationTest(TombstoneTestCase):
    def write_tombstone(self, raw):
        path = self.repository.tombstone_path(raw["object_id"])
        path.write_bytes(json_bytes(raw))
        return path

    def base(self, object_id, kind="removed", redirect_to=None):
        return {
            "object_id": object_id,
            "kind": kind,
            "redirect_to": redirect_to,
            "category": "policies",
            "title": object_id,
            "statement": "A retired statement.",
            "created_at": "2026-08-06T09:00:00Z",
            "reviewer": "Rui",
            "note": "Retired.",
            "manifest_path": None,
        }

    def test_a_tombstoned_object_may_not_also_be_live(self):
        self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
        )
        live = self.object_ids()[0]
        self.write_tombstone(self.base(live))

        with self.assertRaises(SchemaError) as caught:
            self.repository.validate_all()
        self.assertIn("also live", str(caught.exception))

    def test_a_dangling_redirect_is_rejected(self):
        self.write_tombstone(
            self.base("policy-a", kind="merged", redirect_to="policy-missing")
        )

        with self.assertRaises(SchemaError) as caught:
            self.repository.validate_all()
        self.assertIn("redirects to a missing object", str(caught.exception))

    def test_a_redirect_cycle_is_rejected(self):
        self.write_tombstone(self.base("policy-a", kind="merged", redirect_to="policy-b"))
        self.write_tombstone(self.base("policy-b", kind="merged", redirect_to="policy-a"))

        with self.assertRaises(SchemaError) as caught:
            self.repository.validate_all()
        self.assertIn("cycles", str(caught.exception))

    def test_a_removed_tombstone_may_not_redirect(self):
        with self.assertRaises(SchemaError):
            Tombstone.from_dict(self.base("policy-a", redirect_to="policy-b"))

    def test_a_merged_tombstone_requires_a_redirect(self):
        with self.assertRaises(SchemaError):
            Tombstone.from_dict(self.base("policy-a", kind="merged"))

    def test_a_run_manifest_without_suppressions_still_validates(self):
        """Manifests written before tombstones existed must keep validating."""
        self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
        )
        path = next((self.repository.state_dir / "runs").glob("*.json"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["candidates_suppressed"]
        path.write_bytes(json_bytes(raw))

        self.repository.validate_all()


class TombstoneLiftTest(TombstoneTestCase):
    def create_and_remove(self):
        statement = "The FX excess threshold is +/-$500K."
        self.run_pipeline(
            [self.candidate("FX excess threshold", statement)], ["2026-08-01"]
        )
        object_id = self.object_ids()[0]
        KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            [object_id], "Rui", "Superseded.", refresh_indexes=False
        )
        return object_id, statement

    def test_lifting_allows_the_fact_to_return(self):
        object_id, statement = self.create_and_remove()

        result = lift_tombstone(
            self.repository,
            object_id,
            "Rui",
            "Reversed: the threshold is still in force.",
            now_fn=lambda: LATER,
        )

        self.assertEqual([], self.repository.load_tombstones())
        manifest = json.loads(
            (self.repository.root / result.manifest_path).read_text(encoding="utf-8")
        )
        self.assertEqual("tombstone_lift", manifest["operation"])
        self.assertEqual(object_id, manifest["lifted"]["object_id"])

        run = self.run_pipeline(
            [self.candidate("FX excess threshold", statement)],
            ["2026-08-01"],
            force=True,
            now=LATER,
        )
        self.assertEqual([object_id], run.manifest["objects_created"])
        self.assertEqual([], run.manifest["candidates_suppressed"])

    def test_lifting_does_not_restore_content(self):
        object_id, _ = self.create_and_remove()

        lift_tombstone(
            self.repository, object_id, "Rui", "Reversed.", now_fn=lambda: LATER
        )

        self.assertEqual([], self.object_ids())

    def test_dry_run_lift_changes_nothing(self):
        object_id, _ = self.create_and_remove()

        result = lift_tombstone(
            self.repository,
            object_id,
            "Rui",
            "Reversed.",
            dry_run=True,
            now_fn=lambda: LATER,
        )

        self.assertTrue(result.dry_run)
        self.assertEqual(1, len(self.repository.load_tombstones()))

    def test_lifting_is_refused_while_a_chain_runs_through_it(self):
        self.write_source(
            "2026-08-02",
            "metrics.md",
            "# Metrics\n\nThe FX excess threshold is +/-$500K.\n",
        )
        self.run_pipeline(
            {
                self.source_ref: [
                    self.candidate(
                        "FX excess threshold",
                        "The FX excess threshold is +/-$500K.",
                    )
                ],
                "meetings/2026-08-02/metrics.md": [
                    self.candidate(
                        "Excess tolerance band",
                        "Desks hold the excess tolerance band at +/-$500K.",
                        source_ref="meetings/2026-08-02/metrics.md",
                        anchor="FX excess threshold",
                    )
                ],
            },
            ["2026-08-01", "2026-08-02"],
        )
        loser, survivor = self.object_ids()
        KnowledgeMerger(self.repository, now_fn=lambda: FIXED_NOW).merge(
            loser, survivor, "Rui", "Duplicate.", refresh_indexes=False
        )
        KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            [survivor], "Rui", "Retired.", refresh_indexes=False
        )

        with self.assertRaises(RemovalError) as caught:
            lift_tombstone(
                self.repository, survivor, "Rui", "Reversed.", now_fn=lambda: LATER
            )
        self.assertIn(loser, str(caught.exception))
        self.assertEqual(2, len(self.repository.load_tombstones()))

    def test_lifting_an_unknown_id_is_refused(self):
        with self.assertRaises(RemovalError):
            lift_tombstone(
                self.repository, "policy-missing", "Rui", "Reversed.", now_fn=lambda: LATER
            )

    def test_lifting_requires_a_reviewer_and_note(self):
        object_id, _ = self.create_and_remove()
        with self.assertRaises(RemovalError):
            lift_tombstone(self.repository, object_id, "  ", "Reversed.")
        with self.assertRaises(RemovalError):
            lift_tombstone(self.repository, object_id, "Rui", "  ")


class TombstoneBackfillTest(TombstoneTestCase):
    def remove_and_forget(self):
        """Remove an object, then delete its tombstone to model the old world."""
        self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
        )
        object_id = self.object_ids()[0]
        KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            [object_id], "Rui", "Superseded.", refresh_indexes=False
        )
        self.repository.tombstone_path(object_id).unlink()
        return object_id

    def test_backfill_rebuilds_a_removal_tombstone(self):
        object_id = self.remove_and_forget()

        result = backfill_tombstones(self.repository)

        self.assertEqual((object_id,), result.created)
        tombstones = self.repository.load_tombstones()
        self.assertEqual(1, len(tombstones))
        self.assertEqual("removed", tombstones[0].kind)
        self.assertEqual("FX excess threshold", tombstones[0].title)
        self.assertEqual("Rui", tombstones[0].reviewer)
        self.repository.validate_all()

    def test_backfilled_tombstone_suppresses_a_restatement(self):
        self.remove_and_forget()
        backfill_tombstones(self.repository)

        result = self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
            force=True,
        )

        self.assertEqual([], self.object_ids())
        self.assertEqual(1, len(result.manifest["candidates_suppressed"]))

    def test_backfill_skips_an_id_that_is_live_again(self):
        object_id = self.remove_and_forget()
        self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
            force=True,
        )
        self.assertEqual([object_id], self.object_ids())

        result = backfill_tombstones(self.repository)

        self.assertEqual((), result.created)
        self.assertEqual((object_id,), result.skipped_live)
        self.assertEqual([], self.repository.load_tombstones())

    def test_backfill_is_idempotent(self):
        self.remove_and_forget()
        backfill_tombstones(self.repository)

        result = backfill_tombstones(self.repository)

        self.assertEqual((), result.created)
        self.assertEqual(1, len(result.skipped_existing))
        self.assertEqual(1, len(self.repository.load_tombstones()))

    def test_backfill_does_not_reinstate_a_lifted_tombstone(self):
        """The removal manifest outlives the lift, so a naive rebuild reverses it."""
        object_id = self.remove_and_forget()
        backfill_tombstones(self.repository)
        lift_tombstone(
            self.repository, object_id, "Rui", "Reversed.", now_fn=lambda: LATER
        )

        result = backfill_tombstones(self.repository)

        self.assertEqual((), result.created)
        self.assertEqual((object_id,), result.skipped_lifted)
        self.assertEqual([], self.repository.load_tombstones())

    def test_a_removal_after_a_lift_is_still_backfilled(self):
        object_id = self.remove_and_forget()
        backfill_tombstones(self.repository)
        lift_tombstone(
            self.repository, object_id, "Rui", "Reversed.", now_fn=lambda: LATER
        )
        self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
            force=True,
            now=LATER,
        )
        later_removal = LATER + dt.timedelta(days=1)
        KnowledgeRemover(self.repository, now_fn=lambda: later_removal).remove(
            [object_id], "Rui", "Retired again.", refresh_indexes=False
        )
        self.repository.tombstone_path(object_id).unlink()

        result = backfill_tombstones(self.repository)

        self.assertEqual((object_id,), result.created)
        self.assertEqual((), result.skipped_lifted)

    def test_dry_run_backfill_writes_nothing(self):
        object_id = self.remove_and_forget()

        result = backfill_tombstones(self.repository, dry_run=True)

        self.assertEqual((object_id,), result.created)
        self.assertEqual([], self.repository.load_tombstones())


class TombstoneCliTest(TombstoneTestCase):
    def cli(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "--output-dir",
                    str(self.repository.root),
                    "--meetings-dir",
                    str(self.repository.meetings_dir),
                    *arguments,
                ]
            )
        return code, output.getvalue()

    def create_and_remove(self):
        self.run_pipeline(
            [
                self.candidate(
                    "FX excess threshold", "The FX excess threshold is +/-$500K."
                )
            ],
            ["2026-08-01"],
        )
        object_id = self.object_ids()[0]
        KnowledgeRemover(self.repository, now_fn=lambda: FIXED_NOW).remove(
            [object_id], "Rui", "Superseded.", refresh_indexes=False
        )
        return object_id

    def test_list_reports_retired_objects(self):
        object_id = self.create_and_remove()

        code, output = self.cli("tombstone", "list", "--json")

        self.assertEqual(0, code)
        payload = json.loads(output)
        self.assertEqual([object_id], [item["object_id"] for item in payload])
        self.assertEqual("removed", payload[0]["kind"])

    def test_list_filters_by_kind(self):
        self.create_and_remove()

        code, output = self.cli("tombstone", "list", "--kind", "merged", "--json")

        self.assertEqual(0, code)
        self.assertEqual([], json.loads(output))

    def test_lift_removes_the_record(self):
        object_id = self.create_and_remove()

        code, output = self.cli(
            "tombstone", "lift", object_id, "--reviewer", "Rui", "--note", "Reversed.", "--json"
        )

        self.assertEqual(0, code)
        self.assertEqual(object_id, json.loads(output)["object_id"])
        self.assertEqual([], self.repository.load_tombstones())

    def test_backfill_rebuilds_from_cleanup_manifests(self):
        object_id = self.create_and_remove()
        self.repository.tombstone_path(object_id).unlink()

        code, output = self.cli("tombstone", "backfill", "--json")

        self.assertEqual(0, code)
        self.assertEqual([object_id], json.loads(output)["created"])
        self.assertEqual(1, len(self.repository.load_tombstones()))

    def test_lift_dry_run_keeps_the_record(self):
        object_id = self.create_and_remove()

        code, _ = self.cli(
            "tombstone",
            "lift",
            object_id,
            "--reviewer",
            "Rui",
            "--note",
            "Reversed.",
            "--dry-run",
            "--json",
        )

        self.assertEqual(0, code)
        self.assertEqual(1, len(self.repository.load_tombstones()))


if __name__ == "__main__":
    unittest.main()
