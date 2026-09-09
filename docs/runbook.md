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

> **2026-09-05 — the restart loop, and what it actually was.** neo restarted
> `sulliwood-root` **606 times** between 2026-08-22 and 2026-09-05, one every
> 6-25 minutes for the last 14 hours of that window. The full diagnostic pass
> is `~/git/finances/docs/agent-notes/2026-09-04-gdrive-neo-wedge-diagnosis.md`.
> Four findings, all of which changed code here:
>
> 1. **rclone was not crashing.** 443 `SIGQUIT` blocks in the unit `.err.log`
>    against **zero** SIGSEGV, SIGBUS, panic or fatal error in 208,067 lines.
>    Every one was induced by the watchdog's own evidence capture. The
>    watchdog now says so: `induced traceback (not a crash)`.
> 2. **It was a latency wedge, not a fault.** A cold Drive root listing
>    measured **12.766s**; the mount was built with `timeo=100`, which is
>    **10s** on macOS. go-nfs answers one connection serially, so one slow
>    `READDIRPLUS` stalled the whole conversation past the client's patience
>    and macOS set `0x2,not responding`. Now `timeo=600` / `retrans=10`.
> 3. **The `stat` probe was inert.** macOS answered it from the root attribute
>    cache, so it reported `ok` through **345 of 377** flag-driven wedges — the
>    OR of two signals had quietly become the kernel flag alone. Now a readdir,
>    AND-combined.
> 4. **The watchdog converted a self-clearing stall into a cold start.** 271
>    recorded self-recoveries against those 606 restarts, and each restart
>    discarded an in-process dir cache with no on-disk backing, guaranteeing
>    the next cold walk inside the floor. Threshold 2 -> 5, floor 300s ->
>    1800s, grace +180s -> +900s, and `--vfs-refresh` warms the tree at start.
>
> yoga never wedged, on the same rclone, the same remote and the same watchdog
> logic: on Linux the module uses `rclone mount` (FUSE), and there is no RPC
> timeout in that path at all.
>
> **Not proven, and worth knowing before you tune further:** whether the
> `timeo` raise alone would have ended the loop (12.766s was one measurement of
> one shallow listing on a loaded host — deeper directories may cost more than
> 60s); what generated 526k READDIRPLUS and 1.44M ACCESS calls (Spotlight is
> confirmed off for the mount); and why the index runs were failing (no
> `rateLimitExceeded`, `429` or `quotaExceeded` line was found anywhere).

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
   fifteen minutes; the guard alone may legitimately wait two minutes, and a
   cold `nfsmount` then has to walk the remote before it answers). The grace
   applies only until the watchdog has seen the point mounted once; after that
   a disappearance is unambiguous and is acted on immediately.
2. Probes two ways: the kernel's `nfsstat -m` status flags, which answer even
   while the mount does not, and a bounded **readdir** (`ls -1f`) run from an
   expendable child. The child is *abandoned* rather than waited on — a
   `hard,nointr` caller blocks in the kernel where no signal lands, so
   `timeout` cannot rescue it.

   It is a readdir and not a `stat` deliberately. macOS answers `stat` on a
   mountpoint out of the root attribute cache (`acrootdirmin=5`), so the old
   `stat` probe returned `ok` through **345 of 377** flag-driven wedges on neo
   — the filesystem half of the verdict was inert. A readdir goes to the wire.
3. Combines the two with **AND**, not OR: a flag on its own is logged and
   waited out, and only a flag *plus* a genuinely failed filesystem call counts
   as a failure. The `not responding` flag sets and clears by itself — 271
   recorded self-recoveries on neo against 606 restarts — so treating it as a
   verdict on its own is what built a restart loop. On Linux there is no kernel
   signal to AND with and the filesystem call decides alone.
4. Counts consecutive failures and acts on the **fifth**
   (`watchdogFailureThreshold`), i.e. after five minutes of *sustained*
   unhealth at the 60s interval. Two was not enough: the flag commonly clears
   after three or four minutes.
