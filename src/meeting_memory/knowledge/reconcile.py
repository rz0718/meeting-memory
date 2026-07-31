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


# Units that carry material meaning in a treasury statement. A trailing word
# outside this vocabulary is prose rather than a unit, so "5-category
# framework" and "5 categories" are not read as a unit change.
_UNIT_ALIASES = {
    "%": "%",
    "percent": "%",
    "pct": "%",
    "bps": "bps",
    "kg": "kg",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "t": "t",
    "ton": "t",
    "tons": "t",
    "tonne": "t",
    "tonnes": "t",
    "usd": "usd",
    "idr": "idr",
    "sgd": "sgd",
    "eur": "eur",
    "btc": "btc",
    "eth": "eth",
    "usdt": "usdt",
    "k": "k",
    "m": "m",
    "bn": "bn",
}

_CURRENCY_PREFIX = {"$": "usd", "€": "eur", "£": "gbp"}

# Curated statements are written with a typographic minus while extracted
# candidates use a plain hyphen, so "−5 to +5 kg" and "-5 to +5 kg" must fold
# to the same payload before signs are read.
_DASH_FOLD = {ord(char): "-" for char in "−‒–—―"}

# "+/- $500K" and "±$500K" express one symmetric tolerance, not a signed bound,
# so both fold to a single marker that carries no sign into the payload.
_PLUS_MINUS = re.compile(r"[+-]\s*/\s*[+-]")

# A sign only counts when it opens a token: in "2-3 kg" the hyphen separates a
# range, while in "+2 to -8 kg" both signs are real. The value carries the same
# guard so that digits embedded in an identifier such as "U01UFMJ86SV" are not
# mistaken for a threshold. Sign precedes any currency prefix to read "-$777K".
_MEASUREMENT = re.compile(
    r"(?:(?<![\w)])(?P<sign>[+-])\s*)?"
    r"(?:(?P<prefix>[$€£])\s*)?"
    r"(?<![\w.])(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)"
    r"\s*(?:(?P<percent>%)|(?P<unit>[a-z]{1,9})\b)?"
)


def _normalized_number(sign: str, value: str) -> str:
    text = value.replace(",", "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "-" + text if sign == "-" else text


def _measurements(value: str) -> Tuple[set, set]:
    """The signed numeric payload and the recognized units of a statement.

    Numbers and units are collected separately because a range elides the unit
    on its first bound: "+2 to +8 kg" and "+2 kg to +8 kg" state the same
    range. Comparing units as one statement-level set keeps those two equal
    while still catching a real unit change such as kg to tonnes.
    """
    numbers = set()
    units = set()
    folded = _PLUS_MINUS.sub("±", value.lower().translate(_DASH_FOLD))
    for match in _MEASUREMENT.finditer(folded):
        numbers.add(_normalized_number(match.group("sign") or "", match.group("value")))
        prefix = match.group("prefix")
        if prefix:
            units.add(_CURRENCY_PREFIX[prefix])
        if match.group("percent"):
            units.add("%")
        unit = _UNIT_ALIASES.get(match.group("unit") or "")
        if unit:
            units.add(unit)
    return numbers, units


def _numbers(value: str) -> set:
    return _measurements(value)[0]


def _units(value: str) -> set:
    return _measurements(value)[1]


def _negated(value: str) -> bool:
    words = token_set(value)
    return bool(words & {"not", "never", "no", "cannot", "mustnt", "prohibited"})


def conflicting_measurements(left: str, right: str) -> bool:
    """Whether two statements carry different numbers, signs, or units.

    Mirrors the guard _classify_change uses to stop a reworded threshold from
    silently overwriting a different one, reused here so merging two canonical
    objects cannot silently fold together statements that disagree on their
    numeric payload.
    """
    left_numbers, left_units = _measurements(left)
    right_numbers, right_units = _measurements(right)
    changed_numbers = bool(left_numbers or right_numbers) and left_numbers != right_numbers
    changed_units = bool(left_units or right_units) and left_units != right_units
    return changed_numbers or changed_units


class KnowledgeReconciler:
    """Uses stable identity, normalized text, and bounded token similarity."""

    uncertain_threshold = 0.60
    likely_match_threshold = 0.85
    # A restatement must still read as the same sentence before it can be
    # reconfirmed without a human, even once the material payload has matched.
    reword_threshold = 0.60

    @classmethod
    def _titles_contained(cls, left: str, right: str) -> bool:
        """Whether one title's tokens wholly contain the other's.

        Two guards keep containment from firing on a coincidence: the shorter
        title must carry at least two tokens, so a one-word title cannot be
        swallowed by an unrelated longer one, and the pair must still clear
        uncertain_threshold, which bounds how much longer the containing title
        may be than the title it contains.
        """
        left_tokens, right_tokens = token_set(left), token_set(right)
        if min(len(left_tokens), len(right_tokens)) < 2:
            return False
        if not (left_tokens <= right_tokens or right_tokens <= left_tokens):
            return False
        return jaccard(left, right) >= cls.uncertain_threshold

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

        # One title wholly containing the other is a stronger identity signal
        # than the raw overlap ratio it produces: "Treasury Gold A/L exposure
        # targets" only scores 0.83 against "Gold A/L exposure targets", which
        # sits in the dead band below likely_match_threshold, yet the shorter
        # title names no token the longer one omits. Containment is accepted
        # only when exactly one object qualifies, so a genuine ambiguity still
        # reaches a human.
        contained = [
            item
            for item in existing_objects
            if item.category == candidate.category
            and self._titles_contained(candidate.title, item.title)
        ]
        if len(contained) == 1:
            return contained[0], False
        if len(contained) > 1:
            return None, True

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

    @classmethod
    def _classify_change(
        cls,
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
        candidate_numbers, candidate_units = _measurements(candidate.statement)
        existing_numbers, existing_units = _measurements(existing.statement)
        changed_numbers = bool(candidate_numbers or existing_numbers) and (
            candidate_numbers != existing_numbers
        )
        changed_units = bool(candidate_units or existing_units) and (
            candidate_units != existing_units
        )
        if candidate_statement == existing_statement:
            if incompatible_status:
                return ReconciliationDecision(
                    "conflict", existing.id, "same statement has an incompatible approval status"
                )
            # normalize_text keeps only alphanumeric runs, so a sign or a
            # percent marker is invisible to it: "+2 to +8 kg" and "-2 to -8 kg"
            # normalize identically. Comparing the measurement payload here
            # stops a reversed or rescaled threshold from being waved through as
            # the same statement.
            if changed_numbers or changed_units:
                return ReconciliationDecision(
                    "conflict",
                    existing.id,
                    "same wording carries a different threshold, sign, or unit",
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

        changed_negation = _negated(candidate.statement) != _negated(existing.statement)
        if changed_numbers or changed_units or changed_negation or incompatible_status:
            return ReconciliationDecision(
                "conflict", existing.id, "supported statement changes a threshold, polarity, or status"
            )

        # Nothing material moved: the numeric payload, the units, the polarity,
        # and the approval status all agree, so this is the same fact worded
        # differently -- typically a recurring daily recap restating a standing
        # threshold. Reconfirming attaches the new evidence and refreshes
        # last_confirmed while leaving the curated statement untouched, so no
        # human judgement is spent and no wording is silently overwritten.
        if jaccard(candidate.statement, existing.statement) >= cls.reword_threshold:
            return ReconciliationDecision(
                "reconfirmation", existing.id, "immaterial rewording of the same statement"
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
