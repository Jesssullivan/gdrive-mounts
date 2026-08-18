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
- `nix develop` works in this repo (`.envrc` wires direnv).
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

## 5. `just nix-switch macbook-neo` (operator, attended)

The only switch path. Never run by an agent.

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

## Per-org growth

Adding an org after the first is a data-and-secrets change only: one
`orgs.json` entry (schema: `config/orgs.schema.json`), a client/token pair
through steps 0–3, and one `lab` wrapper edit enabling that org. No script
or module change is required.
