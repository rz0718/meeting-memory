# Meeting Memory — Claude Desktop Plugin Plan

Turn the `meeting-memory` Python library into a self-contained Claude Desktop
plugin that non-technical users can install and use quickly, including automatic
nightly ingestion of meeting notes.

## Goal

A single `.mcpb` extension a non-technical user double-clicks to install. After a
one-time consent step, their laptop automatically fetches Google Meet notes +
Slack messages every night, extracts durable knowledge locally, and lets them
query it from inside Claude Desktop by chatting.

## Decisions (locked)

- **Distribution unit:** Claude Desktop extension (`.mcpb`, formerly `.dxt`) —
  one-click install, config via UI form, secrets in the OS keychain. Not a raw
  MCP server (which would require editing `claude_desktop_config.json`).
- **Ingestion host:** each user's own laptop. Every machine fetches its own
  Drive/Slack, runs its own extraction, and serves its own local mirror.
- **Google Drive:** build a real `DriveCollector` (OAuth + Google Docs export to
  Markdown), modeled on the existing `SlackCollector`.
- **Plugin surface:** read-only query tools (`search`, `show`, `context`,
  `status`, `ask`) plus a one-time `setup_nightly` tool. Ingestion is driven by
  a scheduled OS job, not by Claude calling a tool.

## Key constraints (why the design looks like this)

1. **MCP plugins are not background daemons.** Claude Desktop launches the MCP
   server only while a conversation uses it, then it idles. It cannot "wake up
   at 2am." Nightly scheduling must come from an OS scheduler (launchd / Task
   Scheduler / cron), not from the plugin process.
2. **No Google Drive code exists today.** Meeting notes are currently expected to
   already be Markdown files in `meetings_dir/<date>/`. Only Slack has an
   automated collector. Drive fetching is net-new work.
3. **Extraction runs when Claude Desktop may be closed.** The nightly job calls
   the LLM (OpenRouter) directly, so each laptop needs its own OpenRouter key —
   Claude Desktop's own model cannot be reused for the batch extraction step.
4. **Google restricted scopes need verification.** Reading arbitrary Meet notes
   requires the broad `drive.readonly` scope. Google requires app verification
   (and possibly a security assessment) above 100 users; otherwise users see an
   "unverified app" warning. Mitigation: confine notes to a single named Drive
   folder and use the narrowest scopes possible. **This is the highest-risk item
   and must be confirmed before Phase 3.**

## Architecture

Everything runs on each user's laptop:

```
Claude Desktop
  └─ Meeting Memory plugin (.mcpb)
       ├─ query tools (read-only) ──────► local mirror ◄──┐
       └─ "set up nightly sync" tool ──┐                  │
                                       ▼                  │
     launchd / Task Scheduler / cron (registered once)    │
                                       │                  │
                                       ▼ nightly           │
     sync-drive (NEW) + sync-sources (Slack) + process-pending ─► writes mirror
```

The plugin never fetches or extracts inline; it reads the locally generated
knowledge mirror. A scheduled OS job does all ingestion. A "lazy catch-up" check
in the plugin self-heals nights the laptop was asleep.

## Component 1 — Read-only query plugin (mostly reuse)

- New module `src/meeting_memory/knowledge/mcp_server.py` using the official
  `mcp` Python SDK (stdio transport).
- Tools map 1:1 to existing library functions and return the JSON payloads that
  already exist:

  | MCP tool | Wraps | Needs OpenRouter key? |
  |----------|-------|-----------------------|
  | `search`  | `search_documents` / `search_payload`      | No  |
  | `show`    | `exact_document` / `show_payload`          | No  |
  | `context` | `build_context_packet`                     | No  |
  | `status`  | `KnowledgePipeline.status`                 | No  |
  | `ask`     | `OpenRouterAnswerer`                       | Yes |

- Opens the repository with `require_evidence_sources=False` (detached mirror).
- New console entry point `meeting-memory-mcp` in `setup.cfg`, and an `mcp`
  extra under `[options.extras_require]`.
- `manifest.json` (MCPB) declares the tools and a `user_config` form:
  mirror/output folder path + OpenRouter API key (sensitive → keychain).
- Package: `npx @anthropic-ai/mcpb pack` → `meeting-memory.mcpb`.

## Component 2 — Nightly scheduler (per-OS)

- Add a `setup_nightly` MCP tool so the user triggers install once from chat
  ("set up my nightly sync"). It registers an OS job:
  - macOS → launchd plist in `~/Library/LaunchAgents`
  - Windows → `schtasks` Task Scheduler entry
  - Linux → cron / systemd timer
- The scheduled job runs `sync-drive && sync-sources && process-pending`,
  reusing the logic already in `scripts/run_daily_knowledge.sh`.
- **Lazy catch-up safety net:** on the first tool call each day, the plugin
  checks whether the mirror is stale (> ~20h) and kicks off a background
  catch-up sync so a missed night self-heals.

## Component 3 — Google Drive collector (the bulk of the work)

