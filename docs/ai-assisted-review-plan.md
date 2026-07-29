# AI-Assisted Knowledge Review Plan

## Status

Design plan only. No commands or schemas described here are implemented yet.
Revised after a design review against the existing resolver, repository, and
model contracts.

## Goal

Use an LLM to perform the first evidence review for every pending knowledge
conflict, then let a human approve, override, comment, or defer before the
existing deterministic resolver changes canonical knowledge.

The system should support three operating modes:

1. **Advisory:** the AI generates suggestions only.
2. **Hybrid:** the AI suggests; a human comments and explicitly resolves.
3. **Conservative automation:** a policy may resolve a small allowlist of
   mechanically safe cases after independent AI verification and all
   deterministic safeguards pass.

Hybrid is the default and the first mode to ship.

## Guiding decisions

- The LLM is an untrusted adviser, not the component that writes canonical
  knowledge.
- `ReviewResolver` remains the only mutation boundary.
- Suggestions are append-only audit artifacts and are never written into raw
  meeting or Slack evidence.
- Human approval or override records both the AI recommendation and the final
  human decision.
- Existing canonical and evidence freshness checks remain mandatory.
- `--allow-stale-evidence` is never selected automatically.
- Fingerprint checks that guard a mutation are performed by
  `ReviewResolver.resolve` while holding the repository mutation lock, with
  path preconditions rechecked inside `KnowledgeRepository.commit`. A check
  performed only by the caller before entering that locked path is not a
  guarantee.
- The suggestion contract is never looser than the resolver contract. Anything
  `resolve` would reject must fail at suggestion-validation time.
- The provider integration remains model-neutral. Reuse
  `OpenRouterChatClient`, with a Claude model configurable through OpenRouter
  when desired.
- Do not make Claude Code CLI a Phase 1 runtime dependency. A future
  `ClaudeCodeReviewer` adapter can implement the same interface, but a
  subprocess tied to local login state, permissions, and CLI output versions is
  less portable and auditable than the current structured OpenRouter transport.

## Target workflow

```text
Raw meeting and Slack evidence
              |
              v
      Pending review queue
              |
              v
  AI evidence review and suggestion
              |
              v
 Human accept / override / comment / defer
              |
              v
      Deterministic dry-run
              |
              v
         Resolution
              |
              v
 Complete AI + human audit record
```

## CLI design

### Generate one suggestion

```bash
meeting-memory review suggest REVIEW_ID
```

The command is read-only with respect to reviews and canonical knowledge. It
may write only the suggestion artifact and a suggestion-run manifest.

Useful options:

```bash
meeting-memory review suggest REVIEW_ID \
  --model anthropic/claude-model \
  --context-lines 5 \
  --json
```

### Generate suggestions for all conflicts

```bash
meeting-memory review suggest \
  --priority conflict \
  --all
```

Batch filters should match `review list`:

- `--priority`
- `--reason`
- `--category`
- `--existing-id`
- `--source`
- `--limit`

Batch behavior:

- process each review independently;
- reuse a current suggestion only when its exact provider-request
  `input_fingerprint` matches, unless `--force` is supplied;
- continue after an individual provider or validation failure;
- emit a run manifest containing succeeded, reused, and failed review IDs;
- return a non-zero status if any requested review failed.

### Inspect the suggestion with the conflict

```bash
meeting-memory review show REVIEW_ID \
  --with-evidence \
  --with-suggestion
```

When multiple suggestions exist, `--with-suggestion` selects the latest current
suggestion. An exact suggestion can be selected with:

```bash
meeting-memory review show REVIEW_ID \
  --with-evidence \
  --suggestion-id SUGGESTION_ID
```

The display should include:

- suggested action or `human_required`;
- confidence;
- model and prompt version;
- material differences found;
- evidence assessment;
- rationale;
- proposed resolution note;
- risks and blockers;
- whether the suggestion is current;
- deterministic automatic-eligibility result.

### Accept an AI suggestion

```bash
meeting-memory review resolve REVIEW_ID \
  --suggestion-id SUGGESTION_ID \
  --accept-suggestion \
  --reviewer "Reviewer Name" \
  --note "Confirmed. The cited evidence supports the suggested decision." \
  --dry-run
```

After inspecting the dry-run, repeat without `--dry-run`.

`--accept-suggestion` supplies the action and action parameters from the
suggestion. A human `--note` remains required; AI-generated text is retained as
the proposed note but is not silently treated as the human's comment.

### Override an AI suggestion

