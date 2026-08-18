#!/usr/bin/env bash
# smoke.sh — prove one org's credentials reach Drive: list the top-level
# folder names through that org's rendered config.
#
# It renders the config only when one is absent, and it never prints the
# config: names in, names out.
set -euo pipefail

d="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
if [ -f "$d/lib/common.sh" ]; then . "$d/lib/common.sh"; else . "$d/../libexec/gdrive-mounts/common.sh"; fi
no_trace_guard

usage() {
  echo "usage: smoke.sh <org> [--orgs FILE] [--conf-dir DIR]" >&2
  exit 2
}

ORG=""
ORGS=""
CONF_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --orgs) ORGS="${2:-}"; shift 2 ;;
    --conf-dir) CONF_DIR="${2:-}"; shift 2 ;;
    -h | --help) usage ;;
    -*) die "unknown argument: $1 (see --help)" ;;
    *)
      [ -z "$ORG" ] || die "too many arguments"
      ORG="$1"
      shift
      ;;
  esac
done
[ -n "$ORG" ] || usage

require_cmd jq "run inside the flake: nix develop --command just smoke"
require_cmd rclone "run inside the flake: nix develop --command just smoke"
[ -n "$ORGS" ] || ORGS="$(orgs_default)"
[ -f "$ORGS" ] || die "org registry not found: $ORGS"
[ -n "$CONF_DIR" ] || CONF_DIR="$(state_dir "$ORGS")"

remote="$(jq -r --arg n "$ORG" '.orgs[] | select(.name == $n) | .remote' "$ORGS")"
[ -n "$remote" ] && [ "$remote" != "null" ] || die "no such org: $ORG"

conf="$(conf_path "$CONF_DIR" "$ORG")"
if [ ! -r "$conf" ]; then
  info "no rendered config; rendering $ORG"
  bash "$d/render-config.sh" --orgs "$ORGS" --org "$ORG" --out-dir "$CONF_DIR" >&2
fi
[ -r "$conf" ] || die "still no config at $conf"

rclone lsjson --dirs-only --max-depth 1 \
  --config "$conf" \
  --log-level ERROR --retries 1 --timeout 30s --contimeout 15s \
  "${remote}:" | jq -r '.[].Name'
