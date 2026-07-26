#!/usr/bin/env bash
# sandbox-build.sh — build the overlaid PR source under landrun, offline, writes confined
# to .lake. Runs INSIDE landrun (the build workflow invokes it as the fixed one-liner
# `cd base && exec bash scripts/sandbox-build.sh` after landrun sets up confinement).
#
# This is a TRUSTED base copy: the scope guard (scope_guard.sh) routes any PR that edits
# scripts/ to a human, so this script cannot be swapped out to escape the sandbox even
# though it elaborates the PR's (untrusted) overlaid source — which is the whole reason
# it runs under landrun.
#
# Adapted from TauCetiProject/TauCeti@92c79e5 scripts/sandbox-build.sh, licensed
# Apache-2.0, and generalised: the project-specific audits (axioms / module-system /
# lint) are left to the adopting repo — this template performs the confined build only.
# Keeping the landrun payload a fixed, comment-free one-liner (all prose lives here, in a
# file read as a script) is deliberate: a stray apostrophe in a `bash -c` comment can
# close the single-quoted argument early and redden every build. See ../../NOTICE.
#
# cwd on entry is the trusted base checkout, with the PR source already overlaid.
set -euxo pipefail

# Keep Lean/Lake temp writes inside the one landrun-writable dir (.lake); a default
# $TMPDIR outside it would hit EPERM under confinement and break the build.
export TMPDIR="$PWD/.lake/tmp"
mkdir -p "$TMPDIR"

# Build the overlaid source against the trusted base config. landrun keeps this offline
# and confines writes to .lake. `--iofail` (= --fail-level=info) enforces a SILENT build
# by exit code — a stray #check/#eval or a linter note fails it; drop the flag if your
# project legitimately logs above trace at build time.
lake build --iofail

# Adopting repos add their own soundness/lint audits below (all reading the COMPILED
# environment, so they run on the just-built overlaid source), e.g.:
#   lake exe axioms          # reject axioms outside {propext, Classical.choice, Quot.sound}
#   lake exe module-system   # enforce module-system opt-in
#   bash scripts/lint-env.sh # environment-lint ratchet
