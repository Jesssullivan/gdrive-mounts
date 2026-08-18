#!/usr/bin/env bash
# gdrive-index.sh — refresh the local metadata index for all enabled orgs.
# Per org: rclone lsjson -R --fast-list -> <stateDir>/<org>.json (atomic),
# then a sqlite rollup at <stateDir>/index.sqlite (table: files).
# This is the agent/AX query surface — Spotlight does not index these mounts.
set -euo pipefail

ORGS_JSON="${1:-orgs.json}"
STATE_DEFAULT="${XDG_STATE_HOME:-$HOME/.local/state}/gdrive-index"
CONF="${GDRIVE_MOUNTS_RCLONE_CONFIG:-${XDG_STATE_HOME:-$HOME/.local/state}/gdrive-mounts/rclone.conf}"

command -v jq >/dev/null || { echo "gdrive-index: jq required" >&2; exit 1; }
command -v rclone >/dev/null || { echo "gdrive-index: rclone required (nix develop)" >&2; exit 1; }
[[ -r "$CONF" ]] || { echo "gdrive-index: runtime config $CONF missing — run 'just render' first" >&2; exit 1; }

STATE_DIR="$(jq -r '.defaults.indexStateDir // ""' "$ORGS_JSON")"
STATE_DIR="${STATE_DIR:-$STATE_DEFAULT}"
STATE_DIR="${STATE_DIR/#\~/$HOME}"
mkdir -p "$STATE_DIR"

while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  remote="$(jq -r '.remote' <<<"$org")"
  tmp="$STATE_DIR/$name.json.tmp"
  if rclone lsjson -R --fast-list --config "$CONF" "${remote}:" > "$tmp" 2>"$STATE_DIR/$name.err"; then
    mv -f "$tmp" "$STATE_DIR/$name.json"
    count="$(jq 'length' "$STATE_DIR/$name.json")"
    echo "gdrive-index: $name -> $count entries"
  else
    echo "gdrive-index: $name FAILED (see $STATE_DIR/$name.err)" >&2
    rm -f "$tmp"
  fi
done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS_JSON")

if command -v sqlite3 >/dev/null; then
  db="$STATE_DIR/index.sqlite"
  sqlite3 "$db" <<'SQL'
CREATE TABLE IF NOT EXISTS files (
  org TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, mtime TEXT, isdir INTEGER NOT NULL,
  indexed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (org, path)
);
SQL
  while IFS= read -r org; do
    name="$(jq -r '.name' <<<"$org")"
    [[ -f "$STATE_DIR/$name.json" ]] || continue
    jq -r '.[] | [.Path, (.Size // -1), (.ModTime // ""), (if .IsDir then 1 else 0 end)] | @tsv' \
      "$STATE_DIR/$name.json" > "$STATE_DIR/$name.tsv"
    sqlite3 "$db" -cmd ".timeout 5000" <<SQL
.mode tabs
CREATE TEMP TABLE imp(path TEXT, size INTEGER, mtime TEXT, isdir INTEGER);
.import '$STATE_DIR/$name.tsv' imp
DELETE FROM files WHERE org = '$name';
INSERT INTO files (org, path, size, mtime, isdir) SELECT '$name', path, size, mtime, isdir FROM imp;
DROP TABLE imp;
SQL
    rm -f "$STATE_DIR/$name.tsv"
  done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS_JSON")
  echo "gdrive-index: sqlite rollup at $db"
fi