- New `DriveCollector` with the same shape as `SlackCollector`:
  `sync(start_date, end_date) -> writes meetings_dir/<date>/meet-*.md`.
- New CLI command `sync-drive` wired into `cli.py` and the nightly job.
- OAuth: installed-app loopback flow. Ship one Google Cloud OAuth client ID
  (Desktop type); each user consents once in-browser; cache the refresh token
  locally (chmod 600 or OS keychain).
- Fetch: list Meet-generated notes/Docs in the date window, export Google Docs
  to Markdown, write per-date source files.
- Dependencies: `google-auth-oauthlib` + `google-api-python-client` (a departure
  from the current near-stdlib footprint, but hand-rolling OAuth is not worth it).
- Tests modeled on `tests/test_slack_sources.py`.
- **Blocked on the scope decision:** confirm expected user count and whether
  Meet notes can be confined to a single Drive folder, to decide whether we can
  avoid Google's restricted-scope verification.

## Secrets & config (per laptop)

- OpenRouter API key — collected by the `.mcpb` `user_config` form → keychain.
- Google refresh token — produced by the OAuth consent flow → local file/keychain.
- Slack bot token (optional) — existing `meeting-memory.ini` / env mechanism.
- Output/mirror path — `user_config` folder picker.

### Storage paths: add defaults for the plugin

Today the paths are **required with no defaults**: the config loader auto-discovers
the INI file (`MEETING_MEMORY_CONFIG` env → `./meeting-memory.ini` → repo-root
`meeting-memory.ini`, see `configuration.py:15-30`), but `required_storage_paths`
raises `ConfigurationError` if either `meetings_dir` or `output_dir` is blank
(`configuration.py:64-75`). That is fine for a developer INI but a stumbling block
for one-click, non-technical users.

For the plugin, **pre-fill sensible OS-default paths** in the `user_config` form so
install is zero-config — the user can just click through:

- macOS → `~/Library/Application Support/MeetingMemory`
- Windows → `%APPDATA%\MeetingMemory`
- `output_dir` = that app-data dir; `meetings_dir` = a `meetings/` subfolder under it.

(There is already a latent `root/"meetings"` fallback in `KnowledgeRepository`
when `meetings_dir` is `None` at `repository.py:54-58`, but the CLI/runner never
hits it because it always passes both paths explicitly. The plugin should supply
resolved defaults rather than rely on that fallback.)

## Phasing

1. **Plugin shell** — `mcp_server.py` + `manifest.json` + entry point over
   existing data. Low-risk, proves the UX, produces a working `.mcpb`. (~1 day)
2. **Scheduler** — `setup_nightly` tool + lazy catch-up around the existing
   daily-run logic. (~1 day)
3. **Drive collector** — OAuth + Docs export + `sync-drive` + tests. Bulk of the
   effort; gated on the scope/verification decision. (~3–5 days)

## Open questions to resolve before Phase 3

- How many users will install this (drives the Google verification burden)?
- Can Meet notes be confined to a single, named Drive folder so we can use
  narrow scopes instead of `drive.readonly`?

## Ingestion trigger: three options considered

The core design question is *how ingestion is triggered* and *who fetches the
data*. Three viable shapes, from "most build, fully automatic" to "least build,
user-triggered":

### Option A — Own-fetch + OS scheduler (the current plan)

The plugin ships its own `DriveCollector` (OAuth) + `SlackCollector`, and an OS
job (launchd / schtasks / cron) runs `sync-drive && sync-sources &&
process-pending` nightly. Extraction uses OpenRouter.