```bash
meeting-memory review resolve REVIEW_ID \
  --suggestion-id SUGGESTION_ID \
  --action replace \
  --reviewer "Reviewer Name" \
  --note "Override: Treasury confirmed that the new target is approved." \
  --dry-run
```

Supplying `--suggestion-id` with an explicit action records the disposition as
`overridden`. All existing action-specific options continue to work.

### Interactive hybrid review

```bash
meeting-memory review triage --priority conflict
```

For each selected review:

1. show the existing statement, candidate, diff, and evidence;
2. show or generate the current AI suggestion;
3. prompt for accept, override, defer, or quit;
4. collect the human note;
5. display the deterministic resolution dry-run;
6. require confirmation before applying;
7. continue to the next case.

Interactive mode is a wrapper around `suggest`, `show`, and `resolve`; it must
not contain a second resolution implementation.

### Conservative automated mode

```bash
meeting-memory review auto \
  --priority conflict \
  --policy conservative \
  --dry-run
```

Applying changes requires a separate explicit flag:

```bash
meeting-memory review auto \
  --priority conflict \
  --policy conservative \
  --apply
```

The default for `review auto` is dry-run. Automated audit records use a machine
actor such as `automation:conservative-v1`, not a human reviewer name.

## AI review contract

### Input packet

Build the model packet from validated repository data rather than raw
free-form concatenation. It contains:

- the complete semantic review snapshot: ID, title, explanation, reason,
  category, creation timestamp, possible existing IDs, sources, existing
  statement, candidate statement and metadata, and both evidence lists;
- every possible canonical object supplied to the model, including ID,
  statement, metadata, timestamps, evidence references, and relevant history;
- candidate statement and structured metadata;
- unified diff;
- existing and candidate evidence with source, line ranges, fingerprints, and
  bounded line-numbered excerpts;
- a small bounded amount of surrounding context controlled by
  `--context-lines`;
- every structurally possible duplicate pending review supplied to the model,
  including its candidate statement, evidence references, and semantic
  fingerprint.

The packet must clearly delimit all source content as untrusted reference data.
Instructions found inside meeting or Slack text must be ignored.

### Evidence staleness in the packet

Excerpts come from `read_evidence_excerpt`, which already reports `stale` and
`error` per evidence reference. Both flags must appear in every evidence block
of the packet, and the prompt must instruct the model to treat a stale or
unreadable excerpt as unreliable.

Without this, the model reasons over line ranges that may have drifted after a
note was re-synced, then cites those line numbers — and citation validation
accepts them, because they are inside the supplied context. Staleness is not
recoverable downstream; it has to be visible at analysis time.

A review whose candidate evidence is entirely stale or unreadable should
produce a `human_required` suggestion without a provider call.

### Exact model-input fingerprint

Reuse is based on the exact provider request, not on a hand-selected subset of
review fields.

Construct a canonical `ReviewPacket` JSON value containing every field listed
above. Serialize it with sorted keys, fixed separators, and UTF-8. Render the
system and user messages from that packet, then build the provider request
payload without credentials:

```json
{
  "model": "anthropic/claude-model",
  "temperature": 0,
  "response_format": {
    "type": "json_object"
  },
  "messages": [
    {
      "role": "system",
      "content": "..."
    },
    {
      "role": "user",
      "content": "..."
    }
  ]
}
```

`input_fingerprint` is the SHA-256 of the canonical serialization of that exact
request payload. Therefore changing the title, explanation, any supplied
canonical object, surrounding-context size or text, possible duplicate,
staleness/error flag, prompt text, model, temperature, or response format
invalidates reuse automatically.

Keep the following diagnostic fingerprints in the artifact so a stale
suggestion can explain what changed:

- `review_sha256`: all semantic `ReviewItem` fields, including title and
  explanation, but excluding repository path and rendered Markdown bytes;
- `canonical_sha256_by_id`: one semantic fingerprint for every canonical object
  included in the packet, not only the object eventually selected;
- `related_review_sha256_by_id`: one semantic fingerprint for every possible
  duplicate review included in the packet;
- `evidence_sha256_by_source`: the current file digest for every readable source,
  or an explicit missing/unreadable marker;
- `prompt_sha256`: the exact system and user prompt-template contract before
  packet interpolation.

Byte changes to `render_review` alone do not invalidate a suggestion, because
rendered Markdown is not model input. Any semantic field parsed from that
Markdown does invalidate it.

### Required model analysis

The reviewer evaluates:

- whether the claims refer to the same durable fact;
- whether the source expresses an idea, proposal, question, approval,
  completion, cancellation, or replacement;
