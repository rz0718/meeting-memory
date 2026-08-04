"""Command-line interface for daily durable knowledge."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .answers import (
    OpenRouterAnswerer,
    answer_payload,
    insufficient_answer,
    render_answer,
)
from .constants import (
    CATEGORIES,
    CONFIDENCES,
    EXTRACTOR_VERSION,
    RESOLUTION_MODES,
    REVIEW_ACTIONS,
    REVIEW_STATUSES,
    STATUSES,
)
from .configuration import (
    config_path as _config_path,
    openrouter_configuration as _openrouter_configuration,
    path_configuration as _path_configuration,
    slack_configuration as _slack_configuration,
)
from .consumption import load_documents
from .context import (
    InvalidContextBudgetError,
    build_context_packet,
    write_context_packet,
)
from .errors import (
    AuthenticationError,
    ConfigurationError,
    EvidenceError,
    KnowledgeError,
    MergeError,
    RemovalError,
    ReviewNotFoundError,
    ReviewResolutionError,
    SchemaError,
    TransientExtractionError,
)
from .extractors import FakeExtractor, OpenRouterExtractor
from .indexes import generate_indexes
from .merge import KnowledgeMerger
from .removal import KnowledgeRemover
from .openrouter import OpenRouterChatClient
from .pipeline import KnowledgePipeline, PipelineResult
from .presentation import (
    ObjectNotFoundError,
    exact_document,
    render_show,
    show_payload,
)
from .projects import (
    ProjectScope,
    load_project,
    load_projects,
    save_project,
    scope_documents,
)
from .repository import KnowledgeRepository
from .review import (
    ReviewRefresher,
    ReviewResolver,
    exact_review,
    list_reviews,
    render_review_list,
    render_review_show,
    review_payload,
    reviews_payload,
)
from .review_ai import OpenRouterReviewAdvisor, generate_review_suggestions
from .review_suggestions import (
    MAX_CONTEXT_LINES,
    latest_current_suggestion,
    render_suggestion,
    suggestion_display_payload,
)
from .review_triage import ReviewTriage
from .search import (
    SearchFilters,
    render_search_results,
    search_documents,
    search_payload,
)
from .slack import SlackCollector
from .util import atomic_write, local_today, sha256_bytes


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _repository_paths(
    base_dir: Optional[str],
    meetings_dir: Optional[str],
    output_dir: Optional[str],
    config_file: Optional[str] = None,
) -> tuple:
    """Resolve paths using CLI, environment, config, then defaults."""
    configured = _path_configuration(config_file)
    legacy_base = base_dir or os.environ.get("DAILY_KNOWLEDGE_BASE_DIR")
    output = (
        output_dir
        or os.environ.get("MEETING_MEMORY_OUTPUT_DIR")
        or legacy_base
        or configured.get("output_dir")
        or os.getcwd()
    )
    meetings = (
        meetings_dir
        or os.environ.get("MEETING_MEMORY_MEETINGS_DIR")
        or os.environ.get("MEETING_MEMORY_LOGS_DIR")
    )
    if meetings is None:
        meetings = (
            str(Path(legacy_base) / "meetings")
            if legacy_base
            else configured.get("meetings_dir")
        )
    if meetings is None:
        meetings = str(Path(output) / "meetings")
    return Path(output), Path(meetings)


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("may not be negative")
    return parsed


def _context_lines(value: str) -> int:
    parsed = _nonnegative(value)
    if parsed > MAX_CONTEXT_LINES:
        raise argparse.ArgumentTypeError(
            "may not exceed %d" % MAX_CONTEXT_LINES
        )
    return parsed


def _iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD") from exc


def _add_process_options(parser: argparse.ArgumentParser, pending: bool = False) -> None:
    parser.add_argument("--dry-run", action="store_true", help="calculate changes without writing")
    parser.add_argument("--force", action="store_true", help="reprocess unchanged qualifying sources")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("DAILY_KNOWLEDGE_LOOKBACK_DAYS", "30")),
        help="pending-date lookback window (default: 30)",
    )
    if pending:
        parser.add_argument(
            "--include-today",
            action="store_true",
            default=_env_bool("DAILY_KNOWLEDGE_INCLUDE_TODAY"),
            help="include today's meeting directory",
        )
    _add_output(parser)


def _add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=_positive, default=8)
    parser.add_argument("--max-chars", type=_positive, default=30000)
    parser.add_argument("--max-evidence-per-object", type=_nonnegative, default=3)
    parser.add_argument(
        "--include-review-items",
        dest="include_review_items",
        action="store_true",
        default=True,
        help="include connected pending review items (default)",
    )
    parser.add_argument(
        "--no-review-items",
        dest="include_review_items",
        action="store_false",
        help="exclude pending review items",
    )
    parser.add_argument("--include-manual-notes", action="store_true")
    parser.add_argument("--include-evidence-excerpts", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meeting-memory",
        description="Incrementally curate durable knowledge from meeting and Slack sources.",
    )
    parser.add_argument(
        "--config",
        help="INI configuration file (default: MEETING_MEMORY_CONFIG or "
        "meeting-memory.ini in the current/project directory)",
    )
    parser.add_argument(
        "--base-dir",
        help="legacy root containing meetings/ and generated data",
    )
    parser.add_argument(
        "--meetings-dir",
        "--meeting-logs-dir",
        dest="meetings_dir",
        help="meeting-note input directory (env: MEETING_MEMORY_MEETINGS_DIR)",
    )
    parser.add_argument(
        "--output-dir",
        help="directory for knowledge, state, indexes, and reports "
        "(env: MEETING_MEMORY_OUTPUT_DIR)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show tracebacks for maintainers",
    )
    parser.add_argument(
        "--allow-missing-evidence",
        action="store_true",
        help="allow read-only consumption from a detached knowledge mirror",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    process_date = commands.add_parser("process-date", help="process one explicit date")
    process_date.add_argument("date", help="meeting date in YYYY-MM-DD")
    _add_process_options(process_date)

    process_pending = commands.add_parser(
        "process-pending", help="process pending dates oldest first"
    )
    _add_process_options(process_pending, pending=True)

    sync_sources = commands.add_parser(
        "sync-sources", help="collect configured remote sources into dated Markdown"
    )
    sync_range = sync_sources.add_mutually_exclusive_group()
    sync_range.add_argument("--date", type=_iso_date, help="collect one local-calendar date")
    sync_range.add_argument(
        "--lookback-days",
        type=_positive,
        default=1,
        help="number of local-calendar dates ending yesterday (default: 1)",
    )
    sync_sources.add_argument(
        "--include-today",
        action="store_true",
        default=_env_bool("DAILY_KNOWLEDGE_INCLUDE_TODAY"),
        help="make the range end after today instead of before today",
    )
    sync_sources.add_argument("--dry-run", action="store_true", help="fetch and show changes without writing")
    _add_output(sync_sources)

    status = commands.add_parser("status", help="report pending, failed, and curated state")
    status.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("DAILY_KNOWLEDGE_LOOKBACK_DAYS", "30")),
    )
    status.add_argument(
        "--include-today",
        action="store_true",
        default=_env_bool("DAILY_KNOWLEDGE_INCLUDE_TODAY"),
    )
    _add_output(status)

    validate = commands.add_parser("validate", help="validate all durable-knowledge files")
    _add_output(validate)

    index = commands.add_parser("index", help="generate human and machine indexes")
    index.add_argument("--dry-run", action="store_true", help="show changes without writing")
    index.add_argument("--force", action="store_true", help="rewrite generated files")
    index.add_argument(
        "--recent-days", type=_positive, default=30, help="recent-index window (default: 30)"
    )
    _add_output(index)

    merge = commands.add_parser(
        "merge", help="fold one canonical knowledge object into another"
    )
    merge.add_argument("loser_id", help="knowledge object to retire")
    merge.add_argument(
        "--into", dest="survivor_id", required=True, help="knowledge object to keep"
    )
    merge.add_argument("--reviewer", required=True)
    merge.add_argument("--note", required=True)
    merge.add_argument("--statement", help="override the survivor's statement")
    merge.add_argument("--title", help="override the survivor's title")
    merge.add_argument("--status", choices=STATUSES)
    merge.add_argument("--owner")
    merge.add_argument("--clear-owner", action="store_true")
    merge.add_argument("--confidence", choices=CONFIDENCES)
    merge.add_argument("--effective-date", type=_iso_date)
    merge.add_argument("--clear-effective-date", action="store_true")
    merge.add_argument(
        "--allow-cross-category",
        action="store_true",
        help="allow merging objects from different categories",
    )
    merge.add_argument(
        "--allow-conflicting-numbers",
        action="store_true",
        help="allow merging statements whose numbers, signs, or units disagree",
    )
    merge.add_argument("--dry-run", action="store_true")
    merge.add_argument(
        "--no-index", action="store_true", help="do not regenerate browse indexes"
    )
    _add_output(merge)

    remove = commands.add_parser(
        "remove", help="permanently remove canonical objects from an audited ID inventory"
    )
    remove.add_argument(
        "--object-id-file",
        required=True,
        help="newline-delimited canonical object IDs approved for removal",
    )
    remove.add_argument(
        "--confirm-count",
        type=_positive,
        required=True,
        help="refuse removal unless the inventory contains exactly this many IDs",
    )
    remove.add_argument("--reviewer", required=True)
    remove.add_argument("--note", required=True)
    remove.add_argument(
        "--inventory-sha256",
        help="expected SHA-256 of the exact approved object-ID inventory",
    )
    remove.add_argument("--dry-run", action="store_true")
    remove.add_argument(
        "--no-index", action="store_true", help="do not regenerate browse indexes"
    )
    _add_output(remove)

    review = commands.add_parser(
        "review", help="list, inspect, and resolve human-review cases"
    )
    review_commands = review.add_subparsers(dest="review_command", required=True)

    review_list = review_commands.add_parser("list", help="list review cases")
    review_list.add_argument(
        "--status",
        choices=(*REVIEW_STATUSES, "all"),
        default="pending",
        help="review status to list (default: pending)",
    )
    review_list.add_argument(
        "--priority",
        choices=("conflict", "linked", "unlinked", "all"),
        default="all",
        help="review priority bucket",
    )
    review_list.add_argument(
        "--reason", choices=("conflicting_evidence", "ambiguous_match")
    )
    review_list.add_argument("--category", choices=tuple(sorted(CATEGORIES)))
    review_list.add_argument("--existing-id")
    review_list.add_argument("--source", help="case-insensitive source-path substring")
    review_list.add_argument("--limit", type=_positive)
    _add_output(review_list)

    review_show = review_commands.add_parser("show", help="show one exact review case")
    review_show.add_argument("review_id")
    review_show_output = review_show.add_mutually_exclusive_group()
    review_show_output.add_argument(
        "--raw", action="store_true", help="emit the review Markdown"
    )
    review_show_output.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    review_show.add_argument(
        "--with-evidence",
        action="store_true",
        help="include safely bounded evidence excerpts",
    )
    suggestion_selection = review_show.add_mutually_exclusive_group()
    suggestion_selection.add_argument(
        "--with-suggestion",
        action="store_true",
        help="include the latest suggestion that is current for this review",
    )
    suggestion_selection.add_argument(
        "--suggestion-id",
        help="include one exact suggestion, even when it is stale",
    )

    review_suggest = review_commands.add_parser(
        "suggest", help="generate advisory AI suggestions without resolving reviews"
    )
    review_suggest.add_argument("review_id", nargs="?")
    review_suggest.add_argument(
        "--all",
        action="store_true",
        help="suggest every pending review matched by the filters",
    )
    review_suggest.add_argument(
        "--priority",
        choices=("conflict", "linked", "unlinked", "all"),
        default="all",
    )
    review_suggest.add_argument(
        "--reason", choices=("conflicting_evidence", "ambiguous_match")
    )
    review_suggest.add_argument("--category", choices=tuple(sorted(CATEGORIES)))
    review_suggest.add_argument("--existing-id")
    review_suggest.add_argument("--source")
    review_suggest.add_argument("--limit", type=_positive)
    review_suggest.add_argument("--model")
    review_suggest.add_argument("--context-lines", type=_context_lines, default=5)
    review_suggest.add_argument("--timeout", type=_positive, default=120)
    review_suggest.add_argument(
        "--force",
        action="store_true",
        help="write a new append-only artifact even when exact inputs match",
    )
    _add_output(review_suggest)

    review_triage = review_commands.add_parser(
        "triage",
        help="interactively suggest, preview, and resolve pending reviews",
    )
    review_triage.add_argument(
        "--priority",
        choices=("conflict", "linked", "unlinked", "all"),
        default="all",
    )
    review_triage.add_argument(
        "--reason", choices=("conflicting_evidence", "ambiguous_match")
    )
    review_triage.add_argument("--category", choices=tuple(sorted(CATEGORIES)))
    review_triage.add_argument("--existing-id")
    review_triage.add_argument("--source")
    review_triage.add_argument("--limit", type=_positive)
    review_triage.add_argument("--reviewer")
    review_triage.add_argument("--model")
    review_triage.add_argument("--context-lines", type=_context_lines, default=5)
    review_triage.add_argument("--timeout", type=_positive, default=120)

    review_refresh = review_commands.add_parser(
        "refresh",
        help="rebase a pending review onto its current canonical snapshot",
    )
    review_refresh.add_argument("review_id")
    review_refresh.add_argument(
        "--existing-id",
        help="canonical snapshot to refresh when the review has multiple possibilities",
    )
    review_refresh.add_argument(
        "--adopt-existing",
        action="store_true",
        help="bind --existing-id to a review that currently names no candidate object",
    )
    review_refresh.add_argument("--dry-run", action="store_true")
    _add_output(review_refresh)

    review_resolve = review_commands.add_parser(
        "resolve", help="apply an explicit human review decision"
    )
    review_resolve.add_argument("review_id")
    resolve_decision = review_resolve.add_mutually_exclusive_group(required=True)
    resolve_decision.add_argument("--action", choices=REVIEW_ACTIONS)
    resolve_decision.add_argument(
        "--accept-suggestion",
        action="store_true",
        help="apply the exact action and parameters from --suggestion-id",
    )
    review_resolve.add_argument(
        "--suggestion-id",
        help="current suggestion accepted or explicitly overridden by this decision",
    )
    review_resolve.add_argument("--reviewer", required=True)
    review_resolve.add_argument("--note", required=True)
    review_resolve.add_argument(
        "--existing-id", help="canonical object selected by the reviewer"
    )
    review_resolve.add_argument(
        "--duplicate-of", help="pending review retained by merge-duplicate"
    )
    review_resolve.add_argument("--new-id", help="stable ID for create-separate")
    review_resolve.add_argument("--title", help="override the promoted title")
    review_resolve.add_argument("--status", choices=STATUSES)
    review_resolve.add_argument("--owner")
    review_resolve.add_argument("--clear-owner", action="store_true")
    review_resolve.add_argument("--confidence", choices=CONFIDENCES)
    review_resolve.add_argument("--effective-date", type=_iso_date)
    review_resolve.add_argument("--clear-effective-date", action="store_true")
    review_resolve.add_argument(
        "--allow-stale-evidence",
        action="store_true",
        help="explicitly promote historical evidence whose source snapshot changed",
    )
    review_resolve.add_argument(
        "--resolution-mode",
        choices=RESOLUTION_MODES,
        help="record how this decision was reached (default: human, or hybrid with a suggestion)",
    )
    review_resolve.add_argument("--dry-run", action="store_true")
    review_resolve.add_argument(
        "--no-index", action="store_true", help="do not regenerate browse indexes"
    )
    _add_output(review_resolve)

    ui = commands.add_parser(
        "ui", help="serve the local review UI on loopback"
    )
    ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1; the UI has no authentication)",
    )
    ui.add_argument("--port", type=_positive, default=8787)
    ui.add_argument(
        "--reviewer",
        help="reviewer recorded on resolutions (default: MEETING_MEMORY_REVIEWER or rui)",
    )
    ui.add_argument("--model", help="review model used by suggestion generation")
    ui.add_argument("--ask-model", help="model used by the Ask tab (overrides environment)")
    ui.add_argument("--context-lines", type=_context_lines, default=5)
    ui.add_argument("--timeout", type=_positive, default=120)

    project = commands.add_parser(
        "project", help="create and list saved source scopes"
    )
    project_commands = project.add_subparsers(
        dest="project_command", required=True
    )
    project_list = project_commands.add_parser("list", help="list saved projects")
    _add_output(project_list)
    project_create = project_commands.add_parser(
        "create", help="create a project from meeting and Slack source names"
    )
    project_create.add_argument("name", help="project name used by --project")
    project_create.add_argument(
        "--meeting-name",
        "--meeting",
        dest="meeting_names",
        action="append",
        default=[],
        help="fuzzy meeting filename match; repeat to add names",
    )
    project_create.add_argument(
        "--slack-name",
        "--slack",
        dest="slack_names",
        action="append",
        default=[],
        help="exact Slack channel/source name; repeat to add names",
    )
    project_create.add_argument(
        "--replace", action="store_true", help="replace an existing saved project"
    )
    _add_output(project_create)

    search = commands.add_parser("search", help="search durable knowledge deterministically")
    search.add_argument("query")
    search.add_argument("--project", help="saved project scope")
    search.add_argument("--category", choices=tuple(sorted(CATEGORIES)))
    search.add_argument("--status", choices=tuple(sorted(STATUSES)))
    search.add_argument("--owner")
    search.add_argument("--confidence", choices=tuple(sorted(CONFIDENCES)))
    search.add_argument("--updated-since", type=_iso_date)
    search.add_argument("--confirmed-since", type=_iso_date)
    search.add_argument("--limit", type=_positive, default=10)
    _add_output(search)

    show = commands.add_parser("show", help="show one exact stable knowledge ID")
    show.add_argument("knowledge_id")
    output = show.add_mutually_exclusive_group()
    output.add_argument("--raw", action="store_true", help="emit canonical Markdown")
    output.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    show.add_argument(
        "--with-evidence",
        action="store_true",
        help="include safely bounded evidence excerpts",
    )

    context = commands.add_parser("context", help="build a bounded context packet")
    context.add_argument("query")
    context.add_argument("--project", help="saved project scope")
    _add_context_options(context)
    context.add_argument("--output", type=Path)
    _add_output(context)

    ask = commands.add_parser(
        "ask", help="answer a question from bounded durable knowledge"
    )
    ask.add_argument("query")
    ask.add_argument("--project", help="saved project scope")
    _add_context_options(ask)
    ask.add_argument("--model", help="OpenRouter model (overrides environment)")
    ask.add_argument(
        "--timeout",
        type=_positive,
        default=120,
        help="provider timeout in seconds (default: 120)",
    )
    ask.add_argument("--output", type=Path, help="optionally save the rendered answer")
    _add_output(ask)
    return parser


def _ask_api_key(configured: Dict[str, str]) -> Optional[str]:
    return (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or configured.get("api_key")
    )


def _ask_model(explicit: Optional[str], configured: Dict[str, str]) -> Optional[str]:
    return (
        explicit
        or os.environ.get("DAILY_KNOWLEDGE_ASK_MODEL")
        or os.environ.get("DAILY_KNOWLEDGE_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or configured.get("ask_model")
        or configured.get("model")
    )


def _review_model(
    explicit: Optional[str], configured: Dict[str, str]
) -> Optional[str]:
    return (
        explicit
        or os.environ.get("MEETING_MEMORY_REVIEW_MODEL")
        or configured.get("review_model")
        or configured.get("ask_model")
    )


def _answer_output_path(
    repository: KnowledgeRepository, output: Path
) -> Path:
    path = output if output.is_absolute() else repository.root / output
    resolved = path.resolve()
    protected = [repository.knowledge_dir / category for category in CATEGORIES]
    protected.append(repository.review_dir)
    for directory in protected:
        try:
            resolved.relative_to(directory.resolve())
        except ValueError:
            continue
        raise ValueError(
            "answer output may not overwrite canonical knowledge or review files"
        )
    return resolved


def _print_result(result: PipelineResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.manifest, indent=2, ensure_ascii=False))
        return
    manifest = result.manifest
    mode = "Dry run" if result.dry_run else "Run"
    print(
        "%s %s: %s; %d processed, %d skipped, %d errors"
        % (
            mode,
            manifest["run_id"],
            manifest["status"],
            len(manifest["sources_processed"]),
            len(manifest["sources_skipped"]),
            len(manifest["errors"]),
        )
    )
    for error in manifest["errors"]:
        print("ERROR: %s" % error.get("error", error), file=sys.stderr)
    if result.manifest_path:
        print("Manifest: %s" % result.manifest_path)


def _result_exit_code(result: PipelineResult) -> int:
    if result.manifest["status"] == "success":
        return 0
    messages = "\n".join(str(item.get("error", "")) for item in result.manifest["errors"])
    if "AuthenticationError:" in messages:
        return 78
    if "TransientExtractionError:" in messages:
        return 75
    return 1


def _processing_pipeline(
    repository: KnowledgeRepository, configured: Dict[str, str]
) -> KnowledgePipeline:
    extractor = OpenRouterExtractor(
        api_key=_ask_api_key(configured),
        model=(
            os.environ.get("DAILY_KNOWLEDGE_MODEL")
            or os.environ.get("OPENROUTER_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or configured.get("model")
        ),
        version=os.environ.get("DAILY_KNOWLEDGE_EXTRACTOR_VERSION", EXTRACTOR_VERSION)
    )
    return KnowledgePipeline(repository, extractor)


def _slack_token(configured: Dict[str, Any]) -> Optional[str]:
    return (
        os.environ.get("SLACK_BOT_TOKEN")
        or os.environ.get("SLACK_TOKEN")
        or configured.get("bot_token")
    )


def _project_documents(repository: KnowledgeRepository, project_name: Optional[str]):
    # Keep loading ahead of filtering: scoped reads intentionally retain the
    # repository-wide evidence validation behavior of existing read commands.
    documents = load_documents(repository)
    if project_name is None:
        return documents, None
    scope = load_project(repository, project_name)
    return scope_documents(scope, documents), scope


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        detached_commands = {"index", "search", "show", "context", "ask", "validate"}
        detached_review = (
            args.command == "review"
            and args.review_command in ("list", "show")
        )
        detached_project = (
            args.command == "project" and args.project_command == "list"
        )
        if (
            args.allow_missing_evidence
            and args.command not in detached_commands
            and not detached_review
            and not detached_project
        ):
            raise ValueError(
                "--allow-missing-evidence is limited to read-only consumption commands"
            )
        output_dir, meetings_dir = _repository_paths(
            args.base_dir, args.meetings_dir, args.output_dir, args.config
        )
        openrouter = _openrouter_configuration(args.config)
        slack = _slack_configuration(args.config)
        repository = KnowledgeRepository(
            output_dir,
            meetings_dir=meetings_dir,
            require_evidence_sources=not args.allow_missing_evidence,
        )
        if args.command == "sync-sources":
            today = local_today()
            if args.date:
                start_date = args.date
                end_date = start_date + dt.timedelta(days=1)
            else:
                end_date = today + dt.timedelta(days=1) if args.include_today else today
                start_date = end_date - dt.timedelta(days=args.lookback_days)
            with repository.mutation_lock():
                result = SlackCollector(
                    meetings_dir,
                    slack.get("channel_ids", []),
                    token=_slack_token(slack),
                    excluded_user_ids=slack.get("excluded_user_ids", []),
                ).sync(start_date, end_date, dry_run=args.dry_run)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                mode = "Dry run" if result.dry_run else "Sync"
                print(
                    "%s: %d Slack channels, %d messages, %d excluded messages, "
                    "%d changed files, %d unchanged files."
                    % (
                        mode,
                        result.channels,
                        result.messages,
                        result.messages_excluded,
                        len(result.files_changed),
                        len(result.files_unchanged),
                    )
                )
                for path in result.files_changed:
                    print("  changed: %s" % path)
            return 0
        if args.command == "index":
            result = generate_indexes(
                repository,
                recent_days=args.recent_days,
                dry_run=args.dry_run,
                force=args.force,
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                print("Loaded %d knowledge objects." % result.loaded)
                print("Generated %d index files." % result.generated)
                print("Changed %d files." % result.changed)
                print("Unchanged %d files." % result.unchanged)
                if args.dry_run:
                    print("Dry run: no files written.")
            return 0

        if args.command == "merge":
            result = KnowledgeMerger(repository).merge(
                args.loser_id,
                args.survivor_id,
                args.reviewer,
                args.note,
                statement=args.statement,
                title=args.title,
                status=args.status,
                owner=args.owner,
                clear_owner=args.clear_owner,
                confidence=args.confidence,
                effective_date=(
                    args.effective_date.isoformat()
                    if args.effective_date is not None
                    else None
                ),
                clear_effective_date=args.clear_effective_date,
                allow_cross_category=args.allow_cross_category,
                allow_conflicting_numbers=args.allow_conflicting_numbers,
                dry_run=args.dry_run,
                refresh_indexes=not args.no_index,
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                mode = "Dry run" if result.dry_run else "Merged"
                print(
                    "%s %s into %s: %d evidence entries added."
                    % (mode, result.loser_id, result.survivor_id, result.evidence_added)
                )
                if result.before and result.before["statement"] != result.after["statement"]:
                    print(
                        "  statement: %s -> %s"
                        % (result.before["statement"], result.after["statement"])
                    )
                if result.retargeted_review_ids:
                    print(
                        "Retargeted review items: %s"
                        % ", ".join(result.retargeted_review_ids)
                    )
                if result.retargeted_object_ids:
                    print(
                        "Retargeted related objects: %s"
                        % ", ".join(result.retargeted_object_ids)
                    )
                print("Changed paths:")
                for path in result.changed_paths:
                    print("  %s" % path)
                if result.index_changed_paths:
                    print(
                        "Regenerated index paths: %d"
                        % len(result.index_changed_paths)
                    )
            return 0

        if args.command == "remove":
            inventory_path = Path(args.object_id_file).expanduser().resolve()
            inventory_data = inventory_path.read_bytes()
            inventory_sha256 = sha256_bytes(inventory_data)
            if (
                args.inventory_sha256 is not None
                and args.inventory_sha256 != inventory_sha256
            ):
                raise RemovalError(
                    "object inventory SHA-256 does not match --inventory-sha256"
                )
            object_ids = [
                line.strip()
                for line in inventory_data.decode("utf-8").splitlines()
                if line.strip()
            ]
            if len(object_ids) != len(set(object_ids)):
                raise RemovalError("object inventory contains duplicate IDs")
            if len(object_ids) != args.confirm_count:
                raise RemovalError(
                    "object inventory contains %d IDs, expected %d"
                    % (len(object_ids), args.confirm_count)
                )
            result = KnowledgeRemover(repository).remove(
                object_ids,
                args.reviewer,
                args.note,
                inventory_sha256=inventory_sha256,
                dry_run=args.dry_run,
                refresh_indexes=not args.no_index,
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                mode = "Dry run" if result.dry_run else "Removed"
                print("%s %d canonical objects." % (mode, len(result.object_ids)))
                print("Updated related objects: %d" % len(result.updated_object_ids))
                print("Updated reviews: %d" % len(result.updated_review_ids))
                print("Updated source states: %d" % len(result.updated_source_states))
                print("Cleanup manifest: %s" % result.manifest_path)
                if result.index_changed_paths:
                    print(
                        "Regenerated index paths: %d"
                        % len(result.index_changed_paths)
                    )
            return 0

        if args.command == "review":
            if args.review_command == "list":
                values = list_reviews(
                    repository,
                    status=None if args.status == "all" else args.status,
                    priority=args.priority,
                    reason=args.reason,
                    category=args.category,
                    existing_id=args.existing_id,
                    source=args.source,
                    limit=args.limit,
                )
                if args.json:
                    print(
                        json.dumps(
                            reviews_payload(values),
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                else:
                    print(render_review_list(values), end="")
                return 0
            if args.review_command == "show":
                review_values = repository.load_reviews()
                item = exact_review(repository, args.review_id, review_values)
                suggestion = None
                if args.suggestion_id:
                    suggestion = repository.load_suggestion(
                        item.id, args.suggestion_id
                    )
                elif args.with_suggestion:
                    suggestion = latest_current_suggestion(repository, item)
                suggestion_payload = (
                    suggestion_display_payload(repository, suggestion, item)
                    if suggestion is not None
                    else None
                )
                if args.raw:
                    if item.path is None:
                        raise ReviewNotFoundError(
                            "review item has no repository path: %s" % item.id
                        )
                    print(item.path.read_text(encoding="utf-8"), end="")
                    if suggestion_payload is not None:
                        print("\n" + render_suggestion(suggestion_payload))
                elif args.json:
                    payload = review_payload(
                        repository,
                        item,
                        include_excerpts=args.with_evidence,
                        review_values=review_values,
                    )
                    if args.with_suggestion or args.suggestion_id:
                        payload["suggestion"] = suggestion_payload
                    print(
                        json.dumps(
                            payload,
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                else:
                    rendered = render_review_show(
                        repository,
                        item,
                        include_excerpts=args.with_evidence,
                        review_values=review_values,
                    )
                    if suggestion_payload is not None:
                        rendered += (
                            "\n" + render_suggestion(suggestion_payload) + "\n"
                        )
                    elif args.with_suggestion:
                        rendered += (
                            "\nAI suggestion: No current suggestion is available.\n"
                        )
                    print(rendered, end="")
                return 0
            if args.review_command == "suggest":
                if bool(args.review_id) == bool(args.all):
                    raise ValueError(
                        "review suggest requires exactly one REVIEW_ID or --all"
                    )
                filters = {
                    key: value
                    for key, value in {
                        "priority": args.priority,
                        "reason": args.reason,
                        "category": args.category,
                        "existing_id": args.existing_id,
                        "source": args.source,
                        "limit": args.limit,
                    }.items()
                    if value is not None and value != "all"
                }
                if args.review_id:
                    all_reviews = repository.load_reviews()
                    item = exact_review(repository, args.review_id, all_reviews)
                    if item.status != "pending":
                        raise ValueError(
                            "suggestions can only be generated for pending reviews"
                        )
                    values = (item,)
                    filters = {"review_id": args.review_id}
                else:
                    values = list_reviews(
                        repository,
                        status="pending",
                        priority=args.priority,
                        reason=args.reason,
                        category=args.category,
                        existing_id=args.existing_id,
                        source=args.source,
                        limit=args.limit,
                    )
                model = _review_model(args.model, openrouter)
                if not model:
                    raise ConfigurationError(
                        "review model is not configured; use --model, "
                        "MEETING_MEMORY_REVIEW_MODEL, review_model, or ask_model"
                    )
                result = generate_review_suggestions(
                    repository,
                    values,
                    OpenRouterReviewAdvisor(
                        repository,
                        api_key=_ask_api_key(openrouter),
                        model=model,
                        timeout=args.timeout,
                    ),
                    model,
                    context_lines=args.context_lines,
                    force=args.force,
                    filters=filters,
                )
                if args.json:
                    print(
                        json.dumps(
                            result.manifest, indent=2, ensure_ascii=False
                        )
                    )
                else:
                    print(
                        "Suggestion run %s: %s; %d created, %d reused, %d failed."
                        % (
                            result.manifest["run_id"],
                            result.manifest["status"],
                            len(result.manifest["suggestions_created"]),
                            len(result.manifest["suggestions_reused"]),
                            len(result.manifest["failures"]),
                        )
                    )
                    for review_id, suggestion_id in result.manifest[
                        "suggestions_created"
                    ].items():
                        print("  created: %s -> %s" % (review_id, suggestion_id))
                    for review_id, suggestion_id in result.manifest[
                        "suggestions_reused"
                    ].items():
                        print("  reused: %s -> %s" % (review_id, suggestion_id))
                    for failure in result.manifest["failures"]:
                        print(
                            "ERROR: %s: %s"
                            % (failure["review_id"], failure["error"]),
                            file=sys.stderr,
                        )
                    print("Manifest: %s" % result.manifest_path)
                return 1 if result.failed else 0
            if args.review_command == "triage":
                values = list_reviews(
                    repository,
                    status="pending",
                    priority=args.priority,
                    reason=args.reason,
                    category=args.category,
                    existing_id=args.existing_id,
                    source=args.source,
                    limit=args.limit,
                )
                model = _review_model(args.model, openrouter)
                if not model:
                    raise ConfigurationError(
                        "review model is not configured; use --model, "
                        "MEETING_MEMORY_REVIEW_MODEL, review_model, or ask_model"
                    )
                reviewer = (
                    args.reviewer
                    or os.environ.get("MEETING_MEMORY_REVIEWER")
                    or input("Reviewer: ").strip()
                )
                result = ReviewTriage(
                    repository,
                    OpenRouterReviewAdvisor(
                        repository,
                        api_key=_ask_api_key(openrouter),
                        model=model,
                        timeout=args.timeout,
                    ),
                    model,
                    reviewer=reviewer,
                    context_lines=args.context_lines,
                ).run(values)
                print(
                    "Triage summary: %d selected, %d applied, %d deferred, "
                    "%d declined, %d failed%s."
                    % (
                        result.selected,
                        result.applied,
                        result.deferred,
                        result.declined,
                        result.failed,
                        ", quit early" if result.quit else "",
                    )
                )
                return 1 if result.failed else 0
            if args.review_command == "refresh":
                result = ReviewRefresher(repository).refresh(
                    args.review_id,
                    existing_id=args.existing_id,
                    adopt_existing=args.adopt_existing,
                    dry_run=args.dry_run,
                )
                if args.json:
                    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
                else:
                    mode = "Dry run" if result.dry_run else "Refreshed"
                    print(
                        "%s %s against %s."
                        % (mode, result.review_id, result.existing_id)
                    )
                    print(
                        "Canonical statement: %s -> %s"
                        % (
                            result.previous_statement or "none",
                            result.current_statement,
                        )
                    )
                    if result.suggestions_made_stale:
                        print(
                            "Suggestions made stale: %s"
                            % ", ".join(result.suggestions_made_stale)
                        )
                    if not result.changed_paths:
                        print("Snapshot was already current; no files changed.")
                return 0
            result = ReviewResolver(repository).resolve(
                args.review_id,
                args.action,
                args.reviewer,
                args.note,
                suggestion_id=args.suggestion_id,
                accept_suggestion=args.accept_suggestion,
                existing_id=args.existing_id,
                duplicate_of=args.duplicate_of,
                new_id=args.new_id,
                title=args.title,
                status=args.status,
                owner=args.owner,
                clear_owner=args.clear_owner,
                confidence=args.confidence,
                effective_date=(
                    args.effective_date.isoformat()
                    if args.effective_date is not None
                    else None
                ),
                clear_effective_date=args.clear_effective_date,
                allow_stale_evidence=args.allow_stale_evidence,
                resolution_mode=args.resolution_mode,
                dry_run=args.dry_run,
                refresh_indexes=not args.no_index,
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
            else:
                mode = "Dry run" if result.dry_run else "Resolved"
                print(
                    "%s %s with action %s -> %s."
                    % (
                        mode,
                        result.review_id,
                        result.action,
                        result.destination_status,
                    )
                )
                print(
                    "Affected objects: %s"
                    % (", ".join(result.affected_object_ids) or "none")
                )
                for change in result.object_changes:
                    before = change["before"]
                    after = change["after"]
                    print(
                        "  %s: %s -> %s"
                        % (
                            after["id"],
                            before["status"] if before else "new",
                            after["status"],
                        )
                    )
                    if before is None or before["statement"] != after["statement"]:
                        print(
                            "    statement: %s -> %s"
                            % (
                                before["statement"] if before else "none",
                                after["statement"],
                            )
                        )
                print("Changed paths:")
                for path in result.changed_paths:
                    print("  %s" % path)
                if result.index_changed_paths:
                    print(
                        "Regenerated index paths: %d"
                        % len(result.index_changed_paths)
                    )
            return 0

        if args.command == "project":
            if args.project_command == "list":
                scopes = load_projects(repository)
                if args.json:
                    print(
                        json.dumps(
                            {"projects": [scope.to_dict() for scope in scopes]},
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                elif not scopes:
                    print("No saved projects.")
                else:
                    for scope in scopes:
                        print(scope.name)
                        print(
                            "  Meetings: %s"
                            % (", ".join(scope.meeting_names) or "none")
                        )
                        print(
                            "  Slack: %s"
                            % (", ".join(scope.slack_names) or "none")
                        )
                return 0
            if not args.meeting_names and not args.slack_names:
                raise ValueError(
                    "project create needs at least one --meeting-name or --slack-name"
                )
            scope = ProjectScope(
                name=args.name,
                meeting_names=tuple(args.meeting_names),
                slack_names=tuple(args.slack_names),
            )
            path = save_project(
                repository, scope, replace_existing=args.replace
            )
            if args.json:
                print(
                    json.dumps(
                        {"project": scope.to_dict(), "path": path},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print("Saved project %s: %s" % (scope.name, path))
            return 0

        if args.command == "ui":
            try:
                import uvicorn

                from ..ui.app import create_app
            except ImportError as exc:
                raise ConfigurationError(
                    "the review UI needs FastAPI and uvicorn: pip install "
                    "'meeting-memory[ui]'"
                ) from exc
            if args.host != "127.0.0.1":
                print(
                    "WARNING: the review UI has no authentication; binding to %s "
                    "exposes every write path on this host." % args.host,
                    file=sys.stderr,
                )
            application = create_app(
                repository,
                reviewer=(
                    args.reviewer
                    or os.environ.get("MEETING_MEMORY_REVIEWER")
                    or "rui"
                ),
                review_model=_review_model(args.model, openrouter),
                ask_model=_ask_model(args.ask_model, openrouter),
                api_key=_ask_api_key(openrouter),
                timeout=args.timeout,
                context_lines=args.context_lines,
            )
            print("Meeting Memory review UI: http://%s:%d" % (args.host, args.port))
            uvicorn.run(application, host=args.host, port=args.port, log_level="info")
            return 0

        if args.command == "search":
            documents, _ = _project_documents(repository, args.project)
            filters = SearchFilters(
                category=args.category,
                status=args.status,
                owner=args.owner,
                confidence=args.confidence,
                updated_since=args.updated_since,
                confirmed_since=args.confirmed_since,
            )
            results = search_documents(
                documents, args.query, filters=filters, limit=args.limit
            )
            if args.json:
                print(
                    json.dumps(
                        search_payload(args.query, filters, results),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(render_search_results(args.query, results), end="")
            return 0

        if args.command == "show":
            documents = load_documents(repository)
            document = exact_document(documents, args.knowledge_id)
            if args.raw:
                print((repository.root / document.file_path).read_text(encoding="utf-8"), end="")
            elif args.json:
                print(
                    json.dumps(
                        show_payload(repository, document, args.with_evidence),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(
                    render_show(repository, document, args.with_evidence),
                    end="",
                )
            return 0

        if args.command == "context":
            documents, scope = _project_documents(repository, args.project)
            packet = build_context_packet(
                repository,
                documents,
                args.query,
                limit=args.limit,
                max_chars=args.max_chars,
                max_evidence_per_object=args.max_evidence_per_object,
                include_review_items=args.include_review_items,
                include_manual_notes=args.include_manual_notes,
                include_evidence_excerpts=args.include_evidence_excerpts,
                source_predicate=(scope.matches_source if scope else None),
            )
            packet = write_context_packet(repository, packet, args.output)
            if args.json:
                print(json.dumps(packet.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(packet.markdown, end="")
                print("Saved context: %s" % packet.output_path)
            return 0

        if args.command == "ask":
            documents, scope = _project_documents(repository, args.project)
            packet = build_context_packet(
                repository,
                documents,
                args.query,
                limit=args.limit,
                max_chars=args.max_chars,
                max_evidence_per_object=args.max_evidence_per_object,
                include_review_items=args.include_review_items,
                include_manual_notes=args.include_manual_notes,
                include_evidence_excerpts=args.include_evidence_excerpts,
                source_predicate=(scope.matches_source if scope else None),
            )
            if packet.selected:
                client = OpenRouterChatClient(
                    _ask_api_key(openrouter),
                    _ask_model(args.model, openrouter),
                    timeout=args.timeout,
                )
                answer = OpenRouterAnswerer(client).answer(packet)
            else:
                answer = insufficient_answer(packet)
            if args.json:
                rendered = json.dumps(
                    answer_payload(answer), indent=2, ensure_ascii=False
                ) + "\n"
            else:
                rendered = render_answer(answer)
            if args.output:
                atomic_write(
                    _answer_output_path(repository, args.output),
                    rendered.encode("utf-8"),
                )
            print(rendered, end="")
            return 0

        if args.command == "validate":
            counts = repository.validate_all()
            if args.json:
                print(json.dumps({"status": "valid", **counts}, indent=2))
            else:
                print(
                    "Valid: %(knowledge_objects)d objects, %(review_items)d review items, "
                    "%(source_states)d source states, %(run_manifests)d run manifests" % counts
                )
                print("Machine index: %s" % counts["machine_index_status"])
            return 0

        if args.command == "status":
            extractor = FakeExtractor(
                [],
                version=os.environ.get("DAILY_KNOWLEDGE_EXTRACTOR_VERSION", EXTRACTOR_VERSION),
            )
            result = KnowledgePipeline(repository, extractor).status(
                lookback_days=args.lookback_days,
                include_today=args.include_today,
            )
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                latest = result["latest_successful_run"]
                print("Pending sources: %d" % len(result["pending_sources"]))
                print("Failed sources: %d" % len(result["failed_sources"]))
                print("Latest successful run: %s" % (latest["run_id"] if latest else "none"))
                print("Open review items: %d" % result["open_review_item_count"])
                print("Knowledge objects:")
                for category, count in result["knowledge_object_count_by_category"].items():
                    print("  %s: %d" % (category, count))
            return 0

        pipeline = _processing_pipeline(repository, openrouter)
        if args.command == "process-date":
            result = pipeline.process_dates(
                [args.date],
                dry_run=args.dry_run,
                force=args.force,
            )
        else:
            if args.force:
                today = local_today()
                earliest = today - dt.timedelta(days=args.lookback_days)
                dates = [
                    value
                    for value in repository.discover_dates()
                    if earliest <= dt.date.fromisoformat(value) <= today
                    and (args.include_today or dt.date.fromisoformat(value) < today)
                    and repository.qualifying_sources(value)
                ]
            else:
                dates = pipeline.pending_dates(
                    args.lookback_days,
                    include_today=args.include_today,
                )
            result = pipeline.process_dates(
                dates,
                dry_run=args.dry_run,
                force=args.force,
            )
        _print_result(result, args.json)
        return _result_exit_code(result)
    except AuthenticationError as exc:
        print("ERROR: authentication/account failure: %s" % exc, file=sys.stderr)
        return 78
    except ConfigurationError as exc:
        print("ERROR: invalid configuration: %s" % exc, file=sys.stderr)
        return 78
    except TransientExtractionError as exc:
        if args.debug:
            raise
        print("ERROR: temporary provider failure: %s" % exc, file=sys.stderr)
        return 75
    except (ObjectNotFoundError, ReviewNotFoundError) as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 4
    except ReviewResolutionError as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except MergeError as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except RemovalError as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except InvalidContextBudgetError as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except (SchemaError, EvidenceError) as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3 if args.command in ("index", "search", "show", "context", "ask") else 1
    except ValueError as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except (KnowledgeError, OSError) as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
