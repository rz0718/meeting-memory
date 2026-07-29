# Meeting Memory

Meeting Memory turns Google Meet notes and selected Slack channels into an
evidence-backed, searchable memory store. This repository contains only the
memory engine; source notes and generated memory can live outside this checkout.

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
# Optional; falls back to ask_model, but never to extraction model.
review_model =
# Reserved for a later independent automation verifier.
review_critic_model =

[slack]
bot_token = xoxb-your-bot-token
channel_ids =
  C0123456789
  G0123456789
```

The CLI automatically loads `meeting-memory.ini` from the current directory or
the project root. The local file is ignored by Git because its absolute paths
are machine-specific. A different file can be selected with `--config` or
`MEETING_MEMORY_CONFIG`:

```bash
meeting-memory --config /path/to/config.ini search "withdrawal policy"
```

For a read-only mirror that intentionally contains curated `knowledge/` but no
raw meeting notes, add `--allow-missing-evidence` before the consumption
command:

```bash
meeting-memory --config /path/to/config.ini --allow-missing-evidence \
  context "What is the withdrawal policy?" --no-review-items
```

The flag still validates evidence path syntax and knowledge metadata. It only
relaxes the requirement that the referenced meeting files exist locally, and
is rejected for extraction commands.

For interactive CLI commands, configuration precedence is command-line path
options, environment variables, the INI file, then built-in defaults.

The OpenRouter settings provide:

- `api_key`: credential used by extraction and `ask`;
- `model`: default OpenRouter model for knowledge extraction;
- `ask_model`: optional model used only by `ask`.
- `review_model`: model used by advisory `review suggest`; it falls back only
  to `ask_model`;
- `review_critic_model`: reserved for the later independent automation critic.

`MEETING_MEMORY_REVIEW_MODEL` overrides `review_model`. Review suggestions do
not fall back to the extraction `model`, so a missing review model fails
explicitly.

The optional Slack settings provide:

- `bot_token`: Slack bot token (or use `SLACK_BOT_TOKEN` in the scheduler environment);
- `channel_ids`: comma-, whitespace-, or newline-separated public/private channel IDs.

The bot must be a member of each channel and have `channels:history` for public
channels or `groups:history` for private channels. `users:read` is optional; if
it is unavailable, message authors remain stable Slack user IDs.

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

The meetings directory contains Google Meet notes and the Markdown snapshots
generated from Slack. Sources use `YYYY-MM-DD/<source>.md`. The output directory
owns all generated state:

```text
/path/to/meeting-memory-data/
  knowledge/                 canonical memory objects and indexes
  knowledge-review/          conflicts plus append-only AI suggestions
  .knowledge-state/          source, ingestion-run, and review-run state
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

Each scheduled attempt first runs `sync-sources`, which fetches every page of
messages from the configured Slack channels for the lookback window, including
thread replies for threads discovered in that window. It writes deterministic
`YYYY-MM-DD/slack-<channel-id>.md` snapshots using UTC date boundaries. The same
attempt then runs `process-pending`, so newly collected Slack snapshots and
existing Google Meet notes are extracted and reconciled together. Files whose
content has not changed are skipped by the normal source hash checkpoint.

You can inspect or backfill collection separately:

```bash
meeting-memory sync-sources --date 2026-07-21
meeting-memory sync-sources --lookback-days 30 --dry-run
```

The runner writes its dated log to `logs/` beneath the INI-defined
`output_dir`. Cron output is intentionally left enabled so configuration errors
that occur before the output directory is known remain visible to cron mail or
the scheduler's journal.

## Human knowledge review

Raw meeting and Slack snapshots are evidence and should not be edited to resolve
a knowledge conflict. They remain the immutable record of what was said; editing
them can invalidate evidence hashes and line references. Reviewers make an
explicit decision through the CLI, and the system preserves that decision as an
audit record.

The examples below assume the command is run from this repository, where the
CLI automatically loads `meeting-memory.ini`. From another directory, add
`--config /path/to/meeting-memory.ini` immediately after `meeting-memory`.

### 1. Find the conflicts to review

```bash
# Conflicts first, followed by candidates linked to existing objects and
# candidates whose identity is unclear.
meeting-memory review list
meeting-memory review list --priority conflict

# Optional filters for assigning a smaller review batch.
meeting-memory review list --priority conflict --category processes
meeting-memory review list --priority conflict --source 2026-07-20
meeting-memory review list --priority conflict --limit 20
```

`conflict` is the highest-priority bucket. It means the extractor found a
supported candidate that differs from a possible canonical object and could not
safely update that object automatically. It does not always mean that two
people explicitly contradicted each other. The difference may instead be a
change of scope, status, timing, owner, or level of detail.

