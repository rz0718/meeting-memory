# Option C — Connector-Driven Ingestion: Implementation Design

Detailed design for the chosen direction in [`plugin-plan.md`](./plugin-plan.md):
Claude Desktop's **official** Google Drive + Slack connectors do the fetching,
Claude does the extraction, and a small MCP plugin persists everything into the
existing local knowledge mirror. A `sync-meetings` Skill scripts the workflow so
each sync is identical.

## 1. Guiding principle: reuse the pipeline, replace only the extractor

The existing pipeline is already a clean fit. `KnowledgePipeline.process_dates()`
(`pipeline.py:437`) does all the hard work — dedup, reconciliation, review-queue
routing, atomic commit, run manifests — and it depends on a pluggable
`KnowledgeExtractor.extract(source) -> List[KnowledgeCandidate]` boundary
(`extractors.py:21`). Today that boundary is implemented by `OpenRouterExtractor`
(calls an LLM) or `FakeExtractor` (tests).

**Option C adds one more implementation: `SuppliedExtractor`, fed by candidates
the agent produced.** Everything downstream of extraction is reused verbatim. No
changes to reconciliation, storage, models, search, or the on-disk contract.

Two facts from the code drive the whole design:

1. **Notes must be on disk before extraction.** `process_dates` discovers inputs
   via `qualifying_sources()` which reads `meetings_dir/<date>/*.md`
   (`repository.py:125-146`), and `_complete_evidence` (`pipeline.py:100-120`)
   opens each cited source to compute its SHA-256, derive `observed_at` from the
   `meetings/<date>/…` path, and validate that evidence line numbers exist. So the
   raw fetched note has to be written to `meetings_dir/<date>/` *first*, and the
   agent's evidence must reference **real 1-based line numbers** of that file.
2. **Dedup is SHA-256 + version based.** `source_needs_processing`
   (`pipeline.py:66-79`) re-processes a note only if its hash, the extractor
   version, or the schema version changed, or the last result was not success.
   This is exactly the "delta-only" watermark the sync needs — it already exists.

## 2. Component inventory (what gets built)

| # | Artifact | Type | Reuse vs new |
|---|----------|------|--------------|
| 1 | `src/meeting_memory/knowledge/agent_extractor.py` — `SuppliedExtractor` | new (~30 lines) | implements existing `KnowledgeExtractor` ABC |
| 2 | `src/meeting_memory/knowledge/mcp_server.py` — stdio MCP server + tools | new | wraps existing pipeline / repository / search |
| 3 | `plugin/manifest.json` — MCPB manifest | new | declares tools + `user_config` |
| 4 | `plugin/skills/sync-meetings/SKILL.md` — the workflow skill | new | scripts connector→ingest→extract→store |
| 5 | `plugin/icon.png` | new | cosmetic |
| 6 | `meeting-memory-mcp` console entry point + `[mcp]` extra | edit `setup.cfg` | adds `mcp` SDK dep |
| 7 | `tests/test_agent_extractor.py`, `tests/test_mcp_server.py` | new | modeled on `test_slack_sources.py` |
| 8 | `meeting-memory.mcpb` — packaged bundle | build output | `npx @anthropic-ai/mcpb pack` |

Explicitly **not built** (dropped vs Option A): `DriveCollector`, Google OAuth
client, `sync-drive` CLI, the per-OS scheduler, and the per-laptop OpenRouter
key. The official connectors and Claude itself replace all of these.

## 3. The `SuppliedExtractor`

```python
# agent_extractor.py
class SuppliedExtractor(KnowledgeExtractor):
    """Serves candidates the agent produced, keyed by source path.

    Reuses the entire pipeline: process_dates() calls extract(source) per note,
    exactly as it does for OpenRouterExtractor.
    """
    version = "agent-1"  # participates in dedup; bump to force re-extraction

    def __init__(self, candidates_by_source: dict[str, list[KnowledgeCandidate]]):
        self._by_source = candidates_by_source

    def extract(self, source: MeetingSource) -> list[KnowledgeCandidate]:
        if source.relative_path not in self._by_source:
            # Safety: a note on disk that the agent did NOT supply candidates for
            # must NOT be silently marked "processed, zero knowledge". Raising makes
            # the pipeline record it as failed, leaving it pending for a later store.
            raise ExtractionError("no candidates supplied for %s" % source.relative_path)
        return self._by_source[source.relative_path]
```

That raise is the single most important correctness detail (see §7).

## 4. MCP tool surface

Five tools. Read tools return the same JSON payloads the library already emits;
write tools are thin wrappers over the pipeline.

