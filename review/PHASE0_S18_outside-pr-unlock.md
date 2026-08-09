# Phase 0 record — Session 18 (S2/S7 outside-PR review unlock)

Read-only prior-work review completed 2026-07-24 (PLAN gate `wf_69031544-d67`, 10 agents +
two research agents). This is the reference for Phase-2 implementation. **Gate verdict:
CONDITIONAL GO — mechanism only, no Phase-2 code until Gate-1 GO** (Gate-1 decisions recorded
in SESSIONS.md "Session 18"). Raw agent outputs preserved under the session task dir.

## Reframe (accepted by maintainer)

**S18-in-this-repo delivers MECHANISM ONLY; it does not by itself close the P0.** The
vulnerable single-job path lives in the fleet *caller* workflows (ArkLib/VCV-io deploy
branches), and a composite action cannot express a multi-job graph. So this session ships the
two-stage templates + hardened engine; the caller-workflow migration is a **named blocking
follow-on**. ROADMAP reads "mechanism landed, rollout pending" — never S2/S7 "DONE".

## Deliverable 1 — TauCeti reference architecture (`TauCetiProject/TauCeti@92c79e5`, Apache-2.0)

Files (content-pinned): `pr-build.yml` (sha256 `59b21466…`), `review.yml` (`5473f388…`),
`scripts/sandbox-build.sh` (`0bb3d8e3…`). Repo LICENSE Apache-2.0 with **no declared copyright
holder** (template placeholder), **no NOTICE** (so no §4(d) propagation), **no per-file
headers** — attribution = `TauCetiProject/TauCeti@92c79e5` + our own SPDX/change headers on
adapted copies. No hard stops.

Mechanisms to adopt:
- **Two-stage split.** `pr-build.yml` on `pull_request_target` (trusted base definition):
  checkout base credential-free + checkout PR head into a separate dir, then **overlay ONLY the
  PR's source dir** onto the trusted base (`rm -rf base/<src>; cp -a pr/<src> base/<src>`).
  lakefile/toolchain/scripts/config stay trusted-base.
- **Status-only handoff — NO artifacts.** State crosses as immutable head-SHA + commit
  *statuses* (`build`/`scope`/`bump-guard`) posted via `gh api …/statuses/<sha>`. `review.yml`
  fires on `workflow_run`, reads the status. **This eliminates the zip-slip / olean-poisoning /
  artifact-prompt-injection surfaces by construction** — the review stage re-derives from
  source, never consumes attacker-built oleans. Adopt this; do not pass artifacts.
- **Scope guard = allowlist over the trusted GitHub API file list** (`gh api …/pulls/<n>/files`),
  not the PR tree. Out-of-scope → human. Fail-closed on empty/unlistable/300-file-cap. Symlink
  rejection at overlay time (`find -type l` → error; mode-120000 target read). `lake-manifest.json`
  allowed only as a machine-validated forward bump.
- **Fork-safe `workflow_run` handoff.** Reads `pull_requests[0].number` but **falls back to
  `gh api commits/<head_sha>/pulls`** when empty (the fork case). PR identity derives from the
  trusted `head_sha`, never an untrusted artifact.
- **Keyless untrusted phase.** `pr-build.yml` holds no model key; only the default token scoped
  to `contents:read`+`statuses:write`. Least-privilege per-stage `permissions:` (NOT `{}` —
  each stage keeps only what it needs).
- **Token model: `secrets: inherit` into a pinned reusable workflow** — TauCeti uses **neither**
  a GitHub App **nor** a PAT for late-minting. Secrets enter only the privileged stage, after
  the untrusted build has already passed, via a commit-pinned reusable workflow. Adopt this
  (simpler than App/PAT; resolves Gate-1 dec 2).
- **`/review` chatops re-dispatches, never runs directly.** Auth *inside* the step (repo
  permission API + `author_association` fallback), exact-line `grep -qxE '[[:space:]]*/review[[:space:]]*'`,
  comment body via `env:` not inlined. Emits `pr` as an output feeding the gated reusable path.

## Deliverable 2 — sandbox comparison → **landrun** (aligned with lean-eval + TauCeti)

Verified environment facts (`ubuntu-latest` = Ubuntu 24.04, kernel 6.8/6.11):
- **Landlock is active at boot** (in default `CONFIG_LSM` since 5.15) — landrun works
  unprivileged, no root/boot-param.
- **Landlock network control is TCP-only** (ABI 4–6); UDP/DNS/QUIC/raw not restrictable until
  ABI 10 / kernel ~6.16 (absent on the runner). So landrun **cannot block all egress**.
- Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns=1` (blocks bwrap/nsjail
  unprivileged userns unless a `sudo sysctl` lifts it — runner has passwordless sudo).

| tool | block ALL egress | write-confine | root-free 24.04 | license |
| --- | --- | --- | --- | --- |
| **landrun** (chosen) | ⚠️ TCP-only (UDP/DNS escape) | ✅ best | ✅ cleanest | MIT |
| bubblewrap | ✅ `--unshare-net` | ✅ | one `sudo sysctl` | LGPL (wrap-only) |
| nsjail | ✅ netns + seccomp | ✅ | userns tweak | Apache-2.0 |
| container+gVisor | ✅ | ✅ | sudo install; Lean syscall-compat risk | Apache-2.0 |

**Decision: landrun.** Rationale — the bwrap advantage (block UDP/DNS) is only load-bearing if
a secret is present during untrusted execution, and the correct design removes the key from the
untrusted phase (env-allowlist, per lean-eval). Once the key isn't there, landrun's TCP-only
limit is a minor residual. The real egress risk — `review.py`'s own URL-fetch/web-search while
holding the key — is a separate process the Lean sandbox never covers (job-level egress control,
orthogonal to sandbox choice). Aligning with **two official-adjacent Lean projects
(leanprover/lean-eval + TauCeti)** buys ecosystem standardization + reusable probes.

**Reuse from `leanprover/lean-eval`** (its security model — env-allowlist + spawn-restriction,
NOT egress, as the primary secret defense):
- `env_dump_probe.py` — salts parent env with **decoy secrets**, asserts untrusted elaboration
  sees only an allowlist subset. This is our "real, fail-not-skip" secret-absence test.
- `sandbox_engaged_probe.py` — asserts landlock is enforcing at evaluation time.
- `artifact_tamper_probe.py` — spawned-daemon tamper test.
- **Spawn-restriction**: inside the sandbox only `lean` runs; `sh`/`bash`/`cp` → exit 255. Adopt.

Phase-2 landrun caveats: pin version + SHA256; **self-test must probe UDP** (not just TCP) and
**fail closed on unsupported host** (query landlock ABI first; abort, never run PR code
unconfined); redirect `TMPDIR`/`XDG_*` into a writable sandbox dir + grant read-only `~/.elan`
(a `.lake`-only allowlist breaks the Lean build); pre-create `.lake` before sandboxing.

## Deliverable 3 — current-posture audit (corrected)

> **Snapshot as of 2026-07-24 (pre-S18).** The holes this table records were closed in S18
> Phase-2a/S18r: seam 3 now passes `env=scrubbed_env()`, and `scrubbed_env` was converted from
> the denylist regex described below to the `_ENV_ALLOWLIST` allowlist (`lean_tools.py`). The
> table is preserved as the Phase-0 record of the *pre-S18* posture.

Four Lean-elaboration seams, all in the same job as the secret-bearing `run_review` step:

| # | seam | env today |
| --- | --- | --- |
| 1 | `lean_tools._run_lean` (`lake env lean --stdin`, model-directed) | `scrubbed_env()` |
| 2 | `lean_info_extractor.run_lean_command` (`lake env lean --stdin`) | `scrubbed_env()` |
| 3 | `lean_info_extractor.extract_diagnostics` (`lake env lean <pr file>`, l.235) | **NONE — inherits secrets** |
| 4 | `extract_axioms` → `#print axioms` (via seam 2; used by `coverage_matrix`) | `scrubbed_env()` |

Plus the `lake build` **workflow step** (`action.yml`) — the largest elaboration surface; only
a workflow-layer sandbox wraps it. Corrections vs. the original draft: **seam 3 is unscrubbed
today** (most-exposed), and **`scrubbed_env` is a DENYLIST regex**
(`TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL`) — any secret whose name doesn't match is
inherited → **convert to an allowlist** (lean-eval model). The three other subprocess sites
(`gh pr diff`, `pdftotext`) are not Lean elaboration.

## Additional Phase-1 (gate) findings folded into scope

- **Prompt-injection + egress**: artifact/build-log content, `additional_comments`, PR body flow
  into a secret-bearing agent that fetches URLs / web-searches. Egress-restrict the whole secret
  job (or resolve references in the tokenless stage) — the Lean sandbox does not cover this.
- **Ambient `GITHUB_TOKEN`**: dropped/scoped per stage, not left at default read/write.
- **Fail-LOUD prompt load must check INTEGRITY, not emptiness** (a truncated non-empty contract
  is a weakened defense) — sentinel/length/digest, gated at the POST boundary (not module import).
- **`/review` on a hostile fork PR** must be a no-build re-dispatch, or it bypasses the sandbox.
- **Tests**: the flagship properties (secret-absence, real sandbox denial) only run in live CI /
  a landlock kernel — sandbox/probe tests must **fail, not skip**, when the binary/kernel is
  absent; over-mocking (asserting the argv contains "landrun") proves nothing. `authorize.sh`
  needs **distinct exit codes** for DENY vs FAIL (not both `rc!=0`).
- **License**: three distinct TauCeti repos each verified at their own SHA; Apache §4(a)/(b)
  retain-all-notices + change-note on near-verbatim workflow/script copies; README template
  blocks are distributed derivatives needing embedded attribution too.
