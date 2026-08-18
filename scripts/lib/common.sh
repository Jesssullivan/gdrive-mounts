# shellcheck shell=bash
# common.sh — shared helpers for every gdrive-mounts script.
#
# Source it with the two-candidate lookup (in-repo, then installed libexec):
#   d="$(cd "$(dirname "$0")" && pwd)"
#   if [ -f "$d/lib/common.sh" ]; then . "$d/lib/common.sh"
#   else . "$d/../libexec/gdrive-mounts/common.sh"; fi
#
# Rules this file enforces for every caller:
#   - stdout carries machine-readable output only; diagnostics go to stderr
#   - no script writes a secret value anywhere but a file; values move file to file
#   - every created file is 0600 and every created directory is 0700 (umask 077)
#   - tracing is refused unless the caller opts into dummy secrets
#
# wipe_file uses rm -P on darwin and shred on linux. Neither guarantees an
# overwrite on APFS or on any SSD with wear levelling. The real controls are
# umask 077, 0700 directories, and short file lifetime.

[ -n "${GDM_COMMON_SOURCED:-}" ] && return 0
GDM_COMMON_SOURCED=1

umask 077
export LC_ALL=C
PS4=''

GDM_PROG="$(basename -- "${0:-gdrive-mounts}")"
GDM_PROG="${GDM_PROG%.sh}"
GDM_TMPDIR=""

# ── diagnostics (stderr only) ───────────────────────────────────────────────

info() { printf '%s: %s\n' "$GDM_PROG" "$*" >&2; }
warn() { printf '%s: WARN %s\n' "$GDM_PROG" "$*" >&2; }
die() {
  printf '%s: FAIL %s\n' "$GDM_PROG" "$*" >&2
  exit 1
}

# ── guards ──────────────────────────────────────────────────────────────────

# Refuse to run under xtrace: a trace prints secret values. GDM_ALLOW_TRACE=1
# turns tracing off and forces dummy secrets instead of refusing.
no_trace_guard() {
  local traced=0
  case "$-" in *x*) traced=1 ;; esac
  case "${SHELLOPTS:-}" in *xtrace*) traced=1 ;; esac
  [ "$traced" = 1 ] || return 0
  if [ "${GDM_ALLOW_TRACE:-0}" = "1" ]; then
    set +x
    export GDRIVE_MOUNTS_DUMMY_SECRETS=1
    warn "trace requested: xtrace disabled, GDRIVE_MOUNTS_DUMMY_SECRETS=1 forced"
    return 0
  fi
  die "refusing to run under set -x (a trace prints secret values). Unset it, or set GDM_ALLOW_TRACE=1 to run with dummy secrets."
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 required${2:+ — $2}"
}

# GNU coreutils: the secret-handling scripts read modes with stat -c.
have_gnu_coreutils() { stat --version >/dev/null 2>&1; }

assert_gnu_coreutils() {
  have_gnu_coreutils && return 0
  die "GNU coreutils required (found BSD stat). Run inside the flake: nix develop --command just <recipe>"
}

# Octal mode, or empty when neither stat dialect answers.
file_mode() {
  if stat --version >/dev/null 2>&1; then
    stat -c '%a' -- "$1" 2>/dev/null
  else
    stat -f '%Lp' -- "$1" 2>/dev/null
  fi
}

gdm_os() {
  case "$(uname -s)" in
    Darwin) printf 'darwin' ;;
    Linux) printf 'linux' ;;
    *) printf 'other' ;;
  esac
}

# ── temp files and wiping ───────────────────────────────────────────────────

gdm_cleanup() {
  local d="${GDM_TMPDIR:-}"
  [ -n "$d" ] || return 0
  [ -d "$d" ] || return 0
  local f
  while IFS= read -r f; do wipe_file "$f"; done < <(find "$d" -type f 2>/dev/null)
  rm -rf -- "$d" 2>/dev/null || true
  GDM_TMPDIR=""
}

# Set GDM_TMPDIR to a 0700 scratch dir, wiped on exit. Never call it in a
# command substitution: the subshell would take the trap and the dir with it.
secure_tmpdir() {
  [ -n "$GDM_TMPDIR" ] && return 0
  GDM_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/gdrive-mounts.XXXXXX")"
  chmod 700 "$GDM_TMPDIR"
  trap 'gdm_cleanup' EXIT
  trap 'gdm_cleanup; exit 130' INT
  trap 'gdm_cleanup; exit 143' TERM
}

wipe_file() {
  local f
  for f in "$@"; do
    [ -f "$f" ] || continue
    case "$(gdm_os)" in
      darwin) rm -Pf -- "$f" 2>/dev/null && continue ;;
      linux) command -v shred >/dev/null 2>&1 && shred -u -n1 -- "$f" 2>/dev/null && continue ;;
    esac
    rm -f -- "$f" 2>/dev/null || true
  done
}

# ── paths ───────────────────────────────────────────────────────────────────

# The one shell-side ~ expander. nix/lib/plan.nix expandHome is the nix-side one.
expand_home() {
  # shellcheck disable=SC2088  # the literal ~ here is the input being expanded
  case "$1" in
    "~") printf '%s' "$HOME" ;;
    "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}

script_dir() { printf '%s' "${GDM_SCRIPT_DIR:-$(cd "$(dirname -- "$0")" && pwd)}"; }