- temporal ordering;
- status changes;
- material scope changes;
- owner and effective-date changes;
- numeric, financial, policy, or system-control changes;
- whether every material candidate addition has direct evidence;
- whether another pending review is a duplicate;
- missing or ambiguous evidence.

### Structured response

The model returns JSON only:

```json
{
  "suggested_action": "keep-existing",
  "confidence": "medium",
  "existing_id": "metric-example",
  "duplicate_of": null,
  "new_id": null,
  "proposed_knowledge": {
    "category": "metrics",
    "title": "Example Metric",
    "statement": "The existing target remains in effect.",
    "status": "approved",
    "effective_date": null,
    "owner": null,
    "confidence": "high"
  },
  "material_differences": [
    "The candidate adds a numeric target not present in the existing statement."
  ],
  "evidence_findings": [
    {
      "source": "meetings/2026-07-22/example.md",
      "line_start": 20,
      "line_end": 24,
      "finding": "The range is discussed but not explicitly approved."
    }
  ],
  "rationale": "The evidence does not establish replacement.",
  "proposed_note": "Keep the existing target because the source does not establish approval of a replacement.",
  "risks": [
    "The candidate contains a financial threshold."
  ],
  "requires_human": true
}
```

Allowed values:

- `suggested_action`: one of the six existing `REVIEW_ACTIONS`, or `null` when
  the model cannot recommend one;
- `confidence`: `high`, `medium`, or `low`;
- `requires_human`: boolean;
- `proposed_knowledge`: the exact resulting canonical knowledge fields, or
  `null` when the action has no canonical target/result.

There is deliberately no separate `outcome` field. An earlier draft carried
`outcome`, `suggested_action`, and `requires_human` together, but `outcome` was
fully derivable — it was `human_required` exactly when `suggested_action` was
`null` — so it added a third field that could disagree with the other two.
`suggested_action is null` now carries that meaning on its own.

`requires_human` is independent: the model may recommend a concrete action and
still flag that a person should confirm it. It is advisory input to the policy,
never a grant of automation.

The model does not decide whether automatic resolution is allowed. That is
computed by deterministic policy after validating the response.

### Response validation

Reject a model response when:

- it contains unknown keys or invalid enum values;
- its selected canonical or duplicate review ID was not included in the packet;
- an evidence finding cites an unknown source or a line range outside the
  supplied evidence/context;
- required parameters for the suggested action are absent;
- parameters the resolver forbids for that action are present (below);
- `proposed_knowledge` is absent, present for an incompatible action, or
  violates the knowledge schema or action-specific result rules below;
- `new_id` is not a valid knowledge object ID, or already exists;
- confidence, rationale, or proposed note is missing;
- the response attempts to enable stale evidence.

### Exact proposed-result contract

`metadata_overrides` is deliberately not part of the model contract. It cannot
represent the resolver's distinction between an omitted value, a set value,
`--clear-owner`, and `--clear-effective-date` without additional conventions.
It also hides an important resolver behavior: `replace` and `refine` promote
candidate metadata by default even when no CLI override is present.

Instead, `proposed_knowledge` contains exactly these fields:

- `category`;
- `title`;
- `statement`;
- `status`;
- `effective_date`, nullable;
- `owner`, nullable;
- `confidence`.

Validation and presentation compare this complete result against both the
canonical object and candidate, so accepting a suggestion never silently
promotes metadata that the displayed suggestion omitted.

Action-specific result rules:

- `replace` and `refine` require `proposed_knowledge`; category and statement
  must equal the candidate, while every metadata field states the intended final
  value explicitly;
- `create-separate` requires a complete `proposed_knowledge`; category and
  statement must equal the candidate, including for a legacy review whose
  structured candidate metadata is incomplete;
- `reconfirm` requires `proposed_knowledge` to equal the selected current
  canonical knowledge fields exactly;
- `keep-existing` requires the same exact canonical result when a target is
  selected, or `null` when rejecting an unlinked candidate with no canonical
  target;
- `merge-duplicate` and a response with `suggested_action: null` require
  `proposed_knowledge: null`.

A shared pure mapper converts `proposed_knowledge` to the current resolver
arguments:

- different title, status, or confidence becomes the corresponding explicit
  override;
- a different non-null owner or effective date becomes `--owner` or
  `--effective-date`;
- a null owner or effective date that differs from the resolver default becomes
  `--clear-owner` or `--clear-effective-date`;
- matching default values require no override.

