"""Versioned constants for the Version 1 durable-knowledge contract."""

SCHEMA_VERSION = "1"
EXTRACTOR_VERSION = "1"

CATEGORIES = (
    "decisions",
    "policies",
    "processes",
    "projects",
    "systems",
    "metrics",
    "people-and-ownership",
)

STATUSES = ("proposed", "approved", "unclear", "deprecated")
CONFIDENCES = ("high", "medium", "low")
OUTCOMES = (
    "new",
    "duplicate",
    "reconfirmation",
    "refinement",
    "conflict",
    "insufficient_evidence",
    "needs_review",
)
RUN_STATUSES = ("success", "partial_failure", "failed")

CATEGORY_PREFIX = {
    "decisions": "decision",
    "policies": "policy",
    "processes": "process",
    "projects": "project",
    "systems": "system",
    "metrics": "metric",
    "people-and-ownership": "ownership",
}

GENERATED_BEGIN = "<!-- BEGIN GENERATED STATEMENT -->"
GENERATED_END = "<!-- END GENERATED STATEMENT -->"
MANUAL_BEGIN = "<!-- BEGIN MANUAL NOTES -->"
MANUAL_END = "<!-- END MANUAL NOTES -->"

