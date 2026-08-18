# Vendored schemas

| File | Source of truth | Pinned from | Why vendored |
|---|---|---|---|
| `tinyland-repo-manifest.schema.json` | `tinyland-inc/site.scaffold` `docs/schemas/tinyland-repo-manifest.schema.json` | site.scaffold `28934488bcc4` (2026-08-18) | site.scaffold is private; CI cannot fetch the `$schema` URL. `scripts/repo-manifest-validate.sh` prefers a sibling checkout, then this copy, then the network. |

Refresh: copy the file from a current site.scaffold checkout and update the pin here. Do not edit the copy.