Copy the complete `review-...` value shown by `review list`; that is the review
ID used by every later command.

### 2. Inspect one conflict and its evidence

Optionally generate an evidence-grounded first review before inspecting it:

```bash
# One pending review.
meeting-memory review suggest REVIEW_ID --context-lines 5

# Every pending conflict. Failures are isolated per review and recorded in the
# dedicated review-run manifest.
meeting-memory review suggest --priority conflict --all

# Show the latest suggestion whose exact model request still matches current
# repository and evidence state.
meeting-memory review show REVIEW_ID --with-evidence --with-suggestion
```

Suggestions are advisory and append-only. The command writes only
`knowledge-review/suggestions/REVIEW_ID/SUGGESTION_ID.json` and a manifest
under `.knowledge-state/review-runs/`; it never changes a canonical object or
review status. A matching exact request is reused unless `--force` is given.
Use `--suggestion-id SUGGESTION_ID` to inspect a historical suggestion even
after it becomes stale. Phase 1 does not accept suggestions or resolve reviews
automatically; the existing explicit human resolution flow remains the
mutation boundary.

```bash
meeting-memory review show REVIEW_ID --with-evidence
```

Read the output in this order:

1. `Possible existing objects` identifies the canonical object that may be
   affected. If there is more than one, the reviewer must select the correct
   one later with `--existing-id`.
2. `Existing statement` is the curated statement currently used by search and
   context generation.
3. `Candidate statement` is the newly extracted claim.
4. `Diff` shows exactly what wording would change.
5. `Why review is required` explains why automatic reconciliation stopped.
6. `Existing Evidence` and `Candidate Evidence` show the source, inclusive line
   range, and bounded excerpt. Inspect the words actually spoken, not only the
   generated statements.
7. `Candidate metadata` shows status, confidence, owner, and effective date
   when the review was generated by a version that preserves them.

`[STALE]` beside an existing excerpt means that the current source file no
longer has the same fingerprint as the evidence snapshot stored on the
canonical object. It does not itself mean that the candidate contradicts the
existing statement. It tells the reviewer to verify the current excerpt
carefully before promoting evidence.

The human decision should answer:

- Are the existing and candidate claims about the same durable fact?
- Does the raw evidence describe a completed or approved state, or only an
  idea, proposal, question, or future intention?
- Does the evidence support every material addition in the candidate, such as
  a new metric, owner, system, date, or scope?
- Is the candidate newer and explicitly replacing the existing state, merely
  clarifying it, or only confirming it?
- Are two pending reviews actually duplicate representations of the same
  candidate?

If more context is needed, inspect the canonical object and the raw review
record without editing either:

```bash
meeting-memory show EXISTING_OBJECT_ID --with-evidence
meeting-memory review show REVIEW_ID --raw
meeting-memory review show REVIEW_ID --json
```

### 3. Choose one resolution

| Action | Use it when | Effect |
| --- | --- | --- |
| `replace` | The candidate is the new authoritative state and supersedes the canonical wording. | Replaces the canonical statement and candidate metadata, appends the candidate evidence, and resolves the review. |
| `refine` | The candidate is the same fact with a supported clarification or narrower/more precise formulation. | Updates the canonical statement and metadata to the candidate version, appends its evidence, and records that the change was a refinement. |
| `reconfirm` | The new source confirms the existing statement without requiring a wording or metadata change. | Keeps the canonical statement, appends the candidate evidence, and updates its confirmation history. |
| `create-separate` | The candidate is a different durable fact, not a new version of the possible existing object. | Creates a new canonical object from the candidate and resolves the review. |
| `keep-existing` | The canonical object should remain and the candidate should be dropped. | Leaves canonical knowledge unchanged and moves the review to `rejected` with the human rationale. This is the audited “drop” choice. |
| `merge-duplicate` | This pending review duplicates another pending review that should remain for a decision. | Rejects this review as a duplicate and links it to the retained pending review. It does not resolve the retained review. |

Never delete a pending review file to drop a candidate. Use `keep-existing` so
the evidence, reviewer, time, and reason remain traceable.

Write `--note` as a decision record, not as a generic comment. A useful note
states the conclusion, the evidence interpretation, and what is being retained
or changed. For example:

```text
Keep the existing statement. The source discusses this as a proposed approach
and does not establish that the approved policy changed; reject the candidate.
```

### 4. Dry-run the exact decision

Every resolution requires the review ID, action, reviewer, and note. First run
the complete command with `--dry-run`:

```bash
meeting-memory review resolve REVIEW_ID \
  --action refine \
  --reviewer "Rui" \
  --note "The owner confirmed this is an approved refinement." \
  --dry-run \
  --json
```

The dry-run writes nothing. Check its destination status, affected object IDs,
before/after object summaries, and changed paths. In particular, verify that
the affected object is the one selected during the evidence review.

