#!/usr/bin/env bash
# gdrive-index.sh — refresh the local metadata index for every enabled org.
#
# Per org: rclone lsjson -R --fast-list through that org's own rendered config
# (<conf-dir>/rclone-<org>.conf) into <state-dir>/<org>.json, then a sqlite
# rollup at <state-dir>/index.sqlite. This is the agent query surface;
# Spotlight does not index these mounts.
#
# An org with no rendered config is skipped with a warning. An org whose
# listing fails is recorded failed and makes the whole run exit non-zero, so a
# dead token cannot look fresh. Only orgs that succeeded this run are rolled up.
set -euo pipefail

d="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
for _c in "$d/lib/common.sh" "$d/scripts/lib/common.sh" "$d/../libexec/gdrive-mounts/common.sh"; do
  # in-repo | bazel runfiles root (entrypoint renamed to the target name) | installed
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
type no_trace_guard >/dev/null 2>&1 || { echo "gdrive-mounts: common.sh not found next to $0" >&2; exit 70; }
no_trace_guard

usage() {
  cat >&2 <<'USAGE'
usage: gdrive-index.sh [--orgs FILE] [--conf-dir DIR] [--state-dir DIR]
  --orgs FILE      org registry (default: orgs.json next to the repo or package)
  --conf-dir DIR   rendered configs (default: defaults.stateDir)
  --state-dir DIR  index output (default: defaults.indexStateDir)
USAGE
  exit 2
}

ORGS=""
CONF_DIR=""
STATE_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --orgs) ORGS="${2:-}"; shift 2 ;;
    --conf-dir) CONF_DIR="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    -h | --help) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_cmd jq "run inside the flake: nix develop --command just index"
require_cmd rclone "run inside the flake: nix develop --command just index"
[ -n "$ORGS" ] || ORGS="$(orgs_default)"
[ -f "$ORGS" ] || die "org registry not found: $ORGS"
[ -n "$CONF_DIR" ] || CONF_DIR="$(state_dir "$ORGS")"
[ -n "$STATE_DIR" ] || STATE_DIR="$(index_state_dir "$ORGS")"
mkdir_secure "$STATE_DIR"

DB="$STATE_DIR/index.sqlite"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
HAVE_SQLITE=0
if command -v sqlite3 >/dev/null 2>&1; then
  HAVE_SQLITE=1
  sqlite3 -cmd ".timeout 5000" "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS files (
  org TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, mtime TEXT, isdir INTEGER NOT NULL,
  indexed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (org, path)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT NOT NULL, org TEXT NOT NULL, status TEXT NOT NULL,
  entries INTEGER, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, message TEXT
);
CREATE INDEX IF NOT EXISTS runs_org_idx ON runs (org, finished_at);
SQL
  chmod 600 "$DB" 2>/dev/null || true
else
  warn "sqlite3 not on PATH; writing per-org JSON only (no rollup, no runs table)"
fi

record_run() { # org status entries started message
  [ "$HAVE_SQLITE" = 1 ] || return 0
  local entries="NULL"
  [ -n "$3" ] && entries="$(sql_quote "$3")"
  sqlite3 -cmd ".timeout 5000" "$DB" <<SQL
.parameter init
.parameter set :run $(sql_quote "$RUN_ID")
.parameter set :org $(sql_quote "$1")
.parameter set :st $(sql_quote "$2")
.parameter set :n $entries
.parameter set :s $(sql_quote "$4")
.parameter set :f $(sql_quote "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
.parameter set :m $(sql_quote "$5")
INSERT INTO runs (run_id, org, status, entries, started_at, finished_at, message)
  VALUES (:run, :org, :st, CAST(:n AS INTEGER), :s, :f, :m);
SQL
}

rollup() { # org json-path timestamp
  [ "$HAVE_SQLITE" = 1 ] || return 0
  sqlite3 -cmd ".timeout 5000" "$DB" <<SQL
.parameter init
.parameter set :org $(sql_quote "$1")
.parameter set :src $(sql_quote "$2")
.parameter set :ts $(sql_quote "$3")
BEGIN;
DELETE FROM files WHERE org = :org;
INSERT OR REPLACE INTO files (org, path, size, mtime, isdir, indexed_at)
  SELECT :org,
         json_extract(value, '\$.Path'),
         json_extract(value, '\$.Size'),
         json_extract(value, '\$.ModTime'),
         CASE WHEN json_extract(value, '\$.IsDir') THEN 1 ELSE 0 END,
         :ts
  FROM json_each(CAST(readfile(:src) AS TEXT));
COMMIT;
SQL
}

failed=0
indexed=0
skipped=0

while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  remote="$(jq -r '.remote' <<<"$org")"
  conf="$(conf_path "$CONF_DIR" "$name")"
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if [ ! -r "$conf" ]; then
    warn "$name: no rendered config at $conf; skipped (run: just render $name)"
    skipped=$((skipped + 1))
    record_run "$name" skipped "" "$started" "no rendered config"
    continue
  fi

  tmp="$STATE_DIR/$name.json.tmp"
  err="$STATE_DIR/$name.err"
  if rclone lsjson -R --fast-list --config "$conf" "${remote}:" >"$tmp" 2>"$err"; then
    mv -f -- "$tmp" "$STATE_DIR/$name.json"
    count="$(jq 'length' "$STATE_DIR/$name.json")"
    rollup "$name" "$STATE_DIR/$name.json" "$started"
    record_run "$name" ok "$count" "$started" ""
    info "$name: $count entries"
    indexed=$((indexed + 1))
  else
    rm -f -- "$tmp"
    warn "$name: listing failed (see $err)"
    record_run "$name" failed "" "$started" "rclone lsjson failed; see $err"
    failed=$((failed + 1))
  fi
done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS")

[ "$HAVE_SQLITE" = 1 ] && info "rollup at $DB (run $RUN_ID)"
info "indexed $indexed org(s), skipped $skipped, failed $failed"
[ "$failed" = 0 ] || exit 1
