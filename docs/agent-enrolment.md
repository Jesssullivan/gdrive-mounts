# Agent enrolment — from zero to a live org

Read this first if you are an agent (or an operator driving one) landing in
this repo with an enrolment task: add an org, promote an org, or reason about
why a mount refuses writes. It explains the model and points at the doc that
owns each step. The full lace-up runbook is `docs/adoption.md`; the operate
and debug surface is `docs/runbook.md`. Nothing here replaces them.

> **Suite:** **Part 1 — enrolment (this page)** ·
> [Part 2 — OAuth setup](agent-oauth-setup.md) ·
> [Part 3 — navigate & explore](agent-navigate-explore.md) ·
> [Part 4 — symbolic manipulation](agent-symbolic-manipulation.md)

Two invariants bound everything on this page (`AGENTS.md`, invariants 1–3):
no secret material ever lives in this repo, write access is an explicit
per-org operator promotion, and OAuth consent is an operator-in-browser act —
an agent never clicks the GCP download button and never completes consent.

## What an org is

An org is one entry in the `orgs[]` array of `orgs.json` (schema:
`config/orgs.schema.json`), and it owns exactly:

- **One GCP OAuth Desktop client** and **one scope-carrying token** — the
  two-file secrets pair. File shapes: `docs/sops-integration.md`.
- **One rclone process** mounting the whole Drive at `~/GDrive/<org>`
  (`mountRoot` default `~/GDrive`, `orgs.json` `defaults`). There are no
  per-folder rclone processes.
- **Zero or more links**: `~/GDrive/<org>-<link>` is a symlink into the root
  mount (for example `~/GDrive/sulliwood-gftb-stuff` →
  `~/GDrive/sulliwood/GFTB Stuff`), declared in the org's `links[]`. A link
  is navigation, never a second mount.
- **One VFS cache directory** at `<cacheRoot>/<org>` (default
  `/Volumes/TinylandSSD/tinyland/gdrive-cache/<org>`). The mount wrapper
  exits `78` rather than fall back to the boot disk when the cache volume is
  absent — see the exit-code table in `docs/runbook.md`.
- **One unit per platform**: launchd `dev.tinyland.gdrive-mounts.<org>-root`
  on macOS, systemd `--user` `gdrive-mounts-<org>-root` on Linux, each with
  a watchdog sidecar. Unit anatomy, logs, and probes: `docs/runbook.md`.

Current registry: `sulliwood` is enabled at `scope: "drive"` (promotion
parts 1–2 — intent and token — landed 2026-08-22; part 3, the attended
switch, is pending; see the worked example below); `xoxd` and `lmux` are
disabled at `drive.readonly` with `promotionTarget: "drive"`.

Adding an org after the first is a data-and-secrets change only — one
`orgs.json` entry, one client/token pair, one lab wrapper edit. No script or
module change ("Per-org growth", `docs/adoption.md`).

## Read-only versus read-write, and why read-only is the default

Read-only is invariant 2: the default posture, changed only by an explicit
per-org operator promotion. The enforcement is layered so that no single
mistake yields a writable mount:

1. **The scope in the token.** `drive.readonly` is what Google granted —
   even a bug that dropped every mount flag could not write with it. Three
   scopes are allowed; the scope table, what each one grants, and the
   `drive.file` narrow-write nuance are owned by
   [`docs/agent-oauth-setup.md`](agent-oauth-setup.md) ("Scope semantics").
2. **`--read-only` in rclone's VFS.** The plan resolves read-only from a
   `writeScopes = [ "drive" "drive.file" ]` allowlist (`nix/lib/plan.nix`): an
   absent `scope` defaults to `drive.readonly`, and an unknown or typo'd
   scope also resolves read-only. Fail-safe in both directions.
3. **Kernel `MNT_RDONLY` via `--option rdonly`** (post-#12, asserted in
   #15, macOS nfsmount backend). `--read-only` alone answers writes with
   `EACCES` — a *permissions* error — so `mount(8)` flags, `statvfs(2)`,
   and `access(W_OK)` all claim the volume is writable. Photos.app raised
   13 separate permission errors on neo (2026-08-21) instead of one honest
   read-only dialog. `rdonly` sets `MNT_RDONLY`, so the kernel refuses with
   `EROFS` before anything is sent. Full mechanism and the
   not-yet-live-confirmed caveat: `docs/runbook.md`, "`rdonly`, and why
   `--read-only` is not enough".

Layers 2 and 3 are scope-gated **together**: promotion to a write scope
drops both in the same plan evaluation. There is no configuration where the
VFS allows writes but the kernel refuses them.

## Where secrets live

This repo holds none, ever. The full contract is `docs/sops-integration.md`;
the lifecycle stations are:

| Station | Path | Lifetime |
|---|---|---|
| Stage (plaintext, this host) | `~/.local/state/gdrive-mounts/stage/` (`<stateDir>/stage`; override with `GDM_STAGE`) — `0600` files, `0700` dir | From `just import-client` / `just mint-token` until `just seed-lab` **wipes it by default** (`--keep-stage` to keep) |
| Encrypted (lab repo) | `lab:nix/secrets/gdrive-mounts/<org>/{client.json,refresh.json}` — the token is named `refresh.json` because lab's pre-commit refuses staged `*token*.json` | Durable; committed by the **operator**, never by `seed-lab` |
| Materialized (switched host) | `~/.local/state/gdrive-mounts/secrets/<org>/{client.json,token.json}` (`${xdg.stateHome}/gdrive-mounts/secrets/…`, written by sops-nix at switch) | Rewritten every `just nix-switch` |

At runtime the module hands the wrapper per-org env vars
(`<secretEnvPrefix>_CLIENT_FILE` / `_TOKEN_FILE`, e.g.
`GDM_SULLIWOOD_TOKEN_FILE`), falling back to
`$GDRIVE_MOUNTS_SECRET_DIR/<org>/{client.json,token.json}`. CI and local
checks use `GDRIVE_MOUNTS_DUMMY_SECRETS=1`, never real material.

Failure modes to expect:

- `just seed-lab` fails and **prints the `.sops.yaml` stanza to add** when
  lab lacks a creation rule matching `nix/secrets/gdrive-mounts/.*\.json$`.
  Add the rule in lab first; do not hand-encrypt around it.
- The stage wipe means a re-run of `seed-lab` after the wipe has nothing to
  encrypt: re-mint (`just mint-token <org>`) rather than hunting for a copy.
- Any secret value that reaches a transcript, log, or artifact triggers the
  rotation ceremony in `docs/sops-integration.md`, "Rotation".

## The promotion ceremony (three parts, in order)

**Promotion is a re-mint, not a config edit.** `orgs.json` records intent;
the token records what Google actually granted; the lab switch is what
delivers either to a live mount. All three must happen, in this order, and
`just doctor` warns whenever config and token disagree.

1. **Intent** — flip the org's `scope` in `orgs.json` (for example
   `"drive.readonly"` → `"drive"`) in a PR. This changes nothing live; the
   plan still resolves read-only until the token agrees.