5. On confirmation: captures first — `nfsstat -m` plus `core/stats`,
   `core/pid`, `vfs/stats` over the rc unix socket, then `SIGQUIT`, which makes
   the Go runtime dump every goroutine to the unit's `.err.log`. SIGQUIT is a
   capture *and* a kill, so it only ever runs on the path that restarts anyway.

   **That dump is not a crash.** It opens `SIGQUIT: quit` followed by a full
   register dump and reads exactly like a native fault; the watchdog induces
   every one. neo's `.err.log` held 443 of them against zero `SIGSEGV`, zero
   `SIGBUS`, zero `panic` and zero `fatal error` in 208,067 lines. The
   watchdog log says so on the line above each one: `induced traceback (not a
   crash)`.
6. Restarts the mount unit, and writes a wedge record.
7. Will not restart the same mount again inside `watchdogRestartFloorSec`
   (1800s). A held wedge is still recorded — the floor suppresses the restart,
   never the evidence. The floor is long because a restart `SIGKILL`s rclone
   and throws away an **in-process** dir cache with no on-disk backing, so the
   replacement re-walks Drive cold; a floor shorter than a cold start simply
   re-arms the loop.

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
| `probe` | the readdir result: `timeout` (a call never returned), `error`, or `unmounted` (nothing was mounted at all — the quiet class) |

A record with `"nfs_status_flags":"0x2,not responding"` and `"probe":"ok"` is
the signature of the pre-2026-09-05 watchdog, whose `stat` probe was answered
from the attribute cache. It cannot be produced any more: the readdir probe and
the AND rule mean a restart requires both signals to have failed.

A rising `restart_count` with a *falling* `uptime_sec` means the mount is
degrading, not merely flapping. Escalate on that, not on a single record.

The watchdog's own log is `~/Library/Logs/tinyland/gdrive-mounts.<org>-<mount>.watchdog.log`
(`journalctl --user -u gdrive-mounts-<org>-<mount>-watchdog` on Linux). It logs
a heartbeat hourly, every unhealthy cycle, and every decision. Captures land in
`<stateDir>/wedge-capture.<org>-<mount>.log`, and the goroutine dump in the
mount unit's own `.err.log`.

**The captures are never rotated or truncated by this repo, deliberately** —
launchd appends, and the pre-restart evidence is the whole point. Rotate them
by hand when they get large; never let a cleanup delete a capture you have not
read. The mount unit's `.err.log` and the wedge record are the exception: both
are size-capped at start (see "Log hygiene").

### Log hygiene

launchd rotates nothing, and two files here are appended to without bound: the
mount unit's own `.err.log` and the per-mount wedge record. On neo the
`.err.log` reached **13.6 MB across 208,067 lines**, almost all of it goroutine
dumps the watchdog itself induced.

The mount wrapper caps both at start — once per unit start, never on a timer —
above `orgs.json` `defaults.logMaxBytes` (8 MiB), keeping **two** generations
at `<file>.1` and `<file>.2`.

It is a **copy-truncate**, not a rename. launchd opens `StandardErrorPath`
`O_APPEND` before it execs the wrapper, so the running process already holds an
fd on that inode: renaming would send every later line to the renamed copy and
no new `.err.log` would appear until the next start. Truncating in place is
correct because the fd is `O_APPEND` — writes always land at the end.

The wedge **captures** (`<stateDir>/wedge-capture.<org>-<mount>.log`) are
deliberately not capped. They are the pre-restart evidence, they are small, and
losing one to a size limit is the outcome the watchdog exists to prevent.

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
| `timeo=600` | **tenths of a second** on macOS, so 60s per RPC try — the stock default is 1s. Raised from `timeo=100` (10s) on 2026-09-05: a cold Drive root listing measured **12.766s**, so the client was giving up before a single cold `READDIRPLUS` could ever return |
| `retrans=10` | retransmits before a soft mount gives up — ~10 minutes of patience at `timeo=600`, after which `soft` still returns `EIO` rather than blocking |
| `dumbtimer` | use `timeo` literally. Without it the client derives its timeout from the observed round trip, and on a *loopback* server that estimate is microseconds — which is the real reason the stock mount gives up so fast on a Drive-backed call |

