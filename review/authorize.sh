#!/usr/bin/env bash
# Authorize a /review trigger (S18/S2, the deferred S17 item (a)): decide whether an
# actor may run the review, with DISTINCT exit codes so the workflow can tell an
# infrastructure failure apart from a legitimate denial (never conflating them, and
# never failing OPEN).
#
# Adapted from TauCetiProject/TauCeti@92c79e5 .github/workflows/review.yml (the
# authorize step), licensed Apache-2.0. Extracted as a standalone, unit-testable
# script (the resolve_pr_head.sh pattern) and hardened per the S18 PLAN gate:
# distinct exit codes (API-error != deny), 404-non-collaborator treated as a
# definitive deny (not fail-closed), an exact-line /review match, and actor/comment
# read from the ENVIRONMENT (never interpolated into the script) so a crafted login
# or comment body cannot inject shell. See ../NOTICE.
#
# Usage: authorize.sh <owner/repo>
#   Environment (all data, never code):
#     GH_TOKEN       — the API token (read-only permission check).
#     ACTOR          — the actor login to authorize (required).
#     ASSOC          — the webhook's author_association (trusted fallback; optional).
#     COMMENT_BODY   — when set, require an EXACT `/review` command line first.
#   Exit codes:
#     0  AUTHORIZED   — actor has write+ (permission API) or a privileged association
#     1  DENIED       — determined non-privileged (API reachable / 404 non-collaborator)
#     2  FAIL_CLOSED  — could not determine (network/5xx/auth) and association did not rescue
#     3  NOT_COMMAND  — COMMENT_BODY set but contains no exact `/review` line (no-op)
#     4  USAGE        — malformed invocation (empty/invalid repo or actor)
set -euo pipefail

repo="${1-}"
repo="${repo//[[:space:]]/}"
actor="${ACTOR-}"
actor="${actor//[[:space:]]/}"
assoc="${ASSOC-}"

if [ -z "$repo" ]; then echo "::error::authorize: empty repository" >&2; exit 4; fi
if [ -z "$actor" ]; then echo "::error::authorize: empty ACTOR" >&2; exit 4; fi
# A GitHub login is [A-Za-z0-9-]; reject anything else BEFORE it reaches the API path
# so it cannot address a different collaborators/<x>/permission object.
case "$actor" in *[!A-Za-z0-9-]*) echo "::error::authorize: ACTOR has invalid characters: '${actor}'" >&2; exit 4;; esac

# Exact-command gate (issue_comment path): SOME line of the comment must be exactly
# "/review" (optional surrounding whitespace). "/review" mentioned in prose does not
# trigger. COMMENT_BODY is data on stdin to grep — never expanded by the shell.
if [ "${COMMENT_BODY+set}" = set ]; then
  if ! grep -qxE '[[:space:]]*/review[[:space:]]*' <<<"${COMMENT_BODY}"; then
    echo "::notice::authorize: no exact /review command line — skipping"; exit 3
  fi
fi

# A privileged webhook association is a trusted signal (the payload is GitHub's, not the
# actor's) and is the fallback when the permission API cannot be consulted.
assoc_privileged() { case "$assoc" in OWNER|MEMBER|COLLABORATOR) return 0 ;; *) return 1 ;; esac; }

# Permission API: the effective repo role, independent of org-membership visibility.
api_failed=0
perm=""
errf=$(mktemp)
trap 'rm -f "$errf"' EXIT
if perm=$(gh api "repos/${repo}/collaborators/${actor}/permission" --jq '.permission' 2>"$errf"); then
  :
else
  # gh failed: a 404 means the actor is NOT a collaborator — a DEFINITIVE "no repo
  # permission", not an infra failure. Anything else (network, 5xx, auth/scope) means
  # we could not determine the permission -> must fail closed.
  if grep -qiE 'HTTP 404|Not Found' "$errf"; then
    perm="none"
  else
    api_failed=1
  fi
fi

if [ "$api_failed" -eq 1 ]; then
  # Could not determine via API. Fall back to the trusted association; if that does not
  # confirm privilege, FAIL CLOSED (distinct from a definitive deny).
  if assoc_privileged; then echo "::notice::authorize: AUTHORIZED via association (permission API unavailable)"; exit 0; fi
  echo "::warning::authorize: permission API unavailable and association not privileged — failing closed" >&2
  exit 2
fi

case "$perm" in
  admin|write|maintain) echo "::notice::authorize: AUTHORIZED (repo permission: ${perm})"; exit 0 ;;
esac
if assoc_privileged; then echo "::notice::authorize: AUTHORIZED via association (${assoc})"; exit 0; fi
echo "::notice::authorize: DENIED (permission: ${perm:-none}, association: ${assoc:-none})"
exit 1
