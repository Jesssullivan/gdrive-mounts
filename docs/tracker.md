# Tracker — gdrive-mounts

Linear project:
<https://linear.app/tinyland/project/gdrive-mounts-multi-org-drive-mounts-odrive-rclone-bridge-4abf86c8bf77>

Filed 2026-08-18. The original drafts lived in `docs/linear-drafts/` and were
removed once filed; the issues below are the tracker surface.

| Linear ID | Title | State at filing |
|---|---|---|
| [TIN-3877](https://linear.app/tinyland/issue/TIN-3877) | repo scaffold (bazelisk + nix flake + just + gitleaks) — landed PR #1, hardened PR #2 | Done |
| [TIN-3878](https://linear.app/tinyland/issue/TIN-3878) | orgs.json registry + rclone.conf render + two-file sops contract | Done |
| [TIN-3879](https://linear.app/tinyland/issue/TIN-3879) | home-manager module (mounts + index timers) with eval fixture | Done |
| [TIN-3880](https://linear.app/tinyland/issue/TIN-3880) | mount-substrate bakeoff on neo — nfsmount ratified | Done |
| [TIN-3881](https://linear.app/tinyland/issue/TIN-3881) | neo bring-up via lab wrapper (attended switch) — sulliwood read-only | In Progress |
| [TIN-3882](https://linear.app/tinyland/issue/TIN-3882) | sting lane (client-only) | Backlog |
| [TIN-3883](https://linear.app/tinyland/issue/TIN-3883) | "GFTB Stuff" rw promotion (explicit operator gate) | Todo |
| [TIN-3884](https://linear.app/tinyland/issue/TIN-3884) | odrive drawdown decision packet | Backlog |
| [TIN-3885](https://linear.app/tinyland/issue/TIN-3885) | GloriousFlywheel enrollment — reframed 2026-09-05: the v3 `GF_ENROLLED` / `tinyland-nix` plan is retired; what is open is a v4 owner organization + its owner ceremony (`docs/position.md`) | Todo |

Session receipts: finances `docs/agent-notes/2026-08-17-gdrive-mounts-repo-work-plan.md`.

## Neo reliability and GFTB acceptance (2026-09-08)

The table above records **filing-time** states, not current deployment truth.
Keep these acceptance lanes separate:

- [TIN-3883](https://linear.app/tinyland/issue/TIN-3883): the ratified
  Sulliwood write scope and current Neo mount reliability. The scope was
  already promoted; a mount pin update is not another OAuth ceremony.
- [TIN-4000](https://linear.app/tinyland/issue/TIN-4000): the usable photo
  workflow, original preservation, accepted WebPs, content verification and
  explicit retirement evidence. Both issues were still In Progress when
  inspected on 2026-09-08.

TIN-4000 has an August 24 historical soak PASS. It does not certify the
September low-memory profile or the new photo workload. Likewise, the SSD's
August 30 upload receipt predates its August 31 orientation corrections.
Neither a documentation merge nor equal file counts complete today's work.

Runtime fixes already merged in [gdrive-mounts PR #18](https://github.com/Jesssullivan/gdrive-mounts/pull/18);
the consumer integration lane is [lab PR #1691](https://github.com/tinyland-inc/lab/pull/1691).
Use a verified attended local switch, followed by a new acceptance receipt.

The [September 8 implementation receipt](agent-notes/2026-09-08-TIN-4000-neo-gftb-implementation.md)
records the source changes, preserved local delivery candidate, failed Neo
admission and remaining live acceptance. It is not a completed consolidation.
