# leanrepo-utils

[![CI](https://github.com/alexanderlhicks/leanrepo-utils/actions/workflows/ci.yml/badge.svg)](https://github.com/alexanderlhicks/leanrepo-utils/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Utilities for managing Lean 4 repositories: two composite GitHub Actions and a
CLI, sharing a single OpenRouter-backed LLM layer.

| Tool | What it does | Use as |
| --- | --- | --- |
| [`summary/`](summary/) | AI-generated summaries for Lean 4 pull requests (multi-agent pipeline: triage, per-file summaries, synthesis, optional title validation and instruction checks). | `uses: alexanderlhicks/leanrepo-utils/summary@main` |
| [`review/`](review/) | AI code review for Lean 4 pull requests: spec-grounded per-file review, cross-file analysis, dependent-impact pass, adversarial finding verification, real Lean toolchain access for agents. | `uses: alexanderlhicks/leanrepo-utils/review@main` |
| [`sorry-tracker/`](sorry-tracker/) | CLI that finds `sorry`/`admit` obligations in a Lean repo and opens detailed, LLM-analyzed GitHub issues for them. | `cd sorry-tracker && uv run sorry-tracker ...` |
| [`common/`](common/) | Shared library `leanrepo-common`: the OpenRouter LLM provider (`leanrepo_common.llm_provider`) and Lean 4 source utilities (`leanrepo_common.lean_utils`). | dependency of the three tools |

All LLM access goes through [OpenRouter](https://openrouter.ai): one API key,
models selected by slug (e.g. `anthropic/claude-opus-4.8`,
`deepseek/deepseek-v4-pro`), so any upstream provider can be used without code
changes.

## Quick start

Everything here needs one [OpenRouter API key](https://openrouter.ai/keys) —
it reaches Claude, Gemini, GPT, DeepSeek, and other models through a single
endpoint, so you never need per-provider credentials.

### PR summaries and AI review (GitHub Actions)

1. In the Lean repository you want to use this on, add the key as an Actions
   secret named `OPENROUTER_API_KEY`
   (**Settings → Secrets and variables → Actions → New repository secret**).
2. Add a workflow file to that repository:
   - **PR summaries:** copy the example workflow from
     [`summary/README.md`](summary/README.md#usage) to
     `.github/workflows/pr_summary.yml`.
   - **AI review:** copy the ready-made
     [`review/examples/ai-review.yml`](review/examples/ai-review.yml) to
     `.github/workflows/ai-review.yml`.
3. Open a pull request — the summary/review is posted as a PR comment. The
   review can also be re-run on demand by commenting `/review` on the PR.

Because the actions live in subdirectories of this repository, workflows
reference them with a path:

```yaml
- uses: alexanderlhicks/leanrepo-utils/summary@main   # or .../review@main
  with:
    github_token: ${{ secrets.GITHUB_TOKEN }}
    api_key: ${{ secrets.OPENROUTER_API_KEY }}
    ...
```

Each action's README documents every input:
[`summary/README.md`](summary/README.md) ·
[`review/README.md`](review/README.md). The review action defaults to
open-weight models (`deepseek/deepseek-v4-pro`, verified by `z-ai/glm-5.2`);
any OpenRouter slug can be substituted.

### sorry-tracker (CLI)

```bash
git clone https://github.com/alexanderlhicks/leanrepo-utils.git
cd leanrepo-utils/sorry-tracker

export OPENROUTER_API_KEY=sk-or-...
uv run sorry-tracker --repo-path /path/to/your/lean/project --dry-run
```

`--dry-run` previews the `sorry`s it finds, fully offline. Drop the flag to
generate LLM analyses and open GitHub issues (requires an authenticated
[GitHub CLI](https://cli.github.com/): `gh auth login`). See
[`sorry-tracker/README.md`](sorry-tracker/README.md) for all options.

## Requirements

- **Actions:** nothing to install — the actions set up
  [uv](https://docs.astral.sh/uv/) and Lean themselves on the runner. Your
  repository needs a working `lake build` (the review action builds the
  project to query the Lean toolchain).
- **CLI / development:** [uv](https://docs.astral.sh/uv/) only; it provisions
  the right Python automatically (packages require Python ≥ 3.11, pinned to
  3.12 via `.python-version`).

## Development

This repository is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/);
each tool is a member project with its own README and tests.

```bash
uv sync --all-packages        # one venv + one uv.lock at the workspace root

# Tests (per member, from its directory)
(cd common && uv run --no-sync pytest -q)
(cd sorry-tracker && uv run --no-sync pytest -q)
(cd summary && uv run --no-sync pytest -q)
(cd review && uv run --no-sync pytest -q)

uv run --no-sync ruff check . # lint everything
```

Dependency changes go in the member's `pyproject.toml`; re-lock with `uv lock`
at the root (the actions install with `--frozen`). CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff, the four
test suites, and validation of the two `action.yml` files on every PR.

## License

[Apache License 2.0](LICENSE).
