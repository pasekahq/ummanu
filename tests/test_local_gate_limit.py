"""The local gate's per-project limit, its timeout classes and the host's one heavy-check slot."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support.broad_check_fixture import BroadCheckTestCase, _git, _git_out
from ummanu.cli import main
from ummanu.dispatch.gate import LOCAL_GATE_DEFERRED, LOCAL_GATE_TIMEOUT, gate_check
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.types import GateTransportError, HostError
from ummanu.projects.integration_base import resolve_integration_base
from ummanu.runtime.heavy_check import host_slot, slot_path

SRC = Path(__file__).resolve().parents[1] / "src"
TASK = {"ref": "sample-7", "project": "sample", "workspace": {"base_branch": "main"}}


def _workspace(root: Path) -> Path:
    """A worker branch one commit ahead of an `origin` that carries `main`."""
    bare = root / "origin.git"
    bare.mkdir()
    _git_out(bare, "init", "--bare", "--initial-branch", "main")
    ws = root / "ws"
    ws.mkdir()
    _git_out(ws, "init", "--initial-branch", "main")
    _git_out(ws, "config", "user.name", "Test User")
    _git_out(ws, "config", "user.email", "test@example.invalid")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    _git_out(ws, "add", "README.md")
    _git_out(ws, "commit", "-m", "seed")
    _git_out(ws, "remote", "add", "origin", str(bare))
    _git_out(ws, "push", "origin", "main")
    _git_out(ws, "checkout", "-b", "pipeline/sample-7")
    (ws / "work.txt").write_text("work\n", encoding="utf-8")
    _git_out(ws, "add", "work.txt")
    _git_out(ws, "commit", "-m", "work")
    return ws


class _Catalog:
    def __init__(self, adapter: dict) -> None:
        self._adapter = adapter

    def adapter(self, project: str) -> dict:
        return self._adapter

    def integration_base(self, project: str, override: str | None) -> str:
        return resolve_integration_base(default_branch="main", declared=None, override=override)


class _Host(CommandHostRuntime):
    """The real host, with this round's admitted worker receipt naming `admitted_tree`."""

    def __init__(self, root: Path, adapter: dict, admitted_tree: str | None = None) -> None:
        super().__init__(_Catalog(adapter), root, mode="real")  # type: ignore[arg-type]
        self.admitted_tree = admitted_tree

    def worker_admission(self, task: dict, record) -> dict | None:
        return None if self.admitted_tree is None else {"tree_sha": self.admitted_tree}


class LocalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ws = _workspace(self.root)
        self.record = SimpleNamespace(workspace=str(self.ws))
        self.tree = _git_out(self.ws, "rev-parse", "HEAD^{tree}")

    def host(self, command: str, *, timeout: int | None = None, admitted_tree: str | None = None) -> _Host:
        validation: dict = {"ci": "local", "command": command}
        if timeout is not None:
            validation["timeout_seconds"] = timeout
        adapter: dict = {"validation": validation}
        if admitted_tree is not None:
            adapter["broad_check"] = {"module": "tests.broad"}
        return _Host(self.root, adapter, admitted_tree)

    def test_the_projects_timeout_bounds_the_local_gate_and_stays_charged_without_worker_evidence(self):
        started = time.monotonic()
        with self.assertRaises(HostError) as caught:
            gate_check(self.host("sleep 30", timeout=1), TASK, self.record)

        self.assertLess(time.monotonic() - started, 10)
        self.assertNotIsInstance(caught.exception, GateTransportError)
        self.assertIn("timed out after 1 second", str(caught.exception))
        self.assertIn("charged gate failure", str(caught.exception))

    def test_a_timeout_on_the_worker_green_tree_is_infrastructure(self):
        result = gate_check(self.host("sleep 30", timeout=1, admitted_tree=self.tree), TASK, self.record)

        self.assertEqual((result.status, result.failure_class, result.failure_reason),
                         ("red", "infrastructure", LOCAL_GATE_TIMEOUT))
        self.assertIn(self.tree[:12], result.summary)

    def test_a_timeout_on_a_tree_the_worker_never_passed_stays_a_charged_failure(self):
        with self.assertRaises(HostError) as caught:
            gate_check(self.host("sleep 30", timeout=1, admitted_tree="0" * 40), TASK, self.record)

        self.assertIn("charged gate failure", str(caught.exception))

    def test_a_second_heavy_check_is_deferred_while_the_first_holds_the_slot(self):
        host = self.host("touch ran")
        with host_slot(slot_path(self.root), "ummanu check broad in /elsewhere") as first:
            self.assertTrue(first.held)
            deferred = gate_check(host, TASK, self.record)

        self.assertEqual((deferred.status, deferred.failure_reason), ("pending", LOCAL_GATE_DEFERRED))
        self.assertIn("ummanu check broad in /elsewhere", deferred.summary)
        self.assertFalse((self.ws / "ran").exists(), "a deferred gate must not run its command")
        self.assertEqual(gate_check(host, TASK, self.record).status, "green")
        self.assertTrue((self.ws / "ran").exists())


class HostSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = slot_path(Path(temporary.name))

    def test_a_waiting_check_is_told_the_holder_and_runs_once_the_slot_is_free(self):
        told: list[str] = []
        acquired = threading.Event()

        def second() -> None:
            with host_slot(self.path, "second", on_busy=told.append) as slot:
                self.assertTrue(slot.held)
                acquired.set()

        with host_slot(self.path, "first"):
            waiter = threading.Thread(target=second)
            waiter.start()
            self.assertFalse(acquired.wait(0.3))
        waiter.join(5)

        self.assertTrue(acquired.is_set())
        self.assertEqual(told, [f"pid {os.getpid()}: first"])

    def test_a_check_nested_inside_the_holder_does_not_wait_on_it(self):
        with (
            host_slot(self.path, "gate") as outer,
            mock.patch.dict(os.environ, outer.env),
            host_slot(self.path, "nested broad check") as inner,
        ):
            self.assertTrue(inner.held)

    def test_a_killed_holder_frees_the_slot(self):
        script = (
            "import time\nfrom pathlib import Path\nfrom ummanu.runtime.heavy_check import host_slot\n"
            f"with host_slot(Path({str(self.path)!r}), 'crashing check'):\n"
            "    print('held', flush=True)\n    time.sleep(60)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONPATH": str(SRC)},
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with host_slot(self.path, "probe") as busy:
            self.assertFalse(busy.held)
            self.assertEqual(busy.holder, f"pid {holder.pid}: crashing check")

        holder.send_signal(signal.SIGKILL)
        holder.wait(5)

        with host_slot(self.path, "after the crash") as slot:
            self.assertTrue(slot.held)


class CheckBroadSlotTests(BroadCheckTestCase):
    def test_a_full_broad_check_waits_for_the_slot_without_running(self):
        data = Path(self.tmpdir.name) / "data"
        instance = Path(self.tmpdir.name) / "instance"
        (instance / "projects").mkdir(parents=True)
        (instance / "adapters").mkdir()
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: test\ndata_dir: {data}\n"
            "offsite:\n  instance_remote: git@example.invalid:owner/instance.git\n",
            encoding="utf-8",
        )
        (instance / "projects" / "example.yaml").write_text(
            f"id: example\nrepo: {self.root}\nadapter: example\nenabled: true\n", encoding="utf-8"
        )
        (instance / "adapters" / "example.yaml").write_text(
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\nvalidation:\n  ci: github\n"
            "artifact_policy:\n  write_project_files: false\n"
            "broad_check:\n  import_package: ummanu\n  module: project_suite\n",
            encoding="utf-8",
        )
        (self.root / "project_suite.py").write_text(
            "from pathlib import Path\nPath('suite-ran').write_text('yes')\n", encoding="utf-8"
        )
        (self.root / ".gitignore").write_text("/state/\n__pycache__/\n/suite-ran\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "add suite")
        codes: list[int] = []
        stderr = StringIO()
        argv = ["check", "broad", "--root", str(self.root), "--instance", str(instance)]

        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            with host_slot(slot_path(data), "local gate sample-7"):
                runner = threading.Thread(target=lambda: codes.append(main(argv)))
                runner.start()
                deadline = time.monotonic() + 5
                while "heavy-check slot" not in stderr.getvalue() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIn("held by pid", stderr.getvalue())
                self.assertIn("local gate sample-7", stderr.getvalue())
                self.assertFalse((self.root / "suite-ran").exists(), "the suite ran beside the holder")
            runner.join(30)

        self.assertEqual(codes, [0])
        self.assertTrue((self.root / "suite-ran").exists())


if __name__ == "__main__":
    unittest.main()
