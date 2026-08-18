#!/usr/bin/env bash
# endpoint-free-check.sh — no cache/RBE endpoints baked into checked-in bazel
# config (house doctrine). Endpoints may only arrive via runtime env.
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

hits="$(grep -nE 'grpc://|grpcs://|remote_cache|remote_executor|remote_header|bes_backend|remote_instance_name' \
  .bazelrc .bazelrc.* 2>/dev/null | grep -v '^[^:]*:[0-9]*:#' || true)"
if [[ -n "$hits" ]]; then
  echo "endpoint-free-check: FAIL — endpoint material in checked-in bazel config:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "endpoint-free-check: OK"
