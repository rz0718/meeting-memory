# Repository-Driven BigQuery Health Monitor Design

## Purpose

Build a scheduler-friendly monitor that runs on a remote VM, keeps managed local
copies of configured GitHub repositories current, discovers the BigQuery tables
and columns used by those repositories, and reports data-health problems.

The first release produces local JSON and HTML reports plus meaningful process
exit codes. Slack and GitHub notifications are intentionally deferred.

## Scope

The monitor covers only BigQuery tables referenced by configured repositories.
It does not enumerate every table in a project or dataset. Operators may exclude
paths or references in configuration, but the repository dependency catalog is
the source of truth for monitoring scope.

The monitor detects:

- missing or inaccessible referenced tables;
- tables that are late relative to their learned update cadence;
- schema additions, removals, and changes;
- abnormal ranges, scales, distributions, null rates, or cardinality for columns
  used by repository code;
- unresolved dynamic SQL and ambiguous column references;
- incomplete coverage caused by repository, BigQuery, or cost-limit failures.

## Selected Architecture

Use a modular, stateful snapshot pipeline:

```text
TOML configuration
        |
        v
Managed Git clones -> changed commit detection
        |
        v
Static SQL/reference scanner -> table and column dependency catalog
        |
        v
BigQuery metadata collector -> schema and freshness observations
        |
        v
Cost-bounded changed-partition profiler
        |
        v
Historical state + anomaly evaluator
        |
        v
Immutable run record + latest.json + latest.html + exit code
```

The monitor is a standalone application and package, independent of Meeting
Memory. This repository is only the location of the approved requirements and
design document. The implementation may reuse sound general patterns such as
incremental checkpoints, immutable run manifests, atomic state writes, and
deterministic output, but it must not inherit Meeting Memory's package layout,
configuration model, or domain concepts.

## Configuration

Use a dedicated `repo-bq-monitor.toml`. TOML represents multiple repositories and
nested monitoring options without adding a YAML dependency. Configuration
contains:

- repository URL, optional branch, and optional path exclusions;
- managed clone, state, run, and report directories;
- BigQuery billing project and any location hints needed for metadata queries;
- minimum observations and rolling-history limits;
- a maximum number of BigQuery bytes processed per run;
- optional table or column exclusions;
- lock path and report retention settings.

Secrets are not stored in this file. Git uses the VM's credential helper or SSH
agent. BigQuery uses Application Default Credentials or the VM's service
account.

## Repository Synchronization

Each repository has a tool-owned checkout, separate from developer working
copies. On the first run, the monitor clones the configured/default branch. On
later runs it fetches that branch and advances the managed checkout to the
remote commit. Because the checkout is exclusively tool-owned, it may safely be
kept at the exact monitored commit.

The repository state records URL, branch, scanned commit SHA, scan time, and
scanner version. Dependency extraction is skipped when the commit and scanner
version are unchanged. A sync failure is isolated to that repository, preserves
its last known dependency catalog for diagnostic display, and marks current
coverage incomplete rather than healthy.

## Table and Column Discovery

Parse SQL with BigQuery dialect rules and resolve aliases to source tables.
Record each literal table reference with repository, commit SHA, source path,
line number, and extraction confidence. Resolve columns used by projections,
filters, joins, grouping, ordering, and calculations to their source tables when
the query structure permits it.

Scan SQL files and SQL text embedded in supported application source. Literal
fully qualified names such as `project.dataset.table` are authoritative.
Templated or dynamically assembled names are recorded as unresolved rather than
guessed. Ambiguous unqualified columns and `SELECT *` are also surfaced as
coverage warnings. The dependency catalog is the union of current references
across configured repositories.

## Metadata Collection and Freshness Learning

For every referenced table, collect normalized schema, partition metadata, and
modification timestamps. Use the newest partition activity when partitions are
available and fall back to table `last_modified_time` otherwise.

Record observations even when a repository has not changed, because freshness
learning depends on time-series history. Derive update intervals from changes
to the observed modification timestamp. After a provisional minimum of five
intervals, calculate a robust expected cadence using the median and median
absolute deviation. Before sufficient evidence exists, report `learning`
instead of stale.

Compare time since the most recent update with the learned cadence and robust
variation band. Record the observed value, expected interval, learned bound,
and evidence used for every stale decision so alerts remain explainable.

## Schema Change Detection

Normalize and hash field name, type, mode, order-independent nested structure,
and relevant table properties. Compare each successful observation with the
previous successful snapshot and classify changes as:

- breaking: removed field, incompatible type change, or stricter nullability;
- non-breaking: added field or relaxed nullability;
- unknown-risk: complex nested or unsupported property change.

All schema changes appear in the report, including changes to columns that the
scanner did not resolve, while the dependency catalog highlights whether a
changed field is known to be used by repository code.

