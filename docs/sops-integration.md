# sops integration — gdrive-mounts

**This repo holds no secret material.** The sops store lives in `lab`, per
the boundary map in `AGENTS.md`.

## The two-file contract

Each enabled org needs exactly two whole-document JSON files, verbatim,
never retyped or reshaped:

| File | Contents | Produced by |
|---|---|---|
| `client.json` | The GCP OAuth Desktop client download, unmodified — `{"installed": {"client_id": …, "client_secret": …, …}}` (or `{"web": {…}}`) | GCP console, per org (`docs/adoption.md` step 0) |
| `token.json` | The `rclone authorize` output, unmodified | `just mint-token <org>` (`docs/adoption.md` step 2) |

Runtime env vars, per org (`secretEnvPrefix` from `orgs.json`, e.g.
`GDM_SULLIWOOD`): `<PREFIX>_CLIENT_FILE`, `<PREFIX>_TOKEN_FILE`. Fallback
directory when a prefix var is unset:
`$GDRIVE_MOUNTS_SECRET_DIR/<org>/{client.json,token.json}`. Dummy mode for
CI/local checks: `GDRIVE_MOUNTS_DUMMY_SECRETS=1` — never real material.

`render-config.sh` extracts `client_id`/`client_secret` with
`jq '.installed // .web'` and inlines the compacted `token.json`. Neither
file is ever hand-edited into a different shape between the operator's
download and the render.

## Lab side (consumer) shape

Tree: `lab:nix/secrets/gdrive-mounts/<org>/{client.json,refresh.json}` (the
token blob is named `refresh.json` in lab: its pre-commit guard refuses any
staged `*token*.json`; gmailctl precedent),
encrypted in place — whole-document JSON, not a YAML leaf per field (the
gmailctl precedent).

`lab:.sops.yaml` owns the creation rule and actual age-recipient set. Inspect
that reviewed rule and the encrypted leaves' recipient metadata before
enrolling a host. Do not copy a historical two-host recipient example: the
estate has changed, and several host aliases can refer to the same key.
Cryptographic ability to decrypt does **not** enable a mount.

`lab:nix/secrets/default.nix` applies two gates to each secret declaration:

1. This host enables both `tinyland.gdriveMounts.enable` and the org's
   `tinyland.gdriveMounts.orgs.<org>.enable`.
2. The corresponding encrypted file exists.

Both files must be declared before the lab wrapper wires an enabled org;
an incomplete pair is an evaluation failure. Presence alone must never
declare these secrets on unrelated hosts. The upstream module's warning for
missing paths is not a substitute for this consumer assertion.

The lab wrapper module passes the resolved paths into this repo's
home-manager module:

```nix
programs.gdrive-mounts.secrets.<org> = {
  clientFile = config.sops.secrets."gdrive-mounts/<org>/client".path;
  tokenFile = config.sops.secrets."gdrive-mounts/<org>/token".path;
};
```

The runtime paths remain `<stateDir>/secrets/<org>/{client,token}.json`.
Verify their resolved target permissions without printing their contents;
sops paths can be symlinks, so checking the symlink's mode is incorrect.

For deployment, use the attended direct local workflow in
[`adoption.md`](adoption.md#5-direct-local-home-manager-delivery-attended).
Verify source, generation, rendered scope, actual mount and a bounded
remote read separately. A pin update does not require re-minting credentials.

## Scope and promotion

Three scopes are allowed, and only one of them is a read scope:

| `scope` | Google grants | The mount |
|---|---|---|
| `drive.readonly` | read over the whole Drive | read-only (`--read-only`) |
| `drive` | full read/write over the whole Drive | read-write |
| `drive.file` | full read/write, **restricted to the files this client created** or that the user explicitly opened with it | read-write |

`drive.file` is a *narrow write* scope, not a read scope: it grants create,
read, update and delete, and only narrows which files they apply to. It is the
smallest blast radius Google offers for a mount that has to accept the
occasional write — an outbox that only ever sees its own output — so the mount
must not force it read-only. An absent `scope` defaults to `drive.readonly`,
and any value this repo does not recognise resolves read-only.

Scope lives in the token, not just in `orgs.json`. `orgs.json` `scope`
records intent; the token minted by `just mint-token` is what Google
actually granted. **Promotion is a re-mint, not a config edit**: flipping
`orgs.json` `scope: drive.readonly` to `drive` does nothing to a live mount
until `just mint-token <org>` runs again and the operator re-consents in the
browser. `just doctor` compares the two and warns on mismatch.

## Rotation

Any secret value that lands in a transcript, log, or artifact: rotate the
GCP client secret for that org and re-mint the token
(`docs/adoption.md` steps 0–2), then re-run `just seed-lab <org>`. Edit sops
leaves with a targeted re-encrypt, never a broad decrypt-and-dump.
