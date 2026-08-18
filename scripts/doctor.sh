#!/usr/bin/env bash
# doctor.sh — cold-landing status for gdrive-mounts. Read-only: it changes
# nothing and it prints paths, modes and sizes, never a secret value.
#
# Exit 0 all OK, 1 at least one WARN, 2 at least one FAIL. A missing tool is a
# FAIL row, not a crash, so the report is still readable on a bare machine.
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

usage() {
  cat >&2 <<'USAGE'
usage: doctor.sh [--json] [--fast] [--orgs FILE]
  --json      machine-readable report on stdout (schema_version 1)
  --fast      skip the cache-usage walk
  --orgs FILE org registry (default: orgs.json next to the repo or package)
USAGE
  exit 2
}

JSON=0
FAST=0
ORGS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --fast) FAST=1; shift ;;
    --orgs) ORGS="${2:-}"; shift 2 ;;
    -h | --help) usage ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

R_SECTION=()
R_ID=()
R_STATUS=()
R_DETAIL=()
SECTIONS=()
N_FAIL=0
N_WARN=0

row() { # section id status detail
  R_SECTION+=("$1")
  R_ID+=("$2")
  R_STATUS+=("$3")
  R_DETAIL+=("$4")
  case "$3" in
    FAIL) N_FAIL=$((N_FAIL + 1)) ;;
    WARN) N_WARN=$((N_WARN + 1)) ;;
  esac
  case " ${SECTIONS[*]-} " in
    *" $1 "*) ;;
    *) SECTIONS+=("$1") ;;
  esac
}

dev_of() {
  if stat --version >/dev/null 2>&1; then stat -c '%d' -- "$1" 2>/dev/null; else stat -f '%d' -- "$1" 2>/dev/null; fi
}
mtime_of() {
  if stat --version >/dev/null 2>&1; then stat -c '%Y' -- "$1" 2>/dev/null; else stat -f '%m' -- "$1" 2>/dev/null; fi
}
size_of() {
  if stat --version >/dev/null 2>&1; then stat -c '%s' -- "$1" 2>/dev/null; else stat -f '%z' -- "$1" 2>/dev/null; fi
}
is_mountpoint() {
  local p="$1" a b
  [ -d "$p" ] || return 1
  a="$(dev_of "$p")"
  b="$(dev_of "$p/..")"
  [ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]
}
to_kb() { # 100G -> kilobytes; 0 when the string is not a size
  local v="$1" n u
  n="${v%[GMKgmk]}"
  u="${v#"$n"}"
  case "$n" in
    '' | *[!0-9]*)
      echo 0
      return
      ;;
  esac
  case "$u" in
    G | g) echo $((n * 1024 * 1024)) ;;
    M | m) echo $((n * 1024)) ;;
    *) echo "$n" ;;
  esac
}

# ── tools ───────────────────────────────────────────────────────────────────
for t in jq rclone; do
  if command -v "$t" >/dev/null 2>&1; then row tools "$t" OK "$(command -v "$t")"; else
    row tools "$t" FAIL "not on PATH (nix develop provides it)"
  fi
done
for t in sqlite3 sops; do
  if command -v "$t" >/dev/null 2>&1; then row tools "$t" OK "$(command -v "$t")"; else
    row tools "$t" WARN "not on PATH"
  fi
done
if have_gnu_coreutils; then row tools coreutils OK "GNU"; else row tools coreutils WARN "BSD stat; modes and mtimes fall back"; fi

HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1

if [ "$HAVE_JQ" = 1 ]; then
  [ -n "$ORGS" ] || ORGS="$(orgs_default)"
  if [ -r "$ORGS" ] && jq -e . "$ORGS" >/dev/null 2>&1; then
    row orgs registry OK "$ORGS"
  else
    row orgs registry FAIL "org registry unreadable or not JSON: $ORGS"
    HAVE_JQ=0
  fi
else
  row orgs registry FAIL "jq missing; every org-dependent section is skipped"
fi

