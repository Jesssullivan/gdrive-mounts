# Navigating and exploring mounted Drives — agent guide, part 3

How an agent enumerates the mounts on a host, resolves absolute paths through
them safely, and explores their contents without hammering the Drive API.
For what a mount *is* and how to debug one, see `docs/runbook.md`. For how one
comes to exist, see `docs/adoption.md`.

> **Suite:** [Part 1 — enrolment](agent-enrolment.md) ·
> [Part 2 — OAuth setup](agent-oauth-setup.md) ·
> **Part 3 — navigate & explore (this page)** ·
> [Part 4 — symbolic manipulation](agent-symbolic-manipulation.md)

The one rule that outranks everything below: **check the mount table before
touching a path under `~/GDrive`.** The mountpoint directory exists whether or
not anything is mounted on it, and every naive probe lies. Details in
"The parent-is-local trap".

## Enumerate the mounts

### The authority: `effective-settings.json`

`~/.local/state/gdrive-mounts/effective-settings.json` is the machine-readable
record of what the home-manager module actually resolved on this host —
rewritten at every switch, `0600`, never secret-bearing. Its absence means the
host has never been switched, so there is nothing to navigate. Prefer it over
`orgs.json`: a host override of `mountRoot` or `cacheRoot` never reaches
`orgs.json`.

```console
# Every mount unit: org, mountpoint, rendered conf, cache dir, rc socket.
jq -r '.units[] | [.org, .point, .sock] | @tsv' \
  ~/.local/state/gdrive-mounts/effective-settings.json

# Every declared symlink: link path -> target inside the root mount.
jq -r '.links[] | "\(.path) -> \(.target)"' \
  ~/.local/state/gdrive-mounts/effective-settings.json

# The roots everything hangs off.
jq -r '.mountRoot, .indexStateDir, .backend, .platform' \
  ~/.local/state/gdrive-mounts/effective-settings.json
```

Each `units[]` entry also carries `watchdogRecord` (the wedge JSONL) and
`watchdogLabel`, so an agent can go from "this mount" to "this mount's
restart history" without guessing paths.

### The supervisors

```console
# macOS: one launchd agent per org, plus a .watchdog sidecar and the index.
launchctl list | grep dev.tinyland.gdrive-mounts

# Linux (sting): systemd --user.
systemctl --user list-units 'gdrive-mounts-*'
```

Unit names: `dev.tinyland.gdrive-mounts.<org>-root` (launchd) /
`gdrive-mounts-<org>-root` (systemd). Use `launchctl list <label>`, never
`launchctl print` — `print` dumps the unit's inherited environment, which on a
fleet host can carry secret-bearing values.

