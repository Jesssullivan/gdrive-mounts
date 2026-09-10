# Agent symbolic manipulation — verified GFTB photo consolidation

Part 4 of the agent-surface docs. Use `just photos`, not the historical
inline Python/fish conversion examples. Those examples used filename/mtime
shortcuts, could lose orientation work, and treated equal counts as byte
verification. They remain in git history, not as runnable instructions.

## Preservation contract

Ratified 2026-09-08: Drive holds one canonical copy of each distinct original
and one accepted WebP derivative. Preserve the existing corrected
**2000px/quality-80** WebPs and curation; generate only missing outputs.
Deduplicate exact bytes by Drive ID and content evidence, never names alone.
Retirement requires independent verification and explicit manifest approval.

This Neo workflow covers two roots only: the actual Sulliwood mount's
`GFTB Stuff` folder, identified by Drive ID, and
`/Volumes/TinylandSSD/tinyland/gftb-photos`. The `sulliwood-gftb-stuff`
symlink is another route into the same mount, not another copy. Never follow
arbitrary shortcuts or symlinks as new source roots.

The separate `tinyland/gdrive-cache` tree is **not** a duplicate photo source:
it can contain the only pending copy of an upload. Never retire its contents.

## Before real inventory or publication

1. Pass lab's resource/storage preflight: exact SSD identity, free capacity,
   memory pressure and queued uploads. Do not kill unrelated jobs or clear
   caches to make a check pass.
