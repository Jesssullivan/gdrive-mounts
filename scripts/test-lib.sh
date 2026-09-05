#!/usr/bin/env bash
# test-lib.sh — unit tests for scripts/lib/common.sh.
#
# The nix side has three test layers (nix/tests/hm-eval.nix) and none of them
# reach this file: common.sh is sourced by the operator scripts, not by the
# module. So the shell library had no executed test at all, and `file_mode`
# spent months answering a question nobody had asked it — reporting the mode of
# a *symlink* rather than of the file it points at, which made `just doctor`
# warn `mode 755 (want 600)` about every sops-nix secret on every host.
#
# Everything here runs in a 0700 temp dir that is wiped on exit. Zero network,
# zero real secrets, no state outside the temp dir.
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

PASS=0
FAIL=0

# usage: is <what> <got> <want>
is() {
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    printf '%s: FAIL %s\n      got:  %s\n      want: %s\n' "$GDM_PROG" "$1" "${2:-<empty>}" "${3:-<empty>}" >&2
  fi
}

secure_tmpdir
t="$GDM_TMPDIR"

# ── file_mode ───────────────────────────────────────────────────────────────
#
# The contract is: the mode of the file a path RESOLVES to. Every caller
# (doctor's secret check, validate-config's rendered-config check) is asking
# "is the thing behind this path locked down", never "what is the link".

printf 'x' > "$t/f600" && chmod 600 "$t/f600"
printf 'x' > "$t/f400" && chmod 400 "$t/f400"
printf 'x' > "$t/f644" && chmod 644 "$t/f644"
mkdir -p "$t/d700" && chmod 700 "$t/d700"

is "file_mode on a 0600 file" "$(file_mode "$t/f600")" 600
is "file_mode on a 0400 file" "$(file_mode "$t/f400")" 400
is "file_mode on a 0644 file" "$(file_mode "$t/f644")" 644
is "file_mode on a 0700 dir" "$(file_mode "$t/d700")" 700

# The regression. A symlink's own mode is 0777 & ~umask — 0755 in practice, and
# never anything a caller here cares about. sops-nix places every secret as a
# symlink into its own store, so this is not a corner case: it is the shape of
# every secret path this repo checks on a live host.
#
# Neither `stat` dialect dereferences by default. Measured 2026-09-05: GNU
# coreutils 9.11 `stat -c %a` and macOS 26.7 `stat -f %Lp` BOTH print 755 for a
# symlink to a 0400 file. The received explanation — that GNU follows and BSD
# does not — is wrong, and acting on it would have fixed the Darwin branch and
# left the bug live everywhere a GNU stat is on PATH, which in this repo is
# every generated wrapper.
ln -s "$t/f400" "$t/link400"
ln -s "$t/f644" "$t/link644"
ln -s "$t/d700" "$t/linkdir"
is "file_mode follows a symlink to a 0400 file" "$(file_mode "$t/link400")" 400
is "file_mode follows a symlink to a 0644 file" "$(file_mode "$t/link644")" 644
is "file_mode follows a symlink to a 0700 dir" "$(file_mode "$t/linkdir")" 700

# …and the negative cases stay empty rather than inventing a mode. A caller
# comparing against "600" must see a miss, not a plausible-looking answer.
#
# The dangling case is where the two dialects genuinely disagree: GNU stat
# errors, and macOS `stat -L` silently falls back to lstat and reports the
# LINK's mode (0777 & ~umask, so 700 under this library's umask 077). Without
# an explicit guard the same broken secret path would read empty on Linux and
# `700` on Darwin — a mode printed for a file that is not there.
ln -s "$t/nothing-here" "$t/broken"
is "file_mode on a broken symlink is empty" "$(file_mode "$t/broken")" ""
is "file_mode on an absent path is empty" "$(file_mode "$t/absent")" ""

# ── expand_home ─────────────────────────────────────────────────────────────
# The one shell-side `~` expander (nix/lib/plan.nix expandHome is the other).
# shellcheck disable=SC2088  # the literal ~ here is the input being expanded
is "expand_home bare tilde" "$(expand_home '~')" "$HOME"
# shellcheck disable=SC2088  # ditto
is "expand_home tilde path" "$(expand_home '~/a/b')" "$HOME/a/b"
is "expand_home absolute path is untouched" "$(expand_home /a/b)" /a/b
is "expand_home leaves a mid-string tilde alone" "$(expand_home /a/~/b)" /a/~/b

# ── small formatters ────────────────────────────────────────────────────────
# sql_quote feeds sqlite3 `.parameter set`; an unescaped quote there is an
# injection, not a typo.
is "sql_quote wraps" "$(sql_quote abc)" "'abc'"
is "sql_quote doubles an embedded quote" "$(sql_quote "a'b")" "'a''b'"
is "json_escape escapes a quote" "$(json_escape 'a"b')" 'a\"b'
is "json_escape escapes a backslash" "$(json_escape 'a\b')" 'a\\b'

# ── conf_path ───────────────────────────────────────────────────────────────
is "conf_path" "$(conf_path /s sulliwood)" /s/rclone-sulliwood.conf

if [ "$FAIL" != 0 ]; then
  die "$FAIL of $((PASS + FAIL)) assertion(s) failed"
fi
info "OK ($PASS assertions)"
