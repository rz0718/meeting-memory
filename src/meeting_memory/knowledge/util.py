"""Deterministic text, time, fingerprint, and YAML helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import yaml

from .errors import ConfigurationError, SchemaError, StorageError


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def local_timezone() -> dt.timezone:
    """The team's operating calendar day, as a fixed UTC offset.

    A fixed offset (default UTC+8, Singapore) is used instead of the stdlib
    zoneinfo module: zoneinfo is py3.9+ only and this project supports
    python_requires >= 3.8 with no third-party deps. Singapore itself has had
    no DST since 1982, so a fixed offset is also simply correct, not just a
    workaround. This mirrors, in intent rather than mechanism, the sibling
    ai-meeting-memory project's MEETING_TZ (an IANA zone name via zoneinfo,
    since that project targets a newer Python) -- the two are deliberately
    different config knobs and are not read from the same environment
    variable, so keeping the org's local day in agreement across both
    projects is a manual, cross-repo concern.
    """
    raw = os.environ.get("MEETING_TZ_UTC_OFFSET_HOURS", "8")
    try:
        hours = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "MEETING_TZ_UTC_OFFSET_HOURS must be an integer number of UTC offset "
            "hours (got %r)" % raw
        ) from exc
    try:
        return dt.timezone(dt.timedelta(hours=hours))
    except ValueError as exc:
        raise ConfigurationError(
            "MEETING_TZ_UTC_OFFSET_HOURS must be between -23 and 23 (got %r)" % raw
        ) from exc


def local_today() -> dt.date:
    return dt.datetime.now(local_timezone()).date()


def iso_z(value: Optional[dt.datetime] = None) -> str:
    value = value or utc_now()
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_id(value: Optional[dt.datetime] = None) -> str:
    return (value or utc_now()).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str, max_length: int = 72) -> str:
    normalized = value.lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    filler = {"a", "an", "the", "of", "for", "to"}
    words = [word for word in normalized.split("-") if word and word not in filler]
    result = "-".join(words) or "untitled"
    return result[:max_length].rstrip("-")


def normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def token_set(value: str) -> set:
    return set(normalize_text(value).split())


def jaccard(left: str, right: str) -> float:
    a, b = token_set(left), token_set(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


EVIDENCE_CURRENT = "current"
EVIDENCE_VERIFIED = "verified"
EVIDENCE_DRIFTED = "drifted"

_EMOJI_SHORTCODE = re.compile(r":[a-z0-9_+-]+:")


def anchor_in_text(anchor: str, text: str) -> bool:
    """Whether a recorded evidence anchor still reads inside the given text.

    Anchors are written by the extraction model while it reads a rendered
    source, so they routinely drop Slack markup and emoji shortcodes that the
    raw line still carries, and they spell "P&L" where the raw line holds the
    escaped "P&amp;L". Both sides are unescaped, stripped of shortcodes,
    reduced to space-joined alphanumeric tokens, and compared on whole-token
    boundaries so that "tested" never matches "testing".
    """
    needle = _match_form(anchor)
    if not needle:
        return False
    return " %s " % needle in " %s " % _match_form(text)


def _match_form(value: str) -> str:
    return normalize_text(_EMOJI_SHORTCODE.sub(" ", html.unescape(value)))


def evidence_freshness(data: bytes, lines: Sequence[str], evidence: Any) -> str:
    """Classify a stored evidence locator against the source as it reads now.

    A whole-file digest answers "did any byte of this file change", which is
    far broader than the question that matters for a citation: is the quoted
    claim still at the recorded lines. Synced Slack day-files are rewritten
    whenever any later message, reaction, or edit lands, so digest-only
    checking marks untouched citations stale. Re-reading the anchor keeps the
    guarantee while ignoring churn elsewhere in the file.
    """
    if not evidence.source_sha256 or sha256_bytes(data) == evidence.source_sha256:
        return EVIDENCE_CURRENT
    if evidence.line_end > len(lines):
        return EVIDENCE_DRIFTED
    span = "\n".join(lines[evidence.line_start - 1 : evidence.line_end])
    return EVIDENCE_VERIFIED if anchor_in_text(evidence.anchor, span) else EVIDENCE_DRIFTED


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise SchemaError("Markdown file is missing YAML front matter")
    marker = text.find("\n---", 4)
    if marker < 0:
        raise SchemaError("YAML front matter is not terminated")
    yaml_text = text[4:marker]
    body_start = marker + 4
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SchemaError("invalid YAML front matter: %s" % exc) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SchemaError("YAML front matter must be a mapping")
    return raw, text[body_start:]


def dump_frontmatter(raw: Dict[str, Any]) -> str:
    return yaml.safe_dump(
        raw,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).rstrip()


def json_bytes(raw: Any) -> bytes:
    return (json.dumps(raw, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, str(path))
        temp_name = None
    except OSError as exc:
        raise StorageError("atomic write failed for %s: %s" % (path, exc)) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass

