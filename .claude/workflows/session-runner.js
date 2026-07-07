export const meta = {
  name: 'session-runner',
  description: 'Read-only analysis for one leanrepo-utils execution session. Runs the PLAN gate (Phase 0 prior-work review + Phase 1 adversarial plan review) or the REVIEW gate (Phase 3 adversarial diff review). It never mutates the repo — it hands findings to the human, who is the gate.',
  phases: [
    { title: 'Phase 0 — Prior-work review' },
    { title: 'Phase 1 — Plan review' },
    { title: 'Phase 3 — Adversarial diff review' },
  ],
}

// ---- inputs (via args) ----------------------------------------------------
// { session: "1" | "6" | ...,  items: "C1, C2",  phase: "plan" | "review",
//   base: "main" (review: branch to diff against),  extra: "free-text hints" }
// `args` may arrive as a real object OR as a JSON-encoded string (the tool layer
// stringifies it in some invocation paths); accept both so `phase`/`session`
// aren't silently lost and the review gate can't be misrouted to plan.
let A = args || {}
if (typeof A === 'string') { try { A = JSON.parse(A) } catch (_e) { A = {} } }
const REPO = '/home/alh/lscripts/leanrepo-utils'
const session = String(A.session ?? 'unspecified')
const items = String(A.items ?? '')
const gate = (A.phase === 'review') ? 'review' : 'plan'
const base = A.base ? String(A.base) : '' // '' => review uncommitted work; set => milestone range diff
const extra = String(A.extra ?? '')
const OPENROUTER_DOCS = 'https://openrouter.ai/docs/api/reference/overview'

const CONTEXT = `Repo: ${REPO}. Session "${session}" covers ROADMAP items: ${items || '(see SESSIONS.md)'}. `
  + `Authoritative specs: ${REPO}/ROADMAP.md and ${REPO}/SESSIONS.md. `
  + `Follow the SESSIONS.md protocol. ${extra ? 'Extra hints: ' + extra : ''}`

// ---- schemas --------------------------------------------------------------
const S_SPEC = { type: 'object', additionalProperties: true, required: ['scope', 'acceptanceCriteria'], properties: {
  scope: { type: 'string' },
  acceptanceCriteria: { type: 'array', items: { type: 'string' } },
  fileRefs: { type: 'array', items: { type: 'string' } },
  priorArt: { type: 'array', items: { type: 'string' } },
  llmTouching: { type: 'boolean' },
  reusesCode: { type: 'boolean' },
  notes: { type: 'string' } } }

const S_DEPS = { type: 'object', additionalProperties: true, required: ['allGreen', 'dependencies'], properties: {
  allGreen: { type: 'boolean' },
  dependencies: { type: 'array', items: { type: 'object', additionalProperties: true, properties: {
    session: { type: 'string' }, status: { type: 'string' },
    verifiedInCode: { type: 'boolean' }, problem: { type: 'string' } } } },
  drift: { type: 'string' } } }

const S_COMPLIANCE = { type: 'object', additionalProperties: true, required: ['ok'], properties: {
  ok: { type: 'boolean' },
  openrouter: { type: 'string' },        // specific docs section/params to honor, or "n/a"
  reuse: { type: 'array', items: { type: 'object', additionalProperties: true, properties: {
    source: { type: 'string' }, license: { type: 'string' }, compatible: { type: 'boolean' },
    attributionPlan: { type: 'string' } } } },
  blockers: { type: 'array', items: { type: 'string' } } } }

const S_PLAN = { type: 'object', additionalProperties: true, required: ['steps', 'tests'], properties: {
  steps: { type: 'array', items: { type: 'string' } },
  files: { type: 'array', items: { type: 'string' } },
  tests: { type: 'array', items: { type: 'string' } },
  edgeCases: { type: 'array', items: { type: 'string' } },
  openRisks: { type: 'array', items: { type: 'string' } },
  goNoGo: { type: 'string' } } }

