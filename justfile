# gdrive-mounts — sole operator entrypoint. Run inside the flake:
#   nix develop --command just <recipe>
# (direnv via .envrc puts you in the devShell automatically.)

set shell := ["bash", "-euo", "pipefail", "-c"]

# List recipes (default).
default:
    @just --list

# ── Local gates ─────────────────────────────────────────────────────────────

# Every gate that runs without a build slot. This is the day-to-day gate.
check: validate secrets-scan-dir endpoint-free-check repo-manifest-validate hm-eval

# Everything in `check` plus the two gates that need a builder or a bazel fetch.
check-ci: check flake-check bazel-test

# Validate orgs.json, the nix settings SSOT, and the render pipeline (dummy secrets).
validate:
    bash scripts/validate-config.sh

# gitleaks over the working tree, fail-closed. History is a CI lane.
secrets-scan-dir:
    bash scripts/secrets-scan-dir.sh

# Assert no remote-cache or executor endpoint is baked into bazel config.
endpoint-free-check:
    bash scripts/endpoint-free-check.sh

# Validate tinyland.repo.json against the canonical site.scaffold schema.
repo-manifest-validate:
    bash scripts/repo-manifest-validate.sh

# Prove the home-manager module evaluates. Eval only: no build slot needed.
hm-eval:
    nix eval --raw ".#checks.$(nix eval --raw --impure --expr builtins.currentSystem).hm-eval.drvPath"

# nix flake check. Needs a build slot; neo has none, so this is a CI lane.
flake-check:
    nix flake check

# ── Bazel graph ─────────────────────────────────────────────────────────────

# Run the bazel test graph (the shell gates, under bazel).
bazel-test:
    bazelisk test //...

# Build the bazel graph without running it.
bazel-validate:
    bazelisk build //...

# ── Runtime lanes (operator-invoked; real secrets, never printed) ───────────

# Render <stateDir>/rclone-<org>.conf for one org, or for every enabled org.
render ORG="":
    ORG="{{ ORG }}"; bash scripts/render-config.sh ${ORG:+--org "$ORG"}

# Refresh the metadata index for every enabled org (lsjson -> json + sqlite).
index:
    bash scripts/gdrive-index.sh

# Report tools, orgs, secrets, configs, cache, mounts, index, scope. --json for agents.
doctor *ARGS:
    bash scripts/doctor.sh {{ ARGS }}

# ── Adoption lane (operator terminal; consent happens in a browser) ─────────

# Stage the GCP OAuth Desktop client download for one org, then wipe the download.
import-client ORG PATH:
    bash scripts/import-client.sh "{{ ORG }}" "{{ PATH }}"

# Mint a scope-carrying token for one org. Run this on your own terminal.
mint-token ORG:
    bash scripts/mint-token.sh "{{ ORG }}"

# Encrypt the staged secrets for one org into the lab secrets tree with sops.
seed-lab ORG *ARGS:
    bash scripts/seed-lab.sh "{{ ORG }}" {{ ARGS }}

# List the top-level Drive folders for one org through its rendered config.
smoke ORG:
    bash scripts/smoke.sh "{{ ORG }}"
