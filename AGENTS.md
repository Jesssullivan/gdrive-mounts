# AGENTS.md — gdrive-mounts

## What this repo is

IaC for mounting **multiple Google Workspace orgs' Drives** as navigable,
indexable remote filesystems on **neo (macOS, aarch64)** and **sting (Linux)**,
via rclone — the FOSS replacement for the odrive lane (odrive pivoted to a
construction/Procore vertical; free tier died 2026-03-31; no Apple Silicon app
as of 2026-07). This is the **interim bridge** until TCFS/tummycrypt grows a
gsuite async target (multi-year, opendal Operator seam) — not a competitor to
it. Say so plainly in docs; do not create two competing truths.

**Initial contract goal** (ratified 2026-08-17): `jess@sulliwood.org` with the
**"GFTB Stuff"** folder browsable and (after explicit promotion) writable.
`jess@xoxd.ai` and `jess@lmux.ai` are staged disabled in `orgs.json`.

## Build truth

- **`justfile` is the sole operator entrypoint.** Run inside the flake:
  `nix develop --command just check` (direnv wired via `.envrc`).
- **nix flake is the only dependency authority.** rclone comes from nixpkgs
  (1.75.0 at pin time). **brew is not a delivery vehicle for this stack.**
- Bazel (bazelisk, `.bazelversion` 8.1.1, bzlmod) is the candidate/CI graph:
  marker genrules mirror the same scripts. Endpoint-free by doctrine —
  `just endpoint-free-check` guards it; cache/RBE endpoints are runtime-env
  only if GloriousFlywheel enrollment is ever ratified.
- House pattern sources: oauth-mux (secrets/endpoint doctrine, justfile),
  scheduling-kit (bazelisk/version discipline), linear-gsuite (HM module +
  `_FILE` secret pattern).

## Hard rules

1. **No secret material ever lands in this repo.** `orgs.json` and templates
   are non-secret. Tokens/client secrets live only in **lab sops-nix** and
   arrive as file paths (`GDM_<ORG>_CLIENT_ID_FILE` etc.). gitleaks is armed
   from commit zero (`just secrets-scan-dir`, fail-closed).
2. **Read-only by default.** Every org starts `scope = drive.readonly`.
   Promotion to `drive` (rw) is an **explicit, per-org operator gate**, one org
   at a time, recorded in `orgs.json` (`promotionTarget` → `scope` flip) and in
   the tracker. Never infer it.
3. **Never launch GUI apps** (`open`, `xed`, …) from agent work. OAuth consent
   screens are an operator-in-browser act.
4. **Spotlight is not the index.** The agent/AX query surface is the sqlite
   index from `just index` / the index timer. Don't promise Spotlight.
5. **sting is client-only.** It is a tainted honey-cluster compute node with
   ephemeral-scratch storage (TIN-2455 SPOF context). Nothing durable there.
6. Signed commits; PR flow once the repo leaves bootstrap. Never push to main
   directly after the initial scaffold commit.
7. Durable notes: session findings go to the operating repo's
   `docs/agent-notes/` (today: finances), evidence for this stack to
   `docs/evidence/` here.

## Boundary map

- `lab` — secrets (sops/age), deployment (home-manager wrapper
  `nix/home-manager/gdrive-mounts.nix`, attended `just nix-switch macbook-neo`).
- `finances` — billing SSOT; unrelated except as the session-notes home.
- `tummycrypt` — the future long-term successor lane (TCFS); this repo is the bridge.
- Google MCP servers — not suitable for mounting; out of scope by design.

## Verification entrypoints

| Gate | Command |
|---|---|
| Local everything | `nix develop --command just check` |
| Schema/render validation | `just validate` (dummy secrets, zero network) |
| Secrets scan | `just secrets-scan-dir` |
| Flake checks | `nix flake check` |
| Bazel candidate graph | `just bazel-validate` |
