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

CI enrollment authority: `Jesssullivan/jesssullivan-infra` PR #92
(`tofu/stacks/arc-runners/jesssullivan.tfvars`,
`extra_runner_sets.gdrive-mounts-nix`) adds the `tinyland-nix` runner set for
this repo. Applying that runner set is an operator `workflow_dispatch` act in
that repo, not an agent act. After the apply, the operator sets the repo
variable `GF_ENROLLED=true` (`gh variable set GF_ENROLLED --body true`) to
turn the `check (tinyland-nix)` job on, and makes it a required check once
one run is green. The lanes themselves live in `.github/workflows/ci.yml`
here; the hosted `public-source` lane is the required gate until then.

## sting posture

sting is a tainted honey-cluster compute node with ephemeral-scratch storage
(TIN-2455 SPOF context). It gets rendered systemd --user units and nothing
else durable — no state of record, no index database of record. See
`docs/runbook.md` for the Linux unit shape.
