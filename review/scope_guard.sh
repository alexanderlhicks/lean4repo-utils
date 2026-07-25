#!/usr/bin/env bash
# Fail-closed scope guard (S18/S2): decide whether a pull request is safe to
# auto-build-and-review or must route to a human.
#
# Adapted from TauCetiProject/TauCeti@92c79e5 .github/workflows/pr-build.yml
# (the "Scope guard" step), licensed Apache-2.0. Extracted here as a standalone,
# unit-testable script (the resolve_pr_head.sh pattern) and hardened per the S18
# PLAN gate: enumerate from the TRUSTED GitHub API file list (never the PR tree),
# an ALLOWLIST (so a nested/sub-package lakefile or lean-toolchain at ANY depth is
# out-of-scope by default), and a >3000-file cap that fails closed. See ../NOTICE.
#
# Why: under the two-stage split, only the PR's <src>/ dir is overlaid onto the
# trusted base. Anything the PR touches OUTSIDE that dir (lakefile, scripts/,
# .github/, a nested build config) could alter the build config or the sandbox
# script, so it must not be auto-built — it routes to a human. `lake-manifest.json`
# and `lean-toolchain` are permitted only as a machine-validated forward bump
# (scope=bump); the bump validation itself is a separate trusted step.
#
# Usage: scope_guard.sh <owner/repo> <pr_number> <src_dir>
#   reads GH_TOKEN from the environment; writes exactly one `scope=<decision>` to
#   $GITHUB_OUTPUT, where <decision> is one of:
#     in_scope     — only <src>/ (and <src>.lean) changed: safe to sandbox-build
#     bump         — only Lake pins changed: validate as a forward bump, then build
#     out_of_scope — touches paths outside the overlay: route to a human
#     infra        — cannot determine the change set: route to a human (fail closed)
# Fail-closed: any unresolved/empty/oversized/errored listing yields `infra` (human);
# a malformed CLI invocation (empty or non-numeric args) exits non-zero and writes
# nothing, so the workflow cannot fall through to an unguarded build.
set -euo pipefail

repo="${1-}"
pr="${2-}"
src="${3-}"

# Trim whitespace; reject empty/malformed inputs (an unset input arrives as '').
repo="${repo//[[:space:]]/}"
pr="${pr//[[:space:]]/}"
src="${src%/}"; src="${src//[[:space:]]/}"
if [ -z "$repo" ]; then echo "::error::scope_guard: empty repository" >&2; exit 2; fi
if [ -z "$pr" ]; then echo "::error::scope_guard: empty PR number" >&2; exit 2; fi
if [ -z "$src" ]; then echo "::error::scope_guard: empty <src_dir>" >&2; exit 2; fi
case "$pr" in *[!0-9]*) echo "::error::scope_guard: PR number must be numeric, got '${pr}'" >&2; exit 2;; esac
# <src> becomes part of a regex: allow only a safe path token so it can't inject
# alternation/anchors that would widen the allowlist.
case "$src" in *[!A-Za-z0-9_/.-]*) echo "::error::scope_guard: <src_dir> has unsafe characters: '${src}'" >&2; exit 2;; esac

emit() { echo "scope=$1" >> "$GITHUB_OUTPUT"; echo "::notice::scope_guard: $1"; exit 0; }

# The TRUSTED file list (GitHub's own view of the PR), not the PR working tree — an
# attacker cannot hide a path here. --paginate walks all pages (capped at 3000 files).
# Explicit fail-closed on a gh error: `set -e` does not reliably abort on a failed
# `files=$(...)` substitution (it can capture partial stdout + a non-zero status), so
# a 404/auth/network failure must route to a human here, never fall through.
if ! files=$(gh api --paginate "repos/${repo}/pulls/${pr}/files" --jq '.[].filename'); then
  echo "::warning::scope_guard: gh api failed to list PR files — routing to human" >&2; emit infra
fi
n=$(printf '%s\n' "$files" | grep -c . || true)

# Fail closed on an empty or capped listing: a truncated list could hide an
# out-of-scope path, so route to a human rather than assume in-scope.
if [ -z "$files" ] || [ "$n" -eq 0 ]; then
  echo "::warning::scope_guard: empty/unlistable change set — routing to human" >&2; emit infra
fi
if [ "$n" -ge 3000 ]; then
  echo "::warning::scope_guard: change set hit the 3000-file API cap — failing closed" >&2; emit infra
fi

# Allowlist over the trusted list. Anything NOT matching (including a nested
# lakefile/lean-toolchain at any depth, scripts/**, .github/**) => out of scope.
allow="^${src}/|^${src}\.lean$|^lake-manifest\.json$|^lean-toolchain$"
if printf '%s\n' "$files" | grep -vqE "$allow"; then
  echo "::warning::scope_guard: PR touches paths outside ${src}/ + Lake pins — needs human review" >&2
  emit out_of_scope
fi
# Everything is in the allowlist. If any Lake-pin file changed, it is a bump (validate
# separately as forward-only before building); otherwise pure <src>/ changes.
if printf '%s\n' "$files" | grep -qE '^lake-manifest\.json$|^lean-toolchain$'; then
  emit bump
fi
emit in_scope
