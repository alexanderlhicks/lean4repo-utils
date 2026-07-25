"""Lean inspection tools for the review agents (CLI backend).

Gives agents a way to check claims against the real Lean toolchain instead of
guessing — "does this actually typecheck?", "does this lemma exist and what is
its type?", "what does this definition/lemma state?", "what axioms does it
depend on?". This kills the most common false-positive class: confident but
wrong claims about Lean semantics (e.g. "this won't typecheck" when CI builds it
fine).

Backend: `lake env lean --stdin`, the same mechanism as ``lean_info_extractor``.
The public surface — :meth:`LeanToolbox.specs` (OpenAI tool schemas) and
:meth:`LeanToolbox.run` — is a stable interface, so a richer ``lean-lsp-mcp``
backend (goal state, diagnostics, loogle/leansearch) can be slotted in later
without touching the agent-side wiring.

Safety: the Lean subprocess runs with secret-looking environment variables
scrubbed, so model-directed code cannot read credentials (e.g.
``#eval IO.getEnv "GITHUB_TOKEN"``) and smuggle them into the review output.
"""

import logging
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

# Environment ALLOWLIST for Lean subprocesses. `lake env lean` elaborates
# PR-controlled code, so the child must inherit ONLY known-safe infrastructure
# variables — never a credential. An allowlist (rather than the old secret-name
# denylist) is robust to renamed or novel secret vars (`OR_AUTH`, `GH_APP_PEM`,
# `BEARER`, …): a variable is inherited only if it is explicitly listed here, so
# an unrecognised name is dropped by default. Modelled on leanprover/lean-eval's
# env-allowlist probe. Add a name here only after confirming it carries no secret.
_ENV_ALLOWLIST = frozenset({
    # process / locale
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TZ", "TERM",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    # scratch dirs (redirected into the sandbox in CI)
    "TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    # Lean / Lake / elan toolchain
    "ELAN_HOME", "ELAN_TOOLCHAIN", "RUSTUP_HOME",
    "LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT", "LEAN_CC", "LEAN_ABORT_ON_PANIC",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    # CI marker (harmless; some tools branch on it)
    "CI", "GITHUB_ACTIONS",
})
# A var whose name starts with one of these is also allowed (Lake reads `LAKE_*`).
_ENV_ALLOWLIST_PREFIXES = ("LAKE_",)

# Secondary net: even an allowlisted / prefix-allowed name is dropped if it looks
# like a secret (e.g. a stray `LAKE_TOKEN` / `LAKE_AUTH`), so the `LAKE_` prefix rule
# can't admit one. Covers the credential-bearing name fragments, not just TOKEN/KEY.
_SECRET_ENV_RE = re.compile(
    r'(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|BEARER|PEM|PRIVATE|CERT|SIGNING)',
    re.IGNORECASE,
)

# Cap on a single tool's returned text, to keep tool results from bloating the
# conversation fed back to the model.
MAX_TOOL_OUTPUT_CHARS = 4000


def lean_available() -> bool:
    """True if the `lake` executable is on PATH (tools degrade to no-op otherwise)."""
    return shutil.which("lake") is not None


def scrubbed_env() -> Dict[str, str]:
    """A copy of the process env restricted to an allowlist of known-safe
    infrastructure variables (`_ENV_ALLOWLIST` / `_ENV_ALLOWLIST_PREFIXES`).

    `lake env lean` elaborates PR-controlled code (the imported module), so the
    child process must never inherit API keys or tokens: in the secret-bearing
    run-review step an elaboration-time exploit could otherwise read
    `API_KEY`/`GITHUB_TOKEN` (or a novel secret name the old denylist missed)
    straight from its environment. The allowlist inherits only recognised
    infra/toolchain vars, so a secret under any unlisted name is dropped by
    default. This is the default env for every Lean subprocess spawned by the
    review action (the model-directed toolbox here and the axiom/coverage/
    diagnostics extractors in lean_info_extractor). Landlock sandboxing of that
    Lean IO (S7) is applied on top of this by `sandbox_wrap`.
    """
    return {
        k: v for k, v in os.environ.items()
        if (k in _ENV_ALLOWLIST or k.startswith(_ENV_ALLOWLIST_PREFIXES))
        and not _SECRET_ENV_RE.search(k)
    }


_LANDRUN_BIN = "landrun"


class SandboxUnavailable(RuntimeError):
    """Raised when sandboxing is requested (LEANREPO_SANDBOX set) but the landrun
    binary is missing — we fail closed rather than run PR-controlled Lean unconfined."""


_SANDBOX_ON = frozenset({"1", "true", "yes", "on", "require", "enabled"})
_SANDBOX_OFF = frozenset({"", "0", "false", "no", "off", "disabled"})


def sandbox_enabled() -> bool:
    """Whether Lean elaboration should run under the landrun (Landlock) sandbox.

    Off by default (unset/empty) so local dev, unit tests, and CLI runs behave
    normally. The two-stage CI review job sets ``LEANREPO_SANDBOX`` to a truthy value
    for the secret-bearing phase, where every model-directed / PR-file ``lake env
    lean`` must be confined. An UNRECOGNISED non-empty value fails CLOSED (treated as
    enabled, with a warning) rather than silently disabling confinement — a security
    control must not become a no-op because of a value typo (``true`` vs ``1``)."""
    v = os.environ.get("LEANREPO_SANDBOX", "").strip().lower()
    if v in _SANDBOX_ON:
        return True
    if v in _SANDBOX_OFF:
        return False
    logging.warning(f"LEANREPO_SANDBOX={v!r} not recognised; treating as ENABLED (fail closed).")
    return True


def landrun_available() -> bool:
    return shutil.which(_LANDRUN_BIN) is not None


