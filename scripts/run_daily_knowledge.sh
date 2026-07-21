#!/bin/bash
# Scheduled durable-knowledge runner for the standalone meeting-memory project.
export HOME="${HOME:-/home/ruipluang}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${MEETING_MEMORY_PROJECT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG_FILE="${MEETING_MEMORY_CONFIG:-$PROJECT_DIR/meeting-memory.ini}"
LOCK_MAX_AGE_MIN="${DAILY_KNOWLEDGE_LOCK_MAX_AGE_MIN:-180}"
ENV_FILE="${DAILY_KNOWLEDGE_ENV_FILE:-$HOME/.env.meeting-memory}"
MAX_ATTEMPTS="${DAILY_KNOWLEDGE_MAX_ATTEMPTS:-3}"
LOOKBACK_DAYS="${DAILY_KNOWLEDGE_LOOKBACK_DAYS:-30}"
PYTHON_BIN="${DAILY_KNOWLEDGE_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
DRY_RUN=0
FORCE=0
TARGET_DATE=""

usage() {
  echo "Usage: $0 [--dry-run] [--force] [--lookback-days N] [YYYY-MM-DD]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run|-n) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    --lookback-days)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      LOOKBACK_DAYS="$2"
      shift 2
      ;;
    --help|-h) usage; exit 0 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
      [ -z "$TARGET_DATE" ] || { usage; exit 2; }
      TARGET_DATE="$1"
      shift
      ;;
    *) usage; exit 2 ;;
  esac
done

for value_name in MAX_ATTEMPTS LOOKBACK_DAYS LOCK_MAX_AGE_MIN; do
  value="${!value_name}"
  case "$value" in
    ''|*[!0-9]*) echo "$value_name must be a positive integer" >&2; exit 2 ;;
  esac
  [ "$value" -ge 1 ] || { echo "$value_name must be at least 1" >&2; exit 2; }
done

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "No usable Python interpreter found." >&2
  exit 1
fi

# meeting-memory.ini is the single source of truth for storage paths. Use the
# shared Python parser so scheduled runs and interactive CLI runs interpret the
# INI identically, including relative paths and ~ expansion.
configured_paths_file="$(mktemp)"
if ! env \
  PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" -m meeting_memory.knowledge.configuration "$CONFIG_FILE" \
  > "$configured_paths_file"; then
  rm -f "$configured_paths_file"
  exit 78
fi
mapfile -d '' -t configured_paths < "$configured_paths_file"
rm -f "$configured_paths_file"
if [ "${#configured_paths[@]}" -ne 2 ]; then
  echo "Invalid path data returned for $CONFIG_FILE." >&2
  exit 78
fi
MEETINGS_DIR="${configured_paths[0]}"
OUTPUT_DIR="${configured_paths[1]}"
LOG_DIR="$OUTPUT_DIR/logs"
RUN_DATE="$(date +%Y-%m-%d)"
LOG_FILE="${DAILY_KNOWLEDGE_LOG_FILE:-$LOG_DIR/daily-knowledge-$RUN_DATE.log}"
LOCK_DIR="${DAILY_KNOWLEDGE_LOCK_DIR:-$OUTPUT_DIR/.daily-knowledge.lock}"

mkdir -p -m 700 "$LOG_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

redact() {
  sed -E \
    -e 's/sk-ant-[A-Za-z0-9_-]+/[REDACTED_ANTHROPIC_KEY]/g' \
    -e 's/sk-or-[A-Za-z0-9_-]+/[REDACTED_OPENROUTER_KEY]/g' \
    -e 's/xox[baprs]-[A-Za-z0-9-]+/[REDACTED_SLACK_TOKEN]/g' \
    -e 's/(OPENROUTER_API_KEY[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED_OPENROUTER_KEY]/Ig' \
    -e 's/(ANTHROPIC_AUTH_TOKEN[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED_PROVIDER_TOKEN]/Ig' \
    -e 's/(Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]]+/\1[REDACTED_TOKEN]/Ig'
}

log() {
  printf '[%s] %s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$1" | redact | tee -a "$LOG_FILE"
}

