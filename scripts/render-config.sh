#!/usr/bin/env bash
# render-config.sh — render one rclone config per enabled org from orgs.json
# plus the two operator-held secret files for that org.
#
# Secrets per org (two whole-document JSON files, first hit wins):
#   1. <PREFIX>_CLIENT_FILE / <PREFIX>_TOKEN_FILE   (PREFIX = org.secretEnvPrefix)
#   2. $GDRIVE_MOUNTS_SECRET_DIR/<org>/{client.json,token.json}
# client.json is the GCP OAuth Desktop client download, verbatim.
# token.json is the rclone authorize output, verbatim.
# GDRIVE_MOUNTS_DUMMY_SECRETS=1 renders shaped placeholders and reads no secret.
#
# Output: <out-dir>/rclone-<org>.conf, mode 0600, atomic rename, one writer per
# org so rclone's own token write-back never races another org's mount.
# Never prints a secret value.
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
usage: render-config.sh [--org NAME] [--orgs FILE] [--template FILE]
                        [--out FILE | --out-dir DIR] [--best-effort]
  --org NAME      render only this org (default: every enabled org)
  --orgs FILE     org registry (default: orgs.json next to the repo or package)
  --template FILE stanza template (default: config/rclone.conf.template)
  --out FILE      write one org to this exact path (requires a single org)
  --out-dir DIR   write <DIR>/rclone-<org>.conf (default: defaults.stateDir)
  --best-effort   warn and skip orgs whose secrets are missing; exit 0
USAGE
  exit 2
}

ORG=""
ORGS=""
TEMPLATE=""
OUT=""
OUT_DIR=""
BEST_EFFORT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --org) ORG="${2:-}"; shift 2 ;;
    --orgs) ORGS="${2:-}"; shift 2 ;;
    --template) TEMPLATE="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    --best-effort) BEST_EFFORT=1; shift ;;
    -h | --help) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_cmd jq "run inside the flake: nix develop --command just render"