The mapper then computes a resolver preview and requires its resulting knowledge
fields to equal `proposed_knowledge`. The same mapper is used by suggestion
validation and `--accept-suggestion`; it is not reimplemented in the CLI.

### Action parameter matrix

Validation must mirror the rejections `ReviewResolver.resolve` already performs,
so that an accepted suggestion cannot fail at resolution time on a constraint
that was knowable at generation time:

| Parameter | Permitted with |
| --- | --- |
| `proposed_knowledge` | `replace`, `refine`, `reconfirm`, `create-separate`, and targeted `keep-existing` |
| `new_id` | `create-separate` |
| `existing_id` | `replace`, `refine`, `reconfirm`, `keep-existing` |
| `duplicate_of` | `merge-duplicate` |

`merge-duplicate` and `create-separate` must not carry `existing_id`.
`merge-duplicate` requires a `duplicate_of` naming a still-pending review that
was included in the packet, and may not name the review under consideration.

A single shared table should drive both suggestion validation and the
`--accept-suggestion` argument mapping, so the two cannot drift apart.

Use the same bounded retry pattern as `OpenRouterAnswerer`. Failed model
validation must not create an actionable suggestion.

## Suggestion storage and traceability

Store suggestions separately from pending and resolved reviews:

```text
knowledge-review/
  pending/
  resolved/
  rejected/
  suggestions/
    REVIEW_ID/
      SUGGESTION_ID.json
```

`KnowledgeRepository.ensure_layout` currently creates only the three
`REVIEW_STATUSES` directories and must be extended to create `suggestions/`.
`load_reviews` globs `review_dir/<status>/*.md` for the three known statuses
only, so the sibling directory does not affect review discovery or
`validate_all`.

Suggestion runs must not share `.knowledge-state/runs/`. That directory contains
ingestion manifests, and `KnowledgeRepository.latest_successful_run` treats
every valid file there as an ingestion run. Putting an AI review run in that
directory could make `status` report a suggestion batch as the latest successful
knowledge-ingestion run.

Use a dedicated state directory and validator:

```text
.knowledge-state/
  runs/                 # existing ingestion runs only
  review-runs/          # AI suggestion batches only
```

`KnowledgeRepository.ensure_layout` creates `review-runs/`.
`validate_review_run_manifest`, `write_review_run_manifest`, and
`iter_review_run_manifests` are separate from the existing ingestion-manifest
functions. `validate_all` validates both namespaces, while
`latest_successful_run` continues to inspect `runs/` only.

The review-run schema is:

```json
{
  "schema_version": "1",
  "run_type": "review_suggestions",
  "run_id": "review-run-...",
  "started_at": "2026-07-29T00:00:00Z",
  "completed_at": "2026-07-29T00:01:00Z",
  "status": "partial_failure",
  "model": "anthropic/claude-model",
  "prompt_version": "1",
  "filters": {
    "priority": "conflict"
  },
  "requested_review_ids": [
    "review-a",
    "review-b"
  ],
  "suggestions_created": {
    "review-a": "suggestion-a"
  },
  "suggestions_reused": {},
  "failures": [
    {
      "review_id": "review-b",
      "error_type": "provider_error",
      "error": "..."
    }
  ]
}
```

Status uses the existing vocabulary (`success`, `partial_failure`, `failed`) but
is validated by the review-run schema. The status is derived rather than trusted:

- `success`: every requested review was created or reused;
- `partial_failure`: at least one succeeded or was reused and at least one
  failed;
- `failed`: no requested review produced or reused a suggestion.

Every requested review ID must appear exactly once across
`suggestions_created`, `suggestions_reused`, and `failures`. Suggestion IDs must
resolve to stored artifacts for the corresponding review. A review-run manifest
does not participate in ingestion status, source checkpoints, or
`latest_successful_run`.

A suggestion artifact contains:

```json
{
  "schema_version": "1",
  "id": "suggestion-...",
  "review_id": "review-...",
  "generated_at": "2026-07-29T00:00:00Z",
  "model": "anthropic/claude-model",
  "prompt_version": "1",
  "prompt_sha256": "...",
  "input_fingerprint": "...",
  "review_sha256": "...",
  "canonical_sha256_by_id": {
    "metric-example": "..."
  },
  "related_review_sha256_by_id": {
    "review-possible-duplicate": "..."
  },
  "evidence_sha256_by_source": {
    "meetings/2026-07-22/example.md": "..."
  },
  "request_parameters": {
    "context_lines": 5,
    "temperature": 0,
    "response_format": "json_object"
  },
  "recommendation": {},
  "automatic_eligibility": {
    "eligible": false,
    "policy": "conservative-v1",
    "reasons": [
      "keep-existing is human-only"
    ]
  }
}
```

