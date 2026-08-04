"""Session state shared by every UI route.

Holds the repository, the reviewer identity shown in the header, the review
model configuration used for suggestion generation, and the session removal
basket. Nothing here decides anything: the basket is an inventory the human is
assembling, and it is only ever executed through ``KnowledgeRemover``.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..knowledge.answers import OpenRouterAnswerer
from ..knowledge.errors import ConfigurationError, RemovalError, ReviewResolutionError
from ..knowledge.openrouter import OpenRouterChatClient
from ..knowledge.repository import KnowledgeRepository
from ..knowledge.review_ai import OpenRouterReviewAdvisor
from ..knowledge.util import atomic_write, run_id, sha256_bytes, utc_now


DEFAULT_REVIEWER = "rui"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


@dataclass
class RemovalBasket:
    """Object IDs a human has flagged, plus the inventory they last previewed.

    Removal is deliberately not one-click. Assembling the basket changes
    nothing; previewing writes the exact newline-delimited inventory the CLI's
    ``--object-id-file`` would read and reports its SHA-256 and count; applying
    re-reads that same file and asserts both, so the bytes that were approved
    are the bytes that execute.
    """

    object_ids: List[str] = field(default_factory=list)
    inventory_path: Optional[Path] = None
    inventory_sha256: Optional[str] = None
    inventory_count: Optional[int] = None

    def clear_preview(self) -> None:
        self.inventory_path = None
        self.inventory_sha256 = None
        self.inventory_count = None


class UiService:
    """Everything a route needs that is not derived from the request."""

    # Answers a human might still choose to save. Session-only: an answer is a
    # reading of the repository at one instant, not a record of it.
    ANSWER_HISTORY = 20

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        reviewer: str = DEFAULT_REVIEWER,
        review_model: Optional[str] = None,
        ask_model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
        context_lines: int = 5,
        advisor_factory: Optional[Any] = None,
        answerer_factory: Optional[Any] = None,
    ):
        self.repository = repository
        self._reviewer = (reviewer or DEFAULT_REVIEWER).strip() or DEFAULT_REVIEWER
        self.review_model = review_model
        self.ask_model = ask_model
        self.api_key = api_key
        self.timeout = timeout
        self.context_lines = context_lines
        self.advisor_factory = advisor_factory
        self.answerer_factory = answerer_factory
        self.basket = RemovalBasket()
        self._lock = threading.Lock()
        self._previews: Dict[str, str] = {}
        self._answers: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    # -- dry-run ledger -----------------------------------------------------

    @staticmethod
    def decision_fingerprint(arguments: Dict[str, Any]) -> str:
        """Identify a decision by everything except whether it was a dry run."""
        value = {
            key: item for key, item in arguments.items() if key != "dry_run"
        }
        return sha256_bytes(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        )

    def record_preview(self, key: str, arguments: Dict[str, Any]) -> str:
        fingerprint = self.decision_fingerprint(arguments)
        with self._lock:
            self._previews[key] = fingerprint
        return fingerprint

    def require_preview(self, key: str, arguments: Dict[str, Any]) -> None:
        """Refuse an apply whose arguments were never previewed.

        The dry run is the whole gate: an apply that differs from the inspected
        preview by even one field is a different decision, and the human has not
        seen its consequences.
        """
        fingerprint = self.decision_fingerprint(arguments)
        with self._lock:
            recorded = self._previews.get(key)
        if recorded != fingerprint:
            raise ReviewResolutionError(
                "preview this exact decision before applying it"
            )

    def clear_preview(self, key: str) -> None:
        with self._lock:
            self._previews.pop(key, None)

    def read_cache(self):
        """Memoize corpus reads for one read-only request.

        Read routes ask the repository for the same corpus several times over --
        once to render, then again inside each suggestion-currency check. Only
        read routes use this; every write path re-reads under the mutation lock,
        which suspends the cache.
        """
        return self.repository.read_cache()

    # -- reviewer -----------------------------------------------------------

    @property
    def reviewer(self) -> str:
        return self._reviewer

    def set_reviewer(self, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("reviewer may not be empty")
        self._reviewer = value
        return self._reviewer

    # -- suggestion advisor -------------------------------------------------

    def require_model(self) -> str:
        if not self.review_model:
            raise ConfigurationError(
                "review model is not configured; start the UI with --model, or set "
                "MEETING_MEMORY_REVIEW_MODEL, review_model, or ask_model"
            )
        return self.review_model

    def advisor(self) -> Any:
        """A fresh advisor per batch.

        Long provider calls happen outside the repository mutation lock, exactly
        as they do in the CLI; only the artifact write takes the lock. The
        factory seam exists so a test can supply a deterministic advisor without
        the routes knowing which provider is in play.
        """
        if self.advisor_factory is not None:
            return self.advisor_factory(self)
        return OpenRouterReviewAdvisor(
            self.repository,
            api_key=self.api_key,
            model=self.require_model(),
            timeout=self.timeout,
        )

    # -- grounded answers ---------------------------------------------------

    def record_answer(self, result: Dict[str, Any]) -> str:
        """Remember an answer exactly as it was returned to the client.

        Saving must write the answer the human read, not a fresh one: a second
        provider call on the same question is a different sample and could say
        something else. The client saves by token, so the file and the screen
        cannot disagree.
        """
        token = sha256_bytes(
            json.dumps(result, sort_keys=True, default=str).encode("utf-8")
        )
        with self._lock:
            self._answers[token] = result
            for stale in list(self._answers)[: -self.ANSWER_HISTORY]:
                self._answers.pop(stale, None)
        return token

    def recorded_answer(self, token: str) -> Dict[str, Any]:
        with self._lock:
            result = self._answers.get(token)
        if result is None:
            raise ValueError(
                "this answer is no longer in the session; ask the question again"
            )
        return result

    def require_ask_model(self) -> str:
        if not self.ask_model:
            raise ConfigurationError(
                "ask model is not configured; start the UI with --ask-model, or set "
                "DAILY_KNOWLEDGE_ASK_MODEL, OPENROUTER_MODEL, or ask_model"
            )
        return self.ask_model

    def answerer(self) -> Any:
        """A fresh answerer per question.

        Retrieval is deterministic and holds no lock; the provider call happens
        outside the repository read cache, exactly as it does in the CLI. The
        factory seam lets a test supply a deterministic answerer without the
        route knowing which provider is in play.
        """
        if self.answerer_factory is not None:
            return self.answerer_factory(self)
        return OpenRouterAnswerer(
            OpenRouterChatClient(
                self.api_key, self.require_ask_model(), timeout=self.timeout
            )
        )

    # -- removal basket -----------------------------------------------------

    def basket_add(self, object_id: str) -> Tuple[str, ...]:
        object_id = (object_id or "").strip()
        if not object_id:
            raise ValueError("object ID may not be empty")
        known = {value.id for value in self.repository.load_knowledge()}
        if object_id not in known:
            raise RemovalError("knowledge object not found: %s" % object_id)
        with self._lock:
            if object_id not in self.basket.object_ids:
                self.basket.object_ids.append(object_id)
                self.basket.clear_preview()
            return tuple(self.basket.object_ids)

    def basket_remove(self, object_id: str) -> Tuple[str, ...]:
        with self._lock:
            if object_id in self.basket.object_ids:
                self.basket.object_ids.remove(object_id)
                self.basket.clear_preview()
            return tuple(self.basket.object_ids)

    def basket_clear(self) -> None:
        with self._lock:
            self.basket.object_ids = []
            self.basket.clear_preview()

    def basket_inventory_bytes(self) -> bytes:
        return ("\n".join(self.basket.object_ids) + "\n").encode("utf-8")

    def write_basket_inventory(self) -> Dict[str, Any]:
        """Persist the approved ID inventory and record what apply must assert."""
        if not self.basket.object_ids:
            raise RemovalError("the removal basket is empty")
        data = self.basket_inventory_bytes()
        path = (
            self.repository.logs_dir
            / "removal-inventories"
            / ("removal-%s.txt" % run_id(utc_now()))
        )
        atomic_write(path, data)
        with self._lock:
            self.basket.inventory_path = path
            self.basket.inventory_sha256 = sha256_bytes(data)
            self.basket.inventory_count = len(self.basket.object_ids)
        return self.basket_state()

    def read_previewed_inventory(
        self, confirm_count: int, inventory_sha256: str
    ) -> Tuple[List[str], str]:
        """Re-read the previewed inventory under the caller's assertions.

        This is the CLI's ``--object-id-file`` / ``--confirm-count`` /
        ``--inventory-sha256`` contract, byte for byte: the file on disk is the
        authority, and a mismatch refuses rather than falling back to whatever
        the basket happens to hold now.
        """
        path = self.basket.inventory_path
        if path is None:
            raise RemovalError("preview the removal basket before applying it")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RemovalError(
                "approved object inventory cannot be read: %s" % exc
            ) from exc
        digest = sha256_bytes(data)
        if digest != inventory_sha256:
            raise RemovalError(
                "object inventory SHA-256 does not match the approved inventory"
            )
        object_ids = [
            line.strip() for line in data.decode("utf-8").splitlines() if line.strip()
        ]
        if len(object_ids) != len(set(object_ids)):
            raise RemovalError("object inventory contains duplicate IDs")
        if len(object_ids) != confirm_count:
            raise RemovalError(
                "object inventory contains %d IDs, expected %d"
                % (len(object_ids), confirm_count)
            )
        return object_ids, digest

    def basket_state(self) -> Dict[str, Any]:
        by_id = {value.id: value for value in self.repository.load_knowledge()}
        pending = self.basket_inventory_bytes() if self.basket.object_ids else b""
        return {
            "object_ids": list(self.basket.object_ids),
            "count": len(self.basket.object_ids),
            "pending_sha256": sha256_bytes(pending) if pending else None,
            "inventory_path": (
                self.repository._relative(self.basket.inventory_path)
                if self.basket.inventory_path is not None
                else None
            ),
            "inventory_sha256": self.basket.inventory_sha256,
            "inventory_count": self.basket.inventory_count,
            "inventory_matches_basket": (
                self.basket.inventory_sha256 is not None
                and pending != b""
                and sha256_bytes(pending) == self.basket.inventory_sha256
            ),
            "objects": [
                {
                    "id": object_id,
                    "title": by_id[object_id].title if object_id in by_id else None,
                    "category": (
                        by_id[object_id].category if object_id in by_id else None
                    ),
                    "present": object_id in by_id,
                }
                for object_id in self.basket.object_ids
            ],
        }
