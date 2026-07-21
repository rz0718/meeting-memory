# Meeting Memory

Meeting Memory turns Markdown meeting logs into an evidence-backed, searchable
memory store. This repository contains only the memory engine; meeting logs and
generated memory can live in directories outside this checkout.

## Install

```bash
python3 -m pip install -e .
meeting-memory --help
```

## Configure storage

For persistent repository-local configuration, copy the template:

```bash
cp meeting-memory.ini.example meeting-memory.ini
```

Then edit `meeting-memory.ini`:

```ini
[paths]
meetings_dir = /path/to/meeting-logs
output_dir = /path/to/meeting-memory-data

[openrouter]
api_key = sk-or-v1-your-key
model = provider/model-name
# Optional; falls back to model when blank.
ask_model =
```

The CLI automatically loads `meeting-memory.ini` from the current directory or
the project root. The local file is ignored by Git because its absolute paths
are machine-specific. A different file can be selected with `--config` or
`MEETING_MEMORY_CONFIG`:

```bash
meeting-memory --config /path/to/config.ini search "withdrawal policy"
```

For interactive CLI commands, configuration precedence is command-line path
options, environment variables, the INI file, then built-in defaults.

The OpenRouter settings provide:

- `api_key`: credential used by extraction and `ask`;
- `model`: default OpenRouter model for knowledge extraction;
- `ask_model`: optional model used only by `ask`.

Protect the local file after adding the API key:

```bash
chmod 600 meeting-memory.ini
```

The local INI is ignored by Git, and the committed example never contains a
real key. For compatibility, `OPENROUTER_API_KEY`, `DAILY_KNOWLEDGE_MODEL`, and
the other existing model environment variables still override INI values.

Paths can also be passed explicitly:

```bash
meeting-memory \
  --meetings-dir /path/to/meeting-logs \
  --output-dir /path/to/meeting-memory-data \
  status
```

Or configured in the environment:

```bash
export MEETING_MEMORY_MEETINGS_DIR=/path/to/meeting-logs
export MEETING_MEMORY_OUTPUT_DIR=/path/to/meeting-memory-data
meeting-memory status
```

The meetings directory is read as input and must contain
`YYYY-MM-DD/<meeting>.md`. The output directory owns all generated state:

```text
/path/to/meeting-memory-data/
  knowledge/                 canonical memory objects and indexes
  knowledge-review/          conflicts requiring review
  .knowledge-state/          source and run state
  .knowledge-index/          optional machine-readable index
  outputs/                   digests, contexts, and saved answers
  logs/
```

Evidence references remain portable (`meetings/YYYY-MM-DD/file.md`) even when
the configured meeting-log directory has another name or location.

For the note format, extraction rules, model configuration, and complete command
guide, see
[src/meeting_memory/knowledge/README.md](src/meeting_memory/knowledge/README.md).

## Scheduled processing

The production runner is [scripts/run_daily_knowledge.sh](scripts/run_daily_knowledge.sh).
It requires `meetings_dir` and `output_dir` in the project INI and uses those
values as the single source of truth for scheduled storage paths. Path
environment variables are deliberately removed before the CLI is invoked, so
they cannot silently override the INI during a scheduled run. While the INI
`api_key` is blank, `~/.env.meeting-memory` is loaded as a backward-compatible
fallback. Once an INI key is present, the environment file is no longer sourced
by the runner.

The runner writes its dated log to `logs/` beneath the INI-defined
`output_dir`. Cron output is intentionally left enabled so configuration errors
that occur before the output directory is known remain visible to cron mail or
the scheduler's journal.