| Tool | Purpose | Wraps | Writes? |
|------|---------|-------|---------|
| `sync_status` | staleness + what's already ingested (the delta watermark) | `KnowledgePipeline.status`, `repository.iter_source_states`, `latest_successful_run` | no |
| `ingest_note` | persist one fetched note to `meetings_dir/<date>/`; return it line-numbered | `repository.commit` (single file) | yes |
| `store_candidates` | run the pipeline for a date using `SuppliedExtractor` | `KnowledgePipeline.process_dates` | yes |
| `search` | keyword search over extracted knowledge | `search_documents` / search payload | no |
| `show` / `context` | exact object / context packet for a question | `document_from_object`, `build_context_packet` | no |

Note: the plan's `ask` tool (OpenRouter answerer) is **dropped** — the agent
itself is the answerer, so it calls `search`/`context` and composes the answer in
the conversation. That removes the last OpenRouter dependency.

### 4.1 `sync_status()`
Returns, e.g.:
```json
{
  "mirror_stale": true,
  "hours_since_last_run": 27.4,
  "watermark_date": "2026-07-21",
  "ingested_sources": [
    {"source_path": "meetings/2026-07-21/meet-planning.md",
     "source_sha256": "…", "source_date": "2026-07-21", "result": "success"}
  ],
  "open_review_item_count": 2,
  "knowledge_object_count_by_category": {"decisions": 14, "...": 0}
}
```
The agent uses `ingested_sources` (path + sha256) to fetch **only new/changed**
documents — this is what keeps token cost bounded.

### 4.2 `ingest_note(source_date, source_name, markdown, kind="meet")`
- Validates `source_date` is `YYYY-MM-DD` and `source_name` is a safe filename;
  normalizes to `meet-<slug>.md` / `slack-<slug>.md` (avoiding names
  `qualifying_sources` skips — `*.transcript.md`, `*standup*`, `*interview*`,
  `repository.py:130-135`).
- Writes `meetings_dir/<date>/<name>` via `repository.commit` (atomic).
- Returns `{source_path, source_sha256, already_ingested, line_numbered_content}`.
  - `already_ingested` is true when the on-disk hash is unchanged **and** its
    source-state result is `success` → the agent skips extraction for it.
  - `line_numbered_content` is the note rendered as `N: <line>` so the agent can
    cite accurate 1-based evidence line ranges (mirrors what
    `OpenRouterExtractor._prompt` feeds its model, `extractors.py:135`).

### 4.3 `store_candidates(source_date, candidates_by_source)`
- `candidates_by_source`: `{ "meetings/<date>/meet-x.md": [candidate, …], … }`,
  where each candidate is the JSON shape validated by `KnowledgeCandidate.from_dict`
  (`models.py:142-190`): `category, title, statement, status, effective_date,
  owner, confidence, reason_for_durability, evidence[{source, anchor, line_start,
  line_end}]`. (`source_sha256` / `observed_at` are filled in by the pipeline.)
- Builds `SuppliedExtractor(candidates_by_source)` and runs
  `KnowledgePipeline(repo, extractor).process_dates([source_date])`.
- Returns the run manifest summary: created / reconfirmed / refined IDs, review
  items created, candidates rejected, files written, and run status.

## 5. The `sync-meetings` Skill (the deterministic workflow)

The Skill is where determinism lives — it fixes the *content* of every sync even
though the *trigger* is a soft nudge. Steps:

1. Call `sync_status`. If not stale, stop and report "up to date".
2. Use the **Google Drive connector** to list Meet-generated notes/Docs in the
   configured folder dated after `watermark_date`, skipping any `source_path`
   already in `ingested_sources` with an unchanged hash.
3. Use the **Slack connector** to pull configured channels since `watermark_date`.
4. For each new/changed document, **grouped by date**:
   a. `ingest_note(...)` → receive `line_numbered_content`.
   b. If `already_ingested`, skip.
   c. Otherwise extract durable-knowledge candidates from the line-numbered
      content, following the same rules as `OpenRouterExtractor._prompt`
      (`extractors.py:100-136`): only durable facts, correct category/status/
      confidence enums, real line-anchored evidence, no invented approvals/dates.
5. Per date, call `store_candidates(date, {all notes ingested this session for
   that date})`.
6. Summarize what changed from the returned manifests (created / refined / needs
   review).

Session nudge: on the first read-tool call each day, tool responses prepend a
staleness banner so the agent proactively offers to run `sync-meetings`.

## 6. End-to-end flow

