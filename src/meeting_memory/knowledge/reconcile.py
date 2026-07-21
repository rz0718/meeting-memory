"""Conservative deterministic Version 1 reconciliation."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Optional, Tuple

from .constants import CATEGORY_PREFIX
from .models import KnowledgeCandidate, KnowledgeObject, ReconciliationDecision
from .util import jaccard, normalize_text, slugify, token_set


def knowledge_id(candidate: KnowledgeCandidate, existing: Iterable[KnowledgeObject]) -> str:
    base = "%s-%s" % (CATEGORY_PREFIX[candidate.category], slugify(candidate.title))
    by_id = {item.id: item for item in existing}
    if base not in by_id:
        return base
    matched = by_id[base]
    if normalize_text(matched.title) == normalize_text(candidate.title):
        return base
    suffix = hashlib.sha256(
        ("%s\0%s" % (candidate.category, normalize_text(candidate.title))).encode("utf-8")
    ).hexdigest()[:8]
    return ("%s-%s" % (base[:63].rstrip("-"), suffix)).rstrip("-")


def _numbers(value: str) -> set:
    return set(re.findall(r"\b\d+(?:\.\d+)?%?\b", value.lower()))


def _negated(value: str) -> bool:
    words = token_set(value)
    return bool(words & {"not", "never", "no", "cannot", "mustnt", "prohibited"})


class KnowledgeReconciler:
    """Uses stable identity, normalized text, and bounded token similarity."""

    uncertain_threshold = 0.60
    likely_match_threshold = 0.85

    def _find_match(
        self,
        candidate: KnowledgeCandidate,
        existing_objects: List[KnowledgeObject],
    ) -> Tuple[Optional[KnowledgeObject], bool]:
        by_id = {item.id: item for item in existing_objects}
        generated = knowledge_id(candidate, existing_objects)
        if generated in by_id:
            return by_id[generated], False

        normalized_title = normalize_text(candidate.title)
        exact = [
            item
            for item in existing_objects
            if item.category == candidate.category and normalize_text(item.title) == normalized_title
        ]
        if len(exact) == 1:
            return exact[0], False
        if len(exact) > 1:
            return None, True

        if candidate.existing_object_id:
            suggested = by_id.get(candidate.existing_object_id)
            if suggested is None or suggested.category != candidate.category:
                return None, True
            if jaccard(suggested.title, candidate.title) < self.uncertain_threshold:
                return None, True
            return suggested, False

        similarities = sorted(
            (
                (jaccard(candidate.title, item.title), item)
                for item in existing_objects
                if item.category == candidate.category
            ),
            key=lambda pair: (-pair[0], pair[1].id),
        )
        if not similarities or similarities[0][0] < self.uncertain_threshold:
            return None, False
        best_score, best = similarities[0]
        tied = len(similarities) > 1 and similarities[1][0] == best_score
        if tied or best_score < self.likely_match_threshold:
            return None, True
        return best, False

    @staticmethod
    def _classify_change(
        candidate: KnowledgeCandidate,
        existing: KnowledgeObject,
    ) -> ReconciliationDecision:
        candidate_statement = normalize_text(candidate.statement)
        existing_statement = normalize_text(existing.statement)
        if candidate.status == "deprecated":
            return ReconciliationDecision(
                "needs_review", existing.id, "Version 1 never applies deprecation automatically"
            )
        incompatible_status = (
            candidate.status != existing.status
            and "unclear" not in (candidate.status, existing.status)
        )
        if candidate_statement == existing_statement:
            if incompatible_status:
                return ReconciliationDecision(
                    "conflict", existing.id, "same statement has an incompatible approval status"
                )
            existing_keys = {item.key() for item in existing.evidence}
            has_new_evidence = any(item.key() not in existing_keys for item in candidate.evidence)
            return ReconciliationDecision(
                "reconfirmation" if has_new_evidence else "duplicate",
                existing.id,
                "same normalized statement",
            )

        title_similarity = jaccard(candidate.title, existing.title)
        old_tokens = token_set(existing.statement)
        new_tokens = token_set(candidate.statement)
        overlap = len(old_tokens & new_tokens) / float(len(old_tokens or {""}))
        plausible_refinement = old_tokens.issubset(new_tokens) or overlap >= 0.88

        if candidate.relationship == "refinement" and plausible_refinement:
            return ReconciliationDecision("refinement", existing.id, "validated refinement hint")
        if candidate.relationship == "conflict" and title_similarity >= 0.60:
            return ReconciliationDecision("conflict", existing.id, "validated conflict hint")

        if plausible_refinement and len(new_tokens) > len(old_tokens):
            return ReconciliationDecision("refinement", existing.id, "new statement adds compatible detail")

        changed_numbers = bool(_numbers(candidate.statement) or _numbers(existing.statement)) and (
            _numbers(candidate.statement) != _numbers(existing.statement)
        )
        changed_negation = _negated(candidate.statement) != _negated(existing.statement)
        if changed_numbers or changed_negation or incompatible_status:
            return ReconciliationDecision(
                "conflict", existing.id, "supported statement changes a threshold, polarity, or status"
            )
        return ReconciliationDecision(
            "needs_review", existing.id, "semantic relationship is not safe to determine"
        )

    def reconcile(
        self,
        candidate: KnowledgeCandidate,
        existing_objects: List[KnowledgeObject],
    ) -> ReconciliationDecision:
        if not candidate.evidence:
            return ReconciliationDecision("insufficient_evidence", reason="candidate has no evidence")
        if candidate.status == "deprecated":
            return ReconciliationDecision(
                "needs_review", reason="Version 1 never applies deprecation automatically"
            )
        match, uncertain = self._find_match(candidate, existing_objects)
        if uncertain:
            return ReconciliationDecision("needs_review", reason="matching identity is uncertain")
        if match is None:
            return ReconciliationDecision("new", reason="no conservative match found")
        return self._classify_change(candidate, match)