### Identity versus reuse

Two distinct values, because one cannot serve both purposes:

- `input_fingerprint` is the digest of the exact credential-free provider
  request payload described above. It drives reuse: a current suggestion with a
  matching fingerprint is returned without another provider call.
- `id` additionally includes `generated_at`, so regenerating identical inputs
  yields a distinct artifact.

If identity alone were content-derived, `--force` on unchanged inputs would
produce the same ID and overwrite the earlier file — defeating the append-only
guarantee in exactly the case where forcing is most useful, namely when you
distrust the previous answer and the inputs have not moved.

### Resolution audit

Extend the review resolution audit data with:

- `suggestion_id`;
- `suggested_action`;
- `suggestion_disposition`: `accepted`, `overridden`, or `not_used`;
- `resolution_mode`: `human`, `hybrid`, or `automated`;
- `automation_policy` when applicable;
- verifier suggestion/model identifiers when automation is used.

The existing human `reviewer`, `note`, action, timestamp, affected object IDs,
and stale-evidence flag remain authoritative.

These fields are persisted in three coupled places and all three must change
together: `ReviewItem.to_frontmatter`, the `## Resolution` section emitted by
`render_review`, and the resolution parsing in `ReviewItem.from_dict`. The
round-trip must be covered by a test.

All new fields are optional. `from_dict` reads the `resolution` mapping
key-by-key without rejecting unknown keys, so the addition is backward
compatible — but already-resolved reviews written before this change must keep
loading, and a test must assert it rather than leaving it to chance. A
resolution with no `suggestion_id` reads as `resolution_mode: human` and
`suggestion_disposition: not_used`.

## Freshness rules

A suggestion is stale when any of these changed after generation:

- pending review content;
- candidate snapshot;
- any canonical object supplied to the model;
- any possible duplicate review supplied to the model;
- evidence source fingerprint;
- bounded evidence/context text or its stale/error state;
- prompt bytes, model, or provider request parameters.

`review show` may display a stale suggestion for history, but
`--accept-suggestion` and automated resolution must reject it.

A fresh AI suggestion does not bypass the resolver's own freshness validation.
Both layers must pass immediately before mutation.

## Conservative automation policy

### Risk model behind the allowlist

The allowlist is not ordered by "how much canonical text changes." Two failure
modes matter equally:

- **False positive:** an incorrect write to canonical knowledge. Recoverable —
  history is append-only and the change is visible in the object's `History`.
- **False negative:** a real update silently discarded. A rejected review leaves
  no canonical trace at all, so the loss is invisible until someone asks a
  question the knowledge base now answers wrongly.

`keep-existing` and `merge-duplicate` write nothing canonical yet are both
false-negative actions, which is why neither is safe by virtue of "changing
nothing." Any change to the allowlist must be argued in these terms.

### Initially eligible actions

- `reconfirm`, when the candidate and canonical metadata do not differ, no
  material change is present, and the review's reason is not
  `conflicting_evidence`;
- `merge-duplicate`, when structural duplicate validation and the additional
  identity gate below both pass.

`reconfirm` does not touch `statement`, `title`, or metadata; its only durable
effect is appending candidate evidence and advancing `last_confirmed`. The
`conflicting_evidence` exclusion exists because reconfirming a conflict attaches
evidence the extractor flagged as contradictory to a statement it may not
support. That degrades citation quality rather than corrupting the statement,
but it is not something to automate before the Phase 3 evaluation.

`merge-duplicate` is the only allowlisted action that permanently discards a
candidate, so it needs more than `reviews_look_duplicate`. That predicate
compares category, `possible_existing_ids`, source overlap, and *normalized
title* — never the candidate statements or evidence. Two genuinely different
candidates extracted from the same meeting under the same topic title satisfy
it. Automatic `merge-duplicate` therefore additionally requires:

- identical normalized candidate statements; and
- the discarded review's candidate evidence is a subset of the retained
  review's candidate evidence.

Anything short of that stays in the hybrid flow.

### Initially human-only actions

- `replace`;
- `refine`;
- `create-separate`;
- `keep-existing`;
- `reconfirm` on a `conflicting_evidence` review;
- any deprecation;
- any use of stale evidence.

### Mandatory automatic gates

All conditions must pass:

