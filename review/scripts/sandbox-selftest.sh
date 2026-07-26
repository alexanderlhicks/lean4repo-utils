#!/usr/bin/env bash
# sandbox-selftest.sh — prove landrun (Landlock) confinement actually ENFORCES on this
# host BEFORE any PR-controlled Lean runs; abort (fail closed) otherwise. The build
# workflow must call this and refuse to elaborate PR code if it does not exit 0.
#
# Adapted from the inline landrun self-test in TauCetiProject/TauCeti@92c79e5
# .github/workflows/pr-build.yml, licensed Apache-2.0 (extracted to a script), and
# HARDENED per the S18 PLAN gate:
#   * a POSITIVE control — a write INSIDE the allowed dir MUST succeed — so a sandbox
#     misconfigured to deny everything (which would then break every real build) cannot
#     pass by vacuously "denying" every escape;
#   * NO UDP/DNS escape probe: Landlock is TCP-only on the runner kernel, so requiring
#     UDP-denied would be permanently unsatisfiable (a self-inflicted DoA). Egress of the
#     provider key is prevented by env hygiene + the key-file lifecycle (review.py /
#     lean_tools.scrubbed_env), NOT by a network probe here;
#   * explicit fail-closed when landrun is absent or cannot even run a trivial confined
#     command (a runner image without the Landlock LSM), never running PR code unconfined.
# See ../../NOTICE.
#
# Usage: sandbox-selftest.sh <writable_dir>
#   <writable_dir> is the ONLY path the sandbox may write (e.g. "$PWD/.lake"); it must
#   already exist. Exits 0 iff confinement is proven; non-zero + loud otherwise.
set -uo pipefail

wdir="${1-}"
if [ -z "$wdir" ] || [ ! -d "$wdir" ]; then
  echo "::error::sandbox-selftest: writable dir '${wdir}' is missing or not a directory" >&2; exit 2
fi
if ! command -v landrun >/dev/null 2>&1; then
  echo "::error::sandbox-selftest: landrun not on PATH — refusing to run PR code unconfined" >&2; exit 2
fi

# Read-only mounts the probe shell needs to exist (bash + libs); the writable dir is the
# only --rwx. No network flag => egress denied by default.
RO=(--rox /usr --rox /bin --rox /lib --rox /lib64 --rox /etc)
probe() { landrun "${RO[@]}" --rw /dev/null --rwx "$wdir" -- bash -c "$1" >/dev/null 2>&1; }

# POSITIVE CONTROL: a write inside the allowed dir MUST succeed. This also doubles as the
# "is the sandbox even runnable here" check — if the host lacks Landlock, landrun aborts
# and this fails, so we fail closed rather than proceed.
if ! probe "echo ok > '${wdir}/.sbx-canary' && rm -f '${wdir}/.sbx-canary'"; then
  echo "::error::sandbox-selftest: in-sandbox write to '${wdir}' was DENIED (Landlock unavailable or sandbox misconfigured) — abort" >&2
  exit 1
fi

# ESCAPE PROBES: each MUST be denied (the confined command exits non-zero). If any
# SUCCEEDS, confinement is not enforcing and we abort.
fail=0
probe "echo x > /etc/sbx-probe"         && { echo "::error::sandbox-selftest: out-of-tree write to /etc was NOT denied" >&2; fail=1; }
probe "echo x > /dev/shm/sbx-probe"     && { echo "::error::sandbox-selftest: /dev/shm write was NOT denied" >&2; fail=1; }
probe "exec 3<>/dev/tcp/example.com/80" && { echo "::error::sandbox-selftest: TCP egress was NOT denied" >&2; fail=1; }

if [ "$fail" -ne 0 ]; then
  echo "::error::sandbox-selftest: landrun is NOT enforcing confinement — refusing to run PR code" >&2
  exit 1
fi
echo "::notice::sandbox-selftest: confinement proven (writes confined to ${wdir}; out-of-tree write, /dev/shm, and TCP egress all denied)"
