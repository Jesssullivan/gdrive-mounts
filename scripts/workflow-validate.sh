#!/usr/bin/env bash
# workflow-validate.sh — the CI-surface gate. Folds in what the hosted
# `actionlint` job used to do, and adds the v4 ActionPlan checks that job
# never had:
#   1. actionlint over every .github/workflows/*.yml
#   2. .github/lanes.json against the vendored v4 ActionPlan schema (schema 3)
#   3. every action_name the caller submits is declared in that plan
#
# (3) is the drift this repo can actually ship: the caller and the plan are
# two files, and a rename in one is green in the other until the fabric
# refuses the submission at dispatch time, which is the worst place to learn it.
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

shopt -s nullglob
workflows=(.github/workflows/*.yml)
shopt -u nullglob
[ "${#workflows[@]}" -gt 0 ] ||
  die ".github/workflows/*.yml matched nothing — refusing to pass a lint that had no input"

require_cmd actionlint "run inside the flake: nix develop --command just workflow-validate"
require_cmd jq "run inside the flake: nix develop --command just workflow-validate"
require_cmd check-jsonschema "run inside the flake: nix develop --command just workflow-validate"

actionlint "${workflows[@]}" || die "actionlint rejected ${workflows[*]}"

LANES=.github/lanes.json
SCHEMA=docs/schemas/lanes.schema.json
[ -f "$LANES" ] || die "$LANES not found — the v4 caller has no ActionPlan to submit"
[ -r "$SCHEMA" ] || die "$SCHEMA not found (vendored pin, see docs/schemas/README.md)"

check-jsonschema --schemafile "$SCHEMA" "$LANES" >&2 ||
  die "$LANES does not satisfy the v4 ActionPlan schema"

declared="$(jq -r '.actions | keys[]' "$LANES")"
submitted="$(sed -n 's/^[[:space:]]*action_name:[[:space:]]*\([A-Za-z0-9._-]*\).*$/\1/p' "${workflows[@]}" | sort -u)"
[ -n "$submitted" ] ||
  die "no job in ${workflows[*]} submits an action_name — the v4 caller would run nothing"
while IFS= read -r a; do
  printf '%s\n' "$declared" | grep -qxF "$a" ||
    die "the caller submits action_name '$a', which $LANES does not declare"
done <<<"$submitted"

info "OK (actionlint ${#workflows[@]} workflow(s); $LANES schema 3; actions: $(printf '%s' "$submitted" | tr '\n' ' '))"
