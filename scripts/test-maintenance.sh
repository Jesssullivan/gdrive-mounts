#!/usr/bin/env bash
# Simulated controllers and an ordinary worker child: never create a service.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
d="$(cd "$(dirname "$0")" && pwd)"
for test_dir in "$d" "$d/scripts"; do
  if [ -f "$test_dir/test_qualify_maintenance.py" ]; then
    exec python3 -m unittest discover -s "$test_dir" -p 'test_qualify_maintenance.py' "$@"
  fi
done
printf '%s\n' 'test-maintenance: synthetic test entrypoint not found' >&2
exit 70
