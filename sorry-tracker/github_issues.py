"""GitHub (`gh`) and git interactions.

Dependency checking, repo metadata, the HEAD commit SHA used to pin permalinks,
duplicate-issue detection, and issue creation. Issue creation is idempotent: a
hidden stable-id marker in the body lets a later run recognize and skip an
obligation it already filed.
"""

import json
import shutil
import subprocess
import sys


def check_dependencies() -> None:
    """Exit with a helpful message if the GitHub CLI ('gh') is not installed."""
    if not shutil.which("gh"):
        print("❌ Error: The GitHub CLI ('gh') is not installed or not in your PATH.", file=sys.stderr)
        print("   Please install it from https://cli.github.com/", file=sys.stderr)
        sys.exit(1)


def run_command(command: "list[str]") -> str:
    """Run a command and return its stdout, exiting the process on failure."""
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, encoding="utf-8"
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"❌ Error running command: {' '.join(command)}\n{e.stderr}", file=sys.stderr)
        sys.exit(1)


def current_commit_sha() -> str:
    """Return the target repo's HEAD commit SHA, or 'HEAD' if it can't be read.

    The permalink pins to this SHA so the line number stays valid even after the
    proof is completed and the file shifts. Best-effort: a detached/empty repo or
    missing git falls back to the moving 'HEAD' ref rather than aborting the run.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8",
        )
    except FileNotFoundError:
        return "HEAD"
    sha = (result.stdout or "").strip()
    return sha if result.returncode == 0 and sha else "HEAD"


def _find_existing_issue(id_comment: str, repo_name: str) -> "int | None":
    """Return the number of an open issue already tagged with id_comment, or None.

    Returns None both when no issue exists and when the lookup itself fails — a
    failed lookup must not abort the whole run (unlike run_command, which exits
    on a non-zero gh status). A failed check errs toward creating the issue.
    """
    search_query = f'"{id_comment}" in:body repo:{repo_name} is:open'
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--search", search_query, "--json", "number"],
            capture_output=True, text=True, encoding="utf-8",
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        print(
            f"⚠️  Could not check for existing issues (gh exit {result.returncode}); "
            f"proceeding. {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    out = (result.stdout or "").strip()
    if not out or out == "[]":
        return None
    try:
        issues = json.loads(out)
        return issues[0]["number"] if issues else None
    except (json.JSONDecodeError, KeyError, IndexError):
        return None


def create_github_issue(title: str, body: str, repo_name: str, label: str, stable_id: str) -> None:
    """Create a GitHub issue via the CLI, skipping if a stable-ID duplicate is already open."""
    id_comment = f"<!-- sorry-tracker-id: {stable_id} -->"

    existing = _find_existing_issue(id_comment, repo_name)
    if existing is not None:
        print(f"⚠️  Issue #{existing} already exists for '{stable_id}'. Skipping.")
        return

    full_body = f"{body}\n\n{id_comment}"
    command = [
        "gh", "issue", "create",
        "--title", title,
        "--body", full_body,
        "--label", label,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
        print(f"✅ Successfully created issue: '{title}'")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to create GitHub issue.\nTitle: {title}\nError: {e.stderr}", file=sys.stderr)
