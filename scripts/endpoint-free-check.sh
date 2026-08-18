#!/usr/bin/env bash
# endpoint-free-check.sh — no cache or executor endpoint may be baked into
# checked-in bazel config. Endpoints arrive at runtime, from the environment.
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

cd "$(repo_root)"

hits="$(grep -nE 'grpc://|grpcs://|remote_cache|remote_executor|remote_header|bes_backend|remote_instance_name' \
  .bazelrc .bazelrc.* 2>/dev/null | grep -v '^[^:]*:[0-9]*:#' || true)"
if [ -n "$hits" ]; then
  warn "endpoint material in checked-in bazel config:"
  printf '%s\n' "$hits" >&2
  die "remove the endpoints; pass them through the environment instead"
fi
info "OK"
