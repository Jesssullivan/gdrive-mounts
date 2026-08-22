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

`.sops.yaml` needs a creation rule matching this tree before `just seed-lab`
can encrypt into it — the existing catch-all only matches `.yaml$`:

```yaml
- path_regex: nix/secrets/gdrive-mounts/.*\.json$
  key_groups:
    - age:
        - *jsullivan2_macbook_neo   # the two mount clients only
        - *jess_sting
```

(`lab` branch `feat/gdrive-mounts-wrapper` carries this rule; `just seed-lab`
prints the stanza when it is missing.)

Declared in `lab:nix/secrets/default.nix`, presence-gated so an unseeded org
degrades instead of blocking activation:

```nix
sops.secrets."gdrive-mounts/<org>/client" = lib.optionalAttrs
  (builtins.pathExists ./gdrive-mounts/<org>/client.json) {
    sopsFile = ./gdrive-mounts/<org>/client.json;
    format = "json";
    key = "";
    path = "${config.xdg.stateHome}/gdrive-mounts/secrets/<org>/client.json";
  };
sops.secrets."gdrive-mounts/<org>/token" = lib.optionalAttrs
  (builtins.pathExists ./gdrive-mounts/<org>/refresh.json) {
    sopsFile = ./gdrive-mounts/<org>/refresh.json;
    format = "json";
    key = "";
    path = "${config.xdg.stateHome}/gdrive-mounts/secrets/<org>/token.json";
  };
```

The lab wrapper module passes the resolved paths into this repo's
home-manager module:

```nix
programs.gdrive-mounts.secrets.<org> = {
  clientFile = config.sops.secrets."gdrive-mounts/<org>/client".path;
  tokenFile = config.sops.secrets."gdrive-mounts/<org>/token".path;
};
```

`just seed-lab <org>` prints this exact snippet with `<org>` filled in —
copy it, don't retype it.

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
