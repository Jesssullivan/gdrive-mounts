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

- macOS: `~/Library/Logs/tinyland/gdrive-mounts.*.log` — `.out.log` is the
  wrapper's phase log, `.err.log` is rclone's own output plus any `FATAL` line.
- Linux: `journalctl --user -u gdrive-mounts-*`
- Mount-guard breadcrumbs (cache-volume wait, etc.):
  `<stateDir>/last-error.<org>-root`
- Effective settings: `<stateDir>/effective-settings.json` — the paths, knobs,
  backend, units and links the home-manager module actually resolved for this
  host, rewritten at every switch. Non-secret (no secret path is ever written
  into it), `0600`. `just doctor` prefers it over `orgs.json` `defaults`, so a
  host that overrides `cacheRoot` or `mountRoot` is reported as it really is;
  the file's absence means "not switched yet on this host".

## The wrapper phase log

Each agent runs a generated wrapper that emits one timestamped line per phase
to its stdout log before rclone replaces the process. A silent `.out.log` used
to be the only symptom of a wrapper stuck before `exec`; now the last line
names the phase.

```text
2026-08-19T18:04:11Z [gdrive-mounts-sulliwood-root] start: pid 41207, backend nfsmount, point /Users/jess/GDrive/sulliwood
2026-08-19T18:04:11Z [gdrive-mounts-sulliwood-root] guard: not ready — cache volume /Volumes/TinylandSSD does not exist; retrying in 5s
2026-08-19T18:04:16Z [gdrive-mounts-sulliwood-root] guard: ready — volume /Volumes/TinylandSSD, cache /Volumes/TinylandSSD/tinyland/gdrive-cache/sulliwood
2026-08-19T18:04:16Z [gdrive-mounts-sulliwood-root] sweep: nothing mounted at /Users/jess/GDrive/sulliwood
2026-08-19T18:04:16Z [gdrive-mounts-sulliwood-root] render: reusing /Users/jess/.local/state/gdrive-mounts/rclone-sulliwood.conf (rclone owns it after first start)
2026-08-19T18:04:16Z [gdrive-mounts-sulliwood-root] exec: rclone nfsmount gdrive-sulliwood: …
```

Phases, in order: `start`, `guard`, `sweep`, `render`, `exec`. The argv on the
`exec` line is the whole rclone command — paths and flags only, never a secret.

## Exit codes

| Code | Who | Meaning |
|---|---|---|
| `78` | mount wrapper | Cache guard gave up. The cache volume was not a mountpoint, or the cache directory could not be created there, for the whole `cache.waitSeconds` window (120s by default). The wrapper never falls back to the boot disk. The reason, with the offending path and its mode, is on the `FATAL` line in the `.err.log` and in `<stateDir>/last-error.<org>-root`. |
| rclone's own | mount wrapper | Anything else is rclone's exit status, forwarded because the wrapper `exec`s it. `0` after a clean `launchctl bootout` is normal. |
| `0` / `1` / `2` | `just doctor` | All OK / at least one WARN / at least one FAIL. |
| `2` | any CLI | Usage error. |
| `70` | any CLI | `scripts/lib/common.sh` was not found next to the script — a broken install, not a runtime fault. |

The cache guard tests the deepest *existing* ancestor of the cache directory
for writability, not the volume root: an external volume mounts root-owned
(`/Volumes/<name>` is `drwxr-xr-x root:wheel`) while the user-owned cache
subtree below it is perfectly writable. It still requires the volume itself to
be a real mountpoint, which is what keeps the cache off the boot disk.

## Lifecycle commands

```console
nix develop --command just render [ORG]   # materialize rclone-<org>.conf (0600)
nix develop --command just index          # refresh the metadata index now
launchctl kickstart -k gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root
launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root   # stop cleanly (SIGTERM → rclone unmounts)
umount -f ~/GDrive/sulliwood              # only for a dead mount left by a crash
```

## Operator probes

Read-only, in the order worth running. `just doctor` is the one-shot version of
all of them.

```console
# 1. Which units exist, and what did each one last exit with?
launchctl list | grep dev.tinyland.gdrive-mounts

# 2. One unit in detail (PID and LastExitStatus; 78 is the cache guard).
launchctl list dev.tinyland.gdrive-mounts.sulliwood-root

# 3. Why. The phase log first, then rclone's stderr.
tail -20 ~/Library/Logs/tinyland/gdrive-mounts.sulliwood-root.out.log
tail -20 ~/Library/Logs/tinyland/gdrive-mounts.sulliwood-root.err.log

# 4. The breadcrumb the guard leaves. Present means the last start failed;
#    the wrapper deletes it on a start that gets past the guard.
cat ~/.local/state/gdrive-mounts/last-error.sulliwood-root

# 5. Restart one unit after fixing the cause.
launchctl kickstart -k gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root
```

Prefer `launchctl list <label>` over `launchctl print <domain>/<label>`: `print`
dumps the unit's whole inherited environment, which on a fleet host can include
secret-bearing values. `list` prints the PID and exit status, which is what
these probes need.

On Linux the equivalents are `systemctl --user status gdrive-mounts-<org>-root`,
`journalctl --user -u gdrive-mounts-<org>-root -n 20`, and
`systemctl --user restart gdrive-mounts-<org>-root`.

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
| agent restart-loops, exits `78`, breadcrumb says `cache volume … does not exist` or `… is not a mountpoint` | cache SSD absent or not mounted at mount time | mount the SSD; the agent waits ~120s then fails loud once — it never spills onto the boot disk |
| agent restart-loops, exits `78`, breadcrumb says `… is not writable by <user>` and names a mode | the cache subtree is genuinely not writable — a re-formatted or re-plugged volume, or a directory created by another user | the breadcrumb names the exact path and its mode/owner; fix that one directory's ownership, then kickstart the agent. The guard tests the cache subtree, not the volume root, so a root-owned `/Volumes/<name>` is not by itself a fault |
| reads under the mountpoint hang (`stat` still answers from cache) | dead NFS loopback: rclone was killed without unmounting | `umount -f` the mountpoint, then kickstart the agent; the wrapper does this sweep itself on every start (bakeoff evidence 2026-08-18) |
| plain `umount` says `Resource busy` right after activity | NFS handles still cached | stop the agent instead (`launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.<org>-root` sends SIGTERM; rclone unmounts cleanly), or `umount -f` |
| slow first `ls` of a big directory | cold VFS dir cache | expected once; the dir-cache TTL keeps it warm after — use the index for search, not `ls`, on a cold mount |
| `403 rateLimit` errors | per-org Drive API quota | each org has its own client_id, which isolates this; back off the index interval if it recurs |
| a write looks accepted but silently vanishes, or the mount is still read-only after promotion | `orgs.json` `scope` was flipped but the token was never re-minted | scope lives in the token, not the config file — re-run `just mint-token <org>` after any scope change; see `docs/sops-integration.md` |
| `render-config` warns "missing unreadable secret" for one org and no other org is affected | that org's secret isn't seeded in `lab`, or it's enabled in `orgs.json` but never wired into the lab wrapper | seed it (`docs/adoption.md`), wire `secrets.<org>` in the wrapper, re-switch — an unwired org must degrade alone, never take down another org's mount |
| one agent shows a single failed-then-recovered launch in the first ~30s after a fresh switch | sops secret decryption hadn't finished before the agent's first launch attempt | self-heals; only worth investigating if it recurs past that first launch |

## Pending evidence

See `docs/evidence/README.md` and `docs/tracker.md`.
