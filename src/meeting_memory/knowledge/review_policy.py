"""Deterministic policy facts attached to advisory review suggestions."""

from __future__ import annotations

from typing import Any, Dict

from .models import ReviewItem


def advisory_automatic_eligibility(
    item: ReviewItem, recommendation: Any
) -> Dict[str, Any]:
    """Explain eligibility without enabling Phase 4 automatic resolution.

    Phase 1 deliberately has no independent critic or apply command.  Keeping
    this result explicit prevents a model's confidence from being mistaken for
    authority to mutate canonical knowledge.
    """
    reasons = ["automatic resolution is disabled in advisory phase 1"]
    if recommendation.suggested_action is None:
        reasons.append("no concrete action was recommended")
    if recommendation.requires_human:
        reasons.append("the recommendation requires human review")
    return {
        "eligible": False,
        "policy": "conservative-v1",
        "reasons": reasons,
    }
