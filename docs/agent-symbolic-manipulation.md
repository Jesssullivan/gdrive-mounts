# Agent symbolic manipulation — files and folders from the terminal

Part 4 of the agent-surface docs: mutating Drive content through a live
read-write mount, entirely from the terminal, with scripts an agent can run
and re-run. The worked example doubles as the acceptance test for the
2026-08-22 sulliwood write promotion: convert every photo under
`GFTB Stuff` to a very small optimized WebP in a relative `webp/` folder,
preserve every original, and prove the bytes reached Drive.

For what a mount *is* (units, logs, exit codes, the wedge) see
`docs/runbook.md`. For how scope became `drive` in the first place see
`docs/sops-integration.md` ("Scope and promotion") and `docs/adoption.md`.

> **Suite:** [Part 1 — enrolment](agent-enrolment.md) ·
> [Part 2 — OAuth setup](agent-oauth-setup.md) ·
> [Part 3 — navigate & explore](agent-navigate-explore.md) ·
> **Part 4 — symbolic manipulation (this page)**.
> The [validation checklist](#validation-checklist--run-after-the-attended-switch)
> at the end of this page is **post-switch**: nothing in it runs before the
> operator's attended `just nix-switch macbook-neo` in `lab`.

**Placeholders — the two spots that name real data.** Every command below
uses them literally so it is copy-pasteable on neo today:

1. **`sulliwood`** — the org. Substitute your org's `name` from `orgs.json`
   (it decides the mountpoint `~/GDrive/<org>`, the remote
   `gdrive-<org>`, the unit label, the config and socket paths).
2. **`GFTB Stuff`** — the folder inside the mount. Substitute your target
   folder. It is also reachable through the declared symlink
   `~/GDrive/sulliwood-gftb-stuff` (a link *into* the root mount, not a
   second mount — `docs/runbook.md` "Mount layout"); the commands below use
   the real path under the mountpoint so `relative_to()` and `find` behave.

## Preconditions

The RW mount exists only after the operator's attended
`just nix-switch macbook-neo` in `lab` (`docs/adoption.md` step 5 — never
agent-run). sulliwood is `scope: "drive"` in `orgs.json` (#16, intent only)
and the token must first be re-minted at that scope — promotion is a
re-mint, not a config edit (`docs/sops-integration.md`; the full three-part
ceremony is [`docs/agent-enrolment.md`](agent-enrolment.md)). Before
touching anything, prove all three of
the following. Each probe is honest post-#12/#15; the pre-#12 failure mode
is named inline.

**1. Something is mounted at the point — the parent-is-local trap.**
When no mount is present, `~/GDrive/sulliwood` is an ordinary local
directory and writes into it succeed silently onto the boot disk. The full
trap — why every naive probe lies, and the guard patterns for scripts — is
owned by [`docs/agent-navigate-explore.md`](agent-navigate-explore.md)
("The parent-is-local trap"). The two probes, in order:

```console
mount | grep -F "$HOME/GDrive/sulliwood"
```

Expect a `localhost:/ on /Users/jess/GDrive/sulliwood (nfs, ...)` line, and
post-promotion it must **not** say `read-only`. No line at all means the
directory is the local shadow — stop. Cross-check with device numbers
(`/usr/bin/stat` explicitly — GNU `stat -f` *succeeds with garbage*; see
part 3):

```console
/usr/bin/stat -f %d "$HOME/GDrive"; /usr/bin/stat -f %d "$HOME/GDrive/sulliwood"
```

Two *different* device numbers = a mount is present. Equal numbers = shadow.

**2. The mount is answering, not wedged.**

```console
nfsstat -m | sed -n '/GDrive\/sulliwood/,/^$/p'
```

A `not responding` status flag means the wedge class from
`docs/evidence/2026-08-19-nfs-wedge.md`; wait for the watchdog or kickstart
the unit (`docs/runbook.md` "Operator probes") before writing anything.

**3. Writes are really accepted — EROFS vs the old EACCES lie.**
Do a real write; do not trust `access(W_OK)` or a GUI. On a
`--read-only`-only mount (pre-#12) the kernel never sets `MNT_RDONLY`, so
`access(W_OK)` says yes and the write then fails per-file with `EACCES`
("Operation not permitted") — the 13-dialog Photos.app incident. Post-#12 a
read-only unit also carries kernel `rdonly`, so the refusal is an honest
`Read-only file system` (`EROFS`). On the promoted RW mount both layers are
dropped together and this succeeds:

```console
touch "$HOME/GDrive/sulliwood/GFTB Stuff/.gdm-write-probe" \
  && rm "$HOME/GDrive/sulliwood/GFTB Stuff/.gdm-write-probe" \
  && echo "write path open"
```

A `Read-only file system` here means the token is still the old
`drive.readonly` one — scope lives in the token, and `just doctor` warns on
the config-vs-token mismatch. Note the probe's limit: success proves the
kernel and rclone's VFS accept writes; it does not prove Drive durability
(see "Durability under soft + RW" below).

## The conversion pipeline

Contract, both lanes:

- **Originals are never modified or deleted.** Output goes to
  `GFTB Stuff/webp/`, mirroring the source tree relative to `GFTB Stuff`.
- **Idempotent re-runs.** A source is skipped when its target exists and is
  newer (`skip-if-target-newer`). Re-running after a partial failure redoes
  only the missing work.
- **Temp-then-rename, on the SAME mount.** Each file is written to a hidden
  `.tmp.*` sibling in the target directory, then renamed into place. On the
  same mount that is `rename(2)` — atomic, so the final name is either whole
  or absent, never partial. A temp in `/tmp` would cross filesystems and turn
  the rename into a copy (`EXDEV` fallback), losing exactly that guarantee.
- **"Very small" sizing:** WebP quality 45–60 (50 below) plus a long-edge
  cap of 1600 px. The cap does most of the shrinking; quality below ~45
  starts to smear faces.

### Python (Pillow) — the primary lane

Save as `~/gdm-webp.py` (any local path outside the mount). The inline
metadata makes `uv run` fetch Pillow and pillow-heif itself; without uv,
`python3 -m pip install pillow pillow-heif` and run with `python3`.

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow", "pillow-heif"]
# ///
"""Convert photos under ROOT to small WebP in ROOT/webp/, idempotently.

Originals are never touched. Temp-then-rename on the same mount, so a
killed run never leaves a half-written file under its final name.
"""
import os
import sys
from pathlib import Path

from PIL import Image, ImageOps

try:  # HEIC (iPhone) support; harmless if absent
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC = {".heic"}
except ImportError:
    HEIC = set()

EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"} | HEIC
QUALITY = 50      # 45-60 = "very small but still a photo"
LONG_EDGE = 1600  # px; thumbnail() only ever shrinks


def convert(src: Path, dst: Path) -> None:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)  # bake in phone-photo orientation
        im.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        tmp = dst.with_name(f".tmp.{dst.name}")  # SAME dir = same mount = rename(2)
        try:
            im.save(tmp, "WEBP", quality=QUALITY, method=6)
            os.replace(tmp, dst)  # atomic: dst is whole or absent
        except BaseException:
            tmp.unlink(missing_ok=True)  # an EIO mid-write leaves no residue
            raise


def main() -> int:
    root = Path(sys.argv[1]).expanduser()
    out = root / "webp"
    done = skipped = failed = 0
    for src in sorted(root.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in EXTS:
            continue
        if out in src.parents:  # never re-convert our own output
            continue
        dst = (out / src.relative_to(root)).with_suffix(".webp")
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            skipped += 1  # idempotent re-run: target newer, nothing to do
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            convert(src, dst)
            done += 1
            print(f"webp: {dst}")
        except OSError as e:  # EIO from a soft mount lands here — keep going
            failed += 1
            print(f"FAIL {src}: {e}", file=sys.stderr)
    print(f"converted={done} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run it (placeholder spots 1 and 2 both appear here):

```console
uv run ~/gdm-webp.py "$HOME/GDrive/sulliwood/GFTB Stuff"
```

Failure modes, named:

- **First run walks a cold directory tree over NFS** — slow once, then the
  720h dir cache keeps it warm (`docs/runbook.md` "Common failures"). For
  *discovery* on a cold mount prefer the sqlite index, not `ls`.
- **A SIGKILL between `save` and `replace`** can strand a `.tmp.*` file
  (the `except` never runs). Sweep before a re-run:
  `find "$HOME/GDrive/sulliwood/GFTB Stuff/webp" -name '.tmp.*' -delete`.
- **`FAIL ...: [Errno 5] Input/output error`** is the soft-mount timeout
  surfacing — see "Durability under soft + RW". The temp is cleaned up, the
  final name never appeared; fix the mount, re-run, the skip logic redoes
  exactly the failures.

### Fish (cwebp) — the shell lane

Same contract in a fish loop. Fish, deliberately: variables do not
word-split, so `GFTB Stuff` needs no quoting gymnastics inside the loop.
`cwebp` comes from `nix shell nixpkgs#libwebp`; `/usr/bin/sips` (Darwin)
supplies dimensions for the long-edge cap, since cwebp's `-resize` needs to
be told which edge to pin.

```fish
nix shell nixpkgs#libwebp --command fish -c '
set root "$HOME/GDrive/sulliwood/GFTB Stuff"    # PLACEHOLDERS: org + folder
set out "$root/webp"
set q 50
set cap 1600
for src in $root/**.{jpg,jpeg,png,JPG,JPEG,PNG}
    string match -q "$out/*" -- $src; and continue           # skip our own output
    set rel (string replace -- "$root/" "" $src)
    set dst "$out/"(string replace -r "\.[^.]+\$" ".webp" -- $rel)
    test -e $dst; and test $dst -nt $src; and continue       # skip-if-target-newer
    mkdir -p (dirname $dst)
    set w (/usr/bin/sips -g pixelWidth  $src | awk "/pixelWidth/  {print \$2}")
    set h (/usr/bin/sips -g pixelHeight $src | awk "/pixelHeight/ {print \$2}")
    set resize
    if test $w -gt $cap -o $h -gt $cap
        if test $w -ge $h; set resize -resize $cap 0; else; set resize -resize 0 $cap; end
    end
    set tmp (dirname $dst)/.tmp.(basename $dst)
    if cwebp -q $q -m 6 -mt -quiet $resize $src -o $tmp      # temp on the SAME mount
        mv $tmp $dst                                          # rename(2): atomic
    else
        rm -f $tmp
        echo "FAIL $src" >&2
    end
end'
```

Failure modes, named:

- **cwebp ignores EXIF orientation** — phone photos shot sideways come out
  sideways. Use the Python lane for camera rolls; the fish lane is for
  already-upright material.
- **cwebp cannot read HEIC.** The glob excludes it; HEIC sources need the
  Python lane (pillow-heif).
- **An unmatched glob** makes fish print `No matches for wildcard` and skip
  the loop — that is fish telling you the folder path (placeholder 2) is
  wrong, not an empty success.

## Verifying the bytes reached Drive

Three checks, in order. Each one is out-of-band from the layer above it.

**1. The mount stayed up for the whole run — the trap, again.** If the mount
detached mid-run (wedge class 2: nothing mounted, rclone still alive), the
point reverted to a plain local directory and every write after that instant
landed on the boot disk, successfully and silently. The wedge record is the
witness:

```console
tail -5 ~/.local/state/gdrive-mounts/wedge.sulliwood-root.jsonl 2>/dev/null || echo "no wedge records"
```

No rows timestamped inside your run window = the mount held. A row with
`"probe":"unmounted"` in the window means shadow writes are likely. To
inspect the shadow you must take the mount down — it is invisible otherwise:

```console
launchctl bootout gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root
ls -la "$HOME/GDrive/sulliwood/"   # anything here is boot-disk shadow, not Drive
launchctl kickstart -k gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-root
```

**2. The VFS write cache has drained.** Under `vfsCacheMode: full` a
successful `close()` means the bytes are in
`/Volumes/TinylandSSD/tinyland/gdrive-cache/sulliwood`, not yet on Drive;
upload is asynchronous. Ask rclone over its rc unix socket (the same channel
the watchdog uses — it never touches the mount path):

```console
rclone rc --unix-socket ~/.local/state/gdrive-mounts/rc-sulliwood-root.sock vfs/stats
```

Wait for `diskCache.uploadsInProgress` and `diskCache.uploadsQueued` to
reach 0. Comparing Drive listings before the cache drains produces false
mismatches, not evidence.

**3. Drive itself, through the rendered config — never through the mount.**
`rclone lsl` against the org's rendered config goes straight to the Drive
API; it cannot be fooled by the VFS cache or a shadow directory. This is the
same accepted pattern as `just smoke sulliwood` (which lists the Drive root
this way); mirror its flag hygiene and keep the process short-lived — the
config file has a single-writer rule because rclone writes refreshed tokens
back into it (`scripts/render-config.sh` header):

```console
rclone lsl --config ~/.local/state/gdrive-mounts/rclone-sulliwood.conf \
  --log-level ERROR --retries 1 --timeout 30s --contimeout 15s \
  "gdrive-sulliwood:GFTB Stuff/webp" | head
```

Then compare counts, local mount vs Drive:

```console
find "$HOME/GDrive/sulliwood/GFTB Stuff/webp" -type f -name '*.webp' | wc -l
rclone size --config ~/.local/state/gdrive-mounts/rclone-sulliwood.conf \
  --log-level ERROR "gdrive-sulliwood:GFTB Stuff/webp"
```

Equal object counts = every byte the pipeline reported converted is on
Drive. A local count *higher* than Drive's after step 2 drained is the
shadow trap — go back to step 1.

Finally, refresh the index so the new files are queryable (the index is also
built out-of-band, `rclone lsjson -R` through the rendered config, so its
rows are Drive truth):

```console
nix develop --command just index
sqlite3 ~/.local/state/gdrive-index/index.sqlite \
  "SELECT COUNT(*) FROM files WHERE org='sulliwood' AND path LIKE 'GFTB Stuff/webp/%';"
```

## Durability under soft + RW

`defaults.nfsMountOptions` keeps `soft` (with `intr`, `timeo=100`,
`retrans=5`, `dumbtimer`), and the module refuses to activate a read-write
unit with `soft` present unless the operator sets
`programs.gdrive-mounts.allowSoftReadWrite = true` — fail-closed, so the
fact that the RW switch activated at all means that durability trade was
ratified deliberately, not inherited (`docs/runbook.md` "Mount semantics").
What the trade means at this keyboard:

- **Stalls surface as `EIO` instead of hanging forever.** A stalled loopback
  RPC fails after roughly `timeo × retrans` — 10 s × 5 ≈ 50 s with the
  registry defaults (`dumbtimer` makes `timeo` literal). Without `soft`,
  macOS's `hard,nointr` default blocks the caller in the kernel where no
  signal lands — the 2026-08-19 wedge. For a scripted pipeline, `EIO` is the
  right failure: the loop logs it and moves on, and the temp-then-rename
  contract means the aborted file's final name never exists.
- **Retry semantics are "re-run the pipeline", not "trust errno once".**
  After any `FAIL` batch: fix or wait out the mount (watchdog restart floor
  is 300 s), sweep stray `.tmp.*`, re-run. The skip-if-target-newer check
  makes the re-run touch only the failures.
- **`soft` governs one leg only.** It is the app↔rclone loopback contract.
  The rclone↔Drive leg has its own bounded budget (`--timeout 20s`,
  `--low-level-retries 3` — the latency budget in `docs/runbook.md`) and the
  VFS full cache in between.
- **The VFS cache is the durability buffer, and it persists.** Dirty cache
  files live on disk under
  `/Volumes/TinylandSSD/tinyland/gdrive-cache/sulliwood` and survive rclone
  restarts, including watchdog `SIGQUIT` restarts; uploads resume on the
  next start. The real loss window is removing or reformatting the cache SSD
  while `vfs/stats` still shows queued uploads — drain (verification step 2)
  before any cache-volume maintenance, and remember the wrapper exits 78
  rather than ever falling back to the boot disk if the SSD is absent at
  start.

## Validation checklist — run after the attended switch

The RW mount goes live only at the operator's `just nix-switch macbook-neo`;
nothing below is runnable before it. Run top to bottom; each line names its
pass condition. Record the whole run as an evidence receipt —
`docs/evidence/2026-08-22-gftb-webp-acceptance.md`, commands verbatim,
output pasted, one-sentence verdict (`docs/evidence/README.md`).

1. `nix develop --command just doctor` — no FAIL; no scope-vs-token warning
   for sulliwood.
2. `mount | grep -F "$HOME/GDrive/sulliwood"` — one `nfs` line, **without**
   `read-only` (the promotion dropped `--read-only` and kernel `rdonly`
   together).
3. `/usr/bin/stat -f %d` on `~/GDrive` vs `~/GDrive/sulliwood` — different
   device numbers.
4. `nfsstat -m | sed -n '/GDrive\/sulliwood/,/^$/p'` — no `not responding`;
   options still show `soft,intr,timeo=100,retrans=5,dumbtimer` (and, on any
   remaining read-only org, `rdonly` — the still-pending live confirmation
   from `docs/runbook.md` "Mount semantics").
5. The write probe: `touch`+`rm` of `.gdm-write-probe` under `GFTB Stuff`
   succeeds.
6. Pilot run: `uv run ~/gdm-webp.py` against a small subfolder of
   `GFTB Stuff` first — `failed=0`, `.webp` files appear under `webp/`.
7. Full run — `failed=0`, and a second invocation immediately after reports
   `converted=0` with everything skipped (idempotence proof).
8. `rclone rc --unix-socket ~/.local/state/gdrive-mounts/rc-sulliwood-root.sock vfs/stats`
   — uploads drain to 0.
9. Local `find ... | wc -l` equals the object count from
   `rclone size ... "gdrive-sulliwood:GFTB Stuff/webp"`.
10. `wedge.sulliwood-root.jsonl` gained no rows during the run window.
11. `nix develop --command just index`, then the sqlite `COUNT(*)` over
    `GFTB Stuff/webp/%` matches step 9.
12. Write the evidence receipt and link it from `docs/tracker.md`.