- ✅ **Fully automatic and unattended** — runs even when Claude Desktop is closed.
- ✅ Deterministic: guaranteed to fire on schedule.
- ❌ Most to build: Drive OAuth, `sync-drive`, per-OS scheduler code.
- ❌ Carries the Google restricted-scope **verification risk** (constraint #4).

### Option B — Connector-driven ingestion (`ingest` tool)

Skip the collectors. Claude, in-conversation, uses **Claude Desktop's official
Google Drive + Slack connectors** to read notes, then calls a new plugin tool
`ingest(source, date, text)` that persists + extracts into the local mirror.

- ✅ **Deletes the biggest, riskiest work**: no `DriveCollector`, no Google OAuth
  client, no restricted-scope verification. Anthropic's connector owns the fetch.
- ✅ Extraction can be done by Claude itself while it's reading the docs —
  removes the separate OpenRouter dependency too. Plugin shrinks to
  store + index + query.
- ❌ **Not deterministic and not unattended.** The official connectors are
  *model-invoked, in-conversation* tools: only the agent can call them, only
  while the app is open, only when triggered. No out-of-model actor (cron, or
  even a hook) can invoke them.
- ❌ Cost/scale: reading many docs each session burns Claude's context window and
  tokens. Fine for a few daily meetings, poor for bulk backfill.
- ❌ Needs incremental tracking so each session doesn't re-read everything.

### Option C — Connector-driven + session-start nudge

Option B, plus a staleness check that, on first plugin use each day, injects an
instruction telling Claude to run the connector sync. Gets *close* to reliable
without OAuth/verification — but the trigger is still a prompt the model
*usually* obeys, not a guarantee.

### Can a hook make Option B deterministic? (verified — no)

Determinism has two layers: **(1) trigger** ("does something fire reliably?")
and **(2) fetch** ("does the official connector actually get invoked?"). A hook
can help layer 1 but **cannot touch layer 2**, because the official connectors
are model-invoked and a hook runs code *outside* the model — it has no handle to
them (same reason a cron job can't call them). The non-determinism in Option B
lives entirely in layer 2, so a hook does not remove it. The most a hook-style
mechanism buys is Option C's soft nudge.

Two hard platform facts confirm this (verified July 2026 against the official
docs):

- **Hooks are a Claude *Code* feature**, not Claude Desktop. They fire on
  `SessionStart` / `PreToolUse` / `Stop` etc. via `settings.json`. This project
  targets Claude Desktop (`.mcpb`), a different product, for non-technical
  one-click users — Claude Code is a developer CLI, the wrong audience.
- **The `.mcpb` manifest has no lifecycle/scheduling fields.** It is purely a
  launch descriptor (how to spawn the stdio server, tools, `user_config`,
  `compatibility`). Claude Desktop spawns the server as a subprocess only while a
  conversation uses it; there is no on-start / scheduled / background trigger.

Sources:
[Desktop extensions (MCPB) build guide](https://claude.com/docs/connectors/building/mcpb),
[MCPB manifest spec](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md),
[Desktop Extensions blog](https://www.anthropic.com/engineering/desktop-extensions).

### Decision guidance

- Need **guaranteed automatic, app-closed** ingestion → **Option A** (current
  plan). Deterministic fetch fundamentally requires owning the fetch + an OS
  schedule.
- Users open Claude Desktop most workdays and daily volume is small → **Option B
  (or C)** is attractive: it cuts the hardest ~3–5 days (Drive OAuth +
  verification) down to a small `ingest` tool, at the cost of the guarantee.
- A **hybrid** is reasonable: Option B/C as the primary path, keep the lazy
  staleness check to nudge the user when the mirror is stale.

### Chosen direction: Option C (connector-driven + session nudge)

> Full implementation spec: [`option-c-design.md`](./option-c-design.md).

Rationale: the target users are non-technical and **live in Claude Desktop all
day**, which neutralizes Option C's main weakness (only runs while the app is
open). This trades Option A's "guaranteed automatic" for a much smaller build and
no Google restricted-scope verification.

**Key reframe:** determinism moves from the *trigger* to the *workflow*. The
trigger stays soft (a session-start nudge), but the *content* of the sync is made
deterministic by a Skill that scripts the exact steps. This works because the
plugin cannot call the connectors itself — only Claude can — so Claude
orchestrates and the plugin plays a supporting role.

Division of labor:

- **Claude (agent)** — fetches via the official Google Drive + Slack connectors,
  reads each document, **and does the extraction itself** (no OpenRouter key on
  the laptop; extraction quality rides on the conversation model).
- **Plugin (MCP tools)**:
  - `sync_status` — is the mirror stale? returns the ingest watermark and the set
    of already-seen source IDs (Drive `fileId`+revision, Slack `ts`) so Claude
    fetches **only the delta** — this is what keeps token/context cost low.
  - `store(source, date, knowledge)` — persist the extracted knowledge Claude
    produced for one document into the local mirror + index. Idempotent per
    source ID.
  - existing read tools (`search` / `show` / `context` / `status` / `ask`) —
    unchanged from Component 1.
- **A Skill (`sync-meetings`)** — encodes the workflow so every sync is identical:
  call `sync_status` → list recent Meet docs in the named Drive folder since the
  watermark → for each new/changed doc: read → extract → `store` → read the
  relevant Slack channels since the watermark → extract → `store`.

Session nudge: on the first read-tool call each day, tool responses prepend a
staleness banner ("meeting memory is N days stale — want me to run
`sync-meetings`?") so Claude proactively offers to sync.

What this removes vs Option A: `DriveCollector`, the Google OAuth client, the
restricted-scope verification (constraint #4), the per-OS scheduler (Component 2),
**and** the per-laptop OpenRouter key (constraint #3). What it keeps: it is not
truly unattended — if the user never opens Claude Desktop, nothing ingests
(acceptable, since they don't need the memory when they're not working).

Revised phasing under this direction:

1. **Plugin shell** — `mcp_server.py` with read tools + `store` + `sync_status`
   over the existing mirror. (unchanged from Phase 1)
2. **`sync-meetings` Skill + staleness nudge** — replaces the OS-scheduler phase.
   No launchd/schtasks/cron.
3. **Drive/Slack collectors — dropped** in this direction (the connectors do the
   fetch). Keep the existing `SlackCollector`/`DriveCollector` path only if a
   fully-unattended fallback (Option A) is later wanted.

## Non-goals

- No hosted/multi-tenant backend (each laptop is self-contained).
- No shared maintainer-run mirror (ingestion is per-user).
- No write/edit tools exposed through the plugin (read-only query surface).
