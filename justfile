# gdrive-mounts — sole operator entrypoint. Run inside the flake:
#   nix develop --command just <recipe>
# (direnv via .envrc puts you in the devShell automatically.)

set shell := ["bash", "-euo", "pipefail", "-c"]

# List recipes (default).
default:
    @just --list

# ── Local gates ─────────────────────────────────────────────────────────────

# All local gates: config validation + secrets scan + endpoint doctrine + flake check.
check: validate secrets-scan-dir endpoint-free-check flake-check

# Validate orgs.json + the render pipeline (dummy secrets; zero network).
validate:
    bash scripts/validate-config.sh

# gitleaks over the repo dir, fail-closed.
secrets-scan-dir:
    bash scripts/secrets-scan-dir.sh

# Assert no remote-cache/executor endpoints are baked into bazel config.
endpoint-free-check:
    bash scripts/endpoint-free-check.sh

# nix flake check (module eval + sandboxed validation checks).
flake-check:
    nix flake check

# ── Runtime lanes (operator-invoked; need real secrets, never printed) ─────

# Materialize the runtime rclone.conf (0600) from operator-held secret files.
render:
    bash scripts/render-config.sh

# Refresh the local metadata index for all enabled orgs (lsjson -> json + sqlite).
index:
    bash scripts/gdrive-index.sh

# ── Bazel candidate graph ───────────────────────────────────────────────────

# Bazel validation markers (mirrors scripts; for CI/RBE lanes).
bazel-validate:
    bazelisk build //:validate_marker //:secrets_scan_marker
