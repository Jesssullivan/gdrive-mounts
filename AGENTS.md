# AGENTS.md — gdrive-mounts

## What this repo is

IaC for mounting multiple Google Workspace orgs' Drives as navigable,
indexable remote filesystems, via rclone, on neo (macOS) and sting (Linux).
It is the interim bridge until TCFS/tummycrypt grows a Google Workspace
target — see `docs/position.md`.

## Invariants

1. No secret material lives in this repo. Ever.
2. Read-only by default. Write access is a per-org, explicit operator
   promotion — never inferred, never automatic.
3. Never launch a GUI application from agent work. OAuth consent is an
   operator-in-browser act.
4. sting is client-only. Nothing durable runs there.
5. Signed commits, PR flow. No direct push to `main` past the initial
   scaffold commit. Keep this repository private and protect exact
   `refs/heads/main` with required PRs and verified signatures, no deletion
   or force-push, and no bypass actors (operator ratification: 2026-09-08).
6. Photo consolidation keeps each distinct original and one accepted WebP
   in Drive. Preserve existing orientation and publication restrictions.
   Deduplicate by verified content and Drive ID, never filename alone;
   retirement requires an explicitly approved manifest and remote verification.
7. Photo publication writes bytes through the mount to journal-owned UUID
   staging files, independently verifies their Drive IDs/hashes, then promotes
   those same IDs through the Drive API. This exception was ratified 2026-09-08
   because Apple NFS lacks exclusive rename on this backend. Preserve existing
   objects and refuse name ambiguity; never fall back to an overwriting rename
   or claim globally atomic filename exclusion against concurrent writers.

## Where each fact lives

| Fact | Source |
|---|---|
| Org registry, mount layout, scope, cache/index defaults | `orgs.json` (schema: `config/orgs.schema.json`) |
| Why this repo exists, odrive coexistence, extracted-repo boundary, CI posture, sting posture | `docs/position.md` |
| How to lace up a new org, end to end | `docs/adoption.md` |
| How to operate and debug a live mount | `docs/runbook.md` |
| The secrets contract and how `lab` wires it | `docs/sops-integration.md` |
| Linear project, draft issues → filed IDs | `docs/tracker.md` |
| Writing style | `docs/STYLE.md` |
| Dated receipts (bakeoffs, acceptance runs) | `docs/evidence/` |
| The command surface | `justfile` — bare `just` lists recipes; `just doctor` is the agent cold-landing surface; `just check` runs the local gates; `just check-ci` runs `bazelisk test //:ci_validation_suite`, the exact action CI submits |

## House-pattern sources

oauth-mux (secrets/endpoint doctrine, justfile shape), scheduling-kit
(bazelisk/version discipline), linear-gsuite (home-manager module shape,
`_FILE` secret pattern), site.scaffold (`just scaffold-doctor`, the
`tinyland-repo-contract` skill — `tinyland.repo.json`'s schema comes from
here), prompt-toon (agent-output shaping conventions).

## Boundary map

- `lab` — secrets (sops/age) and deploy (home-manager wrapper). Neo-first
  delivery uses an attended `just nix-switch-local 'jess@macbook-neo'`
  from a reviewed candidate, with source and active-generation verification.
  A configured daily switch is not evidence that delivery happened.
- `finances` — session-notes home only; otherwise unrelated.
- `tummycrypt` — the long-term successor (TCFS). See `docs/position.md`.

## Verification entrypoints

| Gate | Command |
|---|---|
| Local everything | `nix develop --command just check` |
| The exact action CI submits | `just check-ci` |
| Schema/render validation | `just validate` |
| Secrets scan | `just secrets-scan-dir` |
| Endpoint-free doctrine | `just endpoint-free-check` |
| Flake checks | `just flake-check` |
| CI surface (actionlint + ActionPlan) | `just workflow-validate` |
| Secrets scan, commit history | `just secrets-scan-history` |
| Shell library unit tests | `just test-lib` |
| Synthetic photo safety contracts | `just test-photos` |
| Home-manager module eval | `just hm-eval` |
| Home-manager eval proof, built | `just hm-eval-build` |
| Bazel candidate graph | `just bazel-test` |
| Manifest conformance | `just repo-manifest-validate` |
| Agent cold-landing / live state | `just doctor` |
