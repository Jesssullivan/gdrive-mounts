#!/usr/bin/env bash
# repo-manifest-validate.sh — validate tinyland.repo.json against the canonical
# Tinyland repo-manifest schema published by site.scaffold.
#
# Schema lookup, first hit wins:
#   1. $TINYLAND_REPO_SCHEMA               (explicit file)
#   2. ../site.scaffold/docs/schemas/...   (sibling checkout)
#   3. docs/schemas/...                    (vendored pin, see docs/schemas/README.md)
#   4. the manifest's own $schema URL      (gh api, then curl, into a scratch dir)
# The file name follows schema_version, the same selection site.scaffold's own
# recipe makes: 1 -> tinyland-repo-manifest.schema.json,
#               2 -> tinyland-repo-manifest.v2.schema.json.
#
# With no schema and no validator reachable this warns and exits 0 off CI, and
# fails on CI, where the network and the toolchain are both expected.
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

cd "$(repo_root)"
MANIFEST=tinyland.repo.json
[ -f "$MANIFEST" ] || die "$MANIFEST not found (run from the repo)"
require_cmd jq "run inside the flake: nix develop --command just repo-manifest-validate"

on_ci() { [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; }

skip_or_fail() { # message
  if on_ci; then die "$1"; fi
  warn "$1"
  warn "skipped off CI; CI enforces this gate"
  exit 0
}

ver="$(jq -r '.schema_version // 1' "$MANIFEST")"
case "$ver" in
  2) name="tinyland-repo-manifest.v2.schema.json" ;;
  *) name="tinyland-repo-manifest.schema.json" ;;
esac

SCHEMA=""
if [ -n "${TINYLAND_REPO_SCHEMA:-}" ]; then
  [ -r "$TINYLAND_REPO_SCHEMA" ] || die "TINYLAND_REPO_SCHEMA is set but unreadable: $TINYLAND_REPO_SCHEMA"
  SCHEMA="$TINYLAND_REPO_SCHEMA"
elif [ -r "../site.scaffold/docs/schemas/$name" ]; then
  SCHEMA="../site.scaffold/docs/schemas/$name"
elif [ -r "docs/schemas/$name" ]; then
  # Vendored pin (docs/schemas/README.md records the source rev). site.scaffold
  # is private, so CI has no other offline path to the canonical schema.
  SCHEMA="docs/schemas/$name"
else
  secure_tmpdir
  # site.scaffold is a private repo: raw.githubusercontent.com answers 404
  # without a token, so try gh (which carries one) before a plain fetch.
  if command -v gh >/dev/null 2>&1 &&
    gh api "repos/tinyland-inc/site.scaffold/contents/docs/schemas/$name" \
      -H "Accept: application/vnd.github.raw" >"$GDM_TMPDIR/$name" 2>/dev/null &&
    [ -s "$GDM_TMPDIR/$name" ]; then
    SCHEMA="$GDM_TMPDIR/$name"
    info "fetched schema with gh api"
  else
    url="$(jq -r '.["$schema"] // ""' "$MANIFEST")"
    if [ -z "$url" ]; then
      skip_or_fail "$MANIFEST declares no \$schema and no local schema was found"
    fi
    if ! command -v curl >/dev/null 2>&1; then
      skip_or_fail "no local schema and curl is not on PATH"
    fi
    if curl -fsSL --max-time 20 -o "$GDM_TMPDIR/$name" "$url"; then
      SCHEMA="$GDM_TMPDIR/$name"
      info "fetched schema from $url"
    else
      skip_or_fail "no local schema at \$TINYLAND_REPO_SCHEMA or ../site.scaffold, gh could not read it, and $url did not answer"
    fi
  fi
fi

if ! command -v check-jsonschema >/dev/null 2>&1; then
  skip_or_fail "check-jsonschema is not on PATH (nix develop provides it)"
fi

check-jsonschema --schemafile "$SCHEMA" "$MANIFEST" >&2 ||
  die "$MANIFEST does not satisfy $name (schema_version $ver)"
info "OK ($MANIFEST satisfies $name, schema_version $ver)"