`soft` is refused on a read-write mount unless
`programs.gdrive-mounts.allowSoftReadWrite = true`: an `EIO` that lands
mid-write is a durability decision, and promoting an org to `scope = "drive"`
must not change write semantics silently.

### `rdonly`, and why `--read-only` is not enough

One more option is appended per unit, not from the registry: a unit whose org
resolves read-only also gets `--option rdonly`.

`--read-only` refuses the write inside rclone's VFS. That is the right layer
for correctness and the wrong one for the user interface. The VFS answers a
refused `CREATE` with `EACCES`, the go-nfs server maps it to `NFS3ERR_ACCES`,
and the kernel hands the caller `EACCES` — a *permissions* error. Nothing in
that chain sets `MNT_RDONLY`, so three signals macOS relies on all lie:

| Signal | What it says on a `--read-only`-only mount | Truth |
|---|---|---|
| mount flags | `MNT_RDONLY` clear | volume accepts no writes |
| `statvfs(2)` `f_flag` | no `ST_RDONLY` | volume accepts no writes |
| `access(path, W_OK)` | succeeds | the write will fail |

Applications believe them. Photos.app exporting 13 files raised 13 separate
"you don't have permission" errors instead of one honest read-only-volume
dialog (neo, 2026-08-21).

`rdonly` is an option of `mount(8)` and `/sbin/mount_nfs`, so it travels the
same path as `soft` and sets `MNT_RDONLY` on the mount itself. The client then
refuses the write with `EROFS` before anything is sent, and all three signals
above agree with the refusal.

Both layers ship. rclone's VFS is what makes the refusal *correct*; the kernel
flag is what makes it *legible*. `rdonly` is scope-gated exactly like
`--read-only`, so promoting an org to a write scope drops both together.

**These are not yet confirmed against a live mount.** `rclone nfsmount` accepts
`--option` and the forwarding path is read from rclone 1.75.0's source, but
whether macOS `mount(8)` honours every one of them in this position is an
empirical question. Confirm on the first deploy, and record it:

```console
# read-only org: expect soft,intr,timeo=600,retrans=10,dumbtimer AND read-only
nfsstat -m | sed -n '/GDrive/,/^$/p'
# the same fact from the kernel's own mount table
mount | grep GDrive                      # expect "(nfs, read-only, ...)"
```

`docs/evidence/TEMPLATE-mount-bakeoff.md` is the form. If an option does not
appear, it was rejected or overridden — remove it rather than leaving a flag
that does nothing.

## Exit codes

| Code | Who | Meaning |
|---|---|---|
| `78` | mount wrapper | Cache guard gave up. The cache volume was not a mountpoint, or the cache directory could not be created there, for the whole `cache.waitSeconds` window (120s by default). The wrapper never falls back to the boot disk. The reason, with the offending path and its mode, is on the `FATAL` line in the `.err.log` and in `<stateDir>/last-error.<org>-root`. |
| `75` | mount wrapper | The stale-mount sweep failed: something is still mounted at the point after `umount -f`. The wrapper refuses to mount on top of it. The supervisor can retry after its throttle interval. Preserve cache and evidence; do not escalate to a manual force-unmount without a separately approved recovery. See the lifecycle safety boundary below. |
| rclone's own | mount wrapper | Anything else is rclone's exit status, forwarded because the wrapper `exec`s it. A handled Unix SIGTERM normally exits 143; SIGQUIT normally exits 2 after dumping goroutines. An exit status alone proves neither non-forcing unmount nor completed uploads. |
| `0` / `1` / `2` | `just doctor` | All OK / at least one WARN / at least one FAIL. |
| `2` | any CLI | Usage error. |
| `70` | any CLI | `scripts/lib/common.sh` was not found next to the script — a broken install, not a runtime fault. |

