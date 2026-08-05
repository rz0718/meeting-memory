"""Read models for project scopes and the source universe behind them.

A project is a saved source filter, not a knowledge type: it names meeting-file
selectors and Slack channels, and retrieval is bounded to the objects whose
evidence comes from those sources. Two things have to be visible for that to be
trustworthy rather than merely narrow.

The first is the **universe**: the scope editor offers the source paths the
index actually cites, so a project cannot be assembled from a file that does not
exist. The second is the **expansion**: meeting selectors match fuzzily by
design (``CCI`` covers every meeting filename containing CCI), so the sources a
selection resolves to are not always the sources that were clicked. Every
resolution below reports both, and marks which admitted sources arrived through
fuzzy matching rather than direct selection.

Nothing in this module writes.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..knowledge.consumption import SearchDocument, normalize_phrase
from ..knowledge.projects import ProjectScope, scope_documents


SLACK_PREFIX = "slack-"

ChannelNames = Optional[Mapping[str, str]]


def _stem(source: str) -> str:
    return PurePosixPath(PurePosixPath(source).name).stem


def channel_label(channel: str, channel_names: ChannelNames = None) -> str:
    """Resolve one Slack channel token to its configured display name.

    The lookup folds case because the two ends disagree on it: configuration
    holds Slack's own uppercase IDs, while the token here was recovered from a
    filename that ``slugify`` lowercased (``slack.py:376``). Without a
    configured name the ID is its own label -- never a guess, and never blank.
    """
    if not channel_names:
        return channel
    folded = channel.casefold()
    for configured, name in channel_names.items():
        if configured.casefold() == folded:
            return name
    return channel


def selector_label(value: str, channel_names: ChannelNames = None) -> str:
    """Label a stored Slack selector, which may be an ID, a stem, or a path.

    A selector that matches no source has no descriptor to borrow a name from,
    and that is exactly the case the UI most needs to name: an unmatched
    selector is reported to its author, who wrote a channel, not a slug.
    """
    stem = _stem(value)
    if stem.casefold().startswith(SLACK_PREFIX):
        stem = stem[len(SLACK_PREFIX) :]
    return channel_label(stem, channel_names)


def source_descriptor(source: str, channel_names: ChannelNames = None) -> Dict[str, Any]:
    """Describe one evidence path as the selector selecting it would produce.

    ``selector`` stays the channel ID even when ``name`` is a human one: the
    stored scope matches by ID, so a display name that leaked into the selector
    would silently stop matching the notes it was named for. ``name`` is for
    reading and ``selector`` is for matching, and Slack is the one kind where
    they differ.
    """
    stem = _stem(source)
    parent = PurePosixPath(source).parent.name
    if stem.casefold().startswith(SLACK_PREFIX):
        channel = stem[len(SLACK_PREFIX) :]
        return {
            "source": source,
            "kind": "slack",
            "date": parent or None,
            "name": channel_label(channel, channel_names),
            "channel_id": channel,
            "selector": channel,
        }
    return {
        "source": source,
        "kind": "meeting",
        "date": parent or None,
        "name": stem,
        "channel_id": None,
        "selector": stem,
    }


def source_universe(
    documents: Sequence[SearchDocument], channel_names: ChannelNames = None
) -> List[Dict[str, Any]]:
    """Every distinct evidence path in the index, newest date first.

    Sources that back no knowledge object are omitted deliberately: they cannot
    change what a scope retrieves, so offering them would invite a project whose
    live count is permanently zero.
    """
    entries: Dict[str, Dict[str, Any]] = {}
    owners: Dict[str, set] = {}
    for document in documents:
        for reference in document.evidence:
            entry = entries.get(reference.source)
            if entry is None:
                entry = source_descriptor(reference.source, channel_names)
                entry.update({"objects": 0, "evidence": 0})
                entries[reference.source] = entry
                owners[reference.source] = set()
            entry["evidence"] += 1
            owners[reference.source].add(document.id)
            entry["objects"] = len(owners[reference.source])
    # Sorting on the display name groups a renamed channel where a reader looks
    # for it, which is the point of showing the name at all.
    return sorted(
        entries.values(),
        key=lambda item: (item["date"] or "", item["kind"], item["name"].casefold()),
        reverse=True,
    )


def _selected_selectors(scope: ProjectScope) -> Tuple[set, set]:
    meetings = {normalize_phrase(_stem(value)) for value in scope.meeting_names}
    slack = {value.casefold() for value in scope.slack_names}
    return meetings, slack


def _is_direct(entry: Dict[str, Any], meetings: set, slack: set) -> bool:
    if entry["kind"] == "slack":
        return entry["selector"].casefold() in slack
    return normalize_phrase(entry["selector"]) in meetings


def _selector_scope(scope: ProjectScope, kind: str, value: str) -> ProjectScope:
    if kind == "slack":
        return ProjectScope(name=scope.name, slack_names=(value,))
    return ProjectScope(name=scope.name, meeting_names=(value,))


def resolve_scope(
    scope: ProjectScope,
    documents: Sequence[SearchDocument],
    scoped: Optional[Sequence[SearchDocument]] = None,
) -> Dict[str, Any]:
    """What this scope actually resolves to against the current index.

    ``scoped`` may be supplied when the caller has already narrowed the corpus,
    so a request that retrieves under a scope does not filter it twice; the
    result is identical either way.
    """
    if scoped is None:
        scoped = scope_documents(scope, documents)
    universe = source_universe(documents)
    meetings, slack = _selected_selectors(scope)

    singles = [
        (field, value, _selector_scope(scope, kind, value))
        for kind, field, values in (
            ("meeting", "meeting_names", scope.meeting_names),
            ("slack", "slack_names", scope.slack_names),
        )
        for value in values
    ]
    sources: List[Dict[str, Any]] = []
    for entry in universe:
        if not scope.matches_source(entry["source"]):
            continue
        admitted = dict(entry)
        admitted["direct"] = _is_direct(entry, meetings, slack)
        # Which selector let it in. The UI has to name it for a source nobody
        # clicked, and deriving that in the client would mean reimplementing
        # the matching rules there.
        admitted["matched_by"] = [
            value
            for _, value, single in singles
            if single.matches_source(entry["source"])
        ]
        sources.append(admitted)

    by_id = {document.id: document for document in scoped}
    evidence_totals = {
        document.id: len(document.evidence)
        for document in documents
        if document.id in by_id
    }
    straddling = [
        {
            "id": document.id,
            "title": document.title,
            "in_scope": len(document.evidence),
            "total": evidence_totals[document.id],
        }
        for document in scoped
        if len(document.evidence) < evidence_totals[document.id]
    ]
    in_scope_evidence = sum(len(document.evidence) for document in scoped)

    return {
        "name": scope.name,
        "slug": scope.slug,
        "meeting_names": list(scope.meeting_names),
        "slack_names": list(scope.slack_names),
        "sources": sources,
        "sources_matched": len(sources),
        "sources_total": len(universe),
        "sources_by_fuzzy_match": sum(
            1 for entry in sources if not entry["direct"]
        ),
        "objects": len(scoped),
        "objects_total": len(documents),
        "object_ids": [document.id for document in scoped],
        "evidence_in_scope": in_scope_evidence,
        # Evidence pruned from objects the scope admitted -- not evidence
        # belonging to objects it excluded outright.
        "evidence_pruned": sum(evidence_totals.values()) - in_scope_evidence,
        "evidence_totals": evidence_totals,
        "straddling": straddling,
        # A selector that matches nothing is not an error -- a project may be
        # written before the meeting it names happens -- but it is silent
        # otherwise, and a scope narrower than its author believes is exactly
        # the failure this feature cannot have.
        "unmatched_selectors": _unmatched_selectors(singles, universe),
    }


def _unmatched_selectors(singles, universe: Sequence[Dict[str, Any]]):
    result: Dict[str, List[str]] = {"meeting_names": [], "slack_names": []}
    for field, value, single in singles:
        if not any(single.matches_source(entry["source"]) for entry in universe):
            result[field].append(value)
    return result


def empty_resolution(documents: Sequence[SearchDocument]) -> Dict[str, Any]:
    """A selection with no selectors yet -- distinct from one that matches none.

    ``ProjectScope`` refuses to exist without a selector, and rightly: a scope
    that selects nothing would be a scope that admits everything the moment a
    caller forgot to check. The editor still needs something to render before
    the first click, so it gets an explicit zero rather than a scope object.
    """
    return {
        "name": "",
        "slug": "",
        "meeting_names": [],
        "slack_names": [],
        "sources": [],
        "sources_matched": 0,
        "sources_total": len(source_universe(documents)),
        "sources_by_fuzzy_match": 0,
        "objects": 0,
        "objects_total": len(documents),
        "object_ids": [],
        "evidence_in_scope": 0,
        "evidence_pruned": 0,
        "evidence_totals": {},
        "straddling": [],
        "unmatched_selectors": {"meeting_names": [], "slack_names": []},
    }


def scope_receipt(
    scope: ProjectScope,
    documents: Sequence[SearchDocument],
    scoped: Sequence[SearchDocument],
) -> Dict[str, Any]:
    """The receipt shown above a scoped answer and saved alongside it.

    A scoped answer and an unscoped one are indistinguishable after the fact
    without this, and a narrow-scope answer read as a global one is worse than
    no answer.
    """
    receipt = resolve_scope(scope, documents, scoped)
    # Pending review items are filtered by the same source predicate the
    # documents were, so a scoped packet carries only in-scope proposals rather
    # than suppressing them wholesale.
    receipt["review_items_scoped"] = True
    return receipt


def projects_payload(
    scopes: Sequence[ProjectScope], documents: Sequence[SearchDocument]
) -> Dict[str, Any]:
    universe = source_universe(documents)
    return {
        "projects": [
            {
                **scope.to_dict(),
                "objects": len(scope_documents(scope, documents)),
                "sources_matched": sum(
                    1 for entry in universe if scope.matches_source(entry["source"])
                ),
            }
            for scope in scopes
        ],
        "universe": {
            "sources": universe,
            "objects_total": len(documents),
            "sources_total": len(universe),
            "meetings": sum(1 for entry in universe if entry["kind"] == "meeting"),
            "slack": sum(1 for entry in universe if entry["kind"] == "slack"),
        },
    }
