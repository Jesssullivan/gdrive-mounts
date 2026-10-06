# Vendored schemas

| File | Source of truth | Pinned from | Why vendored |
|---|---|---|---|
| `tinyland-repo-manifest.schema.json` | `tinyland-inc/site.scaffold` `docs/schemas/tinyland-repo-manifest.schema.json` | site.scaffold `28934488bcc4` (2026-08-18) | site.scaffold is private; CI cannot fetch the `$schema` URL. `scripts/repo-manifest-validate.sh` prefers a sibling checkout, then this copy, then the network. |

| `lanes.schema.json` | `xoxd-ai/ci-templates` `schemas/lanes.schema.json` | ci-templates `ae836d8400d5` (v5.1.1) — the same rev `.github/workflows/ci.yml` pins; byte-identical to the v5.1.0 copy | `scripts/workflow-validate.sh` checks `.github/lanes.json` offline, against the exact schema the pinned v4 workflow was released with. SHA-256 `4fef58645b8cd367a4336a66eaee629388c8a949a06d85becc97cfc1be82e3b8`. |

Refresh: copy the file from a current site.scaffold checkout and update the pin here. Do not edit the copy.
