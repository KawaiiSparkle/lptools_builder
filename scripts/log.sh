#!/usr/bin/env bash
# Shared stage timing and heartbeat helpers. Sourcing does not install traps.
# Entry scripts call lp_finish "$rc" from an EXIT trap that preserves "$rc".
if [ -n "${__LP_LOG_SH:-}" ]; then return 0; fi
__LP_LOG_SH=1

: "${LP_BEAT_SECONDS:=15}"
: "${LP_TS:=1}"

_lp_color() {
  [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != dumb ] &&
    { [ -t 1 ] || [ "${GITHUB_ACTIONS:-}" = true ]; }
}
_lp_stamp() {
  [ "$LP_TS" = 1 ] || return 0
  printf '%s ' "$(date -u +%H:%M:%S)"
}
log() {
  if _lp_color; then printf '%s\033[1;34m==>\033[0m %s\n' "$(_lp_stamp)" "$*"
  else printf '%s==> %s\n' "$(_lp_stamp)" "$*"; fi
}
warn() {
  if _lp_color; then printf '%s\033[1;33m!!!\033[0m %s\n' "$(_lp_stamp)" "$*" >&2
  else printf '%s!!! %s\n' "$(_lp_stamp)" "$*" >&2; fi
}
die() { warn "$*"; exit 1; }
now_s() { date +%s; }
_fmt_dur() {
  local d=$1
  if [ "$d" -lt 60 ]; then printf '%ss' "$d"
  else printf '%dm%02ds' "$((d / 60))" "$((d % 60))"; fi
}
_lp_groups() {
  [ "${LP_NO_GROUPS:-0}" != 1 ] && [ "${GITHUB_ACTIONS:-}" = true ]
}

_LP_STAGES=""
_LP_STAGE_NAME=""
_LP_STAGE_START=0
_LP_GROUP_OPEN=0

group_start() {
  _LP_STAGE_NAME=$1
  _LP_STAGE_START=$(now_s)
  # Keep start/final status outside the fold; raw build output stays inside.
  log "$1"
  if _lp_groups; then printf '::group::%s\n' "$1"; fi
  _LP_GROUP_OPEN=1
}
group_end() {
  [ "$_LP_GROUP_OPEN" = 1 ] || return 0
  local result="${1:-ok}" dur
  dur=$(_fmt_dur "$(( $(now_s) - _LP_STAGE_START ))")
  _LP_STAGES="${_LP_STAGES}${_LP_STAGE_NAME}	${dur}	${result}
"
  if _lp_groups; then printf '::endgroup::\n'; fi
  _LP_GROUP_OPEN=0
  if [ "$result" = ok ]; then log "$result: $_LP_STAGE_NAME ($dur)"
  else warn "$result: $_LP_STAGE_NAME ($dur)"; fi
}
stage_table() {
  [ -n "$_LP_STAGES" ] || return 0
  local table
  table=$(
    printf '\n### Build stages\n\n| Stage | Duration | Result |\n| --- | --- | --- |\n'
    printf '%s' "$_LP_STAGES" | while IFS=$'\t' read -r name duration result; do
      name=${name//|/\\|}
      printf '| %s | %s | %s |\n' "$name" "$duration" "$result"
    done
  )
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    if ! printf '%s\n' "$table" >> "$GITHUB_STEP_SUMMARY"; then
      warn "could not write the step summary"
      printf '%s\n' "$table"
    fi
  else
    printf '%s\n' "$table"
  fi
  _LP_STAGES=""
}
lp_finish() {
  local rc=${1:-0}
  if [ "$_LP_GROUP_OPEN" = 1 ]; then
    if [ "$rc" = 0 ]; then group_end
    else group_end "failed (exit $rc)"; fi
  fi
  stage_table
}

# A private subshell keeps signal handlers out of the caller. Monitor mode gives
# the command (including AOSP shell functions) its own process group. Cancel the
# whole command group before the entry script restores patches or deletes files.
run_beat() (
  local label=$1 start child="" ticker="" rc=0
  shift
  case "$LP_BEAT_SECONDS" in
    ''|*[!0-9]*) warn 'LP_BEAT_SECONDS must be a positive integer'; exit 2 ;;
  esac
  if [[ "$LP_BEAT_SECONDS" != *[1-9]* ]]; then
    warn 'LP_BEAT_SECONDS must be a positive integer'
    exit 2
  fi
  set -m
  _lp_beat_cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [ -n "$ticker" ]; then
      kill -TERM -- "-$ticker" 2>/dev/null || true
      wait "$ticker" 2>/dev/null || true
    fi
    if [ -n "$child" ]; then
      kill -TERM -- "-$child" 2>/dev/null || true
      kill -KILL -- "-$child" 2>/dev/null || true
      wait "$child" 2>/dev/null || true
    fi
    exit "$status"
  }
  trap _lp_beat_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  start=$(now_s)
  log "$label: started"
  # Keep descendants in the command group created by monitor mode above.
  ( set +m; "$@" ) &
  child=$!
  (
    # Inherited monitor mode would put sleep in another, untracked group.
    set +m
    while sleep "$LP_BEAT_SECONDS"; do
      kill -0 "$child" 2>/dev/null || break
      log "... $label still running ($(_fmt_dur "$(( $(now_s) - start ))"))"
    done
  ) &
  ticker=$!
  wait "$child" || rc=$?
  child=""
  exit "$rc"
)