2. **Token** — `just mint-token <org>`, operator-attended: the recipe prints
   a consent URL on stderr and the operator re-consents in the browser at
   the new scope. Then `just seed-lab <org>` re-encrypts into lab, and the
   operator commits the ciphertext there.
3. **Delivery** — the lab pin advance plus `just nix-switch macbook-neo`
   (operator, attended — never agent-run). The new plan drops `--read-only`
   and `rdonly` together, and the new token backs the write scope.

### Worked example: the sulliwood promotion, 2026-08-22

Commit `600e03d` (merged as PR #16) is part 1 exactly: a one-line
`orgs.json` diff, `"scope": "drive.readonly"` → `"drive"`, after TIN-3883's
operator gate cleared in an attended interview. The commit message records
the two rulings that rode with it:

- `drive` was chosen over the `drive.file` outbox split.
- The soft-on-read-write durability guard was ruled
  `allowSoftReadWrite = true` for the consumer: on a **loopback** NFS
  server, `soft` bounds a stalled *local* rclone (the 2026-08-19 wedge
  class) rather than risking remote data, and Google-upload durability is
  the VFS cache's job. Without that ruling, the module's assertion refuses
  `soft` in `nfsMountOptions` the moment any read-write unit exists —
  promotion must not change write semantics silently
  (`docs/runbook.md`, mount-semantics table).

The commit message states its own limit: "This records intent only … the
mount stays effectively read-only until `just mint-token sulliwood` re-mints
the token at the new scope … and the lab switch delivers it." Part 2 landed
the same day: `just mint-token sulliwood` ran operator-attended at ~12:56Z
(consent as the org account at the full `drive` scope, refresh fingerprint
`cd155b7a83ee` per the mint record), and `just seed-lab sulliwood` delivered
the re-encrypted leaves to lab (commit `ca5b9a82`, merged in lab PR #1400).
Part 3 — the attended switch — is the remainder; until it lands, the live
mount still runs the old plan and token. The acceptance test for the
completed promotion — and the post-switch validation checklist that proves
it — is [`docs/agent-symbolic-manipulation.md`](agent-symbolic-manipulation.md).

## Adding a brand-new org, end to end

The authoritative runbook, including the per-step `just doctor` table, is
`docs/adoption.md`. The agent-lane condensation:

```console
# 0. operator, browser: GCP Desktop OAuth client, download JSON.
#    Agents may navigate; never click download, read the file, or
#    screenshot the credential modal.

# 0.5. registry: add the org to orgs.json (enabled: true) in a PR.
#    Copy an existing entry; schema is config/orgs.schema.json.

# 1. stage the client verbatim; wipes the Downloads copy
just import-client <org> ~/Downloads/client_secret_*.json

# 2. mint a scope-carrying token; operator consents in the browser.
#    Never bare `rclone authorize "drive" <id> <secret>` — that mints an
#    UNSCOPED full-drive token. Over ssh: -L 53682:127.0.0.1:53682
just mint-token <org>

# 3. encrypt both stage files into lab; prints the wrapper snippet;
#    wipes the stage; stops before git
just seed-lab <org>

# 4. lab wrapper PR: paste the printed snippet — sops leaves are
#    presence-gated (lib.optionalAttrs + builtins.pathExists) so an
#    unseeded org degrades instead of blocking activation.
#    Wiring shape: docs/sops-integration.md, "Lab side (consumer) shape".

# 5. operator, attended, never agent-run
just nix-switch macbook-neo   # in lab

# 6. verify
just doctor          # or --json for the machine-readable form
just smoke <org>     # lists the org's Drive root
just index           # or wait for the 6h timer; freshness SLO 24h
```

New orgs start at `scope: "drive.readonly"`. Do not lace up an org straight
to a write scope: promotion is its own ceremony (above), with its own
operator gate.

## Doc map for this page

| Question | Doc |
|---|---|
| The full lace-up, step by step, with doctor state per step | `docs/adoption.md` |
| Secrets contract, lab wiring snippets, scope table, rotation | `docs/sops-integration.md` |
| Units, logs, exit codes, wedge recovery, `rdonly` mechanism | `docs/runbook.md` |
| Invariants and the fact-location table | `AGENTS.md` |
| Why this repo exists and where its boundary sits | `docs/position.md` |
