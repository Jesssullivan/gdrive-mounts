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
- One health watchdog per mount, alongside it:
  `dev.tinyland.gdrive-mounts.<org>-root.watchdog` /
  `gdrive-mounts-<org>-root-watchdog.service`. See "The wedge" below.
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

## The wedge, and the watchdog that clears it

A mount can stop serving while every process involved stays alive. Both
observed forms were captured on neo on 2026-08-19 —
`docs/evidence/2026-08-19-nfs-wedge.md` has the dumps:

- **It stops answering.** rclone holds its NFS listener and a healthy Drive
  connection; the macOS client has already marked the mount `not responding`;
  `hard,nointr` means every read blocks forever and cannot be interrupted.
- **It quietly detaches.** The go-nfs server goroutines are gone, the listening
  socket is a leaked fd, nothing is mounted at all — and
  `mountlib.(*MountPoint).Wait` is still blocked, so rclone never exits.

In both, **nothing fails**, so `KeepAlive` never fires and not one line is
logged. A supervisor watching the process cannot see either one. Only something
that asks the mount table can, which is what the watchdog does.

Each cycle (60s by default) it:

1. Stands down if the mount unit is not loaded — an operator who stopped a
   mount does not get it resurrected — or if nothing is mounted yet and the
   unit is younger than the grace window (the cache guard's own budget plus
   three minutes; the guard alone may legitimately wait two minutes). The grace
   applies only until the watchdog has seen the point mounted once; after that
   a disappearance is unambiguous and is acted on immediately.
2. Probes two ways: the kernel's `nfsstat -m` status flags, which answer even
   while the mount does not, and a bounded `stat` run from an expendable child.
   The child is *abandoned* rather than waited on — a `hard,nointr` caller
   blocks in the kernel where no signal lands, so `timeout` cannot rescue it.
3. Counts consecutive failures. It acts on the **second**, never the first: the
   `not responding` flag is observed to set and clear on its own within a
   minute, and a single-sample watchdog would restart the mount continuously.
4. On confirmation: captures first — `nfsstat -m` plus `core/stats`,
   `core/pid`, `vfs/stats` over the rc unix socket, then `SIGQUIT`, which makes
   the Go runtime dump every goroutine to the unit's `.err.log`. SIGQUIT is a
   capture *and* a kill, so it only ever runs on the path that restarts anyway.
5. Restarts the mount unit, and writes a wedge record.
6. Will not restart the same mount again inside `watchdogRestartFloorSec`
   (300s). A held wedge is still recorded — the floor suppresses the restart,
   never the evidence.

### Reading a wedge record

`<stateDir>/wedge.<org>-<mount>.jsonl`, 0600, append-only, one object per line:

```console
$ jq -c . ~/.local/state/gdrive-mounts/wedge.sulliwood-root.jsonl | tail -3
```

```json
{"ts":"2026-08-20T00:35:44Z","unit":"gdrive-mounts-sulliwood-root-watchdog","point":"/Users/jess/GDrive/sulliwood","action":"restarted","restart_count":3,"consecutive_failures":2,"pid":"94104","uptime_sec":"2781","nfs_status_flags":"0x2,not responding","probe":"timeout"}
```

| Field | Read it as |
|---|---|
| `action` | `restarted`, or `held-by-floor` when the restart floor suppressed it |
| `restart_count` | cumulative restarts for this mount, derived from the file itself |
| `uptime_sec` | how long the mount survived this time — the recurrence metric |
| `nfs_status_flags` | `0x2,not responding` = the kernel gave up; `none` on Linux |
| `probe` | `timeout` (a call never returned), `error`, or `unmounted` (nothing was mounted at all — the quiet class) |

A rising `restart_count` with a *falling* `uptime_sec` means the mount is
degrading, not merely flapping. Escalate on that, not on a single record.

The watchdog's own log is `~/Library/Logs/tinyland/gdrive-mounts.<org>-<mount>.watchdog.log`
(`journalctl --user -u gdrive-mounts-<org>-<mount>-watchdog` on Linux). It logs
a heartbeat hourly, every unhealthy cycle, and every decision. Captures land in
`<stateDir>/wedge-capture.<org>-<mount>.log`, and the goroutine dump in the
mount unit's own `.err.log`.

**Neither file is ever rotated or truncated by this repo, deliberately** —
launchd appends, and the pre-restart evidence is the whole point. Rotate them
by hand when they get large; never let a cleanup delete a capture you have not
read.

