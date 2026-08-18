# Linear issue drafts — gdrive-mounts

**Superseded by `docs/tracker.md` once these are filed** — that file carries
the draft → Linear-ID map; this file stops changing once filing starts.

**Status**: UNFILED. The pi `tracker-linear`/`recon` seat is read-only
(30 read tools, 0 writes; verified twice 2026-08-17). File when a
write-capable seat exists, or the operator files by pasting. Team: **Tinyland**.
Suggested label: `infra` (or create `gdrive-mounts`).

---

## Issue 1 — gdrive-mounts: repo scaffold (bazelisk + nix flake + just + gitleaks)

New private repo `Jesssullivan/gdrive-mounts` per house pattern (oauth-mux
secrets/endpoint doctrine, scheduling-kit bazelisk discipline, linear-gsuite
HM module shape). Sole justfile entrypoint; nix flake is the dependency
authority (rclone from nixpkgs, no brew); gitleaks from commit zero;
bazelisk candidate graph, endpoint-free. DoD: `nix develop --command just
check` green locally + in CI (ubuntu-latest + install-nix-action).

## Issue 2 — orgs.json registry + rclone.conf render + sops contract

Non-secret `orgs.json` (3 orgs: sulliwood enabled, xoxd/lmux disabled) driving
a render of the runtime rclone.conf; secrets arrive as file paths from lab
sops-nix (`GDM_<ORG>_*_FILE`). Validation: schema checks + dummy-secret render
+ rclone parse + secret tripwire. DoD: `just validate` green;
`docs/sops-integration.md` ratified by operator.

## Issue 3 — home-manager module: mounts + index timers

`programs.gdrive-mounts`: per-mount launchd agents (macOS, nfsmount) and
systemd --user services (Linux), presence-gated on consumer-provided secret
paths; index timer (lsjson → sqlite) with 24h freshness SLO. Module pure —
no secrets, no consumer paths. DoD: lab wrapper evals; agents render; evidence
in `docs/evidence/`.

## Issue 4 — mount-substrate bakeoff on neo (operator-gated, live)

Empirical comparison on macOS 26.6.2 aarch64: `rclone nfsmount` vs FUSE-T
1.2.7 (+ macFUSE 5.3.3 only if both fail). Criteria: mount success, Finder
visibility, rw roundtrip, big-dir latency, clean umount, launchd restart
survival. Scratch remote only. DoD: evidence table committed; substrate
ratified.

## Issue 5 — neo bring-up via lab (attended switch)

Lab wrapper module + flake input + presence-gated sops leaves; operator seeds
sulliwood secrets and runs `just nix-switch macbook-neo`. DoD: agents loaded,
`~/GDrive/sulliwood/gftb-stuff` browsable in Finder, cache on TinylandSSD
capped 100G, index timer green.

## Issue 6 — sting lane (client-only)

Rendered systemd --user units delivered over tailscale-ssh; rclone from
nixpkgs or static binary per distro reality. DoD: services active, org roots
listable, index timer ran. Constraint: sting stays client-only (tainted
compute node; TIN-2455 SPOF context).

## Issue 7 — GFTB Stuff rw promotion (explicit operator gate)

Flip sulliwood scope `drive.readonly` → `drive` in orgs.json + re-consent
(operator browser act) + rw smoke (create/rename/delete a scratch file in
"GFTB Stuff" only). DoD: roundtrip receipt + 2h soak with clean logs.
xoxd.ai / lmux.ai enablement rides this gate's precedent.

## Issue 8 — odrive drawdown decision packet

Inventory remaining odrive usage; parity checklist against the rclone stack;
cancel/hold recommendation. Operator decides; odrive remains paid meanwhile.
Context: odrive pivoted to Procore/construction; free tier died 2026-03-31;
no Apple Silicon app as of 2026-07.
