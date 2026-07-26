#!/usr/bin/env bash
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

# Same enable semantics as lean_tools.sandbox_enabled: recognised truthy set enables;
# empty/off disables; an UNRECOGNISED non-empty value fails CLOSED (enabled + warning).
enabled=1
case "$(printf '%s' "${LEANREPO_SANDBOX-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  ''|0|false|no|off|disabled) enabled=0 ;;
  1|true|yes|on|require|enabled) enabled=1 ;;
  *) echo "::warning::landrun-wrap: LEANREPO_SANDBOX not recognised; treating as ENABLED (fail closed)" >&2 ;;
esac

if [ "$enabled" -eq 0 ]; then exec "$@"; fi

if ! command -v landrun >/dev/null 2>&1; then
  echo "::error::landrun-wrap: LEANREPO_SANDBOX set but landrun not on PATH — refusing to run PR code unconfined" >&2
  exit 1
fi
if [ -z "$wdir" ]; then echo "::error::landrun-wrap: empty writable dir" >&2; exit 2; fi
mkdir -p "$wdir"

exec landrun \
  --rox /usr --rox /bin --rox /lib --rox /lib64 --rox /etc \
  --rw /dev/null --rox /dev/zero --rox /dev/urandom --rox /dev/random \
  --rox "$HOME/.elan" --rox "$PWD" --rwx "$wdir" \
  --env PATH --env HOME --env CI --env LAKE_NO_CACHE \
  -- "$@"
