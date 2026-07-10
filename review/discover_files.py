import subprocess
import json
import os
import sys

from leanrepo_common.lean_utils import file_path_to_module_name


def get_changed_lean_files(pr_number):
    # S1: pin to the base/head SHAs the worktree was checked out at (PR_BASE_SHA/
    # PR_HEAD_SHA) so the changed-file set matches the reviewed tree even under a
    # mid-run force-push; fall back to the (re-resolving) `gh pr diff` when unset.
    base_sha = (os.environ.get("PR_BASE_SHA") or "").strip()
    head_sha = (os.environ.get("PR_HEAD_SHA") or "").strip()
    if base_sha and head_sha:
        cmd = ["git", "diff", "--name-only", f"{base_sha}...{head_sha}"]
    else:
        cmd = ["gh", "pr", "diff", str(pr_number), "--name-only"]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        changed_files = [f.strip() for f in result.stdout.splitlines() if f.strip().endswith('.lean')]
        return changed_files
    except subprocess.CalledProcessError:
        # Do not echo raw stderr (may carry token-adjacent detail on a public log).
        print(f"::error::Failed to get changed files for PR #{pr_number}.")
        return []


def get_lean_module_name(file_path):
    """Converts a file path to a Lean module name."""
    return file_path_to_module_name(file_path)

def get_dependent_lean_files(changed_modules, lake_graph_json):
    dependent_modules = set()
    for module_info in lake_graph_json:
        module_name = module_info['name']
        if any(imp in changed_modules for imp in module_info.get('imports', [])) and module_name not in changed_modules:
            dependent_modules.add(module_name)
    return list(dependent_modules)

def get_dependency_lean_files(changed_modules, lake_graph_json):
    """Returns direct dependencies only (depth 1). Use get_transitive_dependencies for deeper traversal."""
    dependency_modules = set()
    for module_info in lake_graph_json:
        if module_info['name'] in changed_modules:
            dependency_modules.update(module_info.get('imports', []))
    return list(dependency_modules - changed_modules)


def get_transitive_dependencies(changed_modules, lake_graph_json, max_depth=2):
    """BFS to find transitive dependencies (what changed files import, recursively).
    Returns dict mapping module_name -> depth (1 = direct import, 2 = import-of-import, etc.)."""
    import_map = {m['name']: set(m.get('imports', [])) for m in lake_graph_json}

    visited = {}  # module -> depth
    frontier = set()

    # Seed: direct imports of changed files (depth 1)
    for module in changed_modules:
        for imp in import_map.get(module, []):
            if imp not in changed_modules:
                frontier.add(imp)
                visited[imp] = 1

    # BFS for depth 2..max_depth
    for depth in range(2, max_depth + 1):
        next_frontier = set()
        for module in frontier:
            for imp in import_map.get(module, []):
                if imp not in changed_modules and imp not in visited:
                    visited[imp] = depth
                    next_frontier.add(imp)
        frontier = next_frontier
        if not frontier:
            break

    return visited

def partition_context_tiers(final_file_list, changed_files, dep_files_with_depth, context_limit):
    """Split discovered files into full-context and summary-context tiers.

    Changed files are ordered first (they are the review target), then other
    files by depth-1 before depth-2+. The total full-context tier is
    hard-capped at `context_limit`; anything past the cap — including changed
    files on huge PRs — falls through to the summary tier (signatures only).

    Returns (full_context_files, summary_context_files), both lists of paths.
    """
    changed_set = set(changed_files)
    changed_first = [f for f in final_file_list if f in changed_set]
    others = [f for f in final_file_list if f not in changed_set]
    others.sort(key=lambda fp: (dep_files_with_depth.get(fp, 1), fp))
    all_ordered = changed_first + others
    return all_ordered[:context_limit], all_ordered[context_limit:]