def sandbox_wrap(argv: List[str], write_dir: Optional[str] = None) -> List[str]:
    """Prefix ``argv`` with a landrun (Landlock) confinement invocation when
    sandboxing is enabled; return it unchanged when disabled.

    Confinement: the filesystem is read-only except the Lake build dir (``write_dir``
    / ``.lake``); no network flag is passed, so egress is denied by default (Landlock
    is TCP-only on the current runner kernel — UDP/DNS is a known, accepted residual,
    so the key is kept out of the phase by `scrubbed_env` + the key-file lifecycle, NOT
    by egress). When sandboxing is enabled but landrun is absent, raise
    `SandboxUnavailable` — PR-controlled Lean is never run unconfined once confinement
    is requested. In local/unit runs (`LEANREPO_SANDBOX` unset) this is a structural
    no-op returning ``argv`` unchanged.

    PROVISIONAL flag set: the exact landrun flags below are validated against a real
    landrun in the CI self-test built in S18-2b (with a positive control — an in-``.lake``
    write must SUCCEED — and the escape probes minus the unsatisfiable UDP-deny). The
    review-stage checkout must use ``persist-credentials: false`` so this ``--rox`` of
    the working tree cannot expose ``.git/config`` credentials to elaboration."""
    if not sandbox_enabled():
        return list(argv)
    if not landrun_available():
        raise SandboxUnavailable(
            f"LEANREPO_SANDBOX is set but '{_LANDRUN_BIN}' is not on PATH; "
            "refusing to elaborate PR-controlled Lean unconfined."
        )
    cwd = os.getcwd()
    wdir = write_dir or os.path.join(cwd, ".lake")
    wrapper = [_LANDRUN_BIN]
    for ro in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        wrapper += ["--rox", ro]
    wrapper += ["--rox", os.path.expanduser("~/.elan")]
    wrapper += ["--rox", cwd]        # source tree readable
    wrapper += ["--rwx", wdir]       # Lake build dir writable (more specific wins)
    wrapper += ["--"]                # no network flag => egress denied by default
    return wrapper + list(argv)


def _run_lean(command: str, module: Optional[str], timeout: int) -> str:
    """Run `command` through `lake env lean --stdin`, optionally importing
    `module` first so its declarations are in scope. Returns combined
    stdout+stderr (truncated), or a bounded error string on failure/timeout."""
    prelude = f"import {module}\n" if module else ""
    code = f"{prelude}{command}\n"
    try:
        result = subprocess.run(
            sandbox_wrap(["lake", "env", "lean", "--stdin"]),
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=scrubbed_env(),
        )
    except subprocess.TimeoutExpired:
        return f"(lean tool timed out after {timeout}s)"
    except (FileNotFoundError, OSError) as e:
        return f"(lean tool unavailable: {e})"
    out = (result.stdout + result.stderr).strip()
    if not out:
        return "(no output — elaborated with no messages)"
    return out[:MAX_TOOL_OUTPUT_CHARS]


class LeanToolbox:
    """Lean-inspection tools scoped to a `module` (the file under review, which
    is imported so its declarations are in scope).

    Toolbox interface used by the provider tool loop:
      * ``specs()`` -> list of OpenAI function-tool schemas
      * ``run(name, args)`` -> tool result text
    """

    def __init__(self, module: Optional[str] = None, timeout: int = 30):
        self.module = module
        self.timeout = timeout

    def specs(self) -> List[dict]:
        scope = f" (with `{self.module}` imported)" if self.module else ""
        return [
            {"type": "function", "function": {
                "name": "lean_check",
                "description": (
                    f"Run Lean `#check` on an expression{scope} to get its type, or an "
                    "error if it does not elaborate. Use to confirm a name exists and what "
                    "its type/signature is."
                ),
                "parameters": {"type": "object", "properties": {
                    "expr": {"type": "string", "description": "Expression or name to #check, e.g. 'List.map' or '(2 : Nat) + 2'."},
                }, "required": ["expr"]},
            }},
            {"type": "function", "function": {
                "name": "lean_print",
                "description": (
                    f"Run Lean `#print` on a declaration{scope} to see its definition or "
                    "statement. Use to confirm what a lemma actually states or what a "
                    "definition unfolds to."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Fully-qualified declaration name."},
                }, "required": ["name"]},
            }},
            {"type": "function", "function": {
                "name": "lean_print_axioms",
                "description": (
                    f"Run Lean `#print axioms`{scope} to list the axioms a declaration "
                    "depends on (e.g. detect `sorryAx`, `Classical.choice`)."
                ),
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "Fully-qualified declaration name."},
                }, "required": ["name"]},
            }},
            {"type": "function", "function": {
                "name": "lean_typecheck",
                "description": (
                    f"Elaborate a Lean code snippet{scope} and return diagnostics "
                    "(errors/warnings), or a note that it elaborated cleanly. Use to test "
                    "whether specific code actually compiles BEFORE claiming it does or does not."
                ),
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "description": "Lean code to elaborate; may reference declarations from the imported module."},
                }, "required": ["code"]},
            }},
        ]

    def run(self, name: str, args: dict) -> str:
        args = args or {}
        if name == "lean_check":
            return _run_lean(f"#check {args.get('expr', '')}", self.module, self.timeout)
        if name == "lean_print":
            return _run_lean(f"#print {args.get('name', '')}", self.module, self.timeout)
        if name == "lean_print_axioms":
            return _run_lean(f"#print axioms {args.get('name', '')}", self.module, self.timeout)
        if name == "lean_typecheck":
            return _run_lean(args.get("code", ""), self.module, self.timeout)
        return f"(unknown tool: {name})"
