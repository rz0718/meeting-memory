# Meeting Memory — using it with your own meeting notes

This package incrementally extracts evidence-backed **durable knowledge**
(decisions, policies, processes, projects, systems, metrics, ownership) from a
folder of local Markdown meeting notes, and exposes deterministic search / show /
context commands plus an LLM-backed `ask`.

The `knowledge/README.md` created inside the configured output directory is a
generated browse surface. This file, inside the source checkout, is the
hand-maintained usage guide.

## Pointing it at meeting logs and an output directory

Input and output are independently configurable. Command-line options take
precedence over environment variables:

1. Meeting input: `--meetings-dir` (alias `--meeting-logs-dir`), then
   `MEETING_MEMORY_MEETINGS_DIR` (or `MEETING_MEMORY_LOGS_DIR`).
2. Generated data: `--output-dir`, then `MEETING_MEMORY_OUTPUT_DIR`.
3. When omitted, output defaults to the current directory and meetings default
   to `<output>/meetings`.

`--base-dir` and `DAILY_KNOWLEDGE_BASE_DIR` remain as compatibility shortcuts for
the old combined layout. Explicit input/output options override that shortcut.

The resulting layout is:

```
<meetings-dir>/
  YYYY-MM-DD/<slug>.md            # INPUT: your meeting notes (see below)
<output-dir>/
  knowledge/<category>/*.md       # OUTPUT: curated canonical objects
  knowledge-review/pending/*.md   # OUTPUT: conflicts needing a human
  .knowledge-state/               # OUTPUT: per-source + per-run state
  outputs/Durable-Knowledge/      # OUTPUT: context packets, saved answers
  logs/
```

The source checkout itself does not need to contain either directory.

## What a meeting note must look like

The requirements the pipeline actually enforces are lighter than the full
Google-sync frontmatter.

### 1. Path and file name

- **Location:** `<meetings-dir>/YYYY-MM-DD/<slug>.md`
- The **date comes from the folder name**, not from frontmatter. Only directories
  whose name matches `YYYY-MM-DD` exactly are discovered
  (`repository.discover_dates`). That date is recorded as the evidence
  `source_date`.
- The file must end in `.md`.

### 2. Files that are silently skipped for extraction

`repository.qualifying_sources` excludes a note when **any** of these hold:

- the name ends in `.transcript.md`;
- the name contains `standup` (case-insensitive);
- the name contains `interview` (case-insensitive);
- its frontmatter sets `durable_knowledge: false`.

Everything else in the date folder is treated as a durable-knowledge source.

### 3. Frontmatter (mostly optional for this module)

- The **only** frontmatter key this module reads is `durable_knowledge`
  (set it to `false` to opt a note out). All other keys are ignored by the
  extractor.
- A note is fed to the model as raw text with 1-based line numbers, so any
  frontmatter you include is just extra context the model can cite — it is not
  parsed into fields here.
- **Recommended minimum**, for readable evidence and consistency with the rest of
  the repo (matching workflows, `validate`, human browsing):

  ```yaml
  ---
  title: "Weekly Ops Review"
  date: 2026-07-21
  attendees:
    - "alice@example.com"
    - "bob@example.com"
  ---
  ```

  (Strictly, a note with **no** frontmatter still qualifies — the opt-out check
  tolerates its absence.)

### 4. Body = immutable evidence

- The body is plain Markdown. The extractor cites evidence as **1-based inclusive
  line ranges** into the file, and stores the file's SHA-256.
- Treat committed notes as append-only history. Editing a note changes its hash
  and can invalidate stored evidence line ranges; re-process the date if you must
  edit.

### Minimal working example

`<meetings-dir>/2026-07-21/weekly-ops-review.md`:

```markdown
---
title: "Weekly Ops Review"
date: 2026-07-21
attendees: ["alice@example.com", "bob@example.com"]
---

# Notes

- Decided withdrawals over $50k now require dual approval, effective Aug 1.
  Owner: Treasury (Alice).
- Migrated the settlement job to the new scheduler; old cron is deprecated.
```

## Which commands need an API key

Extraction and `ask` call an OpenRouter/Anthropic-compatible endpoint and need a
key plus a model:

- Preferred local setup: `[openrouter]` in the git-ignored
  `meeting-memory.ini`, with `api_key`, `model`, and optional `ask_model`.
- Compatibility environment key: `OPENROUTER_API_KEY` or
  `ANTHROPIC_AUTH_TOKEN`.
- Compatibility environment model: `DAILY_KNOWLEDGE_ASK_MODEL` /
  `DAILY_KNOWLEDGE_MODEL` / `OPENROUTER_MODEL` / `ANTHROPIC_MODEL` (or
  `ask --model ...`). Environment values override the INI.

| Command | Needs API key? | Reads meeting notes? |
|---|---|---|
| `process-date` / `process-pending` | yes | yes (extracts) |
| `ask` | yes | no (reads curated `knowledge/`) |
| `search` / `show` / `context` | no | no (reads curated `knowledge/`) |
| `index` / `status` / `validate` | no | `status` scans note dates only |

## Quick start with your own notes

```bash
# Copy meeting-memory.ini.example to meeting-memory.ini, then configure:
# [paths]
# meetings_dir = /path/to/meeting-logs
# output_dir = /path/to/memory-data
# [openrouter]
# api_key = sk-or-...
# model = provider/model-name

# 1. Drop notes under <meetings_dir>/YYYY-MM-DD/<slug>.md
# 2. See what would be processed
meeting-memory status

# 3. Extract knowledge for one date (or all pending)
meeting-memory process-date 2026-07-21
meeting-memory process-pending

# 4. Browse / query (deterministic, no key)
meeting-memory search "withdrawal SLA"
meeting-memory index      # rebuild knowledge/README.md + _index/

# 5. Ask a question over the curated layer (needs key)
meeting-memory ask "What is the withdrawal approval policy?"
```

## Adapting notes from another source

If your notes come from something other than the Google sync, write a small
adapter that emits one `.md` file per meeting into
`meetings/YYYY-MM-DD/<slug>.md`. You only need to:

1. bucket notes into `YYYY-MM-DD` folders (the date is taken from the folder);
2. avoid the skip triggers in the file name (`standup`, `interview`,
   `.transcript.md`) unless you intend to exclude them;
3. optionally add `title` / `date` / `attendees` frontmatter for readability.

No other frontmatter is required for this module to process the note.
