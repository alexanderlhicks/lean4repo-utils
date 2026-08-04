# sandbox_flag.sh — the single authoritative SHELL parser for LEANREPO_SANDBOX.
#
# Provenance: this repo's own code. `LEANREPO_SANDBOX` is a lean4repo-utils concept with
# no upstream counterpart; the semantics mirror `lean_tools.sandbox_enabled` in this repo.
# It is NOT derived from TauCeti (whose pr-build.yml has no such flag), so it carries no
# third-party attribution obligation. Recorded explicitly because this file was factored
# out of `landrun-wrap.sh`, which IS a TauCeti derivative — the extracted lines are the
# flag parser only, none of the landrun invocation that makes that file a derivative.
#
# MUST BE SOURCED, never executed for its exit status:
#
#     set -euo pipefail
#     . "<dir>/sandbox_flag.sh"
#     if sandbox_flag_enabled; then ... fi
#
# Sourcing under `set -e` is what makes a missing / unreadable / syntactically broken copy
# FAIL CLOSED — the caller aborts. Running it as `bash sandbox_flag.sh; if [ $? ]` would
# instead collapse bash's 127 (missing), 126 (not executable) and 2 (syntax error, e.g.
# CRLF line endings) into "disabled", and run PR-controlled code UNCONFINED. That is a
# worse failure than the drift this file exists to remove; do not refactor toward it.

# Returns 0 = sandboxing ENABLED, 1 = DISABLED.
#
# Semantics, byte-for-byte with `lean_tools.sandbox_enabled`: trim leading/trailing ASCII
# whitespace, lowercase, then match the recognised sets; an UNRECOGNISED non-empty value
# fails CLOSED (enabled, with a warning) so a typo cannot silently void a security control.
sandbox_flag_enabled() {
  local v="${LEANREPO_SANDBOX-}"

  # Trim EDGES ONLY, mirroring Python's str.strip().
  #  - `tr -d '[:space:]'` (the bug this replaces) deletes INTERIOR whitespace too, so
  #    "o ff" collapsed to "off" and DISABLED the sandbox while Python failed closed.
  #  - `sed 's/^[[:space:]]*//'` cannot be used either: sed is line-oriented, so a value
  #    containing a newline is two records and a leading "\n" survives.
  # The character-by-character loops below are verbose but handle every ASCII whitespace
  # character, newlines included, in pure bash.
  while [ -n "$v" ]; do
    case "$v" in [[:space:]]*) v="${v#?}" ;; *) break ;; esac
  done
  while [ -n "$v" ]; do
    case "$v" in *[[:space:]]) v="${v%?}" ;; *) break ;; esac
  done
  v="${v,,}"

  case "$v" in
    ''|0|false|no|off|disabled)     return 1 ;;
    1|true|yes|on|require|enabled)  return 0 ;;
    *)
      printf '::warning::LEANREPO_SANDBOX=%s not recognised; treating as ENABLED (fail closed)\n' "$v" >&2
      return 0
      ;;
  esac
}
