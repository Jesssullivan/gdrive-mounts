# Position — gdrive-mounts

## The bridge

This repo is the interim bridge between odrive (retiring) and TCFS/tummycrypt
(the long-term successor — multi-year, an opendal Operator seam, no Google
Workspace target yet). It is not a competitor to tummycrypt. When tummycrypt
grows a Workspace target, this repo's mounts retire in its favor. Don't
carry two competing truths about which stack is "the" Drive integration —
this one is temporary by design.

## odrive coexistence

odrive stays installed and paid during the transition. Never let both stacks
write-sync the same Drive folder at once. gdrive-mounts mounts read-only
until an org's explicit promotion gate (`docs/sops-integration.md`) — that
promotion is the coordination point: settle odrive's fate for a folder
before flipping scope on it there. The odrive drawdown decision itself is
tracked in `docs/tracker.md`, not decided here.

## Extracted-repo doctrine

This repo is the source authority for the mount substrate: `orgs.json`, the
rclone template, the scripts, the nix module. `lab` (the fleet/secrets repo)
pins this repo as a flake input, wires it into a home-manager wrapper, and
validates the wired result in its own CI. `lab` never forks or duplicates
this repo's logic — a change to mount behavior lands here first, then `lab`
bumps the pinned input.

Dependency pin: rclone comes from nixpkgs (1.75.0 at time of writing) via
this repo's `flake.nix`. No brew delivery for any part of this stack.

## CI posture — GloriousFlywheel v4 only, and it does not run yet

CI here is one thing: the v4 action fabric. `.github/workflows/ci.yml` is a
thin caller into `tinyland-inc/ci-templates`
`.github/workflows/spoke-ci-v4.yml`, pinned at
`32e39ced0008edf4564ebeb173a5e8fbf069e28f` (v5.1.0). It submits exactly one
action — `check`, declared in `.github/lanes.json` as
`test //:ci_validation_suite`, capability `rbe-linux-x86_64`, result
`status-only`. Every gate this repo enforces is a registered Bazel target in
that suite; adding a gate means adding a target, not adding a job.

There is no hosted lane, no `ubuntu-latest` or `macos-latest` fallback, no
gate, no bridge, and no interim runner. That is a ruling (operator,
2026-09-05), not an implementation gap to be filled in later.

**The v4 check cannot pass today, and that is the ratified state.** The v4
dispatch edge is an owner-overlay runner scale set, and GloriousFlywheel law
admits organizations only:
`tofu/modules/arc-runner/main.tf:112` requires
`var.owner_overlay_github_account_kind == "organization"`, alongside the
`gf-v4-dispatch` plane id, label, and `arc-runners-<owner-slug>` namespace.
`Jesssullivan` is a personal user account, so it cannot be a v4 owner — no
`gf-v4-dispatch` runner answers for this repo, and the job stays queued or
fails at dispatch. The named gap is: **no v4 owner organization is chosen for
this repo, so no `gf-v4-dispatch` runner group exists.** It closes when Jess
picks an organization and that organization's owner ceremony lands, not
before, and nothing in this repo can close it.

Retired with the hosted lanes, so nobody re-derives them from history:

- The repo variable `GF_ENROLLED` and the `check (tinyland-nix)` job it gated.
  The variable is not set on the repo and the job no longer exists.
- `Jesssullivan/jesssullivan-infra` PR #92
  (`tofu/stacks/arc-runners/jesssullivan.tfvars`,
  `extra_runner_sets.gdrive-mounts-nix`). That PR delivered a **v3** runner
  set on the `tinyland-nix` label. v4 does not use it: the v4 fabric dispatches
  to `gf-v4-dispatch` and executes Bazel actions through the GF action client,
  never on a `tinyland-nix` runner. Applying PR #92 would not make this repo's
  CI run.
- The `tinyland-nix` execution pool claim in `tinyland.repo.json`. The
  manifest no longer carries an `enrollment` block, because this repo has no
  enrollment to declare.

Local verification is unchanged and is the operator's real gate until the
fabric can run: `nix develop --command just check` for the day-to-day pass,
`nix develop --command just check-ci` for the exact suite v4 would submit.

## sting posture

sting is a shared compute-expansion node with ephemeral-scratch storage
(TIN-2455 SPOF context). It gets rendered systemd --user units and nothing
else durable — no state of record, no index database of record. See
`docs/runbook.md` for the Linux unit shape.
