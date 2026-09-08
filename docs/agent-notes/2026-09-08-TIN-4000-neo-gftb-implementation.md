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

## Follow-through — September 8, 14:25–14:50 EDT

The operator requested completion. The source and validation work advanced;
live activation and photo mutation remain unperformed.

### Source and real codec verification

Signed photo commit `158fe2f0ac4fa26ec8554717220e2fc61f3fd797` adds a real
32×16 HEIF-to-WebP conversion fixture and a strict codec dependency gate.
Nix and Bazel set `GDM_PHOTOS_REQUIRE_CODECS=1`: absent codecs cannot silently
pass as skips. The fixture verifies rotation, metadata removal and preservation
of original bytes. Ambient tests ran 76 cases with two codec skips; the focused
strict dependency check correctly failed without Pillow.

The declared Honey builder passed `just nix-remote-builder-verify --live --strict`.
`just nix-guard-verify --expect-max-jobs 0 --expect-fallback false` confirmed
that Neo could not compile locally. Honey had 185949 MiB available memory and
199G available under `/nix` at the bounded admission snapshot.

The online Linux photo-check dry-run needed two small derivations and 53 cached
paths (38.3 MiB download). The build ran through the existing Honey SSH builder.
Its 90-second local client deadline expired after the remote check started;
that exit 124 is not a successful local build receipt. Reading the retained
remote log for
`/nix/store/c0qz0d5hrpwbsma8dnfcf1pmdh2a4ajn-gdrive-mounts-photo-contracts.drv`
independently established **76 tests passed, no skips**, in 1.211 seconds.
The local check output import was not verified. This is Linux codec proof,
not realization of the missing Darwin photo runtime.

`just hm-eval-build` also completed successfully:
`/nix/store/c297zv68av7v7g0fd4iz6dw79xcyllqd-gdrive-mounts-hm-eval`.
That run used the source checkout with the photo-runbook documentation edit
uncommitted; implementation and tests matched the signed codec commit.
No full lab activation closure or whole Bazel CI result is claimed.

### Consumer publication needs an owner decision

PR #1691's note-summary lint was corrected and passed. The signed PR branch
is now `32c05ad4cb729a9848712313ab0679672cab80d9`; the signed minimal local
delivery branch is `0282fb4056b911bc335dddcdbda671209b056c94`.
Its private-input cache entry still names the old mount revision. The 24
wrapper assertions do not cover that materializer prerequisite.

The registered exact-head publisher was dispatched once. It failed at its
source-authority admission, not during archive creation:

> nix-private-input: Jesssullivan/gdrive-mounts main must remain an exact protected branch

