# sorry-tracker — GitHub issues for Lean 4 `sorry`s

This script automates the process of finding `sorry` proof obligations in a Lean project, analyzing them with an LLM (any model, via [OpenRouter](https://openrouter.ai)), and creating detailed, context-rich GitHub issues.

It is designed to streamline the management of formal verification projects by converting unfinished proofs into actionable, well-documented tasks.

:warning: This tool has a unit test suite covering its detection, import-resolution, and duplicate-checking logic, but it is less battle-tested in deployment than the sibling review/summary GitHub Actions and may still have rough edges.

## How it Works

The script scans a Lean project for `sorry` statements. For each one it finds, it gathers a comprehensive context package, including:
1.  The content of the file with the `sorry`.
2.  The content of imported files found locally (including from dependencies like `Mathlib`), subject to the size caps noted below.
3.  The content of any academic papers or websites provided as URL references (PDFs are parsed by OpenRouter's file-parser plugin).

This rich context is then sent to the model via OpenRouter, which generates a detailed analysis and proof suggestion. Finally, the script creates a GitHub issue, complete with the AI's analysis, code snippets, and a link to the relevant line of code.

## Features

-   **Automated `sorry`/`admit` Detection**: Recursively scans a Lean project for `sorry` and `admit` proof obligations. Detection is comment- and string-aware — occurrences inside `--` line comments, nested `/- ... -/` block comments, and string literals (including multi-line ones) are ignored, and neither the `sorryAx` axiom nor primed identifiers like `sorry'` are mistaken for a `sorry`. Declaration names are recovered even through attributes (`@[simp]`), modifiers (`partial`, `noncomputable`, …), and indentation, giving each obligation a stable identity for cross-run de-duplication. Multiple obligations in one declaration are collapsed into a single issue.
-   **AI-Powered Analysis**: Uses any model via OpenRouter (selected by slug) to provide high-quality analysis for each proof obligation.
-   **Rich Context Generation**:
    -   **Local Dependencies**: Automatically finds and reads imported files from the project and its `.lake` dependencies (e.g., `Mathlib`). To keep prompts bounded, any single import of 25 KB or more is skipped, and once the combined imports for a file reach 300 KB the rest are omitted (so an `import Mathlib` line, which resolves to a huge aggregator file, simply contributes nothing rather than dominating the context).
    -   **External References**: Ingests content from user-provided URLs (PDFs or websites) to use as context.
-   **Efficient and Safe**:
    -   **Concurrent Processing**: Processes `sorry`s in parallel (bounded worker pool; API calls are further throttled by the provider).
    -   **Duplicate Prevention**: Intelligently checks for existing GitHub issues to avoid creating duplicates.
    -   **Dry Run Mode**: A `--dry-run` flag previews the `sorry`s the script finds completely offline — no LLM calls, no GitHub (`gh`) calls, and no import resolution.
-   **Highly Configurable**:
    -   Customize the issue labels, model slug, and target repository via command-line flags.

## Prerequisites

1.  The [uv](https://docs.astral.sh/uv/) package manager (it provisions the right Python automatically; the package requires Python ≥ 3.11).
2.  **GitHub CLI**: The `gh` command-line tool must be installed and authenticated. Run `gh auth login` to set it up.
3.  **OpenRouter API key**: Set `OPENROUTER_API_KEY` in your environment (get one at https://openrouter.ai/keys).
4.  **Project Dependencies**: Before running the script on a Lean project, ensure you have downloaded its dependencies by running `lake build` within the project's root directory.

## Installation

```bash
# Clone the leanrepo-utils repository (sorry-tracker lives in its sorry-tracker/ member)
git clone https://github.com/alexanderlhicks/leanrepo-utils.git
cd leanrepo-utils/sorry-tracker

# Sync dependencies (optional, checks setup)
uv sync
```

## Usage

The script is run from the command line, pointing it to the target repository you wish to analyze. Run it from the `sorry-tracker/` directory (or pass `--package sorry-tracker` to `uv run` from anywhere in the workspace).

```bash
uv run sorry-tracker --repo-path /path/to/your/lean/project [OPTIONS] [SEARCH_PATH]
```

### Arguments

-   `--repo-path` (required): The absolute or relative path to the root of the target git repository.
-   `search_path` (optional): The sub-directory within the repository to scan. Defaults to the entire repository.

### Options

-   `--dry-run`: Simulate the script's execution. It prints the `sorry`s it finds and exits — fully offline, with no LLM, `gh`, or network calls.
-   `--label LABEL`: The GitHub issue label to use. (Default: `proof wanted`).
-   `--model MODEL`: The OpenRouter model slug to use for analysis. (Default: `deepseek/deepseek-v4-pro`, open-weight; override with any slug).
-   `--reference-url URL`: A URL to a PDF or webpage to be used as context. This flag can be specified multiple times.

### Example Workflow

Imagine you are working on a project at `~/my-lean-project` and want to create issues for all `sorry`s in the `src/theorems` directory. You have a relevant research paper that provides context.

First, ensure the project's dependencies are downloaded:
```bash
cd ~/my-lean-project
lake build
cd -
```

Now, run the script:

```bash
export OPENROUTER_API_KEY=sk-or-...

uv run sorry-tracker \
  --repo-path ~/my-lean-project \
  --reference-url https://arxiv.org/pdf/reference-paper.pdf \
  src/theorems
```

The script will then:
-   Scan for `.lean` files only within the `src/theorems` sub-directory.
-   Fetch the content from the provided arXiv URL.
-   For each `sorry` found, it will:
    -   Find and read the locally imported files (from your project and its dependencies, within the size caps noted in Features).
    -   Call the model (via OpenRouter) with the combined context.
    -   Check if a similar issue already exists.
    -   Create a new GitHub issue with the AI-generated analysis.

## Development

This project uses [uv](https://docs.astral.sh/uv/):

```bash
uv run pytest tests/ -q   # unit tests (detection, import resolution, dedup, main orchestration)
uv run ruff check .       # lint
```

The code is organized into focused, independently testable modules:

| Module | Responsibility |
| --- | --- |
| `leanrepo_common.lean_utils` | Low-level Lean source parsing: comment/string-aware scanning and import resolution (shared `common/` package). |
| `detection.py` | Finds `sorry`/`admit` obligations and builds the `Sorry` model (incl. stable ids for dedup). |
| `references.py` | Fetches reference URLs (PDFs/web pages) as LLM content parts. |
| `analysis.py` | Gathers import context and prompts the LLM for a contributor-facing analysis. |
| `github_issues.py` | All `gh`/`git` interaction: repo metadata, duplicate detection, issue creation. |
| `leanrepo_common.llm_provider` | OpenRouter-backed LLM gateway (single, slug-selected provider; shared `common/` package). |
| `issues.py` | CLI entry point and orchestration (`main`, `process_sorries`). |

The pure logic (detection, import resolution, prompt assembly, dedup) is unit-tested
directly; the `gh` and OpenRouter calls are isolated so the orchestration can be
tested with mocks. Most modules have a matching `tests/test_<module>.py`
(`test_detection.py`, `test_analysis.py`, `test_github_issues.py`, `test_issues.py`);
reference fetching is currently exercised indirectly through the orchestration tests.

## License

This project is licensed under the [Apache License 2.0](../LICENSE).
