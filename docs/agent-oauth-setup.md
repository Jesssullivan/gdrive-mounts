# Agent OAuth setup — mint, verify, refresh, rotate

Part 2 of the agent-facing docs: the Drive OAuth surface from an agent seat.
The operator-facing lace-up flow is `docs/adoption.md`; the secrets contract
and lab wiring are `docs/sops-integration.md`. This page covers what an agent
can drive, what it must hand to the operator, and how to prove what a live
token actually carries.

> **Suite:** [Part 1 — enrolment](agent-enrolment.md) ·
> **Part 2 — OAuth setup (this page)** ·
> [Part 3 — navigate & explore](agent-navigate-explore.md) ·
> [Part 4 — symbolic manipulation](agent-symbolic-manipulation.md)

Boundary first (`AGENTS.md` invariants): **OAuth consent and the GCP
credential download are operator browser acts.** An agent prepares, launches,
verifies, and cleans up — it never opens a browser, never clicks the consent
button, and never reads a secret value into a transcript. Fingerprints and
scope strings are safe to paste; token and client-secret values are not.

## Minting: what is scripted, what is not

`just mint-token <org>` (from a checkout, inside the flake:
`nix develop --command just mint-token <org>`) runs a two-step flow
(`scripts/mint-token.sh`):

1. **Build the scope-carrying blob.** `rclone config create <remote> drive
   client_id=… client_secret=… scope=<scope> --non-interactive
   --config /dev/null` emits a base64 "config token" blob. The blob is the
   only place the requested scope survives — the bare
   `rclone authorize "drive" <id> <secret>` form mints an **unscoped
   full-drive token regardless of intent** and is never used.
   `--config /dev/null` keeps rclone from touching any real config.
2. **Run consent through the loopback server.**
   `rclone authorize "drive" <blob> --auth-no-open-browser` binds
   `127.0.0.1:53682`, prints a `http://127.0.0.1:53682/auth?...` URL on
   stderr, and waits. `--auth-no-open-browser` is why this step is agent-safe:
   without it rclone launches a GUI browser, which the no-GUI-launch
   invariant forbids. stdout carries the token block and goes straight to a
   file — it is never echoed.

Division of labor for the consent window:

| Actor | Does |
|---|---|
| Script | Builds the blob, starts the loopback server, prints the URL on stderr, captures the token block to a `0600` stage file, writes `mint.meta.json`, wipes scratch |
| Operator (browser) | Opens the printed URL, signs in as the **org's** Drive account, approves the consent screen. Google redirects to `127.0.0.1:53682`; rclone catches the code itself. **Nothing is pasted back into the terminal.** |
| Agent | May run the recipe and watch stderr; may navigate a browser lane up to — never through — the consent click (`docs/adoption.md` step 2) |

**Over ssh:** the loopback server binds on the machine running `mint-token`,
but the browser is on the operator's machine. Forward the port before
minting:

```console
ssh -L 53682:127.0.0.1:53682 <host>
```

then open the printed URL locally; the redirect lands on the forwarded port.

**Accepted, stated exposure:** the blob carries the client secret and rclone
takes it on argv, so it is visible in `ps` output for the length of the
consent window. rclone offers no stdin form. Mint on the operator's own
terminal; do not mint on a shared host while untrusted local processes run.

Outputs, both `0600` under `~/.local/state/gdrive-mounts/stage/<org>/`
(override with `GDM_STAGE`):

- `token.json` — the token JSON, whole-document, verbatim. Minting fails
  closed unless it carries both `access_token` and `refresh_token`.
- `mint.meta.json` — non-secret mint record: `org`, `remote`, `scope`,
  `minted_at`, `expiry`, `refresh_fingerprint` (12-hex sha256 prefix of the
  refresh token — safe to paste, and the value that later proves a rotation
  actually changed the token).

Next step in the lane: `just seed-lab <org>` encrypts both stage files into
`lab`, keeps the mint record at
`~/.local/state/gdrive-mounts/mint.meta.<org>.json`, and wipes the stage
(`docs/sops-integration.md`).

## Scope semantics (post-#13)

Three scopes are allowed. `orgs.json` `scope` records intent; the token is
what Google actually granted.

