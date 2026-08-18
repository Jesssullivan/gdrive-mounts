#!/usr/bin/env bash
# validate-config.sh — structural validation of orgs.json, the render pipeline,
# and the two nix-side invariants that keep orgs.json the single source of truth.
# Zero network. Zero real secrets (dummy render only).
#
# --quick runs the structural and nix assertions only: it skips the dummy render
# and the rclone parse. Use it where no rclone is on PATH.
set -euo pipefail

d="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
if [ -f "$d/lib/common.sh" ]; then . "$d/lib/common.sh"; else . "$d/../libexec/gdrive-mounts/common.sh"; fi
no_trace_guard

QUICK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) QUICK=1; shift ;;
    -h | --help)
      echo "usage: validate-config.sh [--quick]" >&2
      exit 2
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

cd "$(repo_root)"

if ! command -v jq >/dev/null 2>&1; then
  if command -v nix >/dev/null 2>&1 && [ "${_GDM_REEXEC:-0}" != "1" ]; then
    reexec=()
    [ "$QUICK" = 1 ] && reexec+=(--quick)
    exec env _GDM_REEXEC=1 nix develop --command bash scripts/validate-config.sh ${reexec[@]+"${reexec[@]}"}
  fi
  die "jq required and no nix to re-exec under"
fi

ORGS=orgs.json
SCHEMA=config/orgs.schema.json
TEMPLATE=config/rclone.conf.template
PLAN=nix/lib/plan.nix
MODULE=nix/modules/home-manager.nix

[ -f "$ORGS" ] || die "$ORGS not found (run from the repo)"
[ -f "$SCHEMA" ] || die "$SCHEMA not found"
[ -f "$TEMPLATE" ] || die "$TEMPLATE not found"

fail() { die "$*"; }

# ── 1. schema ───────────────────────────────────────────────────────────────
if command -v check-jsonschema >/dev/null 2>&1; then
  check-jsonschema --schemafile "$SCHEMA" "$ORGS" >&2 || fail "orgs.json does not satisfy $SCHEMA"
  info "schema OK ($SCHEMA)"
else
  warn "check-jsonschema not on PATH; schema not enforced, jq cross-field checks only"
fi

# ── 2. cross-field rules the schema cannot express ──────────────────────────
jq -e '.schema_version == 1' "$ORGS" >/dev/null || fail "schema_version != 1"
jq -e '(.orgs | length) >= 1' "$ORGS" >/dev/null || fail "no orgs"
jq -e '(.orgs | map(.name)) as $n | ($n | unique | length) == ($n | length)' "$ORGS" >/dev/null || fail "duplicate org names"
jq -e '(.orgs | map(.remote)) as $r | ($r | unique | length) == ($r | length)' "$ORGS" >/dev/null || fail "duplicate remotes"
jq -e '(.orgs | map(.secretEnvPrefix)) as $p | ($p | unique | length) == ($p | length)' "$ORGS" >/dev/null || fail "duplicate secretEnvPrefix"
jq -e 'all(.orgs[]; .scope == "drive.readonly" or .scope == "drive" or .scope == "drive.file")' "$ORGS" >/dev/null || fail "bad scope value"
jq -e 'all(.orgs[]; (.enabled == true) or (.enabled == false))' "$ORGS" >/dev/null || fail "enabled must be boolean"
jq -e 'all(.orgs[]; .account | test("@"))' "$ORGS" >/dev/null || fail "account must be an email-shaped string"

# A disabled org carries no mounts and no links: enabling is a deliberate edit.
jq -e 'all(.orgs[] | select(.enabled == false); ((.mounts // []) | length) == 0)' "$ORGS" >/dev/null ||
  fail "a disabled org declares mounts (enable it deliberately)"
jq -e 'all(.orgs[] | select(.enabled == false); ((.links // []) | length) == 0)' "$ORGS" >/dev/null ||
  fail "a disabled org declares links (enable it deliberately)"

# A shared drive needs its id, or the mount silently lands on My Drive.
jq -e 'all(.orgs[] | select(.driveKind == "shared"); (.teamDriveId // "") != "")' "$ORGS" >/dev/null ||
  fail "driveKind \"shared\" without teamDriveId"

# Mount points must not collide across orgs.
jq -e '[.orgs[].mounts // [] | .[].mountSuffix] as $s | ($s | unique | length) == ($s | length)' "$ORGS" >/dev/null ||
  fail "duplicate mountSuffix across orgs (two mounts would fight for one path)"

# links: a name and a non-empty target inside the org mount, unique per org.
jq -e 'all(.orgs[]; ((.links // []) | all((.name // "") != "" and (.target // "") != "")))' "$ORGS" >/dev/null ||
  fail "a link declares an empty name or target"
jq -e 'all(.orgs[]; ((.links // []) | map(.name)) as $n | ($n | unique | length) == ($n | length))' "$ORGS" >/dev/null ||
  fail "duplicate link name within one org"
