#!/usr/bin/env bash
# secrets-scan-dir.sh — gitleaks over the working tree, fail-closed.
# History is a separate CI lane (gitleaks git), because a secret that was
# committed and then deleted passes every working-tree scan.
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

if ! command -v gitleaks >/dev/null 2>&1; then
  if command -v nix >/dev/null 2>&1 && [ "${_GDM_REEXEC:-0}" != "1" ]; then
    exec env _GDM_REEXEC=1 nix develop --command bash scripts/secrets-scan-dir.sh
  fi
  die "neither gitleaks nor nix is available; refusing to pass a scan that did not run"
fi

gitleaks dir . --config .gitleaks.toml --redact --exit-code 1
info "OK"
