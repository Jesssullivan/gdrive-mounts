#!/usr/bin/env bash
# secrets-scan-dir.sh — gitleaks over the working tree, fail-closed.
# History is a separate CI lane (gitleaks git), because a secret that was
# committed and then deleted passes every working-tree scan.
set -euo pipefail

d="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
if [ -f "$d/lib/common.sh" ]; then . "$d/lib/common.sh"; else . "$d/../libexec/gdrive-mounts/common.sh"; fi
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