### Turning it off

```nix
programs.gdrive-mounts.watchdog.enable = false;   # per host, in the lab wrapper
```

The mounts are untouched by this; only the sidecar units disappear.

## The latency budget

The watchdog is the cure. This is the prevention, and it is the reason wedge
class 1 should become rare rather than merely survivable.

rclone and the macOS NFS client disagree about how long a slow backend is
allowed to take, by three orders of magnitude:

| Layer | Stock tolerance for one slow operation |
|---|---|
| macOS NFS client (`vfs.generic.nfs.client.initialdowndelay`) | **5 seconds**, then the mount is marked `not responding` |
| rclone `--timeout` (IO idle) | 5 minutes |
| × rclone `--low-level-retries` | up to **50 minutes** |

Any Drive call slower than five seconds kills a mount that rclone still
considers perfectly healthy, and `hard,nointr` then makes that state
bistable — it cannot heal itself. On 2026-08-19 a 270-second Drive request
ending in `ECONNRESET` did exactly this; the NFS client had given up on it 265
seconds earlier.

So the defaults pull rclone's stall window back under the client's patience:

| `orgs.json` `defaults` | Value | rclone flag | Why |
|---|---|---|---|
| `ioTimeout` | `20s` | `--timeout` | rclone's own default is `5m` |
| `connectTimeout` | `10s` | `--contimeout` | rclone's own default is `1m` |
| `lowLevelRetries` | `3` | `--low-level-retries` | multiplies the above; default is `10`, so worst case falls from 50m to 60s |
| `attrTimeout` | `5s` | `--attr-timeout` | how long the kernel may trust cached attributes |
| `pollInterval` | `5m` | `--poll-interval` | the change-notify poll is itself backend traffic that can stall; it was mid-flight in the 19:49 dump |
| `dirCacheTime` | `720h` | `--dir-cache-time` | **kept.** Not implicated, and it is what makes the mount usable cold |

These are defaults, not constants. Every one is a module option, so a host may
override it in the `lab` wrapper without touching this repo:

```nix
programs.gdrive-mounts.ioTimeout = "30s";   # per host, in the lab wrapper
```

Overrides are per *host*, not per *org*: `orgs.json` owns one `defaults` block
for the whole registry, and the schema rejects unknown per-org keys. With one
enabled org that distinction costs nothing today; if a second org ever needs a
different budget, that is a schema change, not a config change.

`just doctor` reports the values actually in force from
`<stateDir>/effective-settings.json`, which activation writes — **not** from
`orgs.json`, because a host override never reaches `orgs.json`.

Three coupling rules hold this together, and they are asserted by
`nix/tests/hm-eval.nix` rather than left to review:
`ioTimeout × lowLevelRetries` stays bounded well under two minutes;
`connectTimeout` and `attrTimeout` never exceed `ioTimeout`; and `pollInterval`
sits far below `dirCacheTime` but never below `ioTimeout`.

### What is not tuned, and why

- **`nfsCacheHandleLimit`, `vfsReadAhead`, `cacheMaxSize`.** The goroutine dumps
  refute all three as causes. Changing them as part of a mitigation pass would
  be changing untested things; a test asserts they are left alone.

## Mount semantics

The latency budget makes a stall less likely. This is what stops a stall that
happens anyway from being *permanent*.

`hard,nointr` is a macOS default, not a choice rclone made. Under it a caller
that hits a stalled mount blocks in the kernel forever and cannot be signalled
out — which is why the 2026-08-19 wedge did not self-heal, why `umount -f`
could return `EBUSY`, and why the watchdog cannot bound its own probe with
`timeout`. rclone hardcodes only `port`, `mountport` and `tcp`; everything else
is whatever `mount_nfs` defaults to.

It is reachable, though. `rclone nfsmount` does not implement the mount itself:
on Darwin it shells out to

```
mount -o port=N -o mountport=N -o tcp <our options> localhost:/ <mountpoint>
```

and `mount(8)`, given a `host:/path` special and no `-t`, execs
`/sbin/mount_nfs`. So anything passed as `--option` lands in the kernel NFS
client. `defaults.nfsMountOptions` is that list:

