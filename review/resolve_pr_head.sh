#!/usr/bin/env bash
# Resolve a pull request's HEAD and BASE commit SHAs via the GitHub API, fail-closed.
#
# Why this exists (S1): on the `issue_comment` (/review) path there is NO
# `github.event.pull_request`, so the composite action cannot learn the PR head from
# the event. Without an explicit ref, actions/checkout lands on the BASE branch and the
# reviewer reads the WRONG code (full-file reads, escape-hatch scans, `lake build`,
# lean_tools all run on base) — silently wrong, not failing. We resolve the immutable
# HEAD sha from pr_number and check that out; BASE is resolved too so the diff/discovery
# can be pinned to the SAME two SHAs (closes the resolve→checkout→diff TOCTOU).
#
# Usage: resolve_pr_head.sh <owner/repo> <pr_number>
#   reads GH_TOKEN from the environment; appends head_sha=/base_sha= to $GITHUB_OUTPUT.
# Fail-closed: any empty/null/whitespace input or unresolved SHA exits non-zero — it
# must NEVER fall through to a base-branch checkout.
set -euo pipefail

repo="${1-}"
pr="${2-}"

# Trim whitespace; reject empty (an unset input arrives as '' — must not silently pass).
repo="${repo//[[:space:]]/}"
pr="${pr//[[:space:]]/}"
if [ -z "$repo" ]; then echo "::error::resolve_pr_head: empty repository" >&2; exit 1; fi
if [ -z "$pr" ]; then echo "::error::resolve_pr_head: empty PR number" >&2; exit 1; fi
# Defence-in-depth: a PR number is an integer. Reject anything else BEFORE it reaches
# the gh api path (so a value with path separators can't address a different object,
# e.g. if a future/less-trusted caller ever sources pr_number from a loose parser).
case "$pr" in *[!0-9]*) echo "::error::resolve_pr_head: PR number must be numeric, got '${pr}'" >&2; exit 1;; esac

# One API call; emit "<head_sha> <base_sha>" so both are pinned from a single snapshot.
line="$(gh api "repos/${repo}/pulls/${pr}" --jq '"\(.head.sha) \(.base.sha)"')"
read -r head_sha base_sha <<<"$line"

for pair in "head:$head_sha" "base:$base_sha"; do
  name="${pair%%:*}"; val="${pair#*:}"
  case "$val" in
    ""|null) echo "::error::resolve_pr_head: could not resolve ${name} SHA for PR #${pr}" >&2; exit 1;;
  esac
done

: "${GITHUB_OUTPUT:?resolve_pr_head: GITHUB_OUTPUT not set}"
{
  echo "head_sha=${head_sha}"
  echo "base_sha=${base_sha}"
} >> "${GITHUB_OUTPUT}"
echo "resolve_pr_head: PR #${pr} head=${head_sha} base=${base_sha}" >&2