**A live PID is not a live mount.** Wedge class 2 (`docs/runbook.md`, "The
wedge") is exactly an alive rclone with nothing mounted. Process state answers
"is the unit running"; only the mount table answers "is there a filesystem
here".

### The mount table, and what the flags mean

```console
mount | grep GDrive
# e.g.  localhost:/ on /Users/jess/GDrive/sulliwood (nfs, nosuid, mounted by jess)
```

- `nfs` — the kext-free loopback backend (`rclone nfsmount` serving
  `localhost:/`). On Linux the type is `fuse.rclone` instead.
- `read-only` — kernel `MNT_RDONLY`. Since #12/#15, a unit whose org scope
  resolves read-only gets `--option rdonly` alongside rclone's `--read-only`,
  so this flag is present **iff** the org is read-only, and you can trust it:
  writes fail `EROFS` in the client before any RPC is sent. Its absence on a
  write-scoped org is the expected RW state. If a *read-only* org's line lacks
  it, the option was rejected or the unit predates it — see `docs/runbook.md`,
  "Mount semantics". (As of #16, `sulliwood` is `scope: "drive"` in
  `orgs.json`, so once the attended switch delivers that plan its line shows
  no `read-only`; until that switch the live line still carries it. The flag
  matters for `xoxd`/`lmux` when they enable, and for any future read-only
  org.)
- The full NFS client option list (`soft,intr,timeo=100,retrans=5,dumbtimer`)
  does **not** appear in `mount` output. `nfsstat -m | sed -n '/GDrive/,/^$/p'`
  shows the negotiated options. Note the runbook's standing caveat: the
  option-forwarding path is read from rclone 1.75.0 source and asserted by
  eval tests, but not yet confirmed against a live mount — confirm on first
  deploy.

## The parent-is-local trap

`~/GDrive` and `~/GDrive/<org>` are ordinary directories on the **boot disk**.
Activation runs `mkdir -p` on the mount root, and each mount wrapper runs
`mkdir -p` on its mountpoint before `exec`ing rclone
(`nix/modules/home-manager.nix`). So when a mount is down:

- `test -d ~/GDrive/sulliwood` **succeeds** — it is a real local directory.
- A write to `~/GDrive/sulliwood/anything` **succeeds** — onto the local disk.
  When the mount comes back it mounts *over* that data, which is now invisible,
  unsynced, and easy to mistake for "the upload worked".
- The `~/GDrive/<org>-<link>` symlinks exist even while the mount is down
  (activation writes them with `ln -sfn`), and they resolve into the local
  placeholder directory — so a dangling-or-not check on the link proves
  nothing either.

Three probes that lie, even on a healthy mount (pre-#12 they lied about
read-only too; post-#12 they lie about *presence*):

| Probe | Why it lies |
|---|---|
| `test -d` / `os.path.isdir` / `Path.exists()` | the local placeholder dir satisfies all of them |
| `access(path, W_OK)` / `test -w` / `os.access` | answers for whatever filesystem is there — local disk when the mount is down |
| "the symlink resolves" | it resolves into the local placeholder |

### Proving you are on the mount

**First choice — the mount-table check.** It never touches the path, so it
cannot block on a wedged mount (`hard,nointr` callers block in the kernel
where no signal lands — this is the same rule the watchdog's own
`mountedCheck` obeys):

```console
# macOS — exact-match the mountpoint, exit 0 iff mounted:
mount | grep -qF " on $HOME/GDrive/sulliwood ("

# Linux:
awk -v p="$HOME/GDrive/sulliwood" '$2 == p { found = 1 } END { exit !found }' /proc/self/mounts
```

**Second choice — device-number comparison** (`statfs`): the mountpoint and
its parent sit on different filesystems iff something is mounted. This is
positive proof of *which* filesystem answers, but it stats the point, so run
it only after the mount-table check passes:

```console
# macOS: use /usr/bin/stat explicitly. In nix-managed PATHs `stat` is GNU
# even on Darwin, and GNU `stat -f` is --file-system: it SUCCEEDS and prints
# a statfs report instead of a device number — garbage that looks like data.
[ "$(/usr/bin/stat -f %d "$HOME/GDrive/sulliwood")" != "$(/usr/bin/stat -f %d "$HOME/GDrive")" ] \
  && echo mounted || echo NOT-mounted
```

Never substitute `access(W_OK)`, `test -w`, or "try a write and see" for
either probe.

## The index: search before you walk

Spotlight does not index these mounts, and a cold `ls` of a big directory is
slow once per `dirCacheTime` window. For any "find me the files that…"
question, query the local index instead of walking the mount.

- **Refresh cadence**: launchd `dev.tinyland.gdrive-mounts.index` with
  `StartInterval` = `index.intervalSec` (default **21600s = 6h**); systemd
  timer `gdrive-mounts-index` with `OnBootSec=300`,
  `OnUnitActiveSec=intervalSec`, `Persistent=true`. Freshness SLO 24h
  (`indexFreshnessSloHours`); `just doctor` warns when stale. Refresh now:
  `nix develop --command just index`.
- **Location**: `~/.local/state/gdrive-index/` (the `indexStateDir` in
  `effective-settings.json`) — `index.sqlite` (rollup), one raw `<org>.json`
  per org (verbatim `rclone lsjson -R --fast-list` output), `<org>.err` when
  a listing failed.
- **Schema** (`scripts/gdrive-index.sh`):
  `files(org, path, size, mtime, isdir, indexed_at)` and
  `runs(run_id, org, status, entries, started_at, finished_at, message)`.
  `path` is **Drive-relative**; the absolute path is
  `<mountRoot>/<org>/<path>` (the root mount's remote path is empty).
- **Failure semantics**: a failed listing leaves the previous `<org>.json` in
  place and the previous rows in `files` — only successful runs roll up. So
  check `runs` before trusting `files` freshness; a dead token cannot look
  fresh, but it can look *old*.

```console
# Search by name/extension — instead of a cold recursive ls:
sqlite3 ~/.local/state/gdrive-index/index.sqlite \
  "SELECT path, size FROM files WHERE org='sulliwood' AND isdir=0
     AND path LIKE '%.pdf' ORDER BY size DESC LIMIT 20;"

# Is the index fresh, per org? (latest run status + timestamp)
sqlite3 ~/.local/state/gdrive-index/index.sqlite \
  "SELECT org, status, entries, finished_at FROM runs r
     WHERE finished_at = (SELECT MAX(finished_at) FROM runs WHERE org = r.org)
     GROUP BY org;"

# The raw per-org JSON, when you want rclone's full metadata (MimeType, ID…):
jq -r '.[] | select(.IsDir | not) | select(.Path | endswith(".numbers")) | .Path' \
  ~/.local/state/gdrive-index/sulliwood.json
```

Turn an index hit into a filesystem path only after the liveness guard:

```console
p="$HOME/GDrive/sulliwood/GFTB Stuff/some file.pdf"   # quote — Drive paths have spaces
mount | grep -qF " on $HOME/GDrive/sulliwood (" || { echo "not mounted" >&2; exit 1; }
open -R "$p"   # or read it — see the hydration note below
```

## Exploring from Python

Guard first, always. Then explore metadata-light: with `vfsCacheMode: full`,
**reading file content is a real download** — every byte lands in the VFS
cache under `<cacheRoot>/<org>` (LRU-pruned at `cacheMaxSize`). Listing
directories and reading cached attributes does not hydrate file data. So:
enumerate with `os.scandir` (one `readdir` per directory), skip per-entry
`stat()` unless you need it (each miss is an attribute lookup; the kernel
trusts them for `attrTimeout` = 5s), never `open()` a file just to sniff it,
and answer bulk size/mtime questions from the index, not the mount.

```python
#!/usr/bin/env python3
"""Explore a gdrive-mounts org. The liveness guard runs FIRST, every time."""
import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path.home() / ".local/state/gdrive-mounts"


def units() -> list[dict]:
    """Enumerate mounts from effective-settings.json — the per-host authority."""
    es = STATE / "effective-settings.json"
    if not es.exists():
        sys.exit("gdrive-mounts: no effective-settings.json — host never switched")
    return json.loads(es.read_text())["units"]


def is_mounted(point: str) -> bool:
    """Mount-table check. Deliberately never stats the point: a wedged
    hard,nointr mount blocks stat() in the kernel where no signal lands."""
    if sys.platform == "darwin":
        out = subprocess.run(["mount"], capture_output=True, text=True,
                             check=True).stdout
        return f" on {point} (" in out
    with open("/proc/self/mounts") as f:
        return any(line.split()[1] == point for line in f)


def assert_mounted(point: str) -> None:
    if not is_mounted(point):
        sys.exit(f"REFUSING {point}: not mounted. The directory exists but it "
                 f"is the LOCAL placeholder — writes would land on the boot "
                 f"disk and be shadowed when the mount returns.")


def is_read_only(point: str) -> bool:
    """Honest since #12: `rdonly` sets MNT_RDONLY, so ST_RDONLY tells the
    truth (pre-#12 it lied). statvfs touches the mount — call this only
    after assert_mounted()."""
    return bool(os.statvfs(point).f_flag & os.ST_RDONLY)


def iter_entries(root: Path, max_depth: int = 2):
    """Metadata-light walk: readdir only, bounded depth, no open().
    is_dir(follow_symlinks=False) may fall back to one lstat per entry on
    NFS (d_type can be unknown); that is attribute traffic, not a download."""
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                yield e
                if e.is_dir(follow_symlinks=False) and depth + 1 < max_depth:
                    stack.append((Path(e.path), depth + 1))


if __name__ == "__main__":
    for u in units():
        point = u["point"]
        if not is_mounted(point):
            print(f"{u['org']}: {point} NOT MOUNTED — skipping", file=sys.stderr)
            continue
        ro = is_read_only(point)
        print(f"{u['org']}: {point} mounted read_only={ro}")
        for e in iter_entries(Path(point), max_depth=1):
            print(f"  {'d' if e.is_dir(follow_symlinks=False) else 'f'} {e.name}")
```

Notes:

- `os.access(path, os.W_OK)` is banned for these mounts. It lies twice: on a
  down mount it answers for the local placeholder, and even on a live
  read-only NFS mount its answer does not predict the write outcome
  (`docs/runbook.md`, "Mount semantics").
- Prefer `os.stat(point).st_dev != os.stat(os.path.dirname(point)).st_dev`
  only as a *secondary* confirmation after `is_mounted()` — it stats the
  point.
- Bulk questions ("all PDFs over 10 MB") belong in sqlite:
  `sqlite3.connect(str(Path.home() / ".local/state/gdrive-index/index.sqlite"))`
  against the `files` table — zero Drive traffic, zero wedge exposure.

## Exploring from fish

Same shape: guard first, then look.

```fish
# Liveness guard — mount-table only, never touches the path.
function gdrive_mounted --argument-names point
    switch (uname)
        case Darwin
            mount | string match -q -- "* on $point (*"
        case '*'
            awk -v p="$point" '$2 == p { found = 1 } END { exit !found }' /proc/self/mounts
    end
end

set point ~/GDrive/sulliwood
if not gdrive_mounted $point
    echo "REFUSING $point: not mounted — the dir is the LOCAL placeholder" >&2
    exit 1
end

# Honest read-only check (post-#12 the kernel flag tells the truth):
if mount | string match -q -- "* on $point (*read-only*"
    echo "$point is read-only — writes fail EROFS"
end

# Resolve a declared link to its real path under the root mount, then
# re-guard on the MOUNTPOINT (the link resolves even when the mount is down):
set target (path resolve ~/GDrive/sulliwood-gftb-stuff)
string match -q -- "$point/*" $target
and gdrive_mounted $point
or begin; echo "link escapes the mount or mount is down" >&2; exit 1; end

# Explore shallow; quote everything — Drive paths contain spaces.
for f in $point/*
    test -d "$f"; and echo "d $f"; or echo "f $f"
end
```

- `test -d` / `test -e` are fine for *shape* questions once the guard has
  passed; they are never a substitute for the guard.
- `test -w` is banned here for the same reasons as `os.access`.
- Do not shell out to `stat` without pinning the implementation — in a
  nix-managed PATH `stat` is GNU even on macOS, and GNU `stat -f %d` succeeds
  with a filesystem report, not a device number. Use `/usr/bin/stat -f %d`
  when you need the device compare on Darwin.

## Rate-limit posture

`403 rateLimit` is per-org Drive API quota, and each org has **its own OAuth
client_id**, so orgs are isolated: an agent hammering `sulliwood` cannot
throttle `xoxd`. Inside one org, everything shares the budget — cold directory
listings, per-file attribute misses, content reads, the `--poll-interval`
change poll (5m), and each index run (a full `rclone lsjson -R` listing).

What that means in practice:

- Use the index for search and bulk metadata; walk the mount only for content
  you will actually read.
- Do not parallel-walk cold directory trees in one org; the warm dir cache
  (`dirCacheTime: 720h`) makes the *second* pass free, so serialize the first.
- If `403 rateLimit` recurs, back off the index interval
  (`programs.gdrive-mounts.index.intervalSec` in the lab wrapper) before
  anything else — see the failure table in `docs/runbook.md`.
- A rate-limited org degrades alone; do not "fix" it by touching another
  org's config.