| `scope` | Google grants | Mount behavior |
|---|---|---|
| `drive.readonly` | Read over the whole Drive | Read-only: rclone `--read-only` **and**, on the nfsmount backend, kernel `--option rdonly` so write attempts fail `EROFS` instead of lying (`docs/runbook.md` "Mount semantics") |
| `drive.file` | Create, read, update, delete — **restricted to files this client created or the user explicitly opened with it** | Read-write. A *narrow write* scope, not a read scope: it narrows *which* files, not *what* you can do to them. Must not be forced read-only |
| `drive` | Full read/write over the whole Drive | Read-write |

Fail-safe both directions (`nix/lib/plan.nix` `writeScopes`): an absent
`scope` defaults to `drive.readonly`, and an unrecognized value (typo, future
`drive.appdata`) resolves read-only rather than inheriting write semantics.

**Promotion is a re-mint, not a config edit.** Flipping `orgs.json` from
`drive.readonly` to `drive` changes nothing on a live mount until
`just mint-token <org>` runs again and the operator re-consents. Then
`just seed-lab <org>` and a re-switch carry the new token to the host. Full
promotion doctrine: `docs/sops-integration.md` "Scope and promotion".

## Proving what a live token carries

The token value is opaque and stays in files. Do **not** probe it with
Google's `tokeninfo` endpoint — that puts the access token on a URL/argv,
which the secrets rules here forbid. Use the mint record plus behavior:

1. **Read the mint record** (non-secret, safe to paste whole):

   ```console
   jq . ~/.local/state/gdrive-mounts/mint.meta.<org>.json   # after seed-lab
   jq . ~/.local/state/gdrive-mounts/stage/<org>/mint.meta.json   # before
   ```

   `scope` is what the blob requested at mint time; `refresh_fingerprint`
   changing across mints proves a rotation replaced the token.

2. **`just doctor --json`** — the `scope` row compares `orgs.json` intent
   against the mint record: `OK` with the mint date on match; `WARN
   "orgs.json says X, token was minted Y — re-mint before calling promotion
   done"` on mismatch; `WARN "no mint record; token scope unverified"` when
   neither mint-record path exists (a token seeded before mint records
   existed, or minted on another host).

3. **Read probe** through the rendered config (never through a hand-built
   remote string):

   ```console
   nix develop --command just smoke <org>
   # or directly:
   rclone lsd --config ~/.local/state/gdrive-mounts/rclone-<org>.conf gdrive-<org>:
   rclone about --config ~/.local/state/gdrive-mounts/rclone-<org>.conf gdrive-<org>:
   ```

   `smoke` lists top-level folder names; `about` returns quota and proves the
   token reaches the API at all. **`drive.file` trap:** under `drive.file`,
   listing shows only files this client created or opened — an empty `lsd` on
   a fresh `drive.file` client is expected behavior, not an auth failure.

4. **Write probe**, only when behavior proof of a write scope is required and
   a stray probe file in that org's Drive is acceptable:

   ```console
   rclone touch --config ~/.local/state/gdrive-mounts/rclone-<org>.conf "gdrive-<org>:gdm-scope-probe-$(date -u +%Y%m%dT%H%M%SZ)"
   rclone deletefile --config ~/.local/state/gdrive-mounts/rclone-<org>.conf "gdrive-<org>:gdm-scope-probe-<same-stamp>"
   ```

   A `drive.readonly` token fails this with a 403 `insufficientPermissions`
   from the API (on the mount itself the same attempt is `EROFS`; the kernel
   refuses before rclone is asked — `docs/runbook.md`).

## Token refresh mechanics

- The refresh token is the durable credential; the access token expires
  (`expiry` in `mint.meta.json`, ~1 h) and **rclone refreshes it itself**
  using the refresh token whenever a call needs it. No cron, no agent action.
- **rclone writes refreshed tokens back into the config file it was given** —
  `~/.local/state/gdrive-mounts/rclone-<org>.conf`. That makes **one writer
  per config file a correctness rule** (`nix/lib/plan.nix`): one rendered
  config per org, one rclone process per config. Never point a second rclone
  invocation at a running mount's config for anything long-lived; for ad-hoc
  probes the reads above are safe, but do not run a second *mount* or a
  config-mutating command against it.
- The mount wrapper renders a missing config and otherwise reuses it —
  the phase log line is `render: reusing … (rclone owns it after first
  start)` — precisely so a restart does not undo rclone's write-backs.
  A home-manager switch re-renders from the sops-materialized seed files;
  that resets only the (replaceable) access token, because the seeded
  `token.json` carries the same refresh token Google granted at mint.
