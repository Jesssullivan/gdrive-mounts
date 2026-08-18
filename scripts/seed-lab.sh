#!/usr/bin/env bash
# seed-lab.sh — encrypt the staged secrets for one org into the lab secrets
# tree with sops, in place, as whole JSON documents.
#
# It stops before git: it writes files and prints what to do next. Adding and
# committing the ciphertext is the operator's act.
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
usage: seed-lab.sh <org> [--keep-stage] [--lab-dir DIR] [--orgs FILE]
  --keep-stage   leave the staged plaintext in place (default: wipe it)
  --lab-dir DIR  lab checkout (default: $LAB_DIR, else ~/git/lab)
USAGE
  exit 2
}

ORG=""
ORGS=""
KEEP=0
LAB="${LAB_DIR:-$HOME/git/lab}"
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-stage) KEEP=1; shift ;;
    --lab-dir) LAB="${2:-}"; shift 2 ;;
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

require_cmd jq "run inside the flake: nix develop --command just seed-lab"
require_cmd sops "run inside the flake: nix develop --command just seed-lab"
assert_gnu_coreutils
[ -n "$ORGS" ] || ORGS="$(orgs_default)"
[ -f "$ORGS" ] || die "org registry not found: $ORGS"
jq -e --arg n "$ORG" '[.orgs[] | select(.name == $n)] | length == 1' "$ORGS" >/dev/null || die "no such org: $ORG"
[ -d "$LAB" ] || die "lab checkout not found: $LAB (pass --lab-dir)"

STAGE="$(stage_dir "$ORGS")"
STATE="$(state_dir "$ORGS")"
src_client="$STAGE/$ORG/client.json"
src_token="$STAGE/$ORG/token.json"
[ -r "$src_client" ] || die "no staged client.json (run: just import-client $ORG <download>)"
[ -r "$src_token" ] || die "no staged token.json (run: just mint-token $ORG)"

REL="nix/secrets/gdrive-mounts/$ORG"
DEST="$LAB/$REL"

secure_tmpdir
tmp="$GDM_TMPDIR"

# Probe the creation rule before writing anything: sops with no matching rule
# leaves plaintext behind under a name that looks encrypted.
#
# sops matches path_regex against the path as given, so every sops call here
# runs with the lab checkout as the working directory and a repo-relative
# path. An absolute path never matches a rule written as nix/secrets/...
if ! (cd "$LAB" && printf '{"probe":1}' |
  sops --encrypt --filename-override "$REL/client.json" \
    --input-type json --output-type json /dev/stdin) >"$tmp/probe.json" 2>"$tmp/probe.err"; then
  if grep -qi 'no matching creation rules' "$tmp/probe.err"; then
    cat >&2 <<RULE
seed-lab: FAIL $LAB/.sops.yaml has no creation rule for $REL.
The catch-all rule matches .yaml only, so a .json leaf is never encrypted.
Add this rule (copy the key_groups block from the gmailctl rule in the same file):

  - path_regex: nix/secrets/gdrive-mounts/.*\.json\$
    key_groups:
      - age:
          # same fleet age recipients as the gmailctl rule

Then run seed-lab again.
RULE
    exit 1
  fi
  warn "sops probe failed:"
  tail -5 "$tmp/probe.err" >&2 || true
  die "sops cannot encrypt into $DEST"
fi

mkdir -p "$DEST"
chmod 755 "$DEST" 2>/dev/null || true

seed_one() { # src dest-basename
  local src="$1" base="$2" dest="$DEST/$2" t
  t="$(mktemp "$DEST/.gdm.XXXXXX")"
  cat -- "$src" >"$t"
  chmod 600 "$t"
  mv -f -- "$t" "$dest"
  (cd "$LAB" && sops --encrypt --in-place "$REL/$base") || die "sops could not encrypt $dest"
  jq -e '.sops.mac' "$dest" >/dev/null || die "$dest has no sops.mac; it is not encrypted"
}

seed_one "$src_client" client.json
seed_one "$src_token" token.json

jq -e '(.installed // .web) | .client_secret | startswith("ENC[")' "$DEST/client.json" >/dev/null ||
  die "$DEST/client.json client_secret is not ciphertext"
jq -e '(.access_token // .refresh_token) | startswith("ENC[")' "$DEST/token.json" >/dev/null ||
  die "$DEST/token.json token values are not ciphertext"

# Keep the non-secret mint record where doctor can still read it after the
# stage is wiped: it is what proves which scope was actually granted.
if [ -r "$STAGE/$ORG/mint.meta.json" ]; then
  mkdir_secure "$STATE"
  install_secret_file "$STAGE/$ORG/mint.meta.json" "$STATE/mint.meta.$ORG.json"
  info "kept mint record at $STATE/mint.meta.$ORG.json"
fi

if [ "$KEEP" = 1 ]; then
  warn "staged plaintext kept at $STAGE/$ORG — wipe it once the lab switch is green"
else
  wipe_file "$src_client" "$src_token" "$STAGE/$ORG/mint.meta.json"
  rmdir "$STAGE/$ORG" 2>/dev/null || true
  info "wiped the stage for $ORG"
fi

info "wrote $DEST/client.json and $DEST/token.json (sops ciphertext)"
cat >&2 <<SNIPPET

Lab wiring for $ORG:

  # nix/secrets/default.nix — presence-gated, whole-document JSON
  sops.secrets."gdrive-mounts/$ORG/client" = {
    sopsFile = ./gdrive-mounts/$ORG/client.json;
    format = "json";
    key = "";
    path = "\${config.home.homeDirectory}/.local/state/gdrive-mounts/secrets/$ORG/client.json";
  };
  sops.secrets."gdrive-mounts/$ORG/token" = {
    sopsFile = ./gdrive-mounts/$ORG/token.json;
    format = "json";
    key = "";
    path = "\${config.home.homeDirectory}/.local/state/gdrive-mounts/secrets/$ORG/token.json";
  };

  # nix/home-manager/gdrive-mounts.nix
  programs.gdrive-mounts.secrets."$ORG" = {
    clientFile = config.sops.secrets."gdrive-mounts/$ORG/client".path;
    tokenFile = config.sops.secrets."gdrive-mounts/$ORG/token".path;
  };

SNIPPET

if git -C "$LAB" rev-parse --git-dir >/dev/null 2>&1; then
  info "lab working tree (nothing was staged or committed):"
  git -C "$LAB" status --short -- "$REL" >&2 || true
fi