def build_lean_file_index():
    index = []
    for root, dirs, files in os.walk('.'):
        parts = root.split(os.sep)
        if '.git' in parts or '__pycache__' in parts or '.lake' in parts:
            continue
        for f in files:
            if f.endswith('.lean'):
                path = os.path.normpath(os.path.join(root, f))
                if path.startswith(f".{os.sep}"):
                    path = path[2:]
                index.append(path)
    return index

def convert_module_to_file_path(module_name, index):
    expected_suffix = module_name.replace('.', os.sep) + '.lean'
    for path in index:
        if path.endswith(expected_suffix) or path == expected_suffix:
            return path
    return module_name.replace('.', os.sep) + ".lean"

def main():
    pr_number = os.environ.get('PR_NUMBER')
    if not pr_number:
         print("::error::PR_NUMBER environment variable is required.")
         sys.exit(1)

    changed_files = get_changed_lean_files(pr_number)
    changed_modules = {get_lean_module_name(f) for f in changed_files}

    all_relevant_files = set(changed_files)
    lake_graph_json = []
    dep_files_with_depth = {}  # file_path -> depth (for priority ordering)

    try:
        print("Attempting to generate Lake dependency graph...")
        lake_graph_output = subprocess.run(
            ['lake', 'exe', 'graph', '--json'],
            check=True,
            capture_output=True,
            text=True,
            timeout=300
        ).stdout
        lake_graph_json = json.loads(lake_graph_output)
        print("Successfully generated Lake dependency graph.")

        lean_files_index = build_lean_file_index()

        # Find files that depend ON our changed files (depth 1 only — deeper fans out too fast)
        dependent_modules = get_dependent_lean_files(changed_modules, lake_graph_json)
        dependent_files = {convert_module_to_file_path(m, lean_files_index) for m in dependent_modules}
        all_relevant_files.update(dependent_files)

        # Find transitive dependencies (what our files import, recursively)
        try:
            dep_depth = int(os.environ.get('DEPENDENCY_DEPTH', '2'))
        except ValueError:
            dep_depth = 2
        dep_with_depth = get_transitive_dependencies(changed_modules, lake_graph_json, max_depth=dep_depth)
        dep_files_with_depth = {}  # file_path -> depth
        for module, depth in dep_with_depth.items():
            fp = convert_module_to_file_path(module, lean_files_index)
            dep_files_with_depth[fp] = depth
        all_relevant_files.update(dep_files_with_depth.keys())

    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"::warning::Could not generate or parse Lake graph for full dependency analysis: {e}")
        print("::warning::Falling back to only changed files for context.")

    final_file_list = sorted([f for f in all_relevant_files if os.path.exists(f)])

    # Limit the number of full-context files
    try:
        CONTEXT_LIMIT = int(os.environ.get('CONTEXT_LIMIT', 50))
    except ValueError:
        CONTEXT_LIMIT = 50

    full_context_files, summary_context_files = partition_context_tiers(
        final_file_list, set(changed_files), dep_files_with_depth, CONTEXT_LIMIT,
    )

    if summary_context_files:
        print(f"::notice::Discovered {len(final_file_list)} files. {len(full_context_files)} with full context, {len(summary_context_files)} with summary context (type signatures only).")

    output_string = ','.join(full_context_files)
    summary_string = ','.join(summary_context_files)
    changed_string = ','.join(changed_files)

    print(f"::notice::Discovered files for review: {output_string}")
    if summary_string:
        print(f"::notice::Summary-context files: {summary_string}")

    # Serialize lake graph for downstream steps (avoids re-running lake exe graph)
    lake_graph_serialized = ""
    try:
        lake_graph_serialized = json.dumps(lake_graph_json)
    except Exception:
        pass

    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write(f"changed_files={changed_string}\n")
        f.write(f"discovered_files={output_string}\n")
        f.write(f"summary_files={summary_string}\n")
        f.write(f"lake_graph={lake_graph_serialized}\n")

if __name__ == "__main__":
    main()
