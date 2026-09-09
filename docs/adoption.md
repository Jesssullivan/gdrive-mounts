# Adoption — the lace-up runbook

The one path from zero to a live, mounted org. Every step is either an
operator act (a browser, a `just` command run by a human) or an agent act —
everything except OAuth consent and the GCP credential-download click, which
stay operator-only (`AGENTS.md` invariant 3). No step pastes a secret value
into chat.

## Prerequisites

- The cache volume is mounted (`/Volumes/TinylandSSD` on neo). Absent at
  mount time is a runbook failure mode, not an adoption blocker — see
  `docs/runbook.md`.
- The required tools are available from the pinned devShell. Neo is an
  8 GiB, remote-first host: do not use an unbounded local-build override
  to obtain them. Realize missing dependencies on an approved builder;
  respect lab's resource and storage preflight before an attended activation.
- The org exists in `orgs.json` with `enabled: true`. Adding a new org later
  is a data change only — see "Per-org growth" below.

## 0. GCP Desktop OAuth client (operator, browser)

In the target org's GCP console: enable the Drive API, then create an OAuth
client of type **Desktop app**. Download the JSON. This step is an operator
browser act end to end — an agent may navigate up to the download button but
never clicks it, reads the downloaded file, or screenshots the credential
modal.

## 1. `just import-client <org> <path-to-downloaded.json>`

Copies the downloaded client JSON verbatim into the stage directory
(`0600` file, `0700` parent), wipes the Downloads copy, and prints the
destination path and a fingerprint — never the client secret. File shape:
`docs/sops-integration.md`.

## 2. `just mint-token <org>`

Mints a scope-carrying OAuth token via the two-step rclone blob flow — never
the bare `rclone authorize "drive" <id> <secret>` form, which mints an
unscoped full-drive token regardless of intent. The recipe prints a consent
URL on stderr; the operator (or a `claude-in-chrome` lane that stops before
the credential becomes visible) opens it and consents in the browser as the
org's Drive account. Nothing is pasted back — the token lands directly in
the stage directory. Token shape and rotation: `docs/sops-integration.md`.

## 3. `just seed-lab <org>`

Encrypts both stage files into `lab`'s sops tree and prints the exact nix
snippet to paste into the lab wrapper. Requires a `.sops.yaml` creation rule
for the `gdrive-mounts` JSON tree to already exist in `lab` — if it doesn't,
the recipe fails and prints the rule to add first. Never runs `git add` or
commits in `lab`.

## 4. Lab wrapper PR (in `lab`, not this repo)

Adds this repo as a flake input, a `nix/home-manager/gdrive-mounts.nix`
wrapper module (imports this repo's `homeManagerModules.default`, declares
the two sops secrets per org from step 3's snippet, enables the org), a
registration in `nix/home-manager/default.nix`, a host enable, and an eval
test fixture. This repo does not restate the wrapper's internal shape — it
is `lab`'s to own.

## 5. Direct local Home Manager delivery (attended)

For the Neo-first reliability candidate, run from the reviewed **lab** tree:

```sh
NIX_SWITCH_LOCAL_BUILD_REASON="TIN-3883 Neo Sulliwood stability candidate" \
  just nix-switch-local 'jess@macbook-neo'
```

This is an attended operator-authorized action, not background enrollment.
Follow lab's `docs/operations/LOCAL_CONSOLIDATION_DELIVERY.md`: record the
source revision and dirty state, compare the candidate with the deployed
source, and verify the active generation/closure after activation. Stop on
unrelated changes, unsafe memory/storage pressure, or unknown provenance.
The normal main-source `just nix-switch` lane still exists, but a configured
daily switch is neither a dependency nor proof of delivery.

Changing a mount pin is not a new OAuth ceremony. If the current Sulliwood
credentials already carry the ratified scope, preserve them.

## 6. `just doctor` / `just smoke <org>`

Confirms the mount is live and lists the org's Drive root — for sulliwood,
including "GFTB Stuff". A failing section: `docs/runbook.md`.

## What `just doctor` shows at each step

| After step | Secrets section | Rendered conf | Mount section | Index section |
|---|---|---|---|---|
| 0 — GCP client only | missing | missing | absent | absent |
| 1 — import-client | client present, token missing | missing | absent | absent |
| 2 — mint-token | both present, stage only | missing | absent | absent |
| 3 — seed-lab | both present, encrypted in `lab` | missing (`lab` not switched) | absent | absent |
| 4 — lab PR merged, not switched | same as step 3 | missing | absent | absent |
| 5 — switched | resolved via sops paths | rendered, `0600` | running | absent until first index run |
| 6 — `just index` or timer fires | — | — | running | fresh, within SLO |

## Existing-host rollout, not new enrollment

Source snapshot, 2026-09-08; this table is **not** a live deployment receipt:

| Host | Declared mount posture | Admission before a direct local switch |
|---|---|---|
| neo | Sulliwood enabled; external cache, 20G, low-memory profile | Exact SSD identity, memory pressure, queued uploads, minimal candidate versus active source |
| PZM | Sulliwood enabled; external cache, 40G | Fresh external-volume identity/capacity and host storage gates; do not reuse old receipts |
| Yoga | Sulliwood enabled; user cache, 5G, FUSE | Fresh disk headroom, `/dev/fuse`, `fusermount3`, delivered secret pair |
| mbp-13 | Mount disabled; secret delivery disabled | Remains disabled pending reviewed enrollment; a shared age-key alias is not host enablement |
| sting | No enabled mount in the inspected host wiring | Remains client-only; no durable service added here |

Qualify Neo first, complete its GFTB workload proof, then qualify PZM and
Yoga independently. Run the same reviewed source through each host's local
`just nix-switch-local` with the host's actual Home Manager attribute from
lab inventory. Check both the active generation and effective mount settings
afterwards. Do not infer a running mount from a source enable flag, successful
decryption, an old acceptance comment, or a configured daily-switch flag.

Keep the existing org scope. This rollout neither promotes another org nor
rotates credentials. Softconnect observation/admission is adjacent work,
not authority to restart mounts, stop applications or reboot hosts.

Before step 5 doctor reports the `orgs.json` defaults; from step 5 on it reads
`<stateDir>/effective-settings.json` and reports this host's resolved settings,
including any wrapper override — the `[config] settings` row names which.

## Per-org growth

Adding an org after the first is a data-and-secrets change only: one
`orgs.json` entry (schema: `config/orgs.schema.json`), a client/token pair
through steps 0–3, and one `lab` wrapper edit enabling that org. No script
or module change is required.