repo_root() {
  local r
  r="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$r" ]; then printf '%s' "$r"; return 0; fi
  printf '%s' "$(cd "$(script_dir)/.." && pwd)"
}

# Installed data dir ($out/share/gdrive-mounts), empty when running from a checkout.
share_dir() {
  local d
  d="$(script_dir)/../share/gdrive-mounts"
  [ -d "$d" ] && { (cd "$d" && pwd); return 0; }
  printf ''
}

orgs_default() {
  [ -n "${GDRIVE_MOUNTS_ORGS:-}" ] && { printf '%s' "$GDRIVE_MOUNTS_ORGS"; return 0; }
  local root share
  root="$(repo_root)"
  [ -f "$root/orgs.json" ] && { printf '%s' "$root/orgs.json"; return 0; }
  share="$(share_dir)"
  [ -n "$share" ] && [ -f "$share/orgs.json" ] && { printf '%s' "$share/orgs.json"; return 0; }
  printf 'orgs.json'
}

template_default() {
  [ -n "${GDRIVE_MOUNTS_TEMPLATE:-}" ] && { printf '%s' "$GDRIVE_MOUNTS_TEMPLATE"; return 0; }
  local root share
  root="$(repo_root)"
  [ -f "$root/config/rclone.conf.template" ] && { printf '%s' "$root/config/rclone.conf.template"; return 0; }
  share="$(share_dir)"
  [ -n "$share" ] && [ -f "$share/rclone.conf.template" ] && { printf '%s' "$share/rclone.conf.template"; return 0; }
  printf 'config/rclone.conf.template'
}

# orgs.json defaults.stateDir is the SSOT for runtime state.
state_dir() {
  local orgs="$1" v=""
  [ -n "${GDRIVE_MOUNTS_STATE_DIR:-}" ] && { expand_home "$GDRIVE_MOUNTS_STATE_DIR"; return 0; }
  [ -r "$orgs" ] && v="$(jq -r '.defaults.stateDir // ""' "$orgs" 2>/dev/null || true)"
  # shellcheck disable=SC2088  # a literal default, handed to expand_home below
  [ -n "$v" ] || v="~/.local/state/gdrive-mounts"
  expand_home "$v"
}

index_state_dir() {
  local orgs="$1" v=""
  [ -r "$orgs" ] && v="$(jq -r '.defaults.indexStateDir // ""' "$orgs" 2>/dev/null || true)"
  # shellcheck disable=SC2088  # a literal default, handed to expand_home below
  [ -n "$v" ] || v="~/.local/state/gdrive-index"
  expand_home "$v"
}

stage_dir() {
  [ -n "${GDM_STAGE:-}" ] && { expand_home "$GDM_STAGE"; return 0; }
  printf '%s/stage' "$(state_dir "$1")"
}

secret_dir() {
  [ -n "${GDRIVE_MOUNTS_SECRET_DIR:-}" ] && { expand_home "$GDRIVE_MOUNTS_SECRET_DIR"; return 0; }
  printf '%s/secrets' "$(state_dir "$1")"
}

conf_path() { printf '%s/rclone-%s.conf' "$1" "$2"; }

mkdir_secure() {
  local d
  for d in "$@"; do
    [ -n "$d" ] || continue
    mkdir -p -- "$d"
    chmod 700 -- "$d" 2>/dev/null || true
  done
}

# Atomic 0600 install of a file already staged in a scratch dir.
install_secret_file() {
  local src="$1" dest="$2" tmp
  mkdir_secure "$(dirname -- "$dest")"
  tmp="$(mktemp "$(dirname -- "$dest")/.gdm.XXXXXX")"
  cat -- "$src" >"$tmp"
  chmod 600 "$tmp"
  mv -f -- "$tmp" "$dest"
}

# ── secrets ─────────────────────────────────────────────────────────────────

# 12-hex sha256 prefix of stdin. Never prints the value.
redact() {
  # bazelisk ships its own `sha256sum <file>` helper that can shadow GNU's on
  # PATH, so require the GNU one explicitly, then fall back to shasum/openssl.
  if sha256sum --version 2>/dev/null | grep -q 'GNU coreutils'; then
    sha256sum | cut -c1-12
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -c1-12
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 | sed 's/.*= //' | cut -c1-12
  else
    cat >/dev/null
    printf 'unavailable'
  fi
}

fingerprint_file() { redact <"$1"; }

# Die when a file handed to us is still sops ciphertext.
enc_tripwire() {
  local f="$1" label="${2:-$1}"
  grep -q 'ENC\[AES256_GCM,' -- "$f" 2>/dev/null &&
    die "$label is still sops-encrypted (contains ENC[AES256_GCM,). Decrypt it first, or point at the sops-nix runtime path: $f"
  return 0
}

assert_json() {
  local f="$1" label="${2:-$1}"
  jq -e . "$f" >/dev/null 2>&1 || die "$label is not valid JSON: $f"
}

# ── small formatters ────────────────────────────────────────────────────────

# SQL string literal, single quotes doubled.
sql_quote() { printf "'%s'" "${1//\'/\'\'}"; }

# JSON string body (no surrounding quotes). Control characters are dropped.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' -e 's/[[:cntrl:]]//g'
}
