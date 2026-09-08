#!/usr/bin/env bash
# Same synthetic suite through just, Nix checks, and Bazel runfiles.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
d="$(cd "$(dirname "$0")" && pwd)"
for test_dir in "$d" "$d/scripts"; do
  if [ -f "$test_dir/test_gdrive_photos.py" ]; then
    exec "${GDM_PHOTOS_PYTHON:-python3}" -m unittest discover \
      -s "$test_dir" -p 'test*photos*.py' "$@"
  fi
done
printf '%s\n' 'test-photos: synthetic test entrypoint not found' >&2
exit 70