```
Claude Desktop (session open)
  │  user: "what did we decide about the pricing model?"  (or a daily nudge)
  ▼
sync-meetings Skill
  ├─ sync_status() ─────────────► plugin ► repository state  (stale? watermark?)
  ├─ Drive connector  ─┐
  ├─ Slack connector  ─┤ fetch only the delta
  │                    ▼
  ├─ ingest_note() ──► plugin ► writes meetings_dir/<date>/meet-*.md, returns
  │                              line-numbered text
  ├─ (Claude extracts candidates from that text)
  └─ store_candidates() ─► plugin ► KnowledgePipeline.process_dates()
                                     ├─ SuppliedExtractor feeds candidates
                                     ├─ reconcile + dedup (unchanged)
                                     └─ atomic commit ► knowledge/, review/, state/,
                                                        run manifest, per-date report
  ▼
Claude answers from search/context over the freshly updated mirror
```

## 7. Correctness: why `SuppliedExtractor` raises for un-supplied notes

`process_dates([date])` iterates **every** qualifying note on disk for that date,
not just the ones passed in. If the agent ingested notes A and B but calls
`store_candidates` with candidates for A only, a naive extractor returning `[]`
for B would let the pipeline mark B "processed, success, zero knowledge" and write
a success source-state for it (`pipeline.py:596-606`) — permanently hiding B from
future syncs (dedup would skip it). Raising instead routes B through the
per-source failure path (`pipeline.py:565-588`): B is recorded failed, no success
state is written, and it stays pending until the agent supplies its candidates.
The Skill's rule "extract every note you ingested this session before calling
store" keeps this from happening, and the raise is the backstop.

## 8. Artifacts generated

### 8.1 Build/repo artifacts (checked in or built once)
- Source: `agent_extractor.py`, `mcp_server.py`.
- Packaging: `plugin/manifest.json`, `plugin/icon.png`,
  `plugin/skills/sync-meetings/SKILL.md`; `setup.cfg` gains the
  `meeting-memory-mcp` entry point and an `[mcp]` extra.
- Tests: `tests/test_agent_extractor.py`, `tests/test_mcp_server.py`.
- Distributable: `meeting-memory.mcpb` (from `npx @anthropic-ai/mcpb pack`).

### 8.2 Runtime artifacts (written on the user's laptop during a sync)
All under the two configured roots (`meetings_dir`, `output_dir`), i.e. the
existing mirror layout from `repository.py:59-64`:

| Path | Written by | Contents |
|------|-----------|----------|
| `meetings_dir/<date>/meet-*.md`, `slack-*.md` | `ingest_note` | raw fetched notes (the evidence sources) |
| `output_dir/knowledge/<category>/<id>.md` | `store_candidates` | extracted durable-knowledge objects |
| `output_dir/knowledge-review/pending/<id>.md` | `store_candidates` | items needing human review |
| `output_dir/.knowledge-state/sources/<slug>-<hash>.json` | `store_candidates` | per-source dedup/watermark state |
| `output_dir/.knowledge-state/runs/<run_id>.json` | `store_candidates` | run manifest (audit) |
| `output_dir/outputs/Durable-Knowledge/durable-knowledge-<date>.md` | `store_candidates` | per-date human-readable change report |

Secrets footprint shrinks dramatically vs Option A: **no OAuth refresh token file,
no OpenRouter key** — the only `user_config` value is the mirror folder path
(with an OS default per the plan's config section). Connector auth lives inside
Claude Desktop.

## 9. Failure & catch-up behavior
- **Missed days**: no scheduler, so ingestion happens next time the user is in a
  session. The `sync_status` staleness check surfaces the gap and the nudge
  offers to backfill from `watermark_date`.
- **Partial sync** (session closed mid-run): any note that reached
  `store_candidates` is committed atomically; un-stored notes remain on disk but
  pending (their source-state is absent or failed), so the next sync retries them.
- **Bad candidate** (e.g. evidence line out of range): `_complete_evidence`
  raises, the pipeline records that source failed, and the manifest surfaces it —
  the agent can re-extract with corrected line numbers.

## 10. Testing
- `SuppliedExtractor`: supplied vs missing source (raise), version bump forces
  reprocessing.
- `store_candidates`: reuse `FakeExtractor`-style fixtures from
  `tests/test_slack_sources.py`; assert created/reconfirmed/refined/review paths
  and idempotency (second identical store is a no-op via dedup).
- `ingest_note`: filename normalization, `already_ingested` short-circuit,
  line-numbered rendering.
- `mcp_server`: tool schema round-trips; read tools return the existing payloads.

## 11. Open risks / to confirm
- **Connector coverage**: confirm the Drive connector actually surfaces
  Meet-generated notes in a listable folder, and the Slack connector exposes
  channel history, at the granularity the Skill assumes.
- **Line-number fidelity**: the agent must cite evidence line numbers matching the
  ingested file. Returning `line_numbered_content` from `ingest_note` is the
  mitigation; needs validation that the agent reliably uses it.
- **Cost at volume**: acceptable for a handful of meetings/day; a large backfill
  reads many docs into context. Bound backfill by `watermark_date` and, if needed,
  cap documents per session.