- proposer and critic passes agree on the same action and parameters;
- both return high confidence;
- both structured responses validate;
- review, canonical, and evidence fingerprints are current;
- exactly one valid target is selected when the action needs one;
- action is on the policy allowlist;
- no metadata change;
- no number, percentage, currency, date, threshold, or range changes;
- no negation or change of obligation language;
- no owner, approval-status, policy, or control change;
- no category blocked by policy;
- resolver dry-run succeeds;
- the live fingerprints and repository file preconditions still match under the
  repository mutation lock immediately before commit.

The first policy should block automatic changes in `policies`, `decisions`,
`metrics`, and `people-and-ownership`. These categories can still receive AI
suggestions and use the hybrid flow.

### Mutation lock and commit preconditions

Moving fingerprint checks into `ReviewResolver.resolve` narrows the race but
does not eliminate it by itself. Without a lock, another process can change a
review, canonical object, or evidence source after validation and before
`KnowledgeRepository.commit` replaces files.

Add a repository-scoped exclusive mutation lock and hold it across the complete
apply operation:

1. acquire `KnowledgeRepository.mutation_lock`;
2. reload the suggestion, pending review, all referenced canonical objects,
   related duplicate reviews, and evidence;
3. compare the current exact packet/request fingerprint and diagnostic
   fingerprint maps with the accepted suggestion;
4. build the resolver changes;
5. call `KnowledgeRepository.commit` with path preconditions;
6. inside `commit`, immediately before the first replacement, verify the
   expected byte digest or expected absence of every mutation target;
7. commit, validate, and refresh affected indexes before releasing the lock.

Commit preconditions cover:

- the pending review file that will be deleted;
- every canonical file that will be updated, or expected absence for a newly
  created object;
- the retained duplicate-review file for `merge-duplicate`;
- the suggestion artifact used by hybrid or automated resolution;
- every evidence file whose current content is being promoted.

Mutation-target preconditions are restricted to files beneath the repository
root. Read-only evidence preconditions may also name paths safely resolved
beneath the configured `meetings_dir`, which can be outside the repository
root; they never authorize `commit` to write there.

Any mismatch raises `StaleReviewError` before the first repository write.
`ReviewResolver.resolve` remains the API that assembles and enforces these
preconditions; callers cannot request that they be skipped when a suggestion is
accepted or automation is used.

All Meeting Memory commands that mutate canonical knowledge, review state, or
meeting evidence must honor the same lock, including `process-date`,
`process-pending`, `sync-sources`, and `review resolve`. The lock provides the
guarantee among cooperating Meeting Memory processes. Direct external edits
that ignore the lock cannot be made impossible by application-level advisory
locking, so commit-time digest checks remain required and that limitation must
be documented.

A dry-run acquires the lock only for its own load and preview, then releases it.
The later apply command reacquires the lock and repeats every freshness and
precondition check; a successful dry-run is never treated as authority to apply.

Hybrid `--accept-suggestion` and automated apply use this same resolver path.
There is no second mutation implementation in `review auto` or `review triage`.

## Configuration

Extend `[openrouter]` without breaking existing settings:

```ini
[openrouter]
api_key = ...
model = provider/extraction-model
ask_model = provider/answer-model
review_model = anthropic/claude-reviewer
review_critic_model = anthropic/claude-critic
```

`openrouter_configuration` currently returns a hardcoded key tuple
(`api_key`, `model`, `ask_model`) and must be extended, or the new keys are
silently dropped.

Resolution order:

- reviewer: explicit `--model`, `review_model`, then `ask_model`;
- critic: explicit `--critic-model`, `review_critic_model`, then
  `review_model`.

If none resolves, raise `ConfigurationError`. The chain deliberately stops at
`ask_model` and does not fall back to `model`, which is the extraction model —
a misconfigured `review_model` would otherwise silently produce
plausible-looking suggestions from a model never evaluated for review.

Environment overrides may be added as:

- `MEETING_MEMORY_REVIEW_MODEL`;
- `MEETING_MEMORY_REVIEW_CRITIC_MODEL`.

## Proposed code structure

```text
src/meeting_memory/knowledge/
  review_ai.py          reviewer interface, OpenRouter implementation, prompts
  review_suggestions.py exact request fingerprint; schema, proposed-result
                        mapping, validation, storage, freshness, rendering
  review_policy.py      deterministic automatic-eligibility policies
  review.py             existing resolver; suggestion audit linkage and
                        locked precondition enforcement
  models.py             review-run validation; ReviewItem suggestion audit
                        fields, frontmatter round-trip, backward compatibility
  repository.py         suggestion load/write/validation; mutation lock,
                        commit preconditions, ensure_layout
  configuration.py      review_model and review_critic_model keys
  cli.py                suggest, triage, auto, and resolve flags
```

