"""Interactive human review over the existing suggest/show/resolve contracts."""

from __future__ import annotations

import builtins
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from .constants import REVIEW_ACTIONS
from .models import ReviewItem
from .repository import KnowledgeRepository
from .review import ReviewResolver, exact_review, render_review_show
from .review_ai import generate_review_suggestions
from .review_suggestions import (
    latest_current_suggestion,
    render_suggestion,
    suggestion_display_payload,
)


@dataclass(frozen=True)
class TriageResult:
    selected: int
    applied: int
    deferred: int
    declined: int
    failed: int
    quit: bool


class ReviewTriage:
    """Prompt a human, preview with ReviewResolver, then optionally apply."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        advisor: Any,
        model: str,
        *,
        reviewer: str,
        context_lines: int = 5,
        input_fn: Optional[Callable[[str], str]] = None,
        output_fn: Optional[Callable[[str], None]] = None,
    ):
        self.repository = repository
        self.advisor = advisor
        self.model = model
        self.reviewer = reviewer.strip()
        self.context_lines = context_lines
        self.input = input_fn or builtins.input
        self.output = output_fn or print
        if not self.reviewer:
            raise ValueError("reviewer may not be empty")

    def _prompt_choice(self, prompt: str, choices: Sequence[str]) -> str:
        allowed = {value.casefold(): value for value in choices}
        while True:
            value = self.input(prompt).strip().casefold()
            if value in allowed:
                return allowed[value]
            self.output("Choose one of: %s." % ", ".join(choices))

    def _current_suggestion(self, item: ReviewItem):
        suggestion = latest_current_suggestion(self.repository, item)
        if suggestion is not None:
            return suggestion
        result = generate_review_suggestions(
            self.repository,
            (item,),
            self.advisor,
            self.model,
            context_lines=self.context_lines,
            filters={"review_id": item.id, "triage": True},
        )
        suggestion_id = (
            result.manifest["suggestions_created"].get(item.id)
            or result.manifest["suggestions_reused"].get(item.id)
        )
        if suggestion_id is None:
            failure = next(
                (
                    value["error"]
                    for value in result.manifest["failures"]
                    if value["review_id"] == item.id
                ),
                "suggestion generation failed",
            )
            raise ValueError(failure)
        return self.repository.load_suggestion(item.id, suggestion_id)

    def _override_arguments(self, item: ReviewItem, action: str, suggestion: Any):
        recommendation = suggestion.recommendation
        arguments = {}
        if action in ("replace", "refine", "reconfirm"):
            default = recommendation.existing_id
            if default is None and len(item.possible_existing_ids) == 1:
                default = item.possible_existing_ids[0]
            prompt = "Existing object ID%s: " % (
                " [%s]" % default if default else ""
            )
            selected = self.input(prompt).strip() or default
            if selected:
                arguments["existing_id"] = selected
        elif action == "merge-duplicate":
            default = recommendation.duplicate_of
            prompt = "Retained duplicate review ID%s: " % (
                " [%s]" % default if default else ""
            )
            selected = self.input(prompt).strip() or default
            if selected:
                arguments["duplicate_of"] = selected
        elif action == "create-separate":
            default = recommendation.new_id
            prompt = "New object ID (blank for deterministic default)%s: " % (
                " [%s]" % default if default else ""
            )
            selected = self.input(prompt).strip() or default
            if selected:
                arguments["new_id"] = selected
        return arguments

    def run(self, values: Sequence[ReviewItem]) -> TriageResult:
        applied = deferred = declined = failed = 0
        did_quit = False
        resolver = ReviewResolver(self.repository)
        review_ids = [value.id for value in values]
        for position, review_id in enumerate(review_ids, 1):
            try:
                item = exact_review(self.repository, review_id)
                if item.status != "pending":
                    self.output(
                        "[%d/%d] %s is no longer pending; skipped."
                        % (position, len(review_ids), review_id)
                    )
                    continue
                self.output(
                    "\n[%d/%d] %s" % (position, len(review_ids), review_id)
                )
                self.output(
                    render_review_show(
                        self.repository,
                        item,
                        include_excerpts=True,
                    ).rstrip()
                )
                suggestion = self._current_suggestion(item)
                self.output(
                    render_suggestion(
                        suggestion_display_payload(
                            self.repository, suggestion, item
                        )
                    )
                )
                decision = self._prompt_choice(
                    "Decision [accept/override/defer/quit]: ",
                    ("accept", "override", "defer", "quit"),
                )
                if decision == "quit":
                    did_quit = True
                    break
                if decision == "defer":
                    deferred += 1
                    self.output("Deferred %s; no files changed." % review_id)
                    continue

                accept = decision == "accept"
                if accept:
                    action = suggestion.recommendation.suggested_action
                    if action is None:
                        self.output(
                            "This suggestion requires a human-authored action; "
                            "choose override or defer."
                        )
                        decision = self._prompt_choice(
                            "Decision [override/defer/quit]: ",
                            ("override", "defer", "quit"),
                        )
                        if decision == "quit":
                            did_quit = True
                            break
                        if decision == "defer":
                            deferred += 1
                            continue
                        accept = False
                if not accept:
                    action = self._prompt_choice(
                        "Final action [%s]: " % "/".join(REVIEW_ACTIONS),
                        REVIEW_ACTIONS,
                    )
                    arguments = self._override_arguments(
                        item, action, suggestion
                    )
                else:
                    action = None
                    arguments = {}
                note = self.input("Human review note: ").strip()
                preview = resolver.resolve(
                    review_id,
                    action,
                    self.reviewer,
                    note,
                    suggestion_id=suggestion.id,
                    accept_suggestion=accept,
                    dry_run=True,
                    **arguments,
                )
                self.output(
                    "Deterministic dry-run:\n%s"
                    % json.dumps(
                        preview.to_dict(), indent=2, ensure_ascii=False
                    )
                )
                confirmation = self._prompt_choice(
                    "Apply this resolution? [yes/no]: ", ("yes", "no")
                )
                if confirmation == "no":
                    declined += 1
                    self.output("Declined %s; no resolution was written." % review_id)
                    continue
                applied_result = resolver.resolve(
                    review_id,
                    action,
                    self.reviewer,
                    note,
                    suggestion_id=suggestion.id,
                    accept_suggestion=accept,
                    **arguments,
                )
                applied += 1
                self.output(
                    "Applied %s as %s (%s)."
                    % (
                        review_id,
                        applied_result.action,
                        applied_result.destination_status,
                    )
                )
            except (EOFError, KeyboardInterrupt):
                did_quit = True
                self.output("Triage stopped; completed decisions were retained.")
                break
            except Exception as exc:
                failed += 1
                self.output("ERROR: %s: %s" % (review_id, exc))
                continue
        return TriageResult(
            selected=len(review_ids),
            applied=applied,
            deferred=deferred,
            declined=declined,
            failed=failed,
            quit=did_quit,
        )
