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
excluded_user_ids =
  U0123456789
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
- `channel_ids`: comma-, whitespace-, or newline-separated public/private channel IDs;
- `excluded_user_ids`: optional comma-, whitespace-, or newline-separated Slack
  user IDs whose messages are omitted from snapshots and extraction. Thread
  discovery still happens before filtering, so replies from other users remain.

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
after it becomes stale. Suggestion generation itself never resolves a review;
Phase 2 lets a human accept or override a current artifact through the same
deterministic resolution boundary.

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
  --reviewer "Reviewer Name" \
  --note "The owner confirmed this is an approved refinement." \
  --dry-run \
  --json
```

To accept the AI action and its validated parameters exactly, replace
`--action` and action-specific arguments with:

```bash
meeting-memory review resolve REVIEW_ID \
  --suggestion-id SUGGESTION_ID \
  --accept-suggestion \
  --reviewer "Reviewer Name" \
  --note "Checked the cited evidence and approve the proposed result." \
  --dry-run
```

To modify the recommendation, keep `--suggestion-id` but provide your explicit
`--action` and options. The durable audit will preserve the suggested action,
the final action, and mark the suggestion as `overridden`.

For a guided filtered batch, use:

```bash
meeting-memory review triage --priority conflict --reviewer "Reviewer Name"
```

Triage shows the evidence and current suggestion, offers accept, override,
defer, or quit, displays the deterministic dry-run, and asks before applying.
One failed case does not undo completed earlier decisions.

The dry-run writes no review or canonical changes. Check its destination status, affected object IDs,
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
  --reviewer "Reviewer Name" \
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
stale-evidence override. When a suggestion informed the decision, it also
records the suggestion ID, suggested action, final disposition (`accepted` or
`overridden`), and `hybrid` resolution mode. Direct decisions remain
`human`/`not_used`.

All Meeting Memory writers share a repository-scoped mutation lock. Apply
reloads and verifies suggestion, review, canonical, duplicate-review, and
evidence inputs while holding that lock; commit checks expected byte digests or
expected absence before its first write. A later apply repeats every check
performed by a dry-run. The lock coordinates Meeting Memory processes, while
commit-time preconditions protect against direct external edits that do not
honor the advisory lock.

### Complete example: retain existing knowledge

The following traces a decision to retain an existing statement and reject an
unsupported candidate:

```bash
# 1. Inspect the exact statements, diff, and source excerpts.
meeting-memory review show \
  REVIEW_ID \
  --with-evidence

# 2. Preview the audited drop.
meeting-memory review resolve \
  REVIEW_ID \
  --action keep-existing \
  --reviewer "Reviewer Name" \
  --note "Keep the curated statement. The evidence does not establish that the candidate replaces the existing state." \
  --dry-run

# 3. Apply the same decision.
meeting-memory review resolve \
  REVIEW_ID \
  --action keep-existing \
  --reviewer "Reviewer Name" \
  --note "Keep the curated statement. The evidence does not establish that the candidate replaces the existing state."

# 4. Read back the rejected review record and validate the repository.
meeting-memory review show \
  REVIEW_ID \
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

## Local review UI

`meeting-memory ui` serves a two-tab web UI on `127.0.0.1:8787` for the morning
routine: see what last night's run inserted, then work the review queue with AI
suggestions a human can modify and decide.

```bash
python3 -m pip install -e '.[ui]'
meeting-memory ui
# Optional: a different port, reviewer, or review model.
meeting-memory ui --port 9000 --reviewer rui --model google/gemini-3.6-flash
```

The server binds to loopback and has no authentication, because it exposes every
write path below. Binding elsewhere with `--host` prints a warning; do not do it
on a shared machine.

**Today's Knowledge** renders a run manifest grouped by what changed — created,
refined, reconfirmed, sent to review — with evidence excerpts one click away at
their real source line numbers. Its two actions are the audited paths the CLI
already provides: merge into another object, and flag for removal. Flagging adds
an ID to a session basket; previewing writes the exact newline-delimited ID
inventory and reports the count and SHA-256 that the apply call asserts, so only
the bytes you approved can execute.

**Review queue** is `review triage` as a screen, with the same gates. It shows
existing and candidate side by side with a word-level diff, the AI suggestion as
a comment thread, and a form for the decision. Selecting the suggested action
and touching nothing sends `--accept-suggestion` and is recorded as `accepted`;
touching any radio or field sends the same `--suggestion-id` as an explicit
override and is recorded as `overridden`. A badge states which one the audit
trail will say before you apply.

Every write goes through `ReviewResolver`, `ReviewRefresher`, `KnowledgeMerger`,
or `KnowledgeRemover` — the same objects the CLI wraps — and every write is
gated on a dry run whose preview you have seen. The server refuses an apply
whose arguments differ from the previewed decision. There is no bulk accept, no
inline auto-save, and no undo: reversing a decision is a new audited action.

Canonical drift replaces the action panel with a guided refresh (`refresh
--dry-run` → apply → regenerate the suggestion), because the resolver refuses
drifted resolutions by design and there is no override. Drifted candidate
evidence shows an `Allow stale evidence` checkbox only for the four
evidence-promoting actions, with the override recorded permanently.

Keyboard: `1`/`2` switch tabs, `j`/`k` move through the queue, `a` accepts and
previews, `o` focuses the action radios, `d` defers, `Enter` applies from an
open preview, and `⌘K` / `Ctrl+K` jumps to a review or object by ID or title.

Raw meeting and Slack notes are never editable or writable from the UI.
