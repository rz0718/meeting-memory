"""Optional digest-backed machine-readable knowledge index."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence, Tuple

from .constants import CATEGORIES
from .consumption import SearchDocument
from .repository import KnowledgeRepository
from .util import json_bytes, sha256_file


def canonical_fingerprints(
    repository: KnowledgeRepository,
) -> Tuple[Tuple[str, str], ...]:
    values = []
    for category in CATEGORIES:
        directory = repository.knowledge_dir / category
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md"), key=lambda item: item.name):
            values.append((repository._relative(path), sha256_file(path)))
    return tuple(values)


def canonical_digest(repository: KnowledgeRepository) -> str:
    digest = hashlib.sha256()
    for path, fingerprint in canonical_fingerprints(repository):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(fingerprint.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def machine_index_payloads(
    repository: KnowledgeRepository, documents: Sequence[SearchDocument]
) -> dict:
    fingerprints = canonical_fingerprints(repository)
    digest = canonical_digest(repository)
    documents_payload = {
        "schema_version": 1,
        "canonical_digest": digest,
        "documents": [item.to_dict() for item in documents],
    }
    metadata_payload = {
        "schema_version": 1,
        "canonical_digest": digest,
        "document_count": len(documents),
        "canonical_files": [
            {"path": path, "sha256": fingerprint}
            for path, fingerprint in fingerprints
        ],
    }
    return {
        repository.root / ".knowledge-index" / "documents.json": json_bytes(
            documents_payload
        ),
        repository.root / ".knowledge-index" / "metadata.json": json_bytes(
            metadata_payload
        ),
    }


def machine_index_status(repository: KnowledgeRepository) -> str:
    path = repository.root / ".knowledge-index" / "metadata.json"
    if not path.exists():
        return "missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "invalid"
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return "invalid"
    expected = raw.get("canonical_digest")
    if not isinstance(expected, str):
        return "invalid"
    return "current" if expected == canonical_digest(repository) else "stale"


def machine_index_is_current(repository: KnowledgeRepository) -> bool:
    return machine_index_status(repository) == "current"