2. Deliver the reviewed mount candidate using the attended direct local
   workflow in [`adoption.md`](adoption.md#5-direct-local-home-manager-delivery-attended).
   Verify source, active generation and effective settings separately.
3. Confirm the writable NFS mount is present and answering. An ordinary local
   directory at the same pathname is a shadow, not a fallback.
4. Keep the existing ratified Sulliwood credentials. A pin update or photo
   reconciliation is not a new OAuth ceremony.

The optional `photos` package pins Python, Pillow, HEIF support and rclone:

```sh
nix run .#photos -- --help
```

Inside the pinned devShell, use `just photos`. Realize missing dependencies
on an approved builder, not through an unbounded local build on Neo. Normal
Home Manager mount installations do not depend on the optional photo package.

## Four phases

```sh
just photos plan
just photos apply PLAN_SHA256
just photos verify PLAN_SHA256
just photos retire PLAN_SHA256 --approve-digest PLAN_SHA256
```

Run one phase at a time, inspecting its result before continuing. Replace
`PLAN_SHA256` with the full digest returned by `plan`; do not paste the block
as an unattended batch. Inspect the exact retirement list before approving it.

`plan` does real inventory and hashing, but does not change photos. It writes
an immutable mode-0400 `plan-<sha256>.json` and a private append-only journal
under `<effective stateDir>/photos/sulliwood` (mode 0700). Standard output is
an aggregate result, not the private inventory. `--settings PATH` selects an
effective-settings receipt; `--state-dir PATH` selects private state outside
the mount, source corpus and VFS cache. Neither expands the approved roots.

Resolve missing provenance, filename collisions and unmapped files before
publication. The historical 309 Drive names versus 227 staged originals
establishes ambiguity, not today's count or proof of identical content.

The JSON phase report includes `canonical_location.absolute_path` and
`canonical_location.drive_folder_id`. Planning reports a proposed location,
not a verified mount. Only successful `verify` sets
`mounted_directory_verified: true`: it holds the actual GFTB directory open,
checks mount identity, independently verifies Drive contents, and reopens the
directory to detect replacement. `drive_contents_verified` is separate.
Neither flag is a claim that deduplication or retirement is complete. Private
filenames, warning details and quarantine locations stay in the manifest and
journal; standard output reports counts.

After approved retirement, run `just photos verify PLAN_SHA256 --mounted-readback`.
This requires the completed retirement receipt, reads every canonical payload
through the real mount, compares its SHA256, and independently verifies Drive
again before reporting `mounted_payloads_verified: true`. Missing or changed
payloads fail the command even if retirement has already completed. Retry the
same manifest after resolving the fault; do not regenerate quarantined sources.
Readback also binds the independently verified byte length: a wrong initial
size or an overlong stream refuses before copying excess bytes into private
state. Case-variant curation paths require explicit reconciliation during
planning rather than being silently renamed or treated as exact path matches.

`apply` adopts accepted WebP bytes before considering conversion. The latest
orientation judgments were relative to QuickLook intermediates; their angles
must not be replayed blindly against newly EXIF-transposed HEICs. Preserve
original metadata, videos, sidecars, design selections and do-not-publish
restrictions. This workflow does not change sharing or publish a website.

If a corrected WebP exists only on Drive, the planner preserves its ID/hash
and blocks regeneration pending proven adoption or an orientation decision.
Missing the SSD derivative is not evidence that the accepted output is absent.

When accepted SSD bytes match a Drive WebP under another proven original
alias, keep that existing Drive ID/path instead of creating another copy.
Prefer the adopted filename if an exact-byte copy already exists there;
otherwise choose an existing matching alias deterministically. Other proven
same-byte aliases become explicit `verified-webp-copy` retirement candidates.
Different-byte outputs at other aliases remain preserved for review.
No canonical keeper ID can also be a retirement candidate for another original.

New CLI plans require the reviewed `orientation-judged.csv` (`name,cw,how`)
and the selection/restriction sections of `design-picks.md`. The parser binds
legacy `.jpg` stems through duplicate aliases to one canonical original group.
Missing or ambiguous references block publication. The historical selection
count is evidence to reconcile, not a universal parser rule. The exact reviewed
`IMG_2630_placeholder` no-file row is retained as an annotation; other missing
references do not receive an exemption. CSV rotations remain historical notes,
never encoder instructions. Missing accepted derivatives need orientation
review before any generated replacement can be published.

For missing derivatives, `just photos prepare-review PLAN_SHA256` prepares
private review material only. It downloads originals by the manifest's exact
Drive IDs, checks their fingerprints and original hashes, and creates previews
with the pinned codec recipe. Sources and previews remain under private photo
state, outside the SSD corpus, mount and VFS cache. Their journal records bind
the source and preview bytes without declaring orientation acceptance or
publication consent. Resume rechecks journal-owned files; unowned or changed
files require reconciliation, not replacement.

Preparation does not clear the immutable plan's blockers, change historical
curation, or publish, promote or retire any object. Review the actual previews
before accepting a derivative. An ambiguous orientation remains a decision,
not a reason to replay historical rotations or silently accept encoder output.

After completed, hash-bound visual review, `just photos adopt-reviewed
PLAN_SHA256 --review-digest REVIEW_SHA256` stages the reviewed originals and
unchanged previews into paired SSD `originals/` and `webp/` paths. The private
read-only `review-REVIEW_SHA256.json` decision covers every missing derivative
and binds its source Drive ID, original and preview hashes, and independent
review evidence. This phase supports only reviewed zero-additional-rotation
previews; it refuses ambiguous or incomplete review and never replays old
rotation notes. Acceptance here is for the private collection, not public
sharing, design selection or a change to publication restrictions.

Adoption checks the SSD UUID, anchored paths, original plan identity, source
Drive fingerprints and unchanged curation. It uses journal-owned temporary
files and no-replace promotion; unowned existing destinations are preserved
and refused. Resume only the same review digest with unchanged evidence.
After adoption, run `plan` to produce a new immutable whole-collection plan.
Neither review nor adoption edits the old plan or authorizes retirement.

At warning memory pressure, inspection is limited to the four named root and
`webp/` copies of these documents, at most 64 KiB each, after checking the SSD
UUID and non-symlink paths. Critical pressure refuses even these reads. Corpus
hashing, decoding, builds and mounted writes require normal headroom.
The operator explicitly waived this memory-pressure gate for the current GFTB
tranche on 2026-09-10 (TIN-4000 comment
`d96798ee-85f5-40e7-a83b-0781da41a6cd`). That task-specific exception does not
waive identity, content, curation, upload-safety or exact retirement approval.

Drive and SSD provenance copies must agree by bytes. A different copy blocks
publication for an authority decision; modification time does not select a
winner. The canonical documents remain byte-for-byte unchanged under `webp/`.
Only their two named duplicate root files join the approved retirement scope;
unrelated root files remain untouched.

New private plans retain `alias_bindings`: original and accepted-WebP aliases
join through original SHA256 values and canonical entry keys. Drive aliases
retain their pre-rename path and object ID, including aliases later moved to
quarantine. Superseded different-byte outputs and unknown scratch files are
not labeled accepted aliases. Generated derivatives remain recipe-bound until
their prepared and verified journal records establish the output hash.

The private `verified` receipt includes the binding-map digest alongside the
canonical IDs, paths and hashes. That keeps a non-keeper selection or
restriction reference traceable after retirement without changing the sidecar
bytes. A bare filename or stem returns every matching original, including
nested names; it never silently prefers the root-level file. This is lineage
evidence. New plans also preserve the parsed selection/restriction syntax;
older manifests remain resumable but do not acquire curation or catalog proof
retroactively.

`apply` promotes and independently verifies all media before preparing
`webp/gftb-catalog.json`. It then writes this deterministic catalog through a
second UUID-staging and same-ID-promotion stage under the same manifest. The
catalog links relative paths, canonical Drive IDs/hashes, original aliases and
all curation records. It preserves selection and restriction independently;
absence of a restriction is not publication consent. Its recipe and prepared
hash are journal-bound. Timestamps, retirement status and its own ID/hash are
excluded. Existing unrecognized catalogs block publication instead of being
overwritten. A same-manifest resume must reproduce the exact prepared bytes.

Publication is serial and mount-guarded. Write bytes into journal-owned UUID
staging names through the mount, verify their exact Drive IDs and hashes after
uploads drain, then promote those same IDs through the Drive API. Verify the
promoted ID, parent, name and hash before counting publication as complete.
The operator ratified this explicit API exception on 2026-09-08.

Apple's [NFS client](https://github.com/apple-oss-distributions/NFS/blob/main/kext/nfs_vnops.c)
does not implement the exclusive rename this workflow would otherwise need.
An `O_EXCL` create is not an adequate fallback on this
[go-nfs backend](https://github.com/willscott/go-nfs/blob/v0.0.4/nfs_oncreate.go)
either. Never substitute a pathname existence check plus
ordinary rename. Staging and ID promotion preserve existing objects, but
Google Drive permits concurrent same-name objects: detect that ambiguity and
stop rather than claiming globally atomic filename exclusion. Keep one photo
writer and do not edit the target folders concurrently.

Changed sources, mount replacement, symlink escapes and ambiguous errors stop
progress. Staging names and the journal support reconciliation: never sweep
them with `find -delete`. Resume the same digest only while its inputs and
encoder recipe still match. The immutable plan binds source bytes and the
recipe; a generated output's hash is fixed in its preparation journal before
publication and checked again before retirement approval is acted upon.

`verify` checks canonical objects directly on Drive, independently of the
mount cache. Mounted-write success means the VFS accepted bytes, not that
Drive received them. The upload queue must drain and every canonical ID and
content hash must match. Outstanding uploads produce a refusal, not a polling
loop; resume verification after resolving the cause.

`retire` is recoverable quarantine, **not purge or disk-space reclamation**.
Canonical Drive IDs and unique unverified inputs must remain. Quarantined
files are excluded from future source inventories but still occupy space.
Report that distinction: one active SSOT does not mean recovery copies were
permanently erased. Unresolved lineage means incomplete retirement, not
permission to discard unmatched files. No command purges recovery copies.

After successful retirement, rerunning `apply` on the same digest checks the
mount and canonical Drive IDs/hashes without reopening quarantined inputs.
It records `apply-noop`; changed or substituted canonical objects still fail.

## Acceptance and tests

Demonstrate useful mounted reads and writes, independent Drive verification,
and a no-op rerun. Record the active closure/settings, resource preflight,
actual original/derivative counts, unresolved entries and retirement state.
A quiet watchdog log alone cannot prove health between samples; equal file
counts cannot prove equal bytes. Combine per-operation identity guards,
content evidence and the error journal.

Stop on EIO, ESTALE, auth failures or ambiguous publication. Reconcile pending
uploads before restarting the mount or rolling back. A whole-Drive recursive
index is not a prerequisite; keep index non-overlap and use the bounded photo
manifest for this acceptance workload.

`just test-photos` runs synthetic fixtures only. It is registered in
`just check`, Bazel `photos_test` / `ci_validation_suite`, and the Nix
`photos` check. The Nix and Bazel gates require the actual Pillow/WebP/HEIF
runtime with `GDM_PHOTOS_REQUIRE_CODECS=1`; missing codecs fail those gates.
The tiny real HEIF-to-WebP fixture checks orientation, metadata removal and
unchanged original bytes. Ambient codec skips are not production acceptance.
Run heavy checks on an approved builder, not a pressured Neo.
Record one sanitized, indexed `docs/agent-notes/` receipt and link separate
mount/photo evidence to TIN-3883 and TIN-4000. Raw inventories stay private.
