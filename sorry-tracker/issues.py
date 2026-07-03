"""Command-line orchestration for sorry-tracker.

Scans a Lean project for `sorry`/`admit` obligations (detection.py), gathers
import + reference context and an LLM analysis for each (analysis.py,
references.py), and files a deduplicated GitHub issue per obligation
(github_issues.py).
"""

import argparse
import concurrent.futures
import os
import sys

from analysis import ImportContext, generate_ai_analysis
from detection import Sorry, find_all_sorries
from github_issues import (
    check_dependencies,
    create_github_issue,
    current_commit_sha,
    run_command,
)
from leanrepo_common.llm_provider import ContentPart, create_provider
from references import fetch_reference_parts

ISSUE_LABEL = "proof wanted"
# Open-weight default: strong reasoning for explaining a `sorry` and suggesting a
# real proof approach. Override with --model (any OpenRouter slug).
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
MAX_ANALYSIS_WORKERS = 8  # Cap on parallel analysis threads (API calls are further throttled by the provider).


def _issue_title(s: "Sorry") -> str:
    if s.decl_name:
        return f"Proof obligation for `{s.decl_name}` in `{s.file_path}`"
    return f"Proof obligation in `{s.file_path}` near line {s.line_num}"


def _issue_body(s: "Sorry", ai_analysis: str, repo_name: str, commit_ref: str) -> str:
    analysis_section = f"\n\n**🤖 AI Analysis:**\n{ai_analysis}" if ai_analysis else ""
    link_path = s.file_path.replace(os.sep, "/")
    return (
        f"A proof in `{s.file_path}` contains a `sorry`.{analysis_section}\n\n"
        f"**Goal:** Replace the `sorry` with a complete proof.\n\n"
        f"[Link to the sorry on GitHub](https://github.com/{repo_name}/blob/{commit_ref}/{link_path}#L{s.line_num})\n\n"
        f"**Code Snippet:**\n```lean\n{s.snippet}\n```"
    )


def process_sorries(
    sorries: "list[Sorry]",
    *,
    repo_name: str,
    reference_parts: "list[ContentPart]",
    provider,
    model: str,
    label: str,
    target_repo_path: str,
    commit_ref: str = "HEAD",
) -> None:
    """Analyze each obligation in parallel and file a GitHub issue for it."""
    imports = ImportContext(target_repo_path)

    max_workers = max(1, min(MAX_ANALYSIS_WORKERS, len(sorries)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Resolve import context on the main thread (the cache is not locked).
        future_to_sorry = {
            executor.submit(
                generate_ai_analysis,
                provider,
                model,
                s,
                imports.for_file(s.file_path, s.full_content),
                reference_parts,
            ): s
            for s in sorries
        }
        for future in concurrent.futures.as_completed(future_to_sorry):
            s = future_to_sorry[future]
            try:
                ai_analysis = future.result()
                create_github_issue(
                    _issue_title(s),
                    _issue_body(s, ai_analysis, repo_name, commit_ref),
                    repo_name,
                    label,
                    s.stable_id,
                )
            except Exception as exc:
                print(f"❌ Error processing {s.file_path}: {exc}", file=sys.stderr)


def _parse_args(argv: "list[str] | None" = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find 'sorry' statements in a Lean project and create GitHub issues."
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        help="The absolute or relative path to the root of the target git repository.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the script's execution without calling APIs or creating issues.",
    )
    parser.add_argument(
        "--label",
        default=ISSUE_LABEL,
        help=f"The GitHub issue label to use (default: '{ISSUE_LABEL}').",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter model slug for analysis (default: '{DEFAULT_MODEL}').",
    )
    parser.add_argument(
        "--reference-url",
        action="append",
        metavar="URL",
        help="A URL to a PDF or webpage to be used as context. Can be specified multiple times.",
    )
    parser.add_argument(
        "search_path",
        nargs="?",
        default=".",
        help="The sub-directory within the repository to scan (defaults to the entire repo).",
    )
    return parser.parse_args(argv)


def main() -> None:
    check_dependencies()
    args = _parse_args()

    target_repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(target_repo_path):
        print(f"❌ Error: Repository path not found at '{target_repo_path}'", file=sys.stderr)
        sys.exit(1)

    print(f"✅ Changing working directory to target repository: {target_repo_path}")
    os.chdir(target_repo_path)

    print(f"🔎 Scanning for 'sorry' statements in '{args.search_path}'...")
    print("----------------------------------------------------")

    sorries = find_all_sorries(args.search_path)
    if not sorries:
        print("✅ No 'sorry' statements found.")
        return

    if args.dry_run:
        # Fully offline: no gh calls, no network, no import resolution.
        print("DRY RUN: Would process the following sorries:")
        for s in sorries:
            print(f"  - {s.file_path}:{s.line_num} ({s.decl_name or 'task'})")
        return

    repo_name = run_command(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    print(f"✅ Detected repository: {repo_name}")
    commit_ref = current_commit_sha()

    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("API_KEY")
    if not api_key:
        print("❌ Error: OPENROUTER_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    provider = create_provider(api_key)

    reference_parts = fetch_reference_parts(args.reference_url or [])

    process_sorries(
        sorries,
        repo_name=repo_name,
        reference_parts=reference_parts,
        provider=provider,
        model=args.model,
        label=args.label,
        target_repo_path=target_repo_path,
        commit_ref=commit_ref,
    )

    print("----------------------------------------------------")
    print("🎉 All done! Named issues have been created.")


if __name__ == "__main__":
    main()
