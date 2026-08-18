# Runbook — gdrive-mounts (v0.1.0 skeleton; P2.D fills evidence)

## Mounts

- macOS (neo): launchd agents `dev.tinyland.gdrive-mounts.<org>-<mount>`,
  backend `rclone nfsmount` (native NFS loopback — no kext, no brew).
- Linux (sting): systemd --user services `gdrive-mounts-<org>-<mount>`,
  backend `rclone mount` (FUSE3).
- Mount root: `~/GDrive/<org>/<mount>` (e.g. `~/GDrive/sulliwood/gftb-stuff`).
- Logs: `~/Library/Logs/tinyland/gdrive-mounts.*.log` (macOS),
  `journalctl --user -u gdrive-mounts-*` (Linux).

## Lifecycle commands

```console
nix develop --command just render   # materialize runtime rclone.conf (0600)
nix develop --command just index    # refresh metadata index now
launchctl kickstart -k gui/$(id -u)/dev.tinyland.gdrive-mounts.sulliwood-gftb-stuff
umount ~/GDrive/sulliwood/gftb-stuff   # stop an nfsmount cleanly
```

## Index (the agent/AX query surface)

Spotlight does not index these mounts. Query the sqlite rollup instead:

```console
sqlite3 ~/.local/state/gdrive-index/index.sqlite \
  "SELECT path, size FROM files WHERE org='sulliwood' AND path LIKE '%.pdf' LIMIT 20;"
```

Freshness SLO: 24h (timer default 6h). Raw per-org JSON at
`~/.local/state/gdrive-index/<org>.json`.

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| agent flaps, err log `invalid_grant` | expired/revoked refresh token | re-mint token (docs/sops-integration.md), update sops leaf, kickstart |
| `Device not configured` on mountpoint | stale NFS handle after crash | `umount -f` the point; kickstart agent |
| slow first `ls` of a big dir | cold VFS dir cache | expected once; `--dir-cache-time 720h` keeps it warm; use the index for search |
| 403 rateLimit errors | per-org quota | own client_id per org isolates this; back off index interval |
| render-config exits "missing unreadable secret" | sops leaf not seeded/passed | lab: seed leaf, wire `secrets.<org>` in wrapper, re-switch |

## odrive coexistence

odrive remains installed/paid during transition. Do not let both stacks
write-sync the same Drive folders; our mounts are read-only until per-org
promotion, which is the coordination point.

## Pending evidence (Wave 2/3)

- [ ] mount-bakeoff table (nfsmount vs FUSE-T on macOS 26.6.2 aarch64)
- [ ] acceptance matrix receipts (reboot persistence, rw roundtrip, umount clean)
- [ ] GFTB Stuff rw promotion receipt (operator gate)
