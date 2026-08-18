#!/usr/bin/env bash
# import-client.sh — take the GCP OAuth Desktop client download into the stage
# directory, verbatim, at 0600, and wipe the download.
#
# The file stays a whole document: render-config.sh reads client_id and
# client_secret out of it with jq, so nothing is ever retyped by hand.
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
usage: import-client.sh <org> <downloaded-client.json> [--keep-source] [--orgs FILE]
  --keep-source  leave the download in place (default: wipe it)
USAGE
  exit 2
}

ORG=""
SRC=""
ORGS=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-source) KEEP=1; shift ;;
    --orgs) ORGS="${2:-}"; shift 2 ;;
    -h | --help) usage ;;
    -*) die "unknown argument: $1 (see --help)" ;;
    *)
      if [ -z "$ORG" ]; then ORG="$1"; elif [ -z "$SRC" ]; then SRC="$1"; else die "too many arguments"; fi
      shift
      ;;
  esac
done
[ -n "$ORG" ] && [ -n "$SRC" ] || usage

require_cmd jq "run inside the flake: nix develop --command just import-client"
assert_gnu_coreutils
[ -n "$ORGS" ] || ORGS="$(orgs_default)"
[ -f "$ORGS" ] || die "org registry not found: $ORGS"
[ -r "$SRC" ] || die "cannot read $SRC"

jq -e --arg n "$ORG" '[.orgs[] | select(.name == $n)] | length == 1' "$ORGS" >/dev/null || die "no such org: $ORG"
jq -e --arg n "$ORG" '.orgs[] | select(.name == $n) | .enabled == true' "$ORGS" >/dev/null ||
  die "org is disabled: $ORG (enable it in $ORGS first)"

enc_tripwire "$SRC" "download"
assert_json "$SRC" "download"
jq -e '(.installed // .web) | has("client_id") and has("client_secret")' "$SRC" >/dev/null ||
  die "not an OAuth client download: no installed.client_id/client_secret and no web.* pair"
if ! jq -e 'has("installed")' "$SRC" >/dev/null; then
  warn "this is a Web client, not a Desktop client; the loopback consent flow expects a Desktop client"
fi

STAGE="$(stage_dir "$ORGS")"
DEST="$STAGE/$ORG/client.json"
mkdir_secure "$STAGE" "$STAGE/$ORG"
install_secret_file "$SRC" "$DEST"

mode="$(file_mode "$DEST")"
[ "$mode" = "600" ] || die "staged file is mode $mode, expected 600: $DEST"

fp="$(jq -r '(.installed // .web) | .client_id' "$DEST" | redact)"
info "staged $DEST (mode 600)"
info "client_id fingerprint $fp"

if [ "$KEEP" = 1 ]; then
  warn "source kept at $SRC — delete it once the mint succeeds"
else
  wipe_file "$SRC"
  info "wiped $SRC"
fi
warn "a download may also exist in Time Machine, iCloud Drive, or the browser download list; clear those by hand"
info "next: just mint-token $ORG"
