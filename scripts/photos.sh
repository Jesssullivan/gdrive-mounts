#!/usr/bin/env bash
# In-tree entrypoint. The separate Nix photos package pins the full runtime.
set -euo pipefail
case "$-:${SHELLOPTS:-}" in
  *xtrace*|*x:*) printf '%s\n' 'photos: refusing shell tracing' >&2; exit 70 ;;
esac
umask 077
export PYTHONDONTWRITEBYTECODE=1
d="$(cd "$(dirname "$0")" && pwd)"
for entry in "$d/gdrive_photos.py" "$d/scripts/gdrive_photos.py"; do
  if [ -f "$entry" ]; then
    exec "${GDM_PHOTOS_PYTHON:-python3}" "$entry" "$@"
  fi
done
printf '%s\n' 'photos: Python entrypoint not found; use nix run .#photos -- <phase>' >&2
exit 70