[ -n "$ORGS" ] || ORGS="$(orgs_default)"
[ -n "$TEMPLATE" ] || TEMPLATE="$(template_default)"
[ -f "$ORGS" ] || die "org registry not found: $ORGS"
[ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"
[ -n "$OUT" ] && [ -n "$OUT_DIR" ] && die "--out and --out-dir are exclusive"
[ -n "$OUT" ] && [ -z "$ORG" ] && die "--out needs --org (one file holds one org)"
[ -n "$OUT_DIR" ] || OUT_DIR="$(state_dir "$ORGS")"

DUMMY="${GDRIVE_MOUNTS_DUMMY_SECRETS:-0}"
SECRET_DIR="$(secret_dir "$ORGS")"

# Stanza only. Comment lines never reach the output: a placeholder name inside
# a comment would be substituted too and print the secret a second time.
STANZA_TEMPLATE="$(sed -e '/^[[:space:]]*[#;]/d' "$TEMPLATE" | sed -n '/^[[:space:]]*\[/,$p')"
[ -n "$STANZA_TEMPLATE" ] || die "template has no [remote] stanza: $TEMPLATE"

selector='.orgs[] | select(.enabled == true)'
if [ -n "$ORG" ]; then
  jq -e --arg n "$ORG" '[.orgs[] | select(.name == $n)] | length == 1' "$ORGS" >/dev/null ||
    die "no such org in $ORGS: $ORG"
  jq -e --arg n "$ORG" '.orgs[] | select(.name == $n) | .enabled == true' "$ORGS" >/dev/null ||
    die "org is disabled in $ORGS: $ORG (enable it deliberately)"
  selector=".orgs[] | select(.name == \"$ORG\")"
fi

secure_tmpdir
tmpdir="$GDM_TMPDIR"
rendered=0
skipped=0

while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  remote="$(jq -r '.remote' <<<"$org")"
  scope="$(jq -r '.scope' <<<"$org")"
  prefix="$(jq -r '.secretEnvPrefix' <<<"$org")"
  drive_kind="$(jq -r '.driveKind // "mydrive"' <<<"$org")"
  team_drive="$(jq -r '.teamDriveId // ""' <<<"$org")"

  client_var="${prefix}_CLIENT_FILE"
  token_var="${prefix}_TOKEN_FILE"
  client_path="${!client_var:-}"
  token_path="${!token_var:-}"
  [ -n "$client_path" ] || client_path="$SECRET_DIR/$name/client.json"
  [ -n "$token_path" ] || token_path="$SECRET_DIR/$name/token.json"

  if [ "$DUMMY" = "1" ]; then
    client_id="DUMMY-$name-client-id"
    client_secret="DUMMY-$name-client-secret"
    token_json="{\"access_token\":\"DUMMY-$name-access\",\"token_type\":\"Bearer\",\"refresh_token\":\"DUMMY-$name-refresh\",\"expiry\":\"2099-01-01T00:00:00Z\"}"
  else
    if [ ! -r "$client_path" ] || [ ! -r "$token_path" ]; then
      if [ "$BEST_EFFORT" = "1" ]; then
        warn "$name: secrets not readable yet; skipped (client: $client_path, token: $token_path)"
        skipped=$((skipped + 1))
        continue
      fi
      die "$name: secret file not readable (set \$$client_var and \$$token_var, or place $SECRET_DIR/$name/{client,token}.json)"
    fi
    enc_tripwire "$client_path" "$name client.json"
    enc_tripwire "$token_path" "$name token.json"
    assert_json "$client_path" "$name client.json"
    assert_json "$token_path" "$name token.json"
    client_id="$(jq -er '(.installed // .web) | .client_id' "$client_path")" ||
      die "$name: client.json has no installed.client_id or web.client_id"
    client_secret="$(jq -er '(.installed // .web) | .client_secret' "$client_path")" ||
      die "$name: client.json has no installed.client_secret or web.client_secret"
    jq -e 'has("access_token") or has("refresh_token")' "$token_path" >/dev/null ||
      die "$name: token.json carries neither access_token nor refresh_token"
    # One line: an INI value cannot span lines, and sops hands back pretty JSON.
    token_json="$(jq -ec 'del(.sops)' "$token_path")" ||
      die "$name: token.json could not be compacted"
  fi

  stanza="$STANZA_TEMPLATE"
  stanza="${stanza//'{{REMOTE}}'/$remote}"
  stanza="${stanza//'{{SCOPE}}'/$scope}"
  stanza="${stanza//'@SECRET:client_id@'/$client_id}"
  stanza="${stanza//'@SECRET:client_secret@'/$client_secret}"
  stanza="${stanza//'@SECRET:token_json@'/$token_json}"

  if [ "$drive_kind" = "shared" ]; then
    [ -n "$team_drive" ] || die "$name: driveKind is shared but teamDriveId is unset"
    stanza="$stanza"$'\n'"team_drive = $team_drive"
  fi

  if [ -n "$OUT" ]; then dest="$OUT"; else dest="$(conf_path "$OUT_DIR" "$name")"; fi
  mkdir_secure "$(dirname -- "$dest")"
  {
    printf '# Rendered %s by gdrive-mounts render-config.sh. Do not edit; rclone owns this file after mount.\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' "$stanza"
  } >"$tmpdir/rclone-$name.conf"
  install_secret_file "$tmpdir/rclone-$name.conf" "$dest"
  wipe_file "$tmpdir/rclone-$name.conf"
  info "wrote $dest (mode 0600)"
  rendered=$((rendered + 1))
done < <(jq -c "$selector" "$ORGS")

if [ "$rendered" = 0 ]; then
  [ "$BEST_EFFORT" = "1" ] || die "nothing rendered (no enabled org matched)"
  warn "nothing rendered; $skipped org(s) skipped"
fi
info "rendered $rendered org(s), skipped $skipped"