const S_CRITIQUE = { type: 'object', additionalProperties: true, required: ['lens', 'issues'], properties: {
  lens: { type: 'string' },
  issues: { type: 'array', items: { type: 'object', additionalProperties: true, properties: {
    severity: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' } } } } } }

const S_FINDINGS = { type: 'object', additionalProperties: true, required: ['lens', 'findings'], properties: {
  lens: { type: 'string' },
  findings: { type: 'array', items: { type: 'object', additionalProperties: true, properties: {
    severity: { type: 'string' }, file: { type: 'string' }, line: { type: 'number' },
    summary: { type: 'string' }, evidence: { type: 'string' } } } } } }

// ==========================================================================
if (gate === 'plan') {
  phase('Phase 0 — Prior-work review')
  const [spec, deps, compliance] = await parallel([
    () => agent(`${CONTEXT}\nExtract the full spec for this session from ROADMAP.md/SESSIONS.md: scope, every acceptance criterion, file:line refs, prior-art sources, and whether it is LLM-touching or reuses code from another repo. Read the actual files.`,
      { phase: 'Phase 0 — Prior-work review', label: `spec:${session}`, schema: S_SPEC }),
    () => agent(`${CONTEXT}\nADVERSARIALLY verify this session's dependencies. Find its dependency sessions in SESSIONS.md, then for EACH re-check its ROADMAP acceptance criteria against the actual current code in ${REPO} — do NOT trust a DONE marker. Report any dependency that is missing, regressed, or has drifted, and whether the code state still matches the file:line refs this session assumes.`,
      { phase: 'Phase 0 — Prior-work review', label: `deps:${session}`, schema: S_DEPS }),
    () => agent(`${CONTEXT}\nCompliance pre-check. (1) If this session touches the OpenRouter LLM layer (cost/usage, timeouts, caching, model slugs, streaming, tool-calling, structured output), open ${OPENROUTER_DOCS}, drill into the relevant sub-page, and report the exact endpoint/parameters the implementation must honor. (2) If it adapts code from another repo (see the SESSIONS.md 'Reuse' flag / prior-art), identify the source repo + path + commit, read that repo's LICENSE, judge Apache-2.0 compatibility, and state the required attribution (header + NOTICE) — or recommend clean-room reimplementation if incompatible. Use WebFetch/ToolSearch as needed.`,
      { phase: 'Phase 0 — Prior-work review', label: `compliance:${session}`, schema: S_COMPLIANCE }),
  ])

  phase('Phase 1 — Plan review')
  const draft = await agent(`${CONTEXT}\nDraft a concrete execution plan to satisfy EVERY acceptance criterion. Context:\nSPEC=${JSON.stringify(spec)}\nDEPS=${JSON.stringify(deps)}\nCOMPLIANCE=${JSON.stringify(compliance)}\nList exact files to change, the steps, the tests (incl. edge cases, not just happy path), open risks, and a go/no-go recommendation.`,
    { phase: 'Phase 1 — Plan review', label: `plan:${session}`, schema: S_PLAN })

  const lenses = [
    'correctness & edge cases (find inputs that break it)',
    'security: prompt injection, untrusted PR input, secret exposure',
    'license & attribution compliance (is reuse legal and credited?)',
    'test strategy: over-mocking, uncovered edge cases, tests that assert nothing',
    'scope & blast radius: backward compatibility, drive-by changes, unintended coupling',
  ]
  const critiques = await parallel(lenses.map((lens) => () =>
    agent(`${CONTEXT}\nYou are an ADVERSARIAL reviewer. Try to break this plan through the lens of ${lens}. Be concrete and skeptical; assume the author was optimistic.\nPLAN=${JSON.stringify(draft)}\nSPEC=${JSON.stringify(spec)}`,
      { phase: 'Phase 1 — Plan review', label: `critique:${lens.split(':')[0].split(' ')[0]}`, schema: S_CRITIQUE })))

  const revised = await agent(`${CONTEXT}\nSynthesize a FINAL plan and the list of risks that MUST be resolved before any code is written. Fold in the adversarial critiques.\nDRAFT=${JSON.stringify(draft)}\nCRITIQUES=${JSON.stringify(critiques.filter(Boolean))}`,
    { phase: 'Phase 1 — Plan review', label: `plan-final:${session}`, schema: S_PLAN })

  return { session, items, gate: 'plan', spec, deps, compliance,
    plan: revised, critiques: critiques.filter(Boolean),
    humanGate: 'Approve this plan (and confirm deps are green + compliance ok) before Phase 2 coding.' }
}

// ==========================================================================
// review gate
phase('Phase 3 — Adversarial diff review')
const providedDiff = String(A.diff ?? '')
const diffInstruction = providedDiff
  ? `The diff to review is provided below between <diff> tags.\n<diff>\n${providedDiff}\n</diff>`
  : (base
      ? `Milestone review: run \`git -C ${REPO} diff ${base}...HEAD\` via Bash and review the whole range.`
      : `Per-session review of UNCOMMITTED work (progress lives locally; there is no PR yet): run \`git -C ${REPO} status --porcelain\` and \`git -C ${REPO} diff HEAD\` via Bash, and read any new/untracked files, so you review ALL pending changes before they are committed.`)

const reviewLenses = [
  'correctness & edge cases (construct failing inputs)',
  'security: injection, untrusted input, secret exposure',
  'license & attribution: reused code is compatible AND credited (header + NOTICE)',
  'test quality: tests exercise real behavior + Phase-1 edge cases, not just mocks',
  'acceptance-criteria & docs: every ROADMAP criterion met end-to-end; ROADMAP status/README/CHANGELOG/NOTICE/memory refreshed',
]
const reviews = await parallel(reviewLenses.map((lens) => () =>
  agent(`${CONTEXT}\nADVERSARIAL post-implementation review of session "${session}" through the lens of ${lens}. Your job is to BREAK the work, not bless it. ${diffInstruction}\nCite file:line and give a concrete failure scenario for each finding.`,
    { phase: 'Phase 3 — Adversarial diff review', label: `review:${lens.split(':')[0].split(' ')[0]}`, schema: S_FINDINGS })))

const verdict = await agent(`${CONTEXT}\nSynthesize the adversarial reviews into a single verdict for the human gate. Merge duplicates, rank by severity, and state clearly: BLOCKING findings (must fix before commit) vs non-blocking. Also confirm whether the documentation refresh and attribution are actually present.\nREVIEWS=${JSON.stringify(reviews.filter(Boolean))}`,
  { phase: 'Phase 3 — Adversarial diff review', label: `verdict:${session}`,
    schema: { type: 'object', additionalProperties: true, required: ['blocking', 'nonBlocking', 'docsRefreshed', 'attributionOk'], properties: {
      blocking: { type: 'array', items: { type: 'string' } },
      nonBlocking: { type: 'array', items: { type: 'string' } },
      docsRefreshed: { type: 'boolean' },
      attributionOk: { type: 'boolean' },
      recommendation: { type: 'string' } } } })

return { session, items, gate: 'review', reviews: reviews.filter(Boolean), verdict,
  humanGate: 'Resolve BLOCKING findings, run ruff + tests locally, then commit the session (PR only at a milestone).' }
