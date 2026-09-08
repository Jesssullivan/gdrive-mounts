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

`apply` adopts accepted WebP bytes before considering conversion. The latest
orientation judgments were relative to QuickLook intermediates; their angles
must not be replayed blindly against newly EXIF-transposed HEICs. Preserve
original metadata, videos, sidecars, design selections and do-not-publish
restrictions. This workflow does not change sharing or publish a website.

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
`photos` check. Run heavy checks on an approved builder, not a pressured Neo.
Record one sanitized, indexed `docs/agent-notes/` receipt and link separate
mount/photo evidence to TIN-3883 and TIN-4000. Raw inventories stay private.