The cache guard tests the deepest *existing* ancestor of the cache directory
for writability, not the volume root: an external volume mounts root-owned
(`/Volumes/<name>` is `drwxr-xr-x root:wheel`) while the user-owned cache
subtree below it is perfectly writable. It still requires the volume itself to
be a real mountpoint, which is what keeps the cache off the boot disk.

## Lifecycle commands

### Recovery safety boundary

For a writable mount, an unanswered upload-status request means the queue is
unknown, not empty. Do not use stop/restart commands as read-only probes or
as a way to establish drainage. Preserve the complete org VFS cache, including
data and metadata, the refreshed rclone config and its remote identity, and
existing error/watchdog evidence. Never run two processes against that cache.

In pinned rclone 1.75.0, SIGINT/SIGTERM invoke exit handlers and the foreground
mount finalizer calls the backend unmount. On macOS, the NFS backend requests
`diskutil umount force` unless it has already recognized an external unmount.
Therefore `launchctl bootout` is not a guaranteed non-forcing stop.
See the pinned [signal handler](https://github.com/rclone/rclone/blob/v1.75.0/lib/atexit/atexit_unix.go#L11-L19),
[mount finalizer](https://github.com/rclone/rclone/blob/v1.75.0/cmd/mountlib/mount.go#L382-L413),
and [NFS backend](https://github.com/rclone/rclone/blob/v1.75.0/cmd/nfsmount/nfsmount.go#L97-L126).
VFS shutdown cancels background work; it is not a successful upload-drain
receipt. [Pinned VFS shutdown](https://github.com/rclone/rclone/blob/v1.75.0/vfs/vfs.go#L359-L395).

The repository adds another independent hazard: watchdog capture tolerates
failed RC queries, then sends SIGQUIT and requests a forced restart. Startup
force-unmounts an existing mount before launching its replacement. Stopping a
loaded mount does not make the watchdog stand down, and an already-running
capture has no final unit-state recheck before its signal. A recovery review
must account for both supervisors and any in-flight capture, not only writers.
The Neo pacing candidate changes neither shutdown mechanism.

A proposed no-force recovery must establish paused writers and quiescent
supervision before detach, then a successful non-forcing detach while the
server is still serving, external-unmount recognition and preserved cache
identity before replacement. This is a review
boundary, not an approved or live-tested recovery procedure. If those conditions
cannot be established, stop: do not substitute SIGTERM, SIGQUIT, `kickstart -k`
or force-unmount. Recovery and cache-loss risk require a separate operator
decision; source preparation or general switch permission is not that decision.

For the Neo photo tranche, the operator ratified one conditional non-forcing
attempt on 2026-09-08, including the disclosed risk of unknown buffered writes.
That does not waive the qualification, resource, writer-pause or cache-preservation
gates. Require natural exit of the original rclone process after successful
detach; disappearance from the mount table alone is insufficient. A busy,
timed-out or ambiguous result stops the attempt. No force or signal fallback.

`just qualify-maintenance --attended --scratch-parent /absolute/private/directory`
tests only a random disposable launchd service. The selected existing scratch
parent must be user-owned, mode 0700, inside the authorized workspace and outside
`/tmp`. The command tests enabled restart as a positive control, whether disabling
preserves a live process, suppression of restart after failure, and re-enabling.
Its receipt records the OS, source hash and exact tested supervision settings.
It does not stop, inspect or recover the real mount and does not prove upload
drainage or the separate watchdog's `KeepAlive = true` behavior. A failed or
inconclusive result blocks recovery.

The fixture removes only its own known files and unloads its random service.
An uncertain cleanup preserves the exact directory and reports it. Clearing its
disabled override can leave an enabled label record in launchd's persistent
database; the tool never edits that database. `just test-maintenance` exercises
the controller with simulated services and time, without changing launchd.

### Mutating entrypoints

These commands are not an ordered recovery recipe. Use only the command
covered by the reviewed operation after the boundary above is satisfied.

```console
nix develop --command just render [ORG]   # materialize rclone-<org>.conf (0600)
nix develop --command just index          # refresh the metadata index now
launchctl kickstart -k gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root
launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root   # not a guaranteed non-forcing stop
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
```

Prefer `launchctl list <label>` over `launchctl print <domain>/<label>`: `print`
dumps the unit's whole inherited environment, which on a fleet host can include
secret-bearing values. `list` prints the PID and exit status, which is what
these probes need.

On Linux the read-only equivalents are
`systemctl --user status gdrive-mounts-<org>-root` and
`journalctl --user -u gdrive-mounts-<org>-root -n 20`.
`systemctl --user restart gdrive-mounts-<org>-root` is a mutating lifecycle
operation, not a probe.

## Index (the agent/AX query surface)

Spotlight does not index these mounts. Query the sqlite rollup instead:

```console
sqlite3 <indexStateDir>/index.sqlite \
  "SELECT path, size FROM files WHERE org='sulliwood' AND path LIKE '%.pdf' LIMIT 20;"
```

Freshness SLO: `orgs.json` `defaults.indexFreshnessSloHours` (24h at time of
writing). Raw per-org JSON at `<indexStateDir>/<org>.json`.

### When an index run fails

`<indexStateDir>/<org>.err` is the current run's stderr; `<org>.err.1`,
`.err.2`, `.err.3` are the three runs before it, newest first. Read `.err.1`
after a failure — the current file belongs to whatever ran next.

Before 2026-09-05 there was no rotation and every start truncated the file, so
the reason a run failed was gone by the time anyone looked. `just doctor` could
say a run had failed and nothing on the host could say why.

`index.intervalSec` is a fixed *period* (launchd `StartInterval`, systemd
`OnUnitActiveSec`), not a gap between runs, and a full-tree
`lsjson -R --fast-list` measured 4h40m to 8h44m on neo against a 6h interval —
so the schedule overlapped itself. A start that finds any occupied or
unavailable lock now refuses with

```
gdrive-index: index lock is occupied or unavailable: ...
```

and exits nonzero before indexing. The lock is `<indexStateDir>/index.lock/`;
atomic `mkdir` grants ownership, and `owner.<pid>` identifies the successful
run. A missing, unreadable or stale marker is not permission to remove the
lock: the owner may still be between directory creation and marker creation.
Abandoned locks require explicit recovery with index writers stopped. Never
replace a live lock. Cleanup removes only the current run's matching marker,
then attempts to remove the empty directory, without recursive deletion.

## Common failures

Any stop/restart advice below is subject to the lifecycle safety boundary;
a symptom alone does not authorize replacing a writable mount with unknown uploads.

| Symptom | Likely cause | Fix |
|---|---|---|
| agent flaps, err log `invalid_grant` | expired refresh token | `just mint-token <org>`, `just seed-lab <org>`, re-switch |
| agent flaps, err log `invalid_grant`, right after an operator revoked access in Workspace/GCP admin | revoked consent, not mere expiry | full re-consent required — repeat `docs/adoption.md` steps 0–3 for that org, not just a token refresh |
| agent restart-loops, exits `78`, breadcrumb says `cache volume … does not exist` or `… is not a mountpoint` | cache SSD absent or not mounted at mount time | mount the SSD; the agent waits ~120s then fails loud once — it never spills onto the boot disk |
| agent restart-loops, exits `78`, breadcrumb says `… is not writable by <user>` and names a mode | the cache subtree is genuinely not writable — a re-formatted or re-plugged volume, or a directory created by another user | the breadcrumb names the exact path and its mode/owner; fix that one directory's ownership, then kickstart the agent. The guard tests the cache subtree, not the volume root, so a root-owned `/Volumes/<name>` is not by itself a fault |
| reads under the mountpoint hang (`stat` still answers from cache) | dead NFS loopback may remain after rclone exits; a live but wedged server is also possible | preserve cache and reconcile uploads under the lifecycle safety boundary; a responding `stat` does not authorize force-unmount or restart |
| `nfsstat -m` says `not responding`, rclone is alive, nothing is logged | the mount stopped answering — wedge class 1 | inspect the read-probe and wedge records. Automatic restart depends on confirmed failures, the configured threshold, startup grace and restart floor; no recovery-time guarantee follows. A single flag transition can self-clear. Apply the lifecycle safety boundary before any manual restart |
| `mount` shows nothing at the point, rclone is alive and old, nothing is logged | the mount may have detached — wedge class 2. In the observed case, NFS server goroutines exited while `mountlib` kept waiting | inspect unit state and the wedge record. Automatic recovery is subject to startup grace, restart floor and cold-start work; it is not guaranteed within a fixed number of seconds. Apply the lifecycle safety boundary before any manual restart |
| the watchdog restarts a mount repeatedly | recurrence, not a watchdog fault | read the wedge record: a rising `restart_count` with a falling `uptime_sec` is a degrading mount. Check the captures and the network before raising the restart floor |
| plain `umount` says `Resource busy` right after activity | open writers or cached NFS handles may remain | stop the recovery attempt and preserve the current state. Do not replace the failed non-forcing detach with `bootout` or `umount -f`; review the lifecycle safety boundary |
| slow first `ls` of a big directory | cold VFS dir cache | expected once; the dir-cache TTL keeps it warm after — use the index for search, not `ls`, on a cold mount |
| `403 rateLimit` errors | per-org Drive API quota | each org has its own client_id, which isolates this; back off the index interval if it recurs |
| an app reports "you don't have permission" once **per file** when writing to a read-only mount, instead of naming the volume | `rdonly` did not reach the kernel, so the VFS refusal arrives as `NFS3ERR_ACCES` and every read-only signal (`MNT_RDONLY`, `statvfs` `ST_RDONLY`, `access(W_OK)`) disagrees with it | `mount \| grep GDrive` must say `read-only`. If it does not, the option was rejected or the unit predates it — kickstart the agent, then see "Mount semantics" |
| a write looks accepted but silently vanishes, or the mount is still read-only after promotion | `orgs.json` `scope` was flipped but the token was never re-minted | scope lives in the token, not the config file — re-run `just mint-token <org>` after any scope change; see `docs/sops-integration.md` |
| `doctor` reports `secrets … WARN mode 755 (want 600)` | **fixed 2026-09-05; if you still see it, the host is on an old generation.** The secret paths are symlinks placed by sops-nix into its own 0700 store, and `file_mode` was reading the *link's* mode, not the file's. Neither GNU nor BSD `stat` dereferences by default — measured, both print `755` for a symlink to a `400` file — so the check had never once looked at a real secret. `stat -L` on both branches |
| `render-config` warns "missing unreadable secret" for one org and no other org is affected | that org's secret isn't seeded in `lab`, or it's enabled in `orgs.json` but never wired into the lab wrapper | seed it (`docs/adoption.md`), wire `secrets.<org>` in the wrapper, re-switch — an unwired org must degrade alone, never take down another org's mount |
| one agent shows a single failed-then-recovered launch in the first ~30s after a fresh switch | sops secret decryption hadn't finished before the agent's first launch attempt | self-heals; only worth investigating if it recurs past that first launch |

## Pending evidence

See `docs/evidence/README.md` and `docs/tracker.md`.
