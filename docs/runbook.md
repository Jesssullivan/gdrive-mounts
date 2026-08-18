# Runbook — gdrive-mounts

Operate and debug a live mount. For how a mount gets created in the first
place, see `docs/adoption.md`. For why the substrate looks like this, see
`docs/position.md`.

## Units

- macOS (neo): one launchd agent per org,
  `dev.tinyland.gdrive-mounts.<org>-root`, backend `rclone nfsmount` (native
  NFS loopback — no kext, no brew).
- Linux (sting): one systemd --user service per org,
  `gdrive-mounts-<org>-root`, backend `rclone mount` (FUSE3). sting is
  client-only — see `docs/position.md`.
- One index timer per platform: `gdrive-mounts-index`.

## Mount layout

One root mount per org, at `~/GDrive/<org>` (whole Drive, scope from
`orgs.json`). Named sub-paths are symlinks into the root mount, not separate
rclone processes — e.g. `~/GDrive/sulliwood-gftb-stuff` links to
`~/GDrive/sulliwood/GFTB Stuff` (declared in `orgs.json` `links[]`).

## Logs

- macOS: `~/Library/Logs/tinyland/gdrive-mounts.*.log`
- Linux: `journalctl --user -u gdrive-mounts-*`
- Mount-guard breadcrumbs (cache-volume wait, etc.):
  `<stateDir>/last-error.<org>-root`
- Effective settings: `<stateDir>/effective-settings.json` — the paths, knobs,
  backend, units and links the home-manager module actually resolved for this
  host, rewritten at every switch. Non-secret (no secret path is ever written
  into it), `0600`. `just doctor` prefers it over `orgs.json` `defaults`, so a
  host that overrides `cacheRoot` or `mountRoot` is reported as it really is;
  the file's absence means "not switched yet on this host".

## Lifecycle commands

```console
nix develop --command just render [ORG]   # materialize rclone-<org>.conf (0600)
nix develop --command just index          # refresh the metadata index now
launchctl kickstart -k gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root
launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root   # stop cleanly (SIGTERM → rclone unmounts)
umount -f ~/GDrive/sulliwood              # only for a dead mount left by a crash
```

## Index (the agent/AX query surface)

Spotlight does not index these mounts. Query the sqlite rollup instead:

```console
sqlite3 <indexStateDir>/index.sqlite \
  "SELECT path, size FROM files WHERE org='sulliwood' AND path LIKE '%.pdf' LIMIT 20;"
```

Freshness SLO: `orgs.json` `defaults.indexFreshnessSloHours` (24h at time of
writing). Raw per-org JSON at `<indexStateDir>/<org>.json`.

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| agent flaps, err log `invalid_grant` | expired refresh token | `just mint-token <org>`, `just seed-lab <org>`, re-switch |
| agent flaps, err log `invalid_grant`, right after an operator revoked access in Workspace/GCP admin | revoked consent, not mere expiry | full re-consent required — repeat `docs/adoption.md` steps 0–3 for that org, not just a token refresh |
| agent restart-loops, exits `78`, `last-error.<org>-root` present | cache SSD absent at mount time (`/Volumes/TinylandSSD` not mounted) | mount the SSD; the agent waits ~120s then fails loud once — it never spills onto the boot disk |
| reads under the mountpoint hang (`stat` still answers from cache) | dead NFS loopback: rclone was killed without unmounting | `umount -f` the mountpoint, then kickstart the agent; the wrapper does this sweep itself on every start (bakeoff evidence 2026-08-18) |
| plain `umount` says `Resource busy` right after activity | NFS handles still cached | stop the agent instead (`launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.<org>-root` sends SIGTERM; rclone unmounts cleanly), or `umount -f` |
| slow first `ls` of a big directory | cold VFS dir cache | expected once; the dir-cache TTL keeps it warm after — use the index for search, not `ls`, on a cold mount |
| `403 rateLimit` errors | per-org Drive API quota | each org has its own client_id, which isolates this; back off the index interval if it recurs |
| a write looks accepted but silently vanishes, or the mount is still read-only after promotion | `orgs.json` `scope` was flipped but the token was never re-minted | scope lives in the token, not the config file — re-run `just mint-token <org>` after any scope change; see `docs/sops-integration.md` |
| `render-config` warns "missing unreadable secret" for one org and no other org is affected | that org's secret isn't seeded in `lab`, or it's enabled in `orgs.json` but never wired into the lab wrapper | seed it (`docs/adoption.md`), wire `secrets.<org>` in the wrapper, re-switch — an unwired org must degrade alone, never take down another org's mount |
| one agent shows a single failed-then-recovered launch in the first ~30s after a fresh switch | sops secret decryption hadn't finished before the agent's first launch attempt | self-heals; only worth investigating if it recurs past that first launch |

## Pending evidence

See `docs/evidence/README.md` and `docs/tracker.md`.
