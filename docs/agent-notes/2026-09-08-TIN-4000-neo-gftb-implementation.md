# Neo reliability and GFTB implementation — 2026-09-08

Related to [TIN-3883](https://linear.app/tinyland/issue/TIN-3883) and
[TIN-4000](https://linear.app/tinyland/issue/TIN-4000).

Source implementation is available for review. **Live activation and photo
consolidation are not complete.** No live photos were inventoried, converted,
renamed, quarantined or deleted during this implementation pass. No credentials
were changed. Neo failed admission; PZM and Yoga remain downstream gates.

## Ratified scope

The 2-, 3- and 6-hour scopes are cumulative planning sizes, not deadlines:

1. Neo mount foundation: deliver the wedge fixes and low-memory integration
   through an attended direct local Home Manager switch, then verify the
   active generation and effective settings. Daily switches are not proven
   delivery and are not a prerequisite.
2. GFTB workload proof: reconcile only the Sulliwood mount's `GFTB Stuff`
   and `/Volumes/TinylandSSD/tinyland/gftb-photos`. Drive is the canonical
   active source for distinct originals and accepted WebPs. Preserve current
   corrected 2000px/quality-80 outputs, orientation evidence, design selections
   and do-not-publish restrictions. Exact manifest approval follows independent
   verification before recoverable retirement.
3. After Neo passes, qualify PZM and Yoga separately and deliver reviewed
   local candidates. Keep mbp-13 disabled and sting client-only. No softconnect
   enforcement, application termination, reboot, MDM or TCFS changes.

The operator explicitly approved mount-written UUID staging followed by
verified Drive-ID promotion on September 8. `AGENTS.md` invariant 7 is the
durable ruling. This does not promise atomic exclusion of competing Drive
names: one writer is required, and ambiguity causes refusal.

## Delivered source

The photo workflow adds `just photos plan|apply|verify|retire`, private
immutable manifests, a fsynced journal, ID-aware inventories, guarded serial
mounted I/O, direct Drive readback, same-ID metadata promotion and recoverable
quarantine. The photo runtime is optional and separate from normal Home
Manager mount installations. Tests are registered through `just test-photos`,
Bazel `photos_test` / `ci_validation_suite`, and the Nix `photos` check.

The supported [photo runbook](../agent-symbolic-manipulation.md) replaces
historical inline conversion examples. Enrollment and SOPS documentation now
distinguish host/org opt-in from encrypted-leaf presence, retain the existing
ratified write scope, and require actual local-switch provenance.

[lab PR #1691](https://github.com/tinyland-inc/lab/pull/1691) contains signed
commit `c6dd801ebfa81a3da3d4f6b09fa1e59fe30a0c71`. It advances the mount input
to `82a42340be78e17da66944c6a5fa4db0613572ca`
([gdrive PR #18](https://github.com/Jesssullivan/gdrive-mounts/pull/18)) and
changes the low-memory restart floor to `max 900 registryDefault`, preserving
the upstream 1800-second floor. Cache size, handles, buffers and concurrency
remain bounded by the existing low-memory profile. Auto-merge is not armed.

A narrower signed local delivery branch is preserved in lab:
`deploy/gdrive-neo-reliability-20260908`, at
`809291bf5a073509dddca49f63a3b1d3ea26958f`. It starts at the observed deployed
source `3bdf581ad78664b1c11107ce3c25228d16964cbd` and contains only the pin
and wrapper correction. Its cumulative source diff has six paths; no secret
leaves, `.sops.yaml`, `flake.nix` or unrelated input pins change. This branch
is **not pushed**. The temporary implementation worktree was removed; unrelated
primary-lab edits and other seats' worktrees were left alone.

The source comparison is not a realized closure comparison. Recreate a
reviewed delivery checkout only after fresh admission, compare the realized
candidate with the active generation, and use lab's attended
`just nix-switch-local 'jess@macbook-neo'` procedure.

## Admission evidence: hold

At 13:18 EDT, Neo reported load averages 315.32 / 259.98 / 146.51 and
6287 MiB of swap use on an 8 GiB host. The final resource check at 14:05 EDT
still reported pressure level 2, load 45.22 / 23.85 / 27.82 and
5622.75 MiB of swap use. Mount process identity changed across observations;
the final process was PID 12832 with last exit -15. These are observations,
not attribution to a particular application.

The observed active generation remained
`/nix/store/spv92312m7qv67vs0fd4qwb7w33ika93-home-manager-generation`.
The active low-memory settings included a 20G cache, 16M read chunks and
20000 handles, but old NFS `timeo=100`, `retrans=5`, watchdog threshold 2
and a 900-second restart floor. No switch was performed.

A 13:34 upload-queue request timed out waiting for response headers. The
final bounded request also returned no valid JSON evidence and failed its
strict parser. Neither result proves a drained queue. The earlier pipeline's
null fields must not be interpreted as zero uploads.

The external SSD showed ample free capacity, but its exact volume identity
was not freshly qualified. Capacity alone does not pass the storage gate.
No cache was cleared and no unrelated process was stopped to alter admission.

## Verification and remaining acceptance

Passed during this pass: schema/render/rclone validation, endpoint-free and
repository-manifest checks, workflow validation, shell syntax and ShellCheck,
Nix syntax parsing, shell-library assertions, and working-tree secret scanning.
The lab real configuration fixture passed all 24 registered mount-wrapper
checks for both the current-main PR candidate and the minimal delivery branch.

Synthetic photo tests exercise staging, ambiguous names, crash reconciliation,
source/ancestor substitution, upload evidence, codec recipe identity and
recoverable retirement. The ambient Python lacks Pillow: its real WebP
fixture skips. The PR records the final combined test result; this is not a
claim of pinned codec or live-photo acceptance.

Not run: full Home Manager builds, candidate closure realization, full
`just check` / Bazel CI, the pinned Pillow/HEIF runtime, any live photo phase,
or a Neo/PZM/Yoga activation. Do not close either Linear acceptance issue
from these source changes alone.

Resume only after operator resource relief and fresh storage/queue admission.
Then deliver and verify Neo, inventory the approved roots, review the private
plan, and run one phase at a time. Prove mounted reads/writes, independent
canonical Drive IDs and hashes, preserved curation, and a no-op rerun before
approving the exact retirement manifest. Unknown derivatives and incomplete
staging remain preserved and explicitly unresolved. Quarantine consumes
space; one active SSOT is not permanent deletion of recovery copies.