| Option | Effect |
|---|---|
| `soft` | a stalled call fails with `EIO` after `retrans` intervals instead of blocking forever — recoverable, and *visible* to the watchdog |
| `intr` | its caller can be interrupted, so a wedged `ls` can be killed |
| `timeo=100` | **tenths of a second** on macOS, so 10s — the stock default is 1s |
| `retrans=5` | retransmits before a soft mount gives up |
| `dumbtimer` | use `timeo` literally. Without it the client derives its timeout from the observed round trip, and on a *loopback* server that estimate is microseconds — which is the real reason the stock mount gives up so fast on a Drive-backed call |

`soft` is refused on a read-write mount unless
`programs.gdrive-mounts.allowSoftReadWrite = true`: an `EIO` that lands
mid-write is a durability decision, and promoting an org to `scope = "drive"`
must not change write semantics silently.

**These are not yet confirmed against a live mount.** `rclone nfsmount` accepts
`--option` and the forwarding path is read from rclone 1.75.0's source, but
whether macOS `mount(8)` honours every one of them in this position is an
empirical question. Confirm on the first deploy, and record it:

```console
nfsstat -m | sed -n '/GDrive/,/^$/p'     # expect soft,intr,timeo=100,retrans=5,dumbtimer
```

`docs/evidence/TEMPLATE-mount-bakeoff.md` is the form. If an option does not
appear, it was rejected or overridden — remove it rather than leaving a flag
that does nothing.

## Exit codes

| Code | Who | Meaning |
|---|---|---|
| `78` | mount wrapper | Cache guard gave up. The cache volume was not a mountpoint, or the cache directory could not be created there, for the whole `cache.waitSeconds` window (120s by default). The wrapper never falls back to the boot disk. The reason, with the offending path and its mode, is on the `FATAL` line in the `.err.log` and in `<stateDir>/last-error.<org>-root`. |
| `75` | mount wrapper | The stale-mount sweep failed: something is still mounted at the point after `umount -f`. The wrapper refuses to mount on top of it — that would leave a live process, a listening port and a client addressed to the dead one, silently. The supervisor retries after its throttle interval. If it persists, unmount by hand (`umount -f <point>`) and look for a process blocked on the old mount. |
| rclone's own | mount wrapper | Anything else is rclone's exit status, forwarded because the wrapper `exec`s it. `0` after a clean `launchctl bootout` is normal. `2` is normal after a watchdog `SIGQUIT` — that is the Go runtime dumping goroutines and exiting. |
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
| `nfsstat -m` says `not responding`, rclone is alive, nothing is logged | the mount stopped answering — wedge class 1 | the watchdog restarts it within two probe cycles; if it is off, kickstart the agent. A single flap that clears itself is normal and is not a fault — see `docs/evidence/2026-08-19-nfs-wedge.md` |
| `mount` shows nothing at the point, rclone is alive and old, nothing is logged | the mount detached — wedge class 2. rclone's NFS server goroutines are gone but `mountlib` is still waiting to be unmounted, so it never exits and no supervisor notices | the watchdog restarts it once the unit is past the grace window; the mount is back within ~10s. If it is off, kickstart the agent |
| the watchdog restarts a mount repeatedly | recurrence, not a watchdog fault | read the wedge record: a rising `restart_count` with a falling `uptime_sec` is a degrading mount. Check the captures and the network before raising the restart floor |
| plain `umount` says `Resource busy` right after activity | NFS handles still cached | stop the agent instead (`launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.<org>-root` sends SIGTERM; rclone unmounts cleanly), or `umount -f` |
| slow first `ls` of a big directory | cold VFS dir cache | expected once; the dir-cache TTL keeps it warm after — use the index for search, not `ls`, on a cold mount |
| `403 rateLimit` errors | per-org Drive API quota | each org has its own client_id, which isolates this; back off the index interval if it recurs |
| a write looks accepted but silently vanishes, or the mount is still read-only after promotion | `orgs.json` `scope` was flipped but the token was never re-minted | scope lives in the token, not the config file — re-run `just mint-token <org>` after any scope change; see `docs/sops-integration.md` |
| `render-config` warns "missing unreadable secret" for one org and no other org is affected | that org's secret isn't seeded in `lab`, or it's enabled in `orgs.json` but never wired into the lab wrapper | seed it (`docs/adoption.md`), wire `secrets.<org>` in the wrapper, re-switch — an unwired org must degrade alone, never take down another org's mount |
| one agent shows a single failed-then-recovered launch in the first ~30s after a fresh switch | sops secret decryption hadn't finished before the agent's first launch attempt | self-heals; only worth investigating if it recurs past that first launch |

## Pending evidence

See `docs/evidence/README.md` and `docs/tracker.md`.
