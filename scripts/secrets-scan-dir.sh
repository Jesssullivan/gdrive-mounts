#!/usr/bin/env bash
# secrets-scan-dir.sh — gitleaks over the repo, fail-closed (oauth-mux pattern).
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if ! command -v gitleaks >/dev/null; then
  if command -v nix >/dev/null && [[ "${_GDM_REEXEC:-0}" != "1" ]]; then
    exec env _GDM_REEXEC=1 nix develop --command bash scripts/secrets-scan-dir.sh
  fi
  echo "secrets-scan-dir: FAIL-CLOSED — neither gitleaks nor nix available" >&2
  exit 1
fi

gitleaks dir . --config .gitleaks.toml --redact --exit-code 1
echo "secrets-scan-dir: OK"
