#!/usr/bin/env bash
# mint-token.sh — mint a scope-carrying OAuth token for one org.
#
# rclone authorize "drive" <client_id> <client_secret> mints a full drive
# token with no scope. The scope only survives inside the base64 config blob
# that rclone config create emits, so this script builds that blob first and
# authorizes with it. rclone config create has no --auth-no-open-browser, so it
# is never used to run the consent flow: that would launch a GUI browser.
#
# Accepted exposure, stated rather than hidden: the blob carries the client
# secret and rclone takes it on argv, so it is visible in ps for the length of
# the consent window. rclone offers no stdin form. Mint on the operator's own
# terminal; over ssh forward the loopback port with -L 53682:127.0.0.1:53682.
#
# The token block reaches a 0600 file and never a shell variable, a log, or a
# terminal.
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
  echo "usage: mint-token.sh <org> [--orgs FILE]" >&2
  exit 2
}

ORG=""
ORGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --orgs) ORGS="${2:-}"; shift 2 ;;
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

require_cmd jq "run inside the flake: nix develop --command just mint-token"
require_cmd rclone "run inside the flake: nix develop --command just mint-token"
assert_gnu_coreutils
[ -n "$ORGS" ] || ORGS="$(orgs_default)"
[ -f "$ORGS" ] || die "org registry not found: $ORGS"

org_json="$(jq -c --arg n "$ORG" '.orgs[] | select(.name == $n)' "$ORGS")"
[ -n "$org_json" ] || die "no such org: $ORG"
[ "$(jq -r '.enabled' <<<"$org_json")" = "true" ] || die "org is disabled: $ORG"
remote="$(jq -r '.remote' <<<"$org_json")"
scope="$(jq -r '.scope' <<<"$org_json")"
prefix="$(jq -r '.secretEnvPrefix' <<<"$org_json")"

STAGE="$(stage_dir "$ORGS")"
client="$STAGE/$ORG/client.json"
if [ ! -r "$client" ]; then
  cvar="${prefix}_CLIENT_FILE"
  client="${!cvar:-$(secret_dir "$ORGS")/$ORG/client.json}"
fi
[ -r "$client" ] || die "no client.json for $ORG (run: just import-client $ORG <download>)"
enc_tripwire "$client" "$ORG client.json"
assert_json "$client" "$ORG client.json"

client_id="$(jq -er '(.installed // .web) | .client_id' "$client")" || die "client.json has no client_id"
client_secret="$(jq -er '(.installed // .web) | .client_secret' "$client")" || die "client.json has no client_secret"

secure_tmpdir
tmp="$GDM_TMPDIR"

# Step 1: the scope-carrying blob. --config /dev/null keeps rclone from
# touching any real config; --non-interactive makes it print the blob instead
# of opening a browser.
if ! rclone config create "$remote" drive \
  "client_id=$client_id" \
  "client_secret=$client_secret" \
  "scope=$scope" \
  config_is_local=false \
  --non-interactive \
  --config /dev/null >"$tmp/create.json" 2>"$tmp/create.err"; then
  warn "rclone config create failed:"
  tail -5 "$tmp/create.err" >&2 || true
  die "could not build the authorize blob for $ORG"
fi

# A pipeline failure here must reach the message below, not abort the script.
blob="$(jq -r '.Option.Help // ""' "$tmp/create.json" 2>/dev/null |
  sed -n 's/.*rclone authorize "drive" "\([^"]*\)".*/\1/p' | head -1 || true)"
[ -n "$blob" ] || die "rclone did not return an authorize blob (rclone version changed the config create output?)"

cat >&2 <<BANNER

mint-token: $ORG — consent runs in the operator's own browser.
  scope requested : $scope
  remote          : $remote
  rclone prints a http://127.0.0.1:53682/auth?... URL below. Open it, consent
  as the org account, then come back. Nothing is pasted back into this
  terminal. Over ssh, forward the port first: -L 53682:127.0.0.1:53682

BANNER

# Step 2: consent. stdout carries the token block and goes straight to a file.
# stderr is inherited so the operator sees the URL and rclone's progress.
if ! rclone authorize "drive" "$blob" --auth-no-open-browser >"$tmp/out"; then
  die "rclone authorize failed for $ORG"
fi

sed -n '/Paste the following into your remote machine/,/<---End paste/p' "$tmp/out" |
  sed -e '1d' -e '$d' >"$tmp/token.json"
[ -s "$tmp/token.json" ] || die "no token block between the paste markers (rclone output shape changed?)"
assert_json "$tmp/token.json" "minted token"
jq -e 'has("access_token") and has("refresh_token")' "$tmp/token.json" >/dev/null ||
  die "minted token carries no refresh_token; consent did not complete"

mkdir_secure "$STAGE" "$STAGE/$ORG"
install_secret_file "$tmp/token.json" "$STAGE/$ORG/token.json"

expiry="$(jq -r '.expiry // "unknown"' "$STAGE/$ORG/token.json")"
fp="$(jq -r '.refresh_token' "$STAGE/$ORG/token.json" | redact)"
jq -n \
  --arg org "$ORG" --arg remote "$remote" --arg scope "$scope" \
  --arg minted "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg expiry "$expiry" --arg fp "$fp" \
  '{schema_version: 1, org: $org, remote: $remote, scope: $scope, minted_at: $minted, expiry: $expiry, refresh_fingerprint: $fp}' \
  >"$tmp/mint.meta.json"
install_secret_file "$tmp/mint.meta.json" "$STAGE/$ORG/mint.meta.json"

info "staged $STAGE/$ORG/token.json (mode 600)"
info "scope $scope, expiry $expiry, refresh fingerprint $fp"
info "next: just seed-lab $ORG"
