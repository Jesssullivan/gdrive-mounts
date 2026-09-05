#!/usr/bin/env bash
# secrets-scan-history.sh — gitleaks over the COMMIT HISTORY, fail-closed.
# The working-tree scan (secrets-scan-dir.sh) passes for a secret that was
# committed and then deleted; this is the lane that does not.
#
# History is not a Bazel input. Under `bazel test` each runfile is a symlink
# into the source checkout, so the physical path of a tracked file names the
# repository this scan must read. When no git repository is reachable the
# scan REFUSES rather than reporting a pass it did not earn — a history gate
# that silently degrades to "nothing to scan" is worse than no gate.
set -euo pipefail

d="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
for _c in "$d/lib/common.sh" "$d/scripts/lib/common.sh" "$d/../libexec/gdrive-mounts/common.sh"; do
  # in-repo | bazel runfiles root (entrypoint renamed to the target name) | installed
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
type no_trace_guard >/dev/null 2>&1 || { echo "gdrive-mounts: common.sh not found next to $0" >&2; exit 70; }
no_trace_guard

anchor="$(repo_root)/.gitleaks.toml"
[ -e "$anchor" ] || die "no .gitleaks.toml next to $(repo_root) — cannot locate the source checkout"
src_dir="$(cd "$(dirname "$(resolve_link "$anchor")")" && pwd)"

root="$(git -C "$src_dir" rev-parse --show-toplevel 2>/dev/null || true)"
[ -n "$root" ] ||
  die "no git repository reachable from $src_dir — a history scan cannot be faked; refusing to pass"

cd "$root"

if ! command -v gitleaks >/dev/null 2>&1; then
  if command -v nix >/dev/null 2>&1 && [ "${_GDM_REEXEC:-0}" != "1" ]; then
    exec env _GDM_REEXEC=1 nix develop --command bash scripts/secrets-scan-history.sh
  fi
  die "neither gitleaks nor nix is available; refusing to pass a scan that did not run"
fi

# A shallow clone has no history to scan. Say so instead of passing.
if [ "$(git -C "$root" rev-parse --is-shallow-repository 2>/dev/null || echo unknown)" = "true" ]; then
  die "$root is a shallow clone — fetch full history before this gate can mean anything"
fi

gitleaks git "$root" --config "$root/.gitleaks.toml" --redact --exit-code 1
info "OK ($(git -C "$root" rev-list --count HEAD) commits scanned)"