release_lock() {
  if [ -f "$LOCK_DIR/pid" ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    log "Another daily-knowledge run is active (pid $lock_pid); exiting."
    exit 73
  fi
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +"$LOCK_MAX_AGE_MIN" 2>/dev/null)" ]; then
    log "Recovering stale daily-knowledge lock older than ${LOCK_MAX_AGE_MIN}m."
    rm -f "$LOCK_DIR/pid"
    if ! rmdir "$LOCK_DIR" 2>/dev/null || ! mkdir "$LOCK_DIR" 2>/dev/null; then
      log "Stale-lock recovery lost a race; exiting."
      exit 73
    fi
  else
    log "Daily-knowledge lock exists and is not old enough for safe recovery; exiting."
    exit 73
  fi
fi
printf '%s\n' "$$" > "$LOCK_DIR/pid"
trap release_lock EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -d "$PROJECT_DIR/src/meeting_memory" ]; then
  log "ERROR: meeting-memory source is missing under $PROJECT_DIR/src."
  exit 1
fi
if [ ! -d "$MEETINGS_DIR" ]; then
  log "ERROR: configured meeting directory is missing: $MEETINGS_DIR"
  exit 1
fi
# Compatibility fallback while the local INI API-key field remains blank. Once
# an INI key is present, the runner does not source the legacy environment file.
if ! grep -Eq '^[[:space:]]*api_key[[:space:]]*=[[:space:]]*[^#[:space:]]' "$CONFIG_FILE" 2>/dev/null \
  && [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Extraction has no Slack responsibility; do not expose Slack credentials.
while IFS='=' read -r variable_name _; do
  case "$variable_name" in
    SLACK_*) unset "$variable_name" ;;
  esac
done < <(env)

if [ -n "$TARGET_DATE" ]; then
  command_args=(process-date "$TARGET_DATE" --lookback-days "$LOOKBACK_DAYS")
else
  command_args=(process-pending --lookback-days "$LOOKBACK_DAYS")
fi
[ "$DRY_RUN" -eq 0 ] || command_args+=(--dry-run)
[ "$FORCE" -eq 0 ] || command_args+=(--force)

run_cli() {
  env \
    -u SLACK_BOT_TOKEN \
    -u SLACK_TOKEN \
    -u SLACK_APP_TOKEN \
    -u SLACK_SIGNING_SECRET \
    -u DAILY_KNOWLEDGE_BASE_DIR \
    -u MEETING_MEMORY_LOGS_DIR \
    -u MEETING_MEMORY_MEETINGS_DIR \
    -u MEETING_MEMORY_OUTPUT_DIR \
    PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m meeting_memory.knowledge --config "$CONFIG_FILE" "$@"
}

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  log "Attempt $attempt/$MAX_ATTEMPTS: durable knowledge ${TARGET_DATE:-pending dates}"
  attempt_log="$(mktemp)"
  set +e
  run_cli "${command_args[@]}" 2>&1 | redact | tee -a "$LOG_FILE" "$attempt_log"
  status=${PIPESTATUS[0]}
  set -e

  if [ "$status" -eq 0 ]; then
    rm -f "$attempt_log"
    if [ "$DRY_RUN" -eq 0 ]; then
      log "Generating durable-knowledge browse indexes."
      set +e
      run_cli index 2>&1 | redact | tee -a "$LOG_FILE"
      index_status=${PIPESTATUS[0]}
      set -e
      if [ "$index_status" -ne 0 ]; then
        log "Index generation failed (exit $index_status)."
        exit "$index_status"
      fi
    fi
    log "Daily durable-knowledge run completed successfully."
    exit 0
  fi

  if [ "$status" -eq 78 ] || grep -Eqi \
    'authentication/account failure|invalid configuration|insufficient credits|account limit|usage limit' \
    "$attempt_log"; then
    rm -f "$attempt_log"
    log "Non-retryable authentication, configuration, or account-limit failure."
    exit "$status"
  fi
  if [ "$status" -ne 75 ]; then
    rm -f "$attempt_log"
    log "Non-retryable deterministic application failure (exit $status)."
    exit "$status"
  fi
  rm -f "$attempt_log"

  attempt=$((attempt + 1))
  if [ "$attempt" -le "$MAX_ATTEMPTS" ]; then
    delay=$((attempt * 5))
    log "Transient failure; retrying in ${delay}s."
    sleep "$delay"
  fi
done

log "Daily durable-knowledge run exhausted $MAX_ATTEMPTS transient attempts."
exit 75
