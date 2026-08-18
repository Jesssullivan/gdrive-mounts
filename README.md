# gdrive-mounts

IaC for mounting multiple Google Workspace orgs' Drives as navigable, indexable
remote filesystems — macOS (kext-free NFS loopback) and Linux (FUSE3) — via
rclone, delivered by a nix flake and deployed through home-manager (lab).

Private repo. MIT © 2026 Jess Sullivan.

## Quickstart

```console
nix develop --command just check    # all local gates (validate + gitleaks + flake)
just --list                          # inside the devShell
```

## Layout

| Path | Role |
|---|---|
| `orgs.json` | **Non-secret** org registry (schema: `config/orgs.schema.json`) |
| `config/rclone.conf.template` | Per-org rclone stanza with placeholder secrets |
| `scripts/render-config.sh` | Materializes runtime `rclone.conf` (0600) from operator-held secret files |
| `scripts/validate-config.sh` | Schema + render + tripwire validation (zero network) |
| `scripts/gdrive-index.sh` | `rclone lsjson` snapshots + sqlite rollup (the agent/AX query surface) |
| `scripts/secrets-scan-dir.sh` / `endpoint-free-check.sh` | House doctrine gates |
| `nix/modules/home-manager.nix` | `programs.gdrive-mounts` — launchd (macOS) / systemd --user (Linux) |
| `docs/sops-integration.md` | How lab seeds per-org secrets |
| `docs/runbook.md` | Operate/debug the mounts |
| `docs/linear-drafts/` | Tracker issue drafts (filed when a write seat exists) |

## Secrets

This repo holds **none**. See `docs/sops-integration.md`. OAuth client creation
per org is an operator act (GCP console); rclone's shared Google client_id is
retired during 2026, so per-org clients are required regardless.

## Deploy

Consumed as a flake input by `lab` (`homeManagerModules.default` /
`homeModules.default`), wrapped by lab's `nix/home-manager/gdrive-mounts.nix`,
secrets passed as sops paths. Neo switches are attended-only.
