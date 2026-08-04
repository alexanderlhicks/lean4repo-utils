#!/usr/bin/env bash
#
# Adapted from TauCetiProject/TauCeti@92c79e5e0a618f8c5c2b9909be1ce50f6891dde7
# (.github/workflows/pr-build.yml, the "Build (sandboxed)" step), licensed Apache-2.0;
# see ../../NOTICE. Apache §4(b) changes relative to upstream: extracted from an inline
# workflow step into a reusable script; passes through unchanged when sandboxing is
# disabled; fails CLOSED with an explicit error when sandboxing is requested but landrun
# is absent (upstream assumes it is present); and the enable/disable decision is
# delegated to scripts/sandbox_flag.sh rather than inlined.
#
# landrun-wrap.sh — run a command under landrun (Landlock) confinement when
# LEANREPO_SANDBOX is enabled; pass through unchanged when disabled; FAIL CLOSED when
# enabled but landrun is absent. The shell counterpart of lean_tools.sandbox_wrap — used
# to confine action.yml's `lake build` of untrusted PR Lean in the secret-bearing review
# stage (the Python lean_tools/lean_info seams are confined by sandbox_wrap directly).
#
# KEEP THE FLAG SET IN SYNC with lean_tools.sandbox_wrap and scripts/sandbox-build.sh's
# caller: read-only filesystem except the one writable build dir; no network flag (egress
# denied; Landlock is TCP-only on the runner, so the key is kept out of the phase by env
# hygiene + the key-file lifecycle, not by this).
#
# Usage: landrun-wrap.sh <writable_dir> -- <command> [args...]
set -euo pipefail

wdir="${1-}"
shift || true
if [ "${1-}" != "--" ]; then echo "::error::landrun-wrap: usage: landrun-wrap.sh <dir> -- cmd..." >&2; exit 2; fi
shift
if [ -z "${1-}" ]; then echo "::error::landrun-wrap: no command given" >&2; exit 2; fi

# Enable semantics come from ONE authority shared with action.yml and mirrored by
# lean_tools.sandbox_enabled — see scripts/sandbox_flag.sh. SOURCED, not executed: under
# `set -e` a missing or corrupt copy aborts this script (FAIL CLOSED), whereas testing an
# exit status would read bash's 127/126/2 as "disabled" and run PR code unconfined.
# The previous inline parser used `tr -d '[:space:]'`, which deleted INTERIOR whitespace:
# LEANREPO_SANDBOX='o ff' collapsed to 'off' and silently disabled confinement here while
# the Python side failed closed. Behavioural parity is pinned by the test suite.
. "$(dirname "${BASH_SOURCE[0]}")/sandbox_flag.sh"

# Sourcing alone is NOT sufficient, and assuming it was is a fail-open we shipped and then
# caught: a library that sources CLEANLY but does not define the function (empty file,
# truncated copy, a comments-only stub) leaves `sandbox_flag_enabled` undefined, bash
# returns 127 for the missing command, and — because a command inside an `if !` condition
# is EXEMPT from `set -e` — the `!` inverts 127 into "disabled" and the payload runs
# UNCONFINED. Verify the function exists explicitly, before relying on its status.
if ! declare -F sandbox_flag_enabled >/dev/null 2>&1; then
  echo "::error::landrun-wrap: sandbox_flag.sh did not define sandbox_flag_enabled — refusing to run PR code unconfined" >&2
  exit 1
fi

if ! sandbox_flag_enabled; then exec "$@"; fi

if ! command -v landrun >/dev/null 2>&1; then
  echo "::error::landrun-wrap: LEANREPO_SANDBOX set but landrun not on PATH — refusing to run PR code unconfined" >&2
  exit 1
fi
if [ -z "$wdir" ]; then echo "::error::landrun-wrap: empty writable dir" >&2; exit 2; fi
mkdir -p "$wdir"

# Redirect TMPDIR into the one writable dir: Landlock is default-deny, so lean/leanc/clang
# temp writes to the default /tmp (or $RUNNER_TEMP) would hit EPERM and break the build.
# The var must be PASSED IN (--env TMPDIR); the mount ($wdir, --rwx) already covers it.
export TMPDIR="$wdir/tmp"
mkdir -p "$TMPDIR"

exec landrun \
  --rox /usr --rox /bin --rox /lib --rox /lib64 --rox /etc \
  --rw /dev/null --rox /dev/zero --rox /dev/urandom --rox /dev/random \
  --rox "$HOME/.elan" --rox "$PWD" --rwx "$wdir" \
  --env PATH --env HOME --env CI --env LAKE_NO_CACHE --env TMPDIR \
  -- "$@"