`models.py` and `configuration.py` are not optional: the Phase 2 audit fields
cannot round-trip without the first, and the Phase 1 model settings do not load
without the second.

Core interfaces:

```python
class KnowledgeReviewAdvisor(ABC):
    def suggest(self, packet: ReviewPacket) -> ReviewSuggestion:
        ...


class FakeReviewAdvisor(KnowledgeReviewAdvisor):
    """Deterministic tests and local demonstrations."""


class OpenRouterReviewAdvisor(KnowledgeReviewAdvisor):
    """Structured, evidence-grounded OpenRouter implementation."""
```

Keep packet construction, model transport, suggestion validation, policy, and
resolution separate so no LLM adapter can mutate the repository directly.

## Delivery phases

### Phase 1 — Advisory suggestions

- Add suggestion models and repository storage, including the separated
  `id` / `input_fingerprint` scheme and the `suggestions/` layout.
- Extend `openrouter_configuration` and `ensure_layout`.
- Add canonical `ReviewPacket` construction and exact credential-free provider
  request fingerprinting, including all canonical and related-review maps.
- Add grounded prompts carrying evidence `stale` and `error` flags.
- Add the exact `proposed_knowledge` contract, pure resolver-argument mapper,
  action parameter matrix, and response validation.
- Add `FakeReviewAdvisor` and `OpenRouterReviewAdvisor`.
- Add `review suggest` for one review and filtered batches.
- Add `--with-suggestion` to `review show`.
- Add separately validated suggestion-run manifests under
  `.knowledge-state/review-runs/`; keep ingestion status isolated.
- Document configuration and usage.

No resolution behavior changes in this phase.

### Phase 2 — Hybrid resolution

- Add the suggestion audit fields to `ReviewItem`, `render_review`, and
  resolution parsing, with a round-trip and backward-compatibility test.
- Add the repository mutation lock and commit preconditions, and make every
  Meeting Memory canonical/review/evidence writer honor the lock.
- Add `--suggestion-id` and `--accept-suggestion` to `review resolve`, mapping
  `proposed_knowledge` through the shared mapper and action matrix.
- Record accepted and overridden suggestions in the resolution audit.
- Reject stale suggestions.
- Add `review triage`.
- Update the README with the AI-first human-review trace.

### Phase 3 — Evaluation

- Run suggestions across all pending conflicts.
- Record human accept, override, and defer rates.
- Classify disagreement causes by action, category, and evidence problem.
- Establish a reviewed evaluation set from resolved conflicts.
- Do not enable automatic apply until the evaluation set shows acceptable
  precision for each proposed safe action.

### Phase 4 — Conservative automation

- Add proposer/critic review.
- Add versioned deterministic policies.
- Add `review auto`, dry-run by default.
- Permit only evaluated safe actions.
- Add automated actor and policy fields to the audit.
- Add kill switch and per-category/action allowlists.

## Test plan

### Suggestion schema and grounding

- valid response round-trip;
- unknown key rejection;
- invalid action and confidence rejection;
- unknown object/review ID rejection;
- unknown evidence source rejection;
- out-of-range evidence citation rejection;
- every parameter/action pair the resolver forbids is rejected at suggestion
  validation time, one case per matrix cell;
- invalid or already-used `new_id` rejection;
- invalid, missing, or action-incompatible `proposed_knowledge` rejection;
- explicit owner/effective-date clears map to the resolver clear flags;
- candidate metadata defaults cannot be promoted unless every resulting value
  appears identically in `proposed_knowledge`;
- the mapper's resolver preview exactly matches `proposed_knowledge` for every
  action that has a canonical result;
- stale and unreadable evidence excerpts reach the packet with their flags, and
  a fully stale candidate short-circuits to `human_required` without a provider
  call;
- prompt-injection text in evidence is treated only as reference data;
- provider retry and partial batch failure;
- review-run status is derived from created, reused, and failed results;
- every requested review appears in exactly one review-run result bucket;
- review-run suggestion IDs resolve to the corresponding stored artifacts;
- a successful suggestion run does not change `latest_successful_run`;
- idempotent reuse on matching `input_fingerprint`;
- changing title, explanation, context lines, prompt bytes, model parameters,
  any supplied canonical object, or any supplied duplicate review changes
  `input_fingerprint`;
- `--force` on unchanged inputs writes a second artifact and leaves the first
  file byte-identical;