[Publisher run 34263562578](https://github.com/tinyland-inc/lab/actions/runs/34263562578)
exited 2. A direct GitHub read confirmed `main` at
`2cd5b8779adb3767937341ab2c20b352a1348152` has `protected: false` and the
repository ruleset list is empty. The source repository is private.
No archive or signed cache-registry commit was produced. PR #1691 is draft,
as the publisher's admission requires. Neither branch is delivery-ready until
the owner resolves protection, publication succeeds, and the exact governed
registry commit is carried onto the minimal branch. No protection, visibility,
publisher policy or credentials were changed to bypass this gate.

### Fresh native admission evidence

TinylandSSD's native UUID is
`E3131481-8D53-4688-B20A-E1F71DC0C61E`, matching current lab host declarations;
it had 2.8T available. The exact identity gap is closed, not the entire I/O
health gate. At 14:25, memory pressure remained level 2 with 5464.62 MiB swap.
Direct local HTTP requests to `vfs/stats` and `core/pid` timed out after 12
and 5 seconds respectively, with no response bytes. Queue state remains unknown.

The existing watchdog restarted PID 12832 at 18:26:54Z while its record said
`probe: ok` and NFS `not responding`. The replacement PID 60628 was later
observed in kernel-wait state `U`. No agent triggered this restart.

At 18:34:01Z, the bounded native sample showed 60% idle CPU and roughly
303 MB/s on the sole external physical disk. A preceding-15-minute kernel
transport-error scan found no matching fault events. These observations do
not establish CPU saturation, disk failure, or the cause of the kernel wait.
They do not authorize resets, repair, unmounting or process termination.

The temporary lab source worktree was removed. The primary lab checkout's
other-seat changes remain untouched. The owner protection decision and fresh
Neo queue/recovery evidence are the next gates; photo retirement still needs
its own exact-manifest approval after live verification.

## Follow-through — September 8, private main protection

The operator explicitly approved protecting `main` under this repository's
signed-commit/PR-only contract while retaining private visibility. Invariant 5
of `AGENTS.md` records the ruling.

At 19:00:05Z, GitHub created active repository ruleset
[`main-signed-pr-only` (22571993)](https://github.com/Jesssullivan/gdrive-mounts/rules/22571993),
targeting only `refs/heads/main`. It requires PRs and verified commit
signatures, blocks deletion and non-fast-forward updates, and has no bypass
actors. Independent reviewer approvals, status contexts and a merge queue
were not added. Existing merge, squash and rebase methods remain enabled.

Separate API readbacks verified the active rules, `current_user_can_bypass:
never`, `main.protected: true` at unchanged source
`2cd5b8779adb3767937341ab2c20b352a1348152`, and repository `private: true`.
No credentials, visibility, source commits or publisher admission policy were
changed. The previously observed source-protection blocker is resolved.

After rechecking lab PR #1691 remained open, draft and at exact head
`32c05ad4cb729a9848712313ab0679672cab80d9`, the registered publisher was
dispatched once:
[run 34266512240](https://github.com/tinyland-inc/lab/actions/runs/34266512240).
Dispatch succeeded; completion and publication were not inspected or awaited.
No cache-registry commit is claimed. Before using its result, verify the
GitHub-signed commit has that exact parent and changes only
`config/nix-private-input-cache.json`, then carry it to the signed minimal
delivery branch with provenance. That branch remains at
`0282fb4056b911bc335dddcdbda671209b056c94`, not pushed or activated.

The latest bounded Neo admission at 18:55:39Z still reported memory pressure
level 2, 6099.25 MiB swap use and about 63 MiB free RAM on the 8 GiB host.
TinylandSSD's exact UUID matched, its mount was writable, and container free
space was 2746544390144 bytes. The sole strict `vfs/stats` request timed out
after 12003 ms with zero response bytes. Upload drainage, errors and
out-of-space state remain unknown, not zero. Generation 25, mount PID 60628
and the old watchdog settings remained active.

No live photo operation, switch, remount, workload termination or new worktree
was performed. Fresh queue/recovery admission, a governed registry result,
candidate closure realization and comparison still precede the attended
direct-local switch. The documentation change passed `git diff --check` and
`just secrets-scan-dir`; no new runtime test result is claimed.

## Follow-through — September 8, switch readiness

**Source preparation is complete; switch dispatch is not ready.** The operator
can pass the exact candidate below to the parallel lab agent for closure
preparation and recovery planning, not activation.

One result read found publisher run 34266512240 **failed** its final PR-head
check (`candidate PR head changed`, exit 2), after creating registry commit
`0b276f8f1cfafa1419db235557e23bd2c8128454`. Independent API evidence verifies
its GitHub signature, sole parent `32c05ad4cb729a9848712313ab0679672cab80d9`,
and sole changed path `config/nix-private-input-cache.json`. Archive asset
551118294 and provenance asset 551118320 are uploaded; streamed bytes match
the hashes in that signed registry. Parsed provenance binds the intended
`82a42340be78e17da66944c6a5fa4db0613572ca` source and locked NAR hash, PR
1691, expected parent, publisher run and archive ID. The workflow remains
failed; these independently verified side effects are not a successful final
workflow receipt. No retry or policy relaxation was needed.

Lab's local `deploy/gdrive-neo-reliability-20260908` now points to signed
`478e9067ca578bbdfc986da0b7caecf0fa880627`, carrying the registry commit with
`cherry-pick -S -x` provenance. Its registry bytes match the publisher exactly.
Relative to the observed active source `3bdf581ad78664b1c11107ce3c25228d16964cbd`,
the cumulative diff contains seven paths: the prior six plus the registry.
Only the gdrive lock node changes; `nix/secrets`, `.sops.yaml` and `flake.nix`
remain identical. This local branch is not pushed or activated.

Candidate verification passed without builds: the existing private-input
`audit` binary returned `ok: true` for all four registered inputs; the existing
registered Home Manager wrapper evaluation rendered all 24 assertions `ok`;
agent-note lint and whitespace checks passed. None proves a realized Darwin
activation closure. The clean temporary worktree was removed; the local branch
remains available in `/Users/jess/git/lab`. Other seats' primary-lab edits
remain untouched.

At 19:24:51Z, Neo's memory pressure improved to level 1, CPU was 76% idle,
and swap use was 5069.94 MiB. TinylandSSD's exact expected UUID was present,
writable, with 2746442932224 bytes free. Generation 25 remained active with
old gdrive source `154aca2`, NFS `timeo=100`/`retrans=5`, and watchdog
threshold 2/floor 900. Mount PID 9507 was in kernel wait. The single strict
queue query timed out after 12007 ms with zero bytes: uploads, errors and
out-of-space state remain unknown.

A nonblocking socket-ownership check linked the RC socket to PID 9507.
That is not responsiveness. Rclone's pinned
[cache statistics](https://github.com/rclone/rclone/blob/v1.75.0/vfs/vfscache/cache.go#L140)
depend on live state; historical cleaner logs, free space and on-disk dirty
metadata do not establish a current complete drain verdict.

The parallel lab agent's next work is to realize and compare this exact
candidate without heavy local compilation, then reconcile pending uploads.
The existing `just nix-switch-local` enables one or two local build jobs;
do not use it as the first closure-build attempt on Neo. If read-only recovery
evidence remains incomplete, obtain explicit cache-preserving recovery
authorization before coordinating writers/watchdog and replacing the mount.
Preserve both VFS data and metadata and never run two processes against the
same cache; [rclone's recovery mechanism](https://rclone.org/commands/rclone_mount/#vfs-file-caching)
is not proof that this restart is already safe. No switch, signal, remount or
live photo operation occurred in this pass.

The operator also requested a separate completion notice for GFTB deduplication
and its authoritative absolute path. The intended Neo path is
`/Users/jess/GDrive/sulliwood/GFTB Stuff`; it is not yet a newly verified SSOT
receipt. Completion must report the actual mounted absolute path and canonical
Drive folder ID, content/curation verification, a no-op rerun, unresolved items,
and the result of exact-manifest-approved duplicate retirement. The local SSD
photo source and VFS cache must not be confused with that canonical location.
