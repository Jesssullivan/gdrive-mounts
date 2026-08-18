#!/usr/bin/env bash
# validate-config.sh — structural validation of orgs.json + render pipeline.
# Zero network. Zero real secrets (dummy render only). --quick for bazel marker.
set -euo pipefail
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if ! command -v jq >/dev/null; then
  if command -v nix >/dev/null && [[ "${_GDM_REEXEC:-0}" != "1" ]]; then
    exec env _GDM_REEXEC=1 nix develop --command bash scripts/validate-config.sh ${QUICK:+--quick}
  fi
  echo "validate-config: jq required and no nix to re-exec under" >&2; exit 1
fi

fail() { echo "validate-config: FAIL: $*" >&2; exit 1; }

# ── orgs.json structure ─────────────────────────────────────────────────────
jq -e '.schema_version == 1' orgs.json >/dev/null || fail "schema_version != 1"
jq -e '(.orgs | length) >= 1' orgs.json >/dev/null || fail "no orgs"
jq -e '(.orgs | map(.name)) as $n | ($n | unique | length) == ($n | length)' orgs.json >/dev/null || fail "duplicate org names"
jq -e '(.orgs | map(.remote)) as $r | ($r | unique | length) == ($r | length)' orgs.json >/dev/null || fail "duplicate remotes"
jq -e '(.orgs | map(.secretEnvPrefix)) as $p | ($p | unique | length) == ($p | length)' orgs.json >/dev/null || fail "duplicate secretEnvPrefix"
jq -e 'all(.orgs[]; .scope == "drive.readonly" or .scope == "drive" or .scope == "drive.file")' orgs.json >/dev/null || fail "bad scope value"
jq -e 'all(.orgs[]; (.enabled == true) or (.enabled == false))' orgs.json >/dev/null || fail "enabled must be boolean"
jq -e 'all(.orgs[] | select(.enabled == false); (.mounts | length) == 0)' orgs.json >/dev/null || fail "disabled org must carry zero mounts (enable deliberately)"
jq -e 'all(.orgs[]; .account | test("@"))' orgs.json >/dev/null || fail "account must be an email-shaped string"

# ── secret-material tripwire (defense in depth; gitleaks is the real gate) ──
if grep -rE 'ya29\.[0-9A-Za-z_-]{20,}|1//[0-9A-Za-z_-]{20,}|"access_token"' orgs.json config/ 2>/dev/null; then
  fail "secret-looking material in checked-in config"
fi

# ── render pipeline (dummy secrets) + rclone syntax check ───────────────────
tmpdir="$(mktemp -d)"; trap 'rm -rf "$tmpdir"' EXIT
GDRIVE_MOUNTS_DUMMY_SECRETS=1 bash scripts/render-config.sh orgs.json config/rclone.conf.template "$tmpdir/rclone.conf" >/dev/null
n_orgs="$(jq '[.orgs[] | select(.enabled == true)] | length' orgs.json)"
n_remotes="$(grep -c '^\[' "$tmpdir/rclone.conf")"
[[ "$n_remotes" == "$n_orgs" ]] || fail "rendered $n_remotes remotes, expected $n_orgs"
grep -q 'DUMMY-' "$tmpdir/rclone.conf" || fail "dummy placeholder substitution did not happen"

if command -v rclone >/dev/null; then
  rclone listremotes --config "$tmpdir/rclone.conf" | grep -q '^gdrive-' || fail "rclone rejected rendered config"
  echo "validate-config: rclone parsed rendered config OK"
else
  [[ "$QUICK" == "1" ]] || echo "validate-config: rclone not on PATH; skipped parser check (nix develop provides it)"
fi

echo "validate-config: OK ($n_orgs enabled org(s), schema/render/tripwire green)"
