# Mount bakeoff — 2026-08-18 (P1.D, lane a)

Operator go at Gate D (2026-08-18). Lane (a) only: `rclone nfsmount` showed no
defect that FUSE-T would fix, so lanes (b)/(c) were not tried.

## Environment

| Field | Value |
|---|---|
| Host | neo (macbook-neo) |
| OS / arch | macOS 26.6.2, aarch64 |
| rclone version | 1.75.0 (nixpkgs, `/nix/store/mcjmk66rv9g8f3hf436zdjxgq2mmakqb-rclone-1.75.0`) |
| Backend package + version | `rclone nfsmount` (built-in NFS loopback; no kext, no brew) |
| Org / remote used | `gdrive-sulliwood` (jess@sulliwood.org), conf rendered from the staged client/token |
| Scope | `drive.readonly` (token minted 2026-08-18, refresh fingerprint `25398aef0bce`) |
| Cache | `/Volumes/TinylandSSD/tinyland/gdrive-cache/sulliwood`, `--vfs-cache-mode full`, cap 100G |
| Date/time | 2026-08-18 05:36–05:43 EDT |

## Criteria

| Backend | Mount success | Finder visibility | Big-dir listing latency ("GFTB Stuff", 216 entries) | `umount` clean | Restart survival |
|---|---|---|---|---|---|
| `rclone nfsmount` | yes — `localhost:/ on ~/GDrive/sulliwood (nfs, nodev, nosuid, mounted by jess)` in ~5 s | folder is a normal path under `~/GDrive`; not a `/Volumes` entry (expected for nfsmount at a home path) | cold `ls` 1.30 s, warm 0.01 s; root (969 entries) cold 1.59 s, warm 0.01 s; 1 MiB read 2.68 s cold | SIGTERM → rclone logs `Unmounted rclone mount` and exits (clean). Plain `umount` right after activity returned `Resource busy`; `umount -f` always cleared it | not tested via launchd here (that is the lab switch's proof); crash path tested: see below |
| FUSE-T + `rclone mount` | not tried | | | | |
| macFUSE + `rclone mount` | not tried | | | | |

## Commands and output — `rclone nfsmount`

Mount (flags = the module's argv for a read-only org):

```
rclone nfsmount gdrive-sulliwood: ~/GDrive/sulliwood \
  --config ~/.local/state/gdrive-mounts/rclone-sulliwood.conf \
  --vfs-cache-mode full --cache-dir /Volumes/TinylandSSD/tinyland/gdrive-cache/sulliwood \
  --vfs-cache-max-size 100G --vfs-read-ahead 128M --dir-cache-time 720h \
  --nfs-cache-handle-limit 250000 --volname gdrive-sulliwood-root --read-only \
  --log-level INFO --log-file <scratch>/bakeoff-nfsmount.log
# log: NOTICE: NFS Server running at 127.0.0.1:50547
mount | grep GDrive/sulliwood
# localhost:/ on /Users/jess/GDrive/sulliwood (nfs, nodev, nosuid, mounted by jess)
```

Listing / read / write-denied:

```
cd ~/GDrive/sulliwood
time ls | wc -l                    # 969   real 1.59  (cold)
time ls | wc -l                    # 969   real 0.01  (warm)
time ls "GFTB Stuff" | wc -l       # 216   real 1.30  (cold)
time ls "GFTB Stuff" | wc -l       # 216   real 0.01  (warm)
find "GFTB Stuff" | wc -l          # 217   (216 items + the dir; 215 HEIC, 1 MOV)
head -c 1048576 "<first file>" | wc -c   # 1048576  real 2.68 (cold)
du -sh /Volumes/TinylandSSD/tinyland/gdrive-cache/sulliwood   # 4.9M after the read
touch "GFTB Stuff/.gdm-ro-probe"   # touch: Permission denied   (read-only enforced)
df -h ~/GDrive/sulliwood           # localhost:/  422Gi  234Gi  188Gi  56%
```

Crash path (`kill -9` the rclone process):

```
kill -9 <pid>; stat ~/GDrive/sulliwood     # answers from cache (drwxr-xr-x ... 05:36:05)
ls ~/GDrive/sulliwood                       # HANGS (NFS client retries a dead server) — 5 min timeout hit
umount -f ~/GDrive/sulliwood                # rc=0; mount table clean; hung ls released
```

Consequence baked into the module (PR #5): the wrapper's stale-mount sweep
checks the mount table (`mount | grep -qF " on <point> ("` on darwin,
`mountpoint -q` on linux) and force-unmounts before mounting; a `stat`-based
check would have passed on the cached attributes and then failed the mount.

Remount + stop:

```
rclone nfsmount ... &                       # mounted again in ~8 s, 969 root entries
umount ~/GDrive/sulliwood                   # umount(...): Resource busy -- try 'diskutil unmount'
kill -TERM <pid>
# log: INFO Signal received: terminated / NOTICE ...: Unmounted rclone mount / INFO Exiting...
mount | grep -c GDrive/sulliwood            # 0
```

## Verdict

`rclone nfsmount` is ratified as `mountBackendDarwin`: kext-free, from
nixpkgs, mounts in seconds, warm listings are instant, read-only is enforced by
the token and by `--read-only`, and SIGTERM (the launchd stop path) unmounts
cleanly. Its one sharp edge — a killed server hangs reads until `umount -f` —
is handled by the wrapper's mount-table sweep and by the runbook. FUSE-T is not
needed; it stays the documented fallback.
