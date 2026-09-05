#!/usr/bin/env bash
# hm-eval-build.sh — BUILD the home-manager eval proof, not just evaluate it.
#
# `just hm-eval` stops at .drvPath: it proves the module evaluates and needs no
# build slot, which is what neo can do unattended. This lane goes further and
# realises the derivation, so layers 2 and 3 of nix/tests/hm-eval.nix (the
# generated-wrapper greps and the cache-guard behaviour harness) actually run.
# That is the work the retired hosted `darwin-eval` / `public-source` jobs did.
#
# x86_64-linux is pinned deliberately: one instance evaluates BOTH platform
# branches (see nix/tests/hm-eval.nix), and it is the architecture the v4
# ActionPlan asks for (rbe-linux-x86_64). On a darwin operator seat it
# resolves through the configured remote builder.
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

# Find flake.nix — `$d` under bazel (the runfiles root, where the entrypoint
# sits beside scripts/), `$d/..` from a checkout — then take its PHYSICAL path.
#
# Both steps are load-bearing. repo_root() is wrong here: under bazel it walks
# up out of the runfiles tree into the execroot, whose `.git` symlink names a
# worktree that does not contain these files. And a runfiles tree cannot itself
# be a flake root: nix resolves a flake's symlinks inside the flake, so a
# runfiles `flake.nix -> /Users/.../flake.nix` is read as the in-flake path
# `<root>/Users/.../flake.nix` and fails. Resolving to the real checkout is
# also what makes the source identity honest: the proof is of THIS tree.
root=""
for _cand in "$d" "$d/.."; do
  [ -e "$_cand/flake.nix" ] && { root="$_cand"; break; }
done
[ -n "$root" ] || die "no flake.nix beside $d — the eval proof has no source"
root="$(cd "$(dirname "$(resolve_link "$root/flake.nix")")" && pwd)"

# A git checkout is its own toplevel: address it as a git flake so nix skips
# the gitignored trees (bazel-*). A plain source tree (no git — an RBE input
# tree) is addressed with `path:`.
if [ "$(git -C "$root" rev-parse --show-toplevel 2>/dev/null || true)" = "$root" ]; then
  flake="$root"
else
  flake="path:$root"
fi

require_cmd nix "install nix, or run this lane where nix is on PATH"

out="$(nix build --no-link --print-out-paths "$flake#checks.x86_64-linux.hm-eval")" ||
  die "nix build $flake#checks.x86_64-linux.hm-eval failed"
info "OK ($out)"
