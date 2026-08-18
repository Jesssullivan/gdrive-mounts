#!/usr/bin/env bash
# endpoint-free-check.sh — no cache or executor endpoint may be baked into
# checked-in bazel config. Endpoints arrive at runtime, from the environment.
set -euo pipefail

d="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
if [ -f "$d/lib/common.sh" ]; then . "$d/lib/common.sh"; else . "$d/../libexec/gdrive-mounts/common.sh"; fi
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
