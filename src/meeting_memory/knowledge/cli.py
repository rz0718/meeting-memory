"""Command-line interface for daily durable knowledge."""

from __future__ import annotations

import argparse
import configparser
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
from .constants import CATEGORIES, CONFIDENCES, EXTRACTOR_VERSION, STATUSES
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
    SchemaError,
    TransientExtractionError,
)
from .extractors import FakeExtractor, OpenRouterExtractor
from .indexes import generate_indexes
from .openrouter import OpenRouterChatClient
from .pipeline import KnowledgePipeline, PipelineResult
from .presentation import (
    ObjectNotFoundError,
    exact_document,
    render_show,
    show_payload,
)
from .repository import KnowledgeRepository
from .search import (
    SearchFilters,
    render_search_results,
    search_documents,
    search_payload,
)
from .util import atomic_write


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _config_path(value: Optional[str]) -> Optional[Path]:
    explicit = value or os.environ.get("MEETING_MEMORY_CONFIG")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ConfigurationError("configuration file does not exist: %s" % path)
        return path

    candidates = [
        Path.cwd() / "meeting-memory.ini",
        Path(__file__).resolve().parents[3] / "meeting-memory.ini",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def _path_configuration(value: Optional[str]) -> Dict[str, str]:
    path = _config_path(value)
    if path is None:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ConfigurationError("cannot read configuration file %s: %s" % (path, exc)) from exc
    if not parser.has_section("paths"):
        raise ConfigurationError("configuration file %s is missing [paths]" % path)

    configured: Dict[str, str] = {}
    for key in ("meetings_dir", "output_dir"):
        raw = parser.get("paths", key, fallback="").strip()
        if not raw:
            continue
        configured_path = Path(raw).expanduser()
        if not configured_path.is_absolute():
            configured_path = path.parent / configured_path
        configured[key] = str(configured_path.resolve())
    return configured


def _openrouter_configuration(value: Optional[str]) -> Dict[str, str]:
    path = _config_path(value)
    if path is None:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ConfigurationError("cannot read configuration file %s: %s" % (path, exc)) from exc
    if not parser.has_section("openrouter"):
        return {}
    return {
        key: parser.get("openrouter", key, fallback="").strip()
        for key in ("api_key", "model", "ask_model")
        if parser.get("openrouter", key, fallback="").strip()
    }


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
        description="Incrementally curate durable knowledge from local meeting notes.",
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
    commands = parser.add_subparsers(dest="command", required=True)

    process_date = commands.add_parser("process-date", help="process one explicit date")
    process_date.add_argument("date", help="meeting date in YYYY-MM-DD")
    _add_process_options(process_date)

    process_pending = commands.add_parser(
        "process-pending", help="process pending dates oldest first"
    )
    _add_process_options(process_pending, pending=True)

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

    search = commands.add_parser("search", help="search durable knowledge deterministically")
    search.add_argument("query")
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
    _add_context_options(context)
    context.add_argument("--output", type=Path)
    _add_output(context)

    ask = commands.add_parser(
        "ask", help="answer a question from bounded durable knowledge"
    )
    ask.add_argument("query")
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


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output_dir, meetings_dir = _repository_paths(
            args.base_dir, args.meetings_dir, args.output_dir, args.config
        )
        openrouter = _openrouter_configuration(args.config)
        repository = KnowledgeRepository(output_dir, meetings_dir=meetings_dir)
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

        if args.command == "search":
            documents = load_documents(repository)
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
            documents = load_documents(repository)
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
            )
            packet = write_context_packet(repository, packet, args.output)
            if args.json:
                print(json.dumps(packet.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(packet.markdown, end="")
                print("Saved context: %s" % packet.output_path)
            return 0

        if args.command == "ask":
            documents = load_documents(repository)
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
                today = dt.date.today()
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
    except ObjectNotFoundError as exc:
        if args.debug:
            raise
        print("ERROR: %s" % exc, file=sys.stderr)
        return 4
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