## Cost-Bounded Column Profiling

Profile only columns that the scanner resolves to referenced tables. For
partitioned tables, query only partitions modified since the last successful
profile. Issue a BigQuery dry run before every profile query and execute queries
in priority order until the configured bytes-per-run budget would be exceeded.

Profiles contain aggregates, never raw rows:

- numeric: minimum, maximum, approximate quantiles, mean, standard deviation,
  null rate, zero rate, and approximate distinct count;
- string: length distribution, blank/null rate, and approximate cardinality;
- date/time: minimum, maximum, gaps where derivable, and range;
- boolean and low-cardinality data: value distribution and new categories.

For non-partitioned tables, run a profile only when its dry-run estimate fits in
the remaining budget. Otherwise report `skipped_cost_limit`; metadata freshness
and schema checks still run.

## Anomaly Detection

Compare a changed partition's profile with a rolling history of comparable
partitions. Learn robust bounds rather than imposing one global percentage.
Until enough comparable profiles exist, show `learning`.

Detect and explain:

- `range_shift` when observed bounds leave the learned normal envelope;
- `scale_shift` when median or upper quantiles change by an unusual magnitude;
- `distribution_shift` when multiple quantiles move materially;
- `null_rate_shift` and `cardinality_shift`;
- `new_category` for low-cardinality fields.

A lone extreme normally produces a warning. Multiple abnormal metrics or a
severe order-of-magnitude shift produces a critical alert. The implementation
plan must define deterministic initial constants, make them configurable where
operationally useful, and test learning and noisy-data behavior.

## State and Reports

Use this logical local layout beneath the configured data directory:

```text
state/
  repositories/
  dependencies/
  metadata/
  profiles/
runs/
reports/
  latest.json
  latest.html
```

Run records are immutable. State writes and latest-report publication are
atomic. The JSON report is the canonical machine-readable result; HTML renders
the same model and contains no external runtime dependencies.

Reports contain overall status and completeness, repository commits, source
locations for table/column references, table freshness, schema diffs, column
profile anomalies, unresolved references, skipped work, operational errors,
and estimated/processed BigQuery bytes.

Exit codes are:

- `0`: healthy; learning items are informational;
- `1`: warnings, non-breaking changes, unresolved references, or cost skips;
- `2`: critical anomalies, breaking schema changes, missing tables, or an
  incomplete run caused by operational failure.

## Error Handling and Safety

Failures are isolated by repository and table so one failure does not prevent
unrelated checks. The final status can never be healthy when current coverage
is incomplete. Last known state may be displayed for context but must be marked
historical and must not masquerade as a current observation.

The monitor stores metadata and aggregate statistics only. Logs and reports
must not contain Git credentials, Google credentials, raw query results, or
environment dumps. BigQuery queries use parameters or validated identifiers,
and the bytes cap is checked through dry runs before execution.

## Remote VM and Scheduling

The application is a one-shot batch monitor with a small CLI as its operational
entrypoint. The CLI is not the architecture itself; it is the stable contract
used by people, cron, and systemd to start a run and inspect local results:

```bash
repo-bq-monitor run --config /etc/repo-bq-monitor/monitor.toml
repo-bq-monitor report --config /etc/repo-bq-monitor/monitor.toml
repo-bq-monitor status --config /etc/repo-bq-monitor/monitor.toml
```

`run` performs exactly one bounded attempt and then exits. It uses a nonblocking
single-instance lock; a concurrent invocation exits with a clear operational
status rather than overlapping Git or state writes. It does not daemonize and
does not depend on an interactive shell, which makes it suitable for cron or a
systemd timer.

Provide example systemd service/timer units and a small scheduler entry point
that sets a predictable working directory and restrictive file permissions.
The service account needs read access to the Git repositories, BigQuery metadata
permissions, permission to run aggregate jobs against referenced tables, and
write access only to the configured local data directory. Reports remain local
for the first release and can be served or copied by separate infrastructure.

## Testing

Unit tests cover configuration, SQL/table/column resolution, schema diffs,
cadence learning, anomaly detection, byte-budget prioritization, exit-code
aggregation, atomic state, and report rendering. Tests include partitioned and
non-partitioned tables, nested schemas, dynamic SQL, ambiguous columns, missing
permissions, sparse history, and noisy profiles.

Integration tests use temporary local Git repositories and a fake BigQuery
adapter. Golden tests verify deterministic JSON and self-contained HTML.
Credentialed BigQuery smoke tests are optional and separate from the default
test suite so local and CI runs remain deterministic and inexpensive.

## Deferred Work

- Slack, email, or GitHub notifications;
- automatic querying of every table in a project or dataset;
- full-table profiling that exceeds the configured byte budget;
- row-level samples or storage of raw BigQuery data;
- automatic rewriting of repository SQL after an alert.