emit_and_exit() {
  local status="ok"
  [ "$N_WARN" -gt 0 ] && status="warn"
  [ "$N_FAIL" -gt 0 ] && status="fail"
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [ "$JSON" = 1 ]; then
    printf '{"schema_version":1,'
    printf '"generated_at":"%s",' "$(json_escape "$now")"
    printf '"status":"%s",' "$status"
    printf '"sections":['
    local si=0 s i first_row
    for s in ${SECTIONS[@]+"${SECTIONS[@]}"}; do
      [ "$si" = 0 ] || printf ','
      si=1
      printf '{"name":"%s","rows":[' "$(json_escape "$s")"
      first_row=0
      for i in "${!R_ID[@]}"; do
        [ "${R_SECTION[$i]}" = "$s" ] || continue
        [ "$first_row" = 0 ] || printf ','
        first_row=1
        printf '{"id":"%s","status":"%s","detail":"%s"}' \
          "$(json_escape "${R_ID[$i]}")" \
          "$(printf '%s' "${R_STATUS[$i]}" | tr '[:upper:]' '[:lower:]')" \
          "$(json_escape "${R_DETAIL[$i]}")"
      done
      printf ']}'
    done
    printf ']}\n'
  else
    printf 'gdrive-mounts doctor — %s\n' "$now"
    local s i
    for s in ${SECTIONS[@]+"${SECTIONS[@]}"}; do
      printf '\n[%s]\n' "$s"
      for i in "${!R_ID[@]}"; do
        [ "${R_SECTION[$i]}" = "$s" ] || continue
        printf '  %-4s %-28s %s\n' "${R_STATUS[$i]}" "${R_ID[$i]}" "${R_DETAIL[$i]}"
      done
    done
    printf '\nstatus: %s (%s fail, %s warn)\n' "$status" "$N_FAIL" "$N_WARN"
  fi
  [ "$N_FAIL" -gt 0 ] && exit 2
  [ "$N_WARN" -gt 0 ] && exit 1
  exit 0
}

[ "$HAVE_JQ" = 1 ] || emit_and_exit

STATE="$(state_dir "$ORGS")"
INDEX_STATE="$(index_state_dir "$ORGS")"
SECRETS="$(secret_dir "$ORGS")"
STAGE="$(stage_dir "$ORGS")"
MOUNT_ROOT="$(expand_home "$(jq -r '.defaults.mountRoot // "~/GDrive"' "$ORGS")")"
CACHE_ROOT="$(expand_home "$(jq -r '.defaults.cacheRoot // "~/.cache/gdrive-mounts"' "$ORGS")")"
CACHE_MAX="$(jq -r '.defaults.cacheMaxSize // "100G"' "$ORGS")"
SLO_HOURS="$(jq -r '.defaults.indexFreshnessSloHours // 24' "$ORGS")"
OS="$(gdm_os)"