- `render_review` output changes do not invalidate suggestions, because the
  parsed semantic `ReviewPacket` and exact rendered provider request remain
  unchanged.

### Freshness and audit

- changed review invalidates suggestion;
- changed canonical object, including an unselected object supplied to the
  model, invalidates suggestion;
- changed possible duplicate review invalidates suggestion;
- changed evidence fingerprint invalidates suggestion;
- stale suggestion remains readable;
- stale suggestion cannot be accepted or automated;
- accepted suggestion records AI and human data;
- overridden suggestion preserves both actions;
- resolution audit fields survive a `render_review` / `load_review_file`
  round-trip;
- a resolved review written before the audit fields existed still loads, and
  reads as `resolution_mode: human` / `suggestion_disposition: not_used`;
- direct human resolution remains backward compatible.

### Interactive triage

- accept, override, defer, and quit paths;
- dry-run shown before confirmation;
- declined confirmation performs no write;
- one failed case does not lose completed earlier decisions.

### Automation policy

- only allowlisted actions are eligible;
- proposer/critic disagreement blocks;
- non-high confidence blocks;
- numeric, date, owner, status, negation, and stale changes block;
- blocked categories remain human-only;
- `reconfirm` on a `conflicting_evidence` review blocks;
- `merge-duplicate` blocks when candidate statements differ, or when the
  discarded review cites evidence the retained review does not, even though
  `reviews_look_duplicate` passes;
- resolver dry-run failure blocks;
- a canonical or evidence change made after the dry-run and before the apply
  blocks after the apply command reloads under the mutation lock;
- a simulated mutation immediately before `commit` fails a path precondition
  before any file is replaced;
- automated audit contains model, verifier, and policy identity.

### Repository integrity

- canonical and review changes remain atomic;
- all Meeting Memory evidence/canonical/review writers honor the same repository
  mutation lock;
- commit path preconditions cover updates, deletes, and expected-absent creates;
- suggestion files cannot reference missing reviews;
- resolution cannot reference a missing suggestion;
- resolved/rejected reviews retain suggestion history;
- malformed review-run manifests fail repository validation without being read
  as ingestion runs;
- index and repository validation remain current.

## Acceptance criteria

Phase 1 is complete when:

- one command can generate validated suggestions for all pending conflicts;
- an individual model failure cannot corrupt or stop the rest of the batch;
- every suggestion is grounded to exact repository objects and evidence lines;
- no suggestion command changes canonical knowledge or review status;
- every exact model request is replayable and auditable by its canonical request
  payload, model parameters, prompt digest, and input fingerprint;
- no validated suggestion can name a parameter the resolver would reject for
  its action, and the displayed `proposed_knowledge` exactly matches the
  resolver preview.

Phase 2 is complete when:

- a human can accept or override a suggestion through the CLI;
- the resolver still dry-runs and applies through the existing deterministic
  mutation path;
- apply reloads and verifies under the repository mutation lock, and commit
  preconditions fail before the first write;
- the final review record shows AI recommendation, human comment, final action,
  and whether the human agreed;
- stale suggestions and stale canonical/evidence snapshots are safely refused.

Phase 4 is complete when:

- automated mode is dry-run by default;
- automatic apply is restricted by a versioned, tested policy;
- every automated resolution has proposer, critic, policy, fingerprints, and
  resolver output in its audit trail;
- disabling automation leaves the advisory and hybrid workflows fully usable.

## Non-goals

- Letting an LLM edit raw meeting or Slack files.
- Letting an LLM write canonical knowledge directly.
- Automatically bypassing stale evidence or canonical drift.
- Automatically resolving all conflict types.
- Using model confidence alone as an automation safety control.
- Treating a generated resolution note as human approval.

## Immediate next step

Implement Phase 1 only: persisted, evidence-grounded AI suggestions for one
review and filtered batches. After suggestions have been generated for the
current conflict queue, inspect their quality before adding hybrid acceptance
or any automatic resolution.

Four items must land inside Phase 1 rather than being retrofitted, because each
one shapes artifacts that will already be on disk:

- exact provider-request `input_fingerprint` plus diagnostic fingerprint maps;
- the removal of `outcome` from the response schema;
- the exact `proposed_knowledge` contract, mapper, and action parameter matrix;
- evidence staleness flags in the packet.

The remaining revisions — audit-field persistence, the `merge-duplicate`
identity gate, repository mutation lock and commit preconditions, and the model
resolution chain — belong to their own phases but are recorded here so the phase
boundaries stay honest.
