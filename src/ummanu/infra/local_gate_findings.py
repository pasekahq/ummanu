"""Doctor findings for a `ci: local` gate command the candidate test-guard refuses before any test runs."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from ummanu.config import ConfigError, load_config

LOCAL_GATE_DIRECT_TEST_RUNNER = "local_gate_direct_test_runner"
_SEGMENT = re.compile(r"&&|\|\||[;|&\n]")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPED = '"${UMMANU_REPO:-$HOME/ummanu}/.venv/bin/ummanu" check broad'


def local_gate_findings(instance_dir: Path) -> list[dict[str, Any]]:
    """One finding per adapter whose `ci: local` validation command starts pytest or unittest itself.

    The dispatcher's local gate has no `ummanu check` ancestor, so the workspace test-guard exits 125.
    An unreadable adapter is the config check's finding, not this one.
    """
    findings = []
    adapters = instance_dir / "adapters"
    for path in sorted(adapters.glob("*.yaml")) if adapters.is_dir() else []:
        try:
            adapter = load_config(path)
        except ConfigError:
            continue
        validation = adapter.get("validation") if isinstance(adapter, dict) else None
        if not isinstance(validation, dict) or validation.get("ci") != "local":
            continue
        command = validation.get("command")
        runner = direct_test_runner(command) if isinstance(command, str) else None
        if runner is None:
            continue
        findings.append(
            {
                "code": LOCAL_GATE_DIRECT_TEST_RUNNER,
                "adapter": path.stem,
                "source": str(path),
                "message": (
                    f"adapter {path.stem}: validation.command runs {runner} directly; the local gate's"
                    f" test-guard refuses it with exit 125 before any test runs. Use ummanu check broad,"
                    f" spelled for the dispatcher's PATH as {_WRAPPED}"
                ),
            }
        )
    return findings


def direct_test_runner(command: str) -> str | None:
    """The runner (`pytest` or `unittest`) a shell command starts outside `ummanu check`, if any."""
    for segment in _SEGMENT.split(command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and _ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        program = Path(words[0]).name
        if program in {"pytest", "py.test"}:
            return "pytest"
        if program.startswith("python"):
            for flag, module in zip(words[1:], words[2:]):
                if flag == "-m" and module in {"pytest", "unittest"}:
                    return module
    return None