- The seed copy in `lab`'s sops tree is therefore the authority: rotating it
  (re-mint + `seed-lab` + switch) replaces what every future render uses.
- When the refresh token itself dies you see the agent flap with
  `invalid_grant` in the `.err` log — expiry vs. revocation and their
  different cures are rows in the `docs/runbook.md` failure table.

## Revocation and rotation

- **Rotate** (leaked value, scheduled hygiene): rotate the GCP client secret
  for that org, re-download the client JSON, then `just import-client`,
  `just mint-token`, `just seed-lab`, re-switch (`docs/adoption.md` steps
  0–3; `docs/sops-integration.md` "Rotation"). Edit sops leaves with a
  targeted re-encrypt, never a broad decrypt-and-dump.
- **Revoke from the Google side** (operator, browser): the org account's
  third-party access page — <https://myaccount.google.com/permissions> —
  lists the OAuth client by its GCP display name; removing access kills the
  refresh token immediately. A Workspace-admin revocation looks identical
  from the mount (`invalid_grant`) and needs the full re-consent, not just a
  re-mint of the same grant.
- Re-minting does not revoke the previous token. Treat a superseded-but-
  unrevoked token as live until the operator revokes it or it ages out.

## What can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| `FAIL GNU coreutils required (found BSD stat). Run inside the flake` | `mint-token`/`import-client` run with macOS `/usr/bin/stat` first on PATH — the secret-handling scripts read file modes with GNU `stat -c` and refuse the BSD dialect | Run through the flake: `nix develop --command just mint-token <org>` (direnv via `.envrc` does this automatically in a checkout) |
| `FAIL no client.json for <org> (run: just import-client …)` | Stage is empty (`seed-lab` wipes it by default) and no fallback answered. `mint-token` looks in order: `~/.local/state/gdrive-mounts/stage/<org>/client.json` → `$<PREFIX>_CLIENT_FILE` (e.g. `GDM_SULLIWOOD_CLIENT_FILE`) → `$GDRIVE_MOUNTS_SECRET_DIR/<org>/client.json`, default `~/.local/state/gdrive-mounts/secrets/<org>/client.json` — the sops-materialized path on a seeded, switched host | On a seeded host: nothing to stage — the materialized fallback serves re-mints (promotion, rotation-of-token-only). You must **re-stage** via `just import-client` when the org was never seeded on this host, or the GCP client secret was rotated (the materialized copy is stale) — both need a fresh operator download |
| `FAIL <org> client.json is still sops-encrypted (contains ENC[AES256_GCM,)` | `$<PREFIX>_CLIENT_FILE` pointed at the encrypted leaf in `lab:nix/secrets/gdrive-mounts/` instead of the materialized runtime path | Point at `~/.local/state/gdrive-mounts/secrets/<org>/client.json` (exists only after a switch), or re-stage |
| `FAIL minted token carries no refresh_token; consent did not complete` | Consent screen was approved, but Google omits `refresh_token` when this account already granted this client and no fresh grant happened | Operator revokes the client at <https://myaccount.google.com/permissions> for that account, then re-run `just mint-token <org>` so consent is fresh |
| Consent URL opens but the redirect never lands (browser spins on `127.0.0.1:53682`) | Minting over ssh without the port forward, or something else already holds 53682 on the browser's machine | Reconnect with `ssh -L 53682:127.0.0.1:53682 <host>`; free the local port |
| `FAIL rclone did not return an authorize blob` / `no token block between the paste markers` | rclone changed its `config create`/`authorize` output shape across a version bump | Version regression, not credentials — pin/downgrade rclone in the flake and file it; the parse points are `scripts/mint-token.sh` |
| `just smoke <org>` lists an unexpected Drive | Operator consented as the wrong Google account | Re-mint; consent as the org account. Revoke the stray grant from the wrong account's permissions page |
| Empty listing under `drive.file` | Not a failure — the scope only shows files this client created/opened | Expected; prove liveness with `rclone about` or the write probe above |
| Mount still read-only after promotion, or a write silently vanishes | `orgs.json` `scope` flipped without a re-mint — scope lives in the token | `just mint-token <org>`, `just seed-lab <org>`, re-switch (`docs/runbook.md` failure table) |