Useful action-specific arguments are:

- `--existing-id OBJECT_ID` to select a canonical target when identity was
  ambiguous;
- `--duplicate-of OTHER_REVIEW_ID` with `merge-duplicate`;
- `--new-id OBJECT_ID` with `create-separate`;
- `--title`, `--status`, `--confidence`, `--owner`, and `--effective-date` to
  review candidate metadata for `replace`, `refine`, or `create-separate`;
- `--clear-owner` or `--clear-effective-date` to explicitly remove those
  values.

Reviews generated by older versions may not contain structured candidate
metadata. Creating a separate object from one of those reviews requires
explicit values such as `--status approved --confidence high`, and may also
require `--title`.

### 5. Apply the reviewed decision

Repeat the identical command without `--dry-run`:

```bash
meeting-memory review resolve REVIEW_ID \
  --action refine \
  --reviewer "Rui" \
  --note "The owner confirmed this is an approved refinement."
```

The command commits the review and any canonical changes together, validates
the repository, and regenerates indexes when canonical knowledge changed.
Accepted actions move the review from `knowledge-review/pending/` to
`knowledge-review/resolved/`. `keep-existing` and `merge-duplicate` move it to
`knowledge-review/rejected/`.

### 6. Verify the result and audit trail

```bash
# The same ID now shows status, action, reviewer, timestamp, note, affected
# object IDs, and whether stale evidence was explicitly allowed.
meeting-memory review show REVIEW_ID --with-evidence

# Confirm that it left the pending queue and appears in the expected history.
meeting-memory review list --priority conflict
meeting-memory review list --status resolved
meeting-memory review list --status rejected

# For actions that changed or reconfirmed canonical knowledge.
meeting-memory show AFFECTED_OBJECT_ID --with-evidence

# Final repository and index consistency check.
meeting-memory validate
```

The review Markdown under `knowledge-review/resolved/` or
`knowledge-review/rejected/` is the durable audit record. It contains the
original comparison and evidence plus the resolution action, reviewer,
timestamp, note, affected object IDs, duplicate link when applicable, and any
stale-evidence override.

### Complete example: keep the cohort-reporting knowledge

The following traces a decision to retain the existing cohort-reporting
statement and drop the broader candidate:

```bash
# 1. Inspect the exact statements, diff, and source excerpts.
meeting-memory review show \
  review-cohort-based-reporting-replace-event-based-views-2026-07-20-d07136e1 \
  --with-evidence

# 2. Preview the audited drop.
meeting-memory review resolve \
  review-cohort-based-reporting-replace-event-based-views-2026-07-20-d07136e1 \
  --action keep-existing \
  --reviewer "Rui" \
  --note "Keep the curated statement. The candidate restates the same transition but adds IV and Data Studio scope without establishing a replacement of the curated object." \
  --dry-run

# 3. Apply the same decision.
meeting-memory review resolve \
  review-cohort-based-reporting-replace-event-based-views-2026-07-20-d07136e1 \
  --action keep-existing \
  --reviewer "Rui" \
  --note "Keep the curated statement. The candidate restates the same transition but adds IV and Data Studio scope without establishing a replacement of the curated object."

# 4. Read back the rejected review record and validate the repository.
meeting-memory review show \
  review-cohort-based-reporting-replace-event-based-views-2026-07-20-d07136e1 \
  --with-evidence
meeting-memory validate
```

### If the review became stale

Resolution is refused when the matched canonical object changed after the
review was created. There is no override for canonical drift because applying
an old decision could overwrite newer curated knowledge. Inspect the current
object, then preview a snapshot refresh:

```bash
meeting-memory review refresh REVIEW_ID --dry-run
```

If the selected object and refreshed statement are correct, apply it and
generate a new suggestion:

```bash
meeting-memory review refresh REVIEW_ID
meeting-memory review suggest REVIEW_ID
meeting-memory review show REVIEW_ID --with-evidence --with-suggestion
```

Refresh changes only the pending review's existing-side statement, evidence,
timestamp, and fingerprint. It preserves the candidate and append-only
suggestion artifacts; prior suggestions become stale. When a review has
multiple possible canonical objects, select the intended snapshot with
`--existing-id`.

Resolution is also refused when candidate evidence no longer matches the source
fingerprint. First run `review show REVIEW_ID --with-evidence` again and inspect
the current source. If a human confirms that the cited current lines still
support the candidate, evidence-promoting actions (`replace`, `refine`,
`reconfirm`, and `create-separate`) may add `--allow-stale-evidence`. The
override is stored in the resolution record. It is not valid with
`keep-existing` or `merge-duplicate`, because those actions do not promote the
candidate evidence.
