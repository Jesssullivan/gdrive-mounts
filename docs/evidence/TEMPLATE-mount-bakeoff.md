# Mount bakeoff — TEMPLATE

Copy to `docs/evidence/YYYY-MM-DD-mount-bakeoff.md` and fill in. One row per
backend actually tried. Leave a later backend's row as "not tried" once an
earlier backend passes and the operator doesn't ask for the next one.

## Environment

| Field | Value |
|---|---|
| Host | |
| OS / arch | |
| rclone version | |
| Backend package + version | |
| Org / remote used (scratch only — never GFTB Stuff) | |
| Scope | |
| Date/time | |

## Criteria

| Backend | Mount success | Finder visibility | Big-dir listing latency ("GFTB Stuff") | `umount` clean | launchd restart survival |
|---|---|---|---|---|---|
| `rclone nfsmount` | | | | | |
| FUSE-T + `rclone mount` | | | | | |
| macFUSE + `rclone mount` (only if both above fail) | | | | | |

Commands run and their raw output go below, one subsection per backend —
verbatim, not summarized (`docs/evidence/README.md`).

## Verdict

One sentence: which backend is ratified as `mountBackendDarwin`, and why.
