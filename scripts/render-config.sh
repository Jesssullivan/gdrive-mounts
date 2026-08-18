#!/usr/bin/env bash
# render-config.sh — materialize the runtime rclone.conf from orgs.json +
# operator-held secret files. House rule: this repo holds NO secret material.
#
# Secret resolution per enabled org, first hit wins:
#   1. <PREFIX>_CLIENT_ID_FILE / <PREFIX>_CLIENT_SECRET_FILE / <PREFIX>_TOKEN_FILE
#      (PREFIX = org.secretEnvPrefix, e.g. GDM_SULLIWOOD) — sops-nix paths from lab
#   2. $GDRIVE_MOUNTS_SECRET_DIR/<org>/{client_id,client_secret,token.json}
# Validation renders use GDRIVE_MOUNTS_DUMMY_SECRETS=1 (never real values).
#
# Output: $XDG_STATE_HOME/gdrive-mounts/rclone.conf, mode 0600, atomic rename.
# Never prints secret values.
set -euo pipefail

ORGS_JSON="${1:-orgs.json}"
TEMPLATE="${2:-config/rclone.conf.template}"
OUT="${3:-${XDG_STATE_HOME:-$HOME/.local/state}/gdrive-mounts/rclone.conf}"
DUMMY="${GDRIVE_MOUNTS_DUMMY_SECRETS:-0}"

command -v jq >/dev/null || { echo "render-config: jq required (use: nix develop --command just render)" >&2; exit 1; }
[[ -f "$ORGS_JSON" ]] || { echo "render-config: $ORGS_JSON not found" >&2; exit 1; }
[[ -f "$TEMPLATE" ]] || { echo "render-config: $TEMPLATE not found" >&2; exit 1; }

read_secret() { # $1=env-file-var $2=fallback-path $3=label -> stdout, fail-closed
  local file_var="$1" fallback="$2" label="$3" path=""
  path="${!file_var:-}"
  [[ -z "$path" && -n "$fallback" ]] && path="$fallback"
  if [[ "$DUMMY" == "1" ]]; then printf 'DUMMY-%s' "$label"; return 0; fi
  [[ -n "$path" && -r "$path" ]] || { echo "render-config: missing unreadable secret for $label (set \$$file_var)" >&2; exit 1; }
  head -c 65536 "$path" | tr -d '\r' | sed -e '1s/^[[:space:]]*//' -e '$s/[[:space:]]*$//'
}

tmp="$(mktemp "${OUT}.tmp.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  echo "# Rendered $(date -u +%Y-%m-%dT%H:%M:%SZ) by gdrive-mounts render-config.sh — do not edit."
  while IFS= read -r org; do
    name="$(jq -r '.name' <<<"$org")"
    remote="$(jq -r '.remote' <<<"$org")"
    scope="$(jq -r '.scope' <<<"$org")"
    prefix="$(jq -r '.secretEnvPrefix' <<<"$org")"
    sdir="${GDRIVE_MOUNTS_SECRET_DIR:-${XDG_RUNTIME_DIR:-$HOME/.local/state}/gdrive-mounts/secrets}/$name"
    client_id="$(read_secret "${prefix}_CLIENT_ID_FILE" "$sdir/client_id" "$name-client-id")"
    client_secret="$(read_secret "${prefix}_CLIENT_SECRET_FILE" "$sdir/client_secret" "$name-client-secret")"
    token_json="$(read_secret "${prefix}_TOKEN_FILE" "$sdir/token.json" "$name-token")"
    stanza="$(cat "$TEMPLATE")"
    stanza="${stanza//'{{REMOTE}}'/$remote}"
    stanza="${stanza//'{{SCOPE}}'/$scope}"
    stanza="${stanza//'@SECRET:client_id@'/$client_id}"
    stanza="${stanza//'@SECRET:client_secret@'/$client_secret}"
    stanza="${stanza//'@SECRET:token_json@'/$token_json}"
    printf '%s\n' "$stanza"
  done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS_JSON")
} > "$tmp"
chmod 600 "$tmp"
mkdir -p "$(dirname "$OUT")"
mv -f "$tmp" "$OUT"
trap - EXIT
echo "render-config: wrote $OUT ($(grep -c '^\[' "$OUT") remotes; mode 0600)"
