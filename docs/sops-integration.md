# sops+age integration contract

**Rule zero: gdrive-mounts (this repo) holds no secret material.** The sops
store lives in `lab` (the fleet/secrets repo), per house boundary doctrine.

## What needs seeding, per org

Each enabled org in `orgs.json` carries `secretEnvPrefix` (e.g. `GDM_SULLIWOOD`).
At activation, the runtime rclone.conf is rendered from three files per org:

| Material | Env var (path) | Default fallback path |
|---|---|---|
| OAuth client id | `<PREFIX>_CLIENT_ID_FILE` | `$GDRIVE_MOUNTS_SECRET_DIR/<org>/client_id` |
| OAuth client secret | `<PREFIX>_CLIENT_SECRET_FILE` | `…/<org>/client_secret` |
| OAuth token JSON (refresh) | `<PREFIX>_TOKEN_FILE` | `…/<org>/token.json` |

## Lab side (consumer) shape

1. Create `lab:nix/secrets/gdrive-mounts/<org>.yaml` (sops, age recipients per
   `.sops.yaml`; add a `creation_rules` entry for the new tree, following the
   `gmailctl/` / `google-workspace/` precedent):

   ```yaml
   client_id: <gcp oauth desktop client id>
   client_secret: <gcp oauth client secret>
   token_json: |
     {"access_token":"…","token_type":"Bearer","refresh_token":"…","expiry":"…"}
   ```

2. Declare in `lab:nix/secrets/default.nix`, **presence-gated** (unseeded leaf
   must degrade, never fail activation — eap-tls-renew precedent):

   ```nix
   sops.secrets."gdrive-mounts/sulliwood/client_id" = { };
   sops.secrets."gdrive-mounts/sulliwood/client_secret" = { };
   sops.secrets."gdrive-mounts/sulliwood/token_json" = { };
   ```

3. The lab wrapper module passes paths into this repo's HM module:

   ```nix
   programs.gdrive-mounts.secrets.sulliwood = {
     clientIdFile = config.sops.secrets."gdrive-mounts/sulliwood/client_id".path;
     clientSecretFile = config.sops.secrets."gdrive-mounts/sulliwood/client_secret".path;
     tokenFile = config.sops.secrets."gdrive-mounts/sulliwood/token_json".path;
   };
   ```

## Minting the token (operator, per org)

1. GCP console for the org → OAuth **Desktop** client (rclone's shared
   client_id retires during 2026; per-org clients are required).
2. Consent is a browser act — the operator does it. Recommended mint lane:
   `rclone authorize` on any machine with the per-org client id/secret, scope
   `drive.readonly` first; move the resulting token JSON into the sops leaf
   (never through chat, never into this repo).
3. Scope promotion (`drive.readonly` → `drive`) = re-consent = explicit per-org
   operator gate. sulliwood first, for "GFTB Stuff" rw.

## Optional later: config encryption

rclone supports `--password-command`; a sops/age-backed command
(e.g. `sops exec-file`) can encrypt the rendered runtime rclone.conf at rest.
Deferred — plaintext runtime conf with 0600 perms under `$XDG_STATE_HOME` is
the ratified v1 (operator ruling 2026-08-17); VFS cache is likewise plaintext.

## Rotation

Token exposure in any transcript/artifact → rotate the GCP client secret +
re-mint the refresh token for that org, then update the sops leaf. Single-key
edits via `sops --extract` only; never broad `sops -d` dumps.