jq -e 'all(.orgs[]; ((.links // []) | all((.target | startswith("/")) | not)))' "$ORGS" >/dev/null ||
  fail "a link target is absolute; targets are relative to the org mount"

info "structure OK ($(jq '[.orgs[] | select(.enabled == true)] | length' "$ORGS") enabled org(s))"

# ── 3. secret-material tripwire (gitleaks is the real gate) ─────────────────
if grep -rEq 'ya29\.[0-9A-Za-z_-]{20,}|1//[0-9A-Za-z_-]{20,}|"access_token"|ENC\[AES256_GCM,' "$ORGS" config/ 2>/dev/null; then
  fail "secret-looking material in checked-in config"
fi

# ── 4. orgs.json is the settings SSOT ───────────────────────────────────────
# The key list is read out of plan.nix so it cannot rot: every defaults.<key>
# that the nix side reads must exist in orgs.json defaults.
if [ -d nix ]; then
  [ -f "$PLAN" ] || fail "$PLAN missing; the nix side has no settings reader to check orgs.json against"
  [ -f "$MODULE" ] || fail "$MODULE missing"
  plan_keys="$(grep -oE 'defaults\.[A-Za-z_][A-Za-z0-9_]*' "$PLAN" | sed 's/^defaults\.//' | sort -u || true)"
  [ -n "$plan_keys" ] || fail "read no defaults.<key> out of $PLAN; the SSOT assertion would pass vacuously"
  missing=""
  while IFS= read -r k; do
    [ -n "$k" ] || continue
    jq -e --arg k "$k" '.defaults | has($k)' "$ORGS" >/dev/null || missing="$missing $k"
  done <<<"$plan_keys"
  [ -z "$missing" ] || fail "orgs.json defaults is missing key(s) that $PLAN reads:$missing"
  info "defaults SSOT OK ($(printf '%s\n' "$plan_keys" | wc -l | tr -d ' ') key(s) read from $PLAN)"

  # The platform decision lives in exactly one place, so the eval fixture can
  # flip it and exercise both branches on one runner.
  # grep -o then wc -l: grep -c would count matching lines, not occurrences.
  n_stdenv="$(grep -o 'stdenv\.is' "$MODULE" | wc -l | tr -d ' ' || true)"
  [ -n "$n_stdenv" ] || n_stdenv=0
  [ "$n_stdenv" = "1" ] || fail "'stdenv.is' appears $n_stdenv time(s) in $MODULE; exactly one platform decision is allowed"
  info "platform decision OK (one stdenv.is in $MODULE)"
else
  warn "no nix/ directory here; skipped the defaults-SSOT and platform assertions"
fi

# ── 5. render pipeline (dummy secrets) ──────────────────────────────────────
if [ "$QUICK" = 1 ]; then
  info "OK (--quick: render and rclone parse skipped)"
  exit 0
fi

secure_tmpdir
out="$GDM_TMPDIR/conf"
GDRIVE_MOUNTS_DUMMY_SECRETS=1 bash scripts/render-config.sh --orgs "$ORGS" --template "$TEMPLATE" --out-dir "$out" >/dev/null 2>&1 ||
  fail "dummy render failed (run it directly to see why)"

n_orgs="$(jq '[.orgs[] | select(.enabled == true)] | length' "$ORGS")"
n_conf="$(find "$out" -maxdepth 1 -name 'rclone-*.conf' | wc -l | tr -d ' ')"
[ "$n_conf" = "$n_orgs" ] || fail "rendered $n_conf config(s), expected $n_orgs"

while IFS= read -r name; do
  conf="$out/rclone-$name.conf"
  [ -f "$conf" ] || fail "no rendered config for $name"
  grep -q '^\[' "$conf" || fail "$name: rendered config has no stanza header"
  grep -q 'DUMMY-' "$conf" || fail "$name: placeholder substitution did not happen"
  if grep -q '@SECRET:' "$conf"; then fail "$name: an unsubstituted @SECRET: placeholder survived"; fi
  [ "$(grep -c '^token = ' "$conf")" = "1" ] || fail "$name: token is not exactly one line"
  [ "$(file_mode "$conf")" = "600" ] || fail "$name: rendered config is not mode 0600"
done < <(jq -r '.orgs[] | select(.enabled == true) | .name' "$ORGS")

if command -v rclone >/dev/null 2>&1; then
  while IFS= read -r name; do
    remote="$(jq -r --arg n "$name" '.orgs[] | select(.name == $n) | .remote' "$ORGS")"
    rclone listremotes --config "$out/rclone-$name.conf" | grep -qx "${remote}:" ||
      fail "rclone rejected the rendered config for $name"
  done < <(jq -r '.orgs[] | select(.enabled == true) | .name' "$ORGS")
  info "rclone parsed every rendered config"
else
  warn "rclone not on PATH; skipped the parser check (nix develop provides it)"
fi

info "OK ($n_orgs enabled org(s); schema, structure, SSOT, render green)"
