"""Static guards on deploy/deploy.sh.

`bash -n` is a SYNTAX checker, not a linker: it happily accepts a script that calls a function
nobody defines, or one whose body has been replaced by `return 0`. Both happened here.

Removing the Caddy support, a regex walked backwards from a comment and replaced `bring_up()`
— the function that runs `compose up` — with a no-op. The script passed `bash -n`, passed
review, deployed, ran every migration, and then waited 180 seconds for an app container that
was never created. These tests are what `bash -n` cannot do.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")

# Shell builtins and external commands a call-site scan will see and must not flag.
_NOT_FUNCTIONS = {
    "local",
    "return",
    "echo",
    "exit",
    "shift",
    "set",
    "readonly",
    "declare",
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "while",
    "do",
    "done",
    "case",
    "esac",
    "cd",
    "source",
    "trap",
    "printf",
    "sleep",
    "rm",
    "mkdir",
    "chmod",
    "ln",
    "mv",
    "cp",
    "cat",
    "grep",
    "sed",
    "awk",
    "test",
    "eval",
    "export",
    "unset",
    "wait",
    "true",
    "false",
    "docker",
    "curl",
    "timeout",
    "command",
    "date",
    "tr",
    "head",
    "tail",
    "sort",
    "uniq",
    "wc",
    "touch",
    "flock",
    "exec",
    "read",
    "continue",
    "break",
    "umask",
    "id",
    "hostname",
}


def _defined() -> set[str]:
    return set(re.findall(r"^([a-z_][a-z0-9_]*)\(\) \{", SOURCE, re.M))


def _body(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{(.*?)^\}}", SOURCE, re.S | re.M)
    assert match, f"{name}() is not defined in deploy.sh"
    return match.group(1)


def test_every_function_called_is_defined() -> None:
    """THE regression. Deleting `reload_caddy` left its call site behind; `bash -n` passed and
    the deploy would have died with `reload_caddy: command not found`."""
    defined = _defined()
    called = set(re.findall(r"^\s+([a-z_][a-z0-9_]*)(?:\s|$)", SOURCE, re.M))
    unknown = sorted(called - defined - _NOT_FUNCTIONS)
    suspicious = [c for c in unknown if "_" in c]  # our naming style, not a stray binary
    assert not suspicious, f"called but never defined: {suspicious}"


def test_bring_up_actually_starts_the_stack() -> None:
    """The one that cost a failed production deploy: `bring_up` was replaced by `return 0`,
    so migrations ran, nothing started, and the script waited 180s for a container that did
    not exist."""
    body = _body("bring_up")
    assert "compose up" in body, "bring_up must run `compose up` — a no-op here deploys nothing"


def test_the_deploy_path_brings_the_stack_up_and_records_the_tag() -> None:
    body = _body("cmd_deploy")
    for required in ("pull_image", "bring_up", "save_state"):
        assert required in body, f"cmd_deploy must call {required}"


def test_rollback_refuses_without_a_previous_tag() -> None:
    """A first deploy has nothing to roll back to. Saying so plainly beats a confusing
    secondary failure that masks the real one — which is exactly what it did here."""
    body = _body("do_rollback")
    assert "PREVIOUS_TAG" in body and "die" in body


def test_no_reverse_proxy_is_managed_by_this_script() -> None:
    """The host's nginx serves seven other sites. A deploy that reloads or binds :80/:443
    would take them down."""
    msg = "this stack ships no proxy; the host nginx owns :80/:443"
    assert "caddy" not in SOURCE.lower(), msg
    assert ":80:" not in SOURCE and ":443:" not in SOURCE


def test_the_script_is_executable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "deploy.yml invokes it directly"


@pytest.mark.parametrize("guard", ["set -euo pipefail"])
def test_it_fails_fast(guard: str) -> None:
    assert guard in SOURCE
