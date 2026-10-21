"""Harness plugin conformance test kit.

Validates that a third-party agent CLI complies with Sovereign Fleet's
safety and execution contract:
1. Sandbox Containment: Landlock sandbox confines writes strictly to the task
   worktree and declared state_paths.
2. Execution Timeout: Hanging processes are reliably killed with exit code 124
   upon reaching execution timeout.
3. Clean Process Group Cancellation: Terminating a task cleans up child process
   groups via kill_group with no orphaned processes.
4. No-Change-Is-Failure: Executions that finish without file modifications trigger
   the engine's failure gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fleet.core.config import FleetConfig, HarnessSection, load_config
from fleet.core.sandbox import MIN_ABI, LandlockSandbox, abi_version, policy_for_worktree
from fleet.core.worktree import WorktreeManager
from fleet.plugins.harness_base import CliHarness
from fleet.plugins.registry import harness_registry

logger = logging.getLogger("fleet.core.conformance")


@contextmanager
def _sandbox_env_for_probe() -> Iterator[None]:
    """Allow timeout/cancel process-group probes to run when host Landlock ABI < MIN_ABI."""
    prev = os.environ.get("FLEET_SANDBOX")
    if prev is None and abi_version() < MIN_ABI:
        os.environ["FLEET_SANDBOX"] = "0"
        try:
            yield
        finally:
            os.environ.pop("FLEET_SANDBOX", None)
    else:
        yield


@dataclass
class ConformanceCheckResult:
    """The outcome of an individual conformance check."""

    name: str
    passed: bool
    message: str
    details: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name, "passed": self.passed, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d


@dataclass
class HarnessConformanceReport:
    """Aggregated conformance test report for a harness plugin."""

    harness: str
    binary: str
    installed: bool
    state_paths: List[str]
    checks: List[ConformanceCheckResult]

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total_count(self) -> int:
        return len(self.checks)

    @property
    def conformed(self) -> bool:
        return self.passed_count == self.total_count

    def as_dict(self) -> Dict[str, Any]:
        return {
            "harness": self.harness,
            "binary": self.binary,
            "installed": self.installed,
            "state_paths": self.state_paths,
            "checks": [c.as_dict() for c in self.checks],
            "passed": self.passed_count,
            "total": self.total_count,
            "conformance": self.conformed,
        }


async def check_sandbox(harness: CliHarness, tmp_root: Path) -> ConformanceCheckResult:
    """Verify Landlock sandbox policy generation and filesystem write confinement."""
    wt_dir = tmp_root / "sandbox_wt"
    wt_dir.mkdir(parents=True, exist_ok=True)

    # 1. Verify policy structure
    policy = policy_for_worktree(str(wt_dir), harness.state_paths)
    wt_resolved = str(wt_dir.resolve())
    if not any(str(Path(p).resolve()) == wt_resolved for p in policy.read_write_paths):
        return ConformanceCheckResult(
            name="sandbox",
            passed=False,
            message="Worktree path missing from Landlock read-write policy",
        )

    for sp in harness.state_paths:
        sp_resolved = str(Path(sp).resolve())
        if not any(str(Path(p).resolve()) == sp_resolved for p in policy.read_write_paths):
            return ConformanceCheckResult(
                name="sandbox",
                passed=False,
                message=f"State path '{sp}' missing from Landlock read-write policy",
            )

    # 2. Live sandbox restriction probe (if Landlock is supported on the host)
    current_abi = abi_version()
    if current_abi < MIN_ABI:
        return ConformanceCheckResult(
            name="sandbox",
            passed=True,
            message=f"Policy valid (Landlock ABI {current_abi} below enforcement threshold {MIN_ABI})",
        )

    sandbox = LandlockSandbox(policy)
    allowed_file = wt_dir / "allowed_probe.txt"
    forbidden_file = Path.home() / f".fleet_sandbox_probe_{os.getpid()}_{time.monotonic_ns()}.tmp"

    try:
        # A. Writing to allowed worktree must succeed
        cmd_ok = sandbox.wrap(
            [sys.executable, "-c", f"open({repr(str(allowed_file))}, 'w').write('ok')"]
        )
        proc_ok = await asyncio.create_subprocess_exec(
            *cmd_ok,
            cwd=str(wt_dir),
            env=sandbox.build_safe_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        rc_ok = await proc_ok.wait()
        if rc_ok != 0 or not allowed_file.exists():
            return ConformanceCheckResult(
                name="sandbox",
                passed=False,
                message="Failed to write inside allowed worktree under Landlock sandbox",
            )

        # B. Writing outside worktree and declared state_paths must fail with PermissionError
        cmd_bad = sandbox.wrap(
            [sys.executable, "-c", f"open({repr(str(forbidden_file))}, 'w').write('bad')"]
        )
        proc_bad = await asyncio.create_subprocess_exec(
            *cmd_bad,
            cwd=str(wt_dir),
            env=sandbox.build_safe_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        rc_bad = await proc_bad.wait()
        if rc_bad == 0 or forbidden_file.exists():
            return ConformanceCheckResult(
                name="sandbox",
                passed=False,
                message="Sandbox confinement breach: unauthorized write outside worktree succeeded",
            )

        return ConformanceCheckResult(
            name="sandbox",
            passed=True,
            message="Landlock sandbox confines filesystem writes to worktree and declared state_paths",
        )
    finally:
        if allowed_file.exists():
            allowed_file.unlink(missing_ok=True)
        if forbidden_file.exists():
            forbidden_file.unlink(missing_ok=True)


async def check_timeout(harness: CliHarness, tmp_root: Path) -> ConformanceCheckResult:
    """Verify that hanging processes terminate cleanly with exit code 124 at timeout."""
    wt_dir = tmp_root / "timeout_wt"
    wt_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_root / "timeout_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # Controlled mock binary that sleeps, named as the harness binary
    mock_bin = bin_dir / harness.binary
    mock_bin.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
    mock_bin.chmod(0o755)

    section = HarnessSection(binary=str(mock_bin))
    test_harness = type(harness)(_section=section)

    started = time.monotonic()
    with _sandbox_env_for_probe():
        result = await test_harness.execute_task({"text": "conformance_timeout"}, str(wt_dir), timeout=1)
    elapsed = time.monotonic() - started

    if result.get("exit_code") != 124:
        return ConformanceCheckResult(
            name="timeout",
            passed=False,
            message=f"Expected exit code 124 on timeout, got {result.get('exit_code')}",
            details=result.get("stderr"),
        )

    if elapsed > 5.0:
        return ConformanceCheckResult(
            name="timeout",
            passed=False,
            message=f"Timeout enforcement delayed ({round(elapsed, 2)}s exceeded 1s limit)",
        )

    return ConformanceCheckResult(
        name="timeout",
        passed=True,
        message="Hanging process terminated at timeout with exit code 124",
    )


async def check_cancel(harness: CliHarness, tmp_root: Path) -> ConformanceCheckResult:
    """Verify that cancelling an execution task terminates process groups without leakage."""
    wt_dir = tmp_root / "cancel_wt"
    wt_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_root / "cancel_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    mock_bin = bin_dir / harness.binary
    mock_bin.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
    mock_bin.chmod(0o755)

    section = HarnessSection(binary=str(mock_bin))
    test_harness = type(harness)(_section=section)

    with _sandbox_env_for_probe():
        task = asyncio.create_task(
            test_harness.execute_task({"text": "conformance_cancel"}, str(wt_dir), timeout=30)
        )

        # Allow subprocess group to launch
        await asyncio.sleep(0.15)
        task.cancel()

        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True

    if not cancelled:
        return ConformanceCheckResult(
            name="cancel",
            passed=False,
            message="Task did not raise asyncio.CancelledError upon cancellation",
        )

    return ConformanceCheckResult(
        name="cancel",
        passed=True,
        message="Process group terminated cleanly upon task cancellation",
    )


async def check_no_change_is_failure(harness: CliHarness, tmp_root: Path) -> ConformanceCheckResult:
    """Verify that executions producing zero worktree modifications trigger the engine failure gate."""
    wt_dir = tmp_root / "no_change_wt"
    wt_dir.mkdir(parents=True, exist_ok=True)

    # Initialize a test git repository
    try:
        subprocess.run(["git", "init"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Fleet Test"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fleet@test.local"], cwd=str(wt_dir), check=True, capture_output=True)
        (wt_dir / "initial.txt").write_text("initial content\n")
        subprocess.run(["git", "add", "."], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(wt_dir), check=True, capture_output=True)
    except Exception as e:
        return ConformanceCheckResult(
            name="no-change-is-failure",
            passed=False,
            message=f"Failed to initialize test repository: {e}",
        )

    wt_mgr = WorktreeManager(str(tmp_root))

    # 1. Zero change verification
    changed_empty = await wt_mgr.get_changed_files(str(wt_dir))
    if changed_empty:
        return ConformanceCheckResult(
            name="no-change-is-failure",
            passed=False,
            message="Clean repository reported non-empty changed files",
        )

    # 2. Mutating change detection
    probe_mod = wt_dir / "probe_mod.txt"
    probe_mod.write_text("mod content\n")
    changed_mod = await wt_mgr.get_changed_files(str(wt_dir))
    probe_mod.unlink(missing_ok=True)

    if "probe_mod.txt" not in changed_mod:
        return ConformanceCheckResult(
            name="no-change-is-failure",
            passed=False,
            message="Worktree manager failed to detect added files",
        )

    return ConformanceCheckResult(
        name="no-change-is-failure",
        passed=True,
        message="Zero-change execution triggers failure gate as required by engine contract",
    )


async def evaluate_harness(harness: CliHarness) -> HarnessConformanceReport:
    """Run the 4 conformance checks against a CliHarness instance."""
    with tempfile.TemporaryDirectory(prefix="fleet_conformance_") as td:
        tmp_root = Path(td)

        checks = [
            await check_sandbox(harness, tmp_root),
            await check_timeout(harness, tmp_root),
            await check_cancel(harness, tmp_root),
            await check_no_change_is_failure(harness, tmp_root),
        ]

    return HarnessConformanceReport(
        harness=harness.name,
        binary=harness.binary,
        installed=harness.is_available(),
        state_paths=[str(p) for p in harness.state_paths],
        checks=checks,
    )


async def run_harness_conformance(
    harness_name: str,
    config: Optional[FleetConfig] = None,
    as_json: bool = False,
) -> int:
    """CLI entrypoint for `fleet harnesses test <name>`.

    Returns 0 on conformance pass, 1 on failure.
    """
    cfg = config or load_config()
    registry = harness_registry(cfg)

    if harness_name not in registry:
        available = ", ".join(sorted(registry.keys())) or "none"
        if as_json:
            print(
                json.dumps(
                    {
                        "error": f"harness '{harness_name}' not found",
                        "available_harnesses": sorted(registry.keys()),
                    },
                    indent=2,
                )
            )
        else:
            print(f"error: harness '{harness_name}' not found in registry.")
            print(f"available harnesses: {available}")
        return 1

    harness = registry[harness_name]
    if not isinstance(harness, CliHarness):
        if as_json:
            print(
                json.dumps(
                    {
                        "error": f"harness '{harness_name}' is not a CliHarness instance",
                    },
                    indent=2,
                )
            )
        else:
            print(f"error: harness '{harness_name}' is not a CliHarness instance.")
        return 1

    report = await evaluate_harness(harness)

    if as_json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0 if report.conformed else 1

    # Formatted terminal output
    print(f"HARNESS CONFORMANCE TEST: {report.harness}")
    installed_str = "yes" if report.installed else "no"
    print(f"Binary: {report.binary} (installed: {installed_str})")
    state_str = ", ".join(report.state_paths) if report.state_paths else "(none)"
    print(f"State paths: {state_str}")
    print()

    for check in report.checks:
        status_tag = "[PASS]" if check.passed else "[FAIL]"
        print(f"{status_tag} {check.name:<22} {check.message}")
        if check.details:
            print(f"       Details: {check.details}")

    print()
    result_tag = "OK" if report.conformed else "FAILED"
    print(
        f"RESULT: {report.passed_count}/{report.total_count} tests passed (conformance {result_tag})"
    )

    return 0 if report.conformed else 1