# ── orgs ────────────────────────────────────────────────────────────────────
while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  enabled="$(jq -r '.enabled' <<<"$org")"
  detail="$(jq -r '"scope=\(.scope) driveKind=\(.driveKind) mounts=\((.mounts // []) | length) links=\((.links // []) | length)"' <<<"$org")"
  if [ "$enabled" = "true" ]; then row orgs "$name" OK "enabled $detail"; else row orgs "$name" SKIP "disabled $detail"; fi
done < <(jq -c '.orgs[]' "$ORGS")

# ── secrets (paths, modes, sizes; never contents) ───────────────────────────
check_secret() { # org label path
  local org="$1" label="$2" path="$3" mode size
  if [ ! -e "$path" ]; then
    row secrets "$org.$label" FAIL "absent: $path"
    return
  fi
  if [ ! -r "$path" ]; then
    row secrets "$org.$label" FAIL "not readable: $path"
    return
  fi
  mode="$(file_mode "$path")"
  size="$(size_of "$path")"
  if grep -q 'ENC\[AES256_GCM,' "$path" 2>/dev/null; then
    row secrets "$org.$label" FAIL "still sops ciphertext: $path"
  elif [ "${size:-0}" -lt 2 ]; then
    row secrets "$org.$label" FAIL "empty: $path"
  elif [ "$mode" != "600" ] && [ "$mode" != "400" ]; then
    row secrets "$org.$label" WARN "mode $mode (want 600): $path"
  else
    row secrets "$org.$label" OK "mode $mode, ${size}B: $path"
  fi
}

while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  prefix="$(jq -r '.secretEnvPrefix' <<<"$org")"
  cvar="${prefix}_CLIENT_FILE"
  tvar="${prefix}_TOKEN_FILE"
  cpath="${!cvar:-}"
  tpath="${!tvar:-}"
  [ -n "$cpath" ] || cpath="$SECRETS/$name/client.json"
  [ -n "$tpath" ] || tpath="$SECRETS/$name/token.json"
  check_secret "$name" client "$cpath"
  check_secret "$name" token "$tpath"
done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS")

# ── rendered configs ────────────────────────────────────────────────────────
while IFS= read -r name; do
  conf="$(conf_path "$STATE" "$name")"
  if [ ! -e "$conf" ]; then
    row config "$name" WARN "absent: $conf (run: just render $name)"
  elif [ "$(file_mode "$conf")" != "600" ]; then
    row config "$name" FAIL "mode $(file_mode "$conf") (want 600): $conf"
  elif ! grep -q '^\[' "$conf"; then
    row config "$name" FAIL "no stanza header: $conf"
  else
    row config "$name" OK "mode 600: $conf"
  fi
done < <(jq -r '.orgs[] | select(.enabled == true) | .name' "$ORGS")

# ── cache volume and error breadcrumbs ──────────────────────────────────────
case "$CACHE_ROOT" in
  /Volumes/* | /mnt/*)
    vol="$CACHE_ROOT"
    while :; do
      parent="$(dirname "$vol")"
      case "$parent" in /Volumes | /mnt | /) break ;; esac
      vol="$parent"
    done
    if is_mountpoint "$vol"; then
      row cache volume OK "mounted: $vol"
    else
      row cache volume FAIL "cache volume not mounted: $vol (mounts wait then fail loud)"
    fi
    ;;
  *) row cache volume OK "on the boot volume: $CACHE_ROOT" ;;
esac

if [ -d "$CACHE_ROOT" ]; then
  if [ "$FAST" = 1 ]; then
    row cache usage SKIP "--fast"
  else
    used_kb="$(du -sk "$CACHE_ROOT" 2>/dev/null | awk '{print $1}' || true)"
    max_kb="$(to_kb "$CACHE_MAX")"
    if [ -n "${used_kb:-}" ] && [ "${max_kb:-0}" -gt 0 ]; then
      pct=$((used_kb * 100 / max_kb))
      if [ "$pct" -gt 85 ]; then
        row cache usage WARN "${pct}% of $CACHE_MAX ($CACHE_ROOT)"
      else
        row cache usage OK "${pct}% of $CACHE_MAX ($CACHE_ROOT)"
      fi
    else
      row cache usage WARN "usage unreadable: $CACHE_ROOT"
    fi
  fi
else
  row cache usage WARN "cache dir absent: $CACHE_ROOT"
fi

breadcrumbs="$(find "$STATE" -maxdepth 1 -name 'last-error.*' 2>/dev/null | tr '\n' ' ' || true)"
if [ -n "${breadcrumbs// /}" ]; then
  row cache errors FAIL "mount error breadcrumbs present: $breadcrumbs"
else
  row cache errors OK "no last-error breadcrumbs in $STATE"
fi

# ── mounts, agents, links ───────────────────────────────────────────────────
agent_state() { # label unit
  local label="$1" unit="$2" out
  if [ "$OS" = darwin ]; then
    if ! command -v launchctl >/dev/null 2>&1; then
      printf 'launchctl absent'
      return 1
    fi
    if out="$(launchctl list "$label" 2>/dev/null)"; then
      printf 'pid=%s lastExit=%s' \
        "$(printf '%s' "$out" | sed -n 's/.*"PID" = \([0-9]*\);.*/\1/p' | head -1)" \
        "$(printf '%s' "$out" | sed -n 's/.*"LastExitStatus" = \([0-9-]*\);.*/\1/p' | head -1)"
      return 0
    fi
    printf 'not loaded'
    return 1
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    printf 'systemctl absent'
    return 1
  fi
  out="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
  printf '%s' "${out:-unknown}"
  [ "$out" = active ]
}

while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  root_suffix="$(jq -r '((.mounts // []) | map(select(.name == "root")) | .[0].mountSuffix) // ((.mounts // []) | .[0].mountSuffix) // ""' <<<"$org")"
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    mname="$(jq -r '.name' <<<"$m")"
    suffix="$(jq -r '.mountSuffix' <<<"$m")"
    point="$MOUNT_ROOT/$suffix"
    if is_mountpoint "$point"; then
      row mounts "$name.$mname" OK "mounted: $point"
    elif [ -d "$point" ]; then
      row mounts "$name.$mname" WARN "not mounted: $point"
    else
      row mounts "$name.$mname" WARN "mount point absent: $point"
    fi
    label="dev.tinyland.gdrive-mounts.$name-$mname"
    unit="gdrive-mounts-$name-$mname.service"
    if detail="$(agent_state "$label" "$unit")"; then
      row mounts "$name.$mname.agent" OK "$detail"
    else
      row mounts "$name.$mname.agent" WARN "$detail ($label)"
    fi
  done < <(jq -c '(.mounts // [])[]' <<<"$org")

  while IFS= read -r l; do
    [ -n "$l" ] || continue
    lname="$(jq -r '.name' <<<"$l")"
    target="$(jq -r '.target' <<<"$l")"
    link="$MOUNT_ROOT/$name-$lname"
    want="$MOUNT_ROOT/$root_suffix/$target"
    if [ -L "$link" ]; then
      have="$(readlink "$link")"
      if [ "$have" = "$want" ]; then
        row mounts "$name.$lname.link" OK "$link -> $want"
      else
        row mounts "$name.$lname.link" WARN "$link points at $have, expected $want"
      fi
    else
      row mounts "$name.$lname.link" WARN "symlink absent: $link"
    fi
  done < <(jq -c '(.links // [])[]' <<<"$org")
done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS")

# ── index freshness ─────────────────────────────────────────────────────────
now_epoch="$(date +%s)"
db="$INDEX_STATE/index.sqlite"
while IFS= read -r name; do
  snap="$INDEX_STATE/$name.json"
  snap_mtime="$(mtime_of "$snap" || true)"
  if [ -r "$snap" ] && [ -n "$snap_mtime" ]; then
    age_h=$(((now_epoch - snap_mtime) / 3600))
    if [ "$age_h" -gt "$SLO_HOURS" ]; then
      row index "$name.freshness" WARN "${age_h}h old, SLO ${SLO_HOURS}h: $snap"
    else
      row index "$name.freshness" OK "${age_h}h old, SLO ${SLO_HOURS}h"
    fi
  else
    row index "$name.freshness" WARN "no snapshot yet: $snap (run: just index)"
  fi
  if [ -r "$db" ] && command -v sqlite3 >/dev/null 2>&1; then
    rows="$(sqlite3 "$db" "SELECT count(*) FROM files WHERE org = $(sql_quote "$name");" 2>/dev/null || echo "")"
    last="$(sqlite3 "$db" "SELECT status || ' @ ' || finished_at FROM runs WHERE org = $(sql_quote "$name") ORDER BY finished_at DESC LIMIT 1;" 2>/dev/null || echo "")"
    row index "$name.rows" OK "${rows:-0} row(s); last run: ${last:-none}"
  fi
done < <(jq -r '.orgs[] | select(.enabled == true) | .name' "$ORGS")

# ── scope versus what was actually minted ───────────────────────────────────
while IFS= read -r org; do
  name="$(jq -r '.name' <<<"$org")"
  want_scope="$(jq -r '.scope' <<<"$org")"
  meta=""
  for cand in "$STATE/mint.meta.$name.json" "$STAGE/$name/mint.meta.json"; do
    [ -r "$cand" ] && { meta="$cand"; break; }
  done
  if [ -z "$meta" ]; then
    row scope "$name" WARN "no mint record; token scope unverified (run: just mint-token $name)"
    continue
  fi
  got_scope="$(jq -r '.scope // ""' "$meta")"
  if [ "$got_scope" = "$want_scope" ]; then
    row scope "$name" OK "$want_scope (minted $(jq -r '.minted_at // "?"' "$meta"))"
  else
    row scope "$name" WARN "orgs.json says $want_scope, token was minted $got_scope — re-mint before calling promotion done"
  fi
done < <(jq -c '.orgs[] | select(.enabled == true)' "$ORGS")

emit_and_exit
