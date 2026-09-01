"""Offline bounded runner primitives for Slice B.

The runner deliberately uses argv-only subprocesses, a copied Git workspace,
and explicit policies. It is a development reference implementation, not an
OS/container security boundary.
"""

from __future__ import annotations

import hashlib
import os
import signal
import shutil
import subprocess
import tempfile
import selectors
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, Sequence


class RunnerError(ValueError):
    pass


class UnsafeConfiguration(RunnerError):
    pass


class PolicyViolation(RunnerError):
    pass


class BoundExceeded(RunnerError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SHELL_TOKENS = (";", "&&", "||", "|", "&", "$", "`", ">", "<", "\n", "\r")
_SHELL_WRAPPERS = {"sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh"}


def _validate_relative_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise UnsafeConfiguration(f"{label} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise UnsafeConfiguration(f"unsafe {label}: {value!r}")
    return path.as_posix()


def _validate_argv(argv: Sequence[str], label: str = "command") -> tuple[str, ...]:
    if not isinstance(argv, (tuple, list)) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
        raise UnsafeConfiguration(f"{label} must be a non-empty argv list")
    if Path(argv[0]).name in _SHELL_WRAPPERS:
        raise UnsafeConfiguration(f"{label} cannot invoke a shell wrapper")
    if any(token in arg for arg in argv for token in _SHELL_TOKENS):
        raise UnsafeConfiguration(f"{label} contains shell composition")
    return tuple(argv)


@dataclass(frozen=True)
class RunnerConfig:
    run_id: str
    repository: str
    source_sha: str
    allowed_paths: tuple[str, ...]
    allowed_commands: tuple[tuple[str, ...], ...]
    objective: str
    required_checks: tuple[tuple[str, ...], ...] = ()
    timeout_seconds: float = 60.0
    command_timeout_seconds: float = 20.0
    max_output_size: int = 64 * 1024
    max_commands: int = 20
    environment: Mapping[str, str] = field(default_factory=dict)
    network_requested: bool = False
    max_review_cycles: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in self.run_id):
            raise UnsafeConfiguration("run_id must be a simple identifier")
        repo = Path(self.repository)
        if not repo.is_absolute() or not repo.is_dir() or not (repo / ".git").exists():
            raise UnsafeConfiguration("repository must be an absolute Git checkout")
        if not isinstance(self.source_sha, str) or not self.source_sha:
            raise UnsafeConfiguration("source_sha is required")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise UnsafeConfiguration("objective is required")
        if not self.allowed_paths:
            raise UnsafeConfiguration("allowed_paths must not be empty")
        for path in self.allowed_paths:
            _validate_relative_path(path, "allowed path")
        if not self.allowed_commands:
            raise UnsafeConfiguration("allowed_commands must not be empty")
        for command in self.allowed_commands:
            _validate_argv(command, "allowed command")
        if not self.required_checks:
            raise UnsafeConfiguration("required_checks must contain at least one validation command")
        for check in self.required_checks:
            normalized = _validate_argv(check, "required check")
            if normalized not in self.allowed_commands:
                raise UnsafeConfiguration("every required check must be explicitly allowlisted")
        if self.timeout_seconds <= 0 or self.command_timeout_seconds <= 0:
            raise UnsafeConfiguration("timeouts must be positive")
        if self.max_output_size < 1 or self.max_commands < 1 or self.max_review_cycles < 1:
            raise UnsafeConfiguration("resource bounds must be positive")
        if self.network_requested:
            raise UnsafeConfiguration("network access is denied: no hard network sandbox is available")
        if not isinstance(self.environment, Mapping) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.environment.items()):
            raise UnsafeConfiguration("environment must be an explicit string mapping")


class CommandPolicy:
    def __init__(self, allowed_commands: Sequence[Sequence[str]], max_commands: int):
        self.allowed = {_validate_argv(command, "allowed command") for command in allowed_commands}
        self.max_commands = max_commands
        self.executed: list[tuple[str, ...]] = []

    def authorize(self, argv: Sequence[str]) -> tuple[str, ...]:
        command = _validate_argv(argv)
        if len(self.executed) >= self.max_commands:
            raise BoundExceeded("maximum command count exceeded")
        if command not in self.allowed:
            raise PolicyViolation(f"command is not allowlisted: {command!r}")
        self.executed.append(command)
        return command


class PathPolicy:
    def __init__(self, allowed_paths: Sequence[str]):
        self.allowed = tuple(_validate_relative_path(path, "allowed path") for path in allowed_paths)

    def is_allowed(self, path: str) -> bool:
        normalized = _validate_relative_path(path, "changed path")
        return any(normalized == allowed or normalized.startswith(allowed + "/") for allowed in self.allowed)

    def verify(self, changed_files: Sequence[str]) -> None:
        violations = [path for path in changed_files if not self.is_allowed(path)]
        if violations:
            raise PolicyViolation(f"out-of-scope changes: {violations}")


class EnvironmentPolicy:
    def __init__(self, values: Mapping[str, str]):
        self.values = {key: value for key, value in values.items()}

    def build(self) -> dict[str, str]:
        # Deliberately do not merge os.environ: inheritance is opt-in per key.
        return dict(self.values)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {"argv": list(self.argv), "exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr, "duration_seconds": self.duration_seconds}


class BoundedCommandRunner:
    def __init__(self, workspace: Path, policy: CommandPolicy, environment: Mapping[str, str], command_timeout: float, overall_deadline: float, max_output: int, audit: list[dict[str, str]]):
        self.workspace = workspace
        self.policy = policy
        self.environment = dict(environment)
        self.command_timeout = command_timeout
        self.overall_deadline = overall_deadline
        self.max_output = max_output
        self.audit = audit
        self.results: list[CommandResult] = []
        self.captured_output = 0

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    def run(self, argv: Sequence[str]) -> CommandResult:
        command = self.policy.authorize(argv)
        remaining = self.overall_deadline - time.monotonic()
        if remaining <= 0:
            raise BoundExceeded("overall runner timeout exceeded")
        started = time.monotonic()
        self.audit.append({"action": "command_attempted", "command": " ".join(command), "at": _now()})
        process = subprocess.Popen(command, cwd=self.workspace, env=self.environment, shell=False,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = min(self.overall_deadline, started + self.command_timeout)
        try:
            while selector.get_map() or process.poll() is None:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, max(0, deadline - started))
                for key, _ in selector.select(timeout=min(0.05, deadline - time.monotonic())):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if self.captured_output + len(chunk) > self.max_output:
                        raise BoundExceeded("maximum captured output exceeded")
                    buffers[key.data].extend(chunk)
                    self.captured_output += len(chunk)
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            self.audit.append({"action": "timeout", "command": " ".join(command), "at": _now()})
            raise BoundExceeded(f"command timeout: {command!r}") from exc
        except BoundExceeded:
            self._terminate(process)
            raise
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        stdout, stderr = bytes(buffers["stdout"]), bytes(buffers["stderr"])
        result = CommandResult(command, process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"), time.monotonic() - started)
        self.results.append(result)
        self.audit.append({"action": "command_completed" if result.exit_code == 0 else "command_failed", "command": " ".join(command), "at": _now()})
        if result.exit_code != 0:
            raise RunnerError(f"allowlisted command failed with exit code {result.exit_code}: {command!r}")
        return result


class CodexAdapter(Protocol):
    def run(self, objective: str, workspace: Path, commands: BoundedCommandRunner) -> "AdapterResult": ...


@dataclass(frozen=True)
class AdapterResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    failure_reason: str | None = None


class FakeCodexAdapter:
    """Offline adapter. The action receives only the isolated workspace and policy runner."""

    def __init__(self, action: Callable[[str, Path, BoundedCommandRunner], AdapterResult] | None = None):
        self.action = action

    def run(self, objective: str, workspace: Path, commands: BoundedCommandRunner) -> AdapterResult:
        if self.action:
            return self.action(objective, workspace, commands)
        return AdapterResult(0, "fake codex completed", "")


@dataclass(frozen=True)
class AuditRecord:
    action: str
    at: str
    details: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {"action": self.action, "at": self.at, "details": dict(self.details)}


@dataclass(frozen=True)
class RunnerResult:
    run_id: str
    source_sha: str
    branch: str
    workspace_id: str
    status: str
    exit_code: int | None
    commands_executed: tuple[CommandResult, ...]
    files_changed: tuple[str, ...]
    checks_attempted: tuple[tuple[str, ...], ...]
    required_checks: tuple[tuple[str, ...], ...]
    missing_checks: tuple[tuple[str, ...], ...]
    validation_passed: bool
    stdout_summary: str
    stderr_summary: str
    failure_reason: str | None
    started_at: str
    ended_at: str
    duration_seconds: float
    audit: tuple[AuditRecord, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id, "source_sha": self.source_sha, "branch": self.branch,
            "workspace_id": self.workspace_id, "status": self.status, "exit_code": self.exit_code,
            "commands_executed": [item.to_dict() for item in self.commands_executed],
            "files_changed": list(self.files_changed), "tests_checks_attempted": [list(item) for item in self.checks_attempted],
            "required_checks": [list(item) for item in self.required_checks],
            "missing_checks": [list(item) for item in self.missing_checks],
            "validation_passed": self.validation_passed,
            "stdout_summary": self.stdout_summary, "stderr_summary": self.stderr_summary,
            "failure_reason": self.failure_reason, "started_at": self.started_at, "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds, "audit": [item.to_dict() for item in self.audit],
        }


class Workspace:
    def __init__(self, root: Path, workspace_id: str, branch: str, source_sha: str):
        self.root, self.workspace_id, self.branch, self.source_sha = root, workspace_id, branch, source_sha
        self.baseline = self.inventory()

    def inventory(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in self.root.rglob("*"):
            if ".git" in path.parts:
                continue
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                files[relative] = "SYMLINK"
            elif path.is_file():
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def changed_files(self) -> tuple[str, ...]:
        current = self.inventory()
        return tuple(sorted(path for path in set(self.baseline) | set(current) if self.baseline.get(path) != current.get(path)))


class WorkspaceManager:
    def __init__(self, config: RunnerConfig, audit: list[dict[str, str]]):
        self.config, self.audit = config, audit
        self.root: Path | None = None
        self.workspace: Workspace | None = None
        self.workspace_id = f"ws-{uuid.uuid4().hex}"

    def prepare(self) -> Workspace:
        repo = Path(self.config.repository)
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if dirty.returncode != 0:
            raise RunnerError(f"cannot inspect source checkout: {dirty.stderr.strip()}")
        if dirty.stdout:
            raise PolicyViolation("source checkout is dirty")
        verified = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{self.config.source_sha}^{{commit}}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if verified.returncode != 0 or verified.stdout.strip() != self.config.source_sha:
            raise PolicyViolation("source SHA is not an exact commit in the repository")
        parent = Path(tempfile.mkdtemp(prefix=f"orchestrator-{self.config.run_id}-"))
        target = parent / "workspace"
        branch = f"codex/{self.config.run_id}-{uuid.uuid4().hex[:8]}"
        clone = subprocess.run(["git", "clone", "--no-local", str(repo), str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if clone.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            raise RunnerError(f"workspace clone failed: {clone.stderr.strip()}")
        for command in (["git", "checkout", "--detach", self.config.source_sha], ["git", "switch", "-c", branch]):
            result = subprocess.run(command, cwd=target, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if result.returncode != 0:
                shutil.rmtree(parent, ignore_errors=True)
                raise RunnerError(f"workspace preparation failed: {result.stderr.strip()}")
        self.root = parent
        self.workspace = Workspace(target, self.workspace_id, branch, self.config.source_sha)
        if any(value == "SYMLINK" for value in self.workspace.baseline.values()):
            shutil.rmtree(parent, ignore_errors=True)
            self.root = None
            self.workspace = None
            raise PolicyViolation("source checkout contains symlinks; workspace rejected")
        self.audit.append({"action": "workspace_created", "workspace_id": self.workspace_id, "at": _now()})
        self.audit.append({"action": "source_sha_verified", "source_sha": self.config.source_sha, "at": _now()})
        return self.workspace

    def cleanup(self) -> None:
        if self.root:
            shutil.rmtree(self.root, ignore_errors=True)
            self.audit.append({"action": "cleanup", "workspace_id": self.workspace_id, "at": _now()})


class RunLeaseRegistry:
    """In-process duplicate-run guard; durable leases belong to a later slice."""

    _active: set[str] = set()
    _lock = threading.Lock()

    def acquire(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._active:
                return False
            self._active.add(run_id)
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            self._active.discard(run_id)


class BoundedRunner:
    def __init__(self, adapter: CodexAdapter, leases: RunLeaseRegistry | None = None):
        self.adapter, self.leases = adapter, leases or RunLeaseRegistry()

    def run(self, config: RunnerConfig) -> RunnerResult:
        started_clock, started_at = time.monotonic(), _now()
        audit_raw: list[dict[str, str]] = []
        workspace_manager = WorkspaceManager(config, audit_raw)
        workspace: Workspace | None = None
        status, exit_code, failure = "failed", None, None
        commands: tuple[CommandResult, ...] = ()
        missing_checks: tuple[tuple[str, ...], ...] = config.required_checks
        validation_passed = False
        changed: tuple[str, ...] = ()
        stdout, stderr = "", ""
        if not self.leases.acquire(config.run_id):
            failure = "run already active"
            audit_raw.append({"action": "runner_rejected_duplicate", "at": _now()})
        else:
            try:
                workspace = workspace_manager.prepare()
                deadline = time.monotonic() + config.timeout_seconds
                policy = CommandPolicy(config.allowed_commands, config.max_commands)
                command_runner = BoundedCommandRunner(workspace.root, policy, EnvironmentPolicy(config.environment).build(), config.command_timeout_seconds, deadline, config.max_output_size, audit_raw)
                result = self.adapter.run(config.objective, workspace.root, command_runner)
                commands = tuple(command_runner.results)
                stdout = result.stdout
                stderr = result.stderr
                if command_runner.captured_output + len(stdout.encode()) + len(stderr.encode()) > config.max_output_size:
                    raise BoundExceeded("adapter output exceeded maximum captured output")
                changed = workspace.changed_files()
                if any(value == "SYMLINK" for value in workspace.inventory().values()):
                    raise PolicyViolation("symlinks are not permitted in the worker workspace")
                PathPolicy(config.allowed_paths).verify(changed)
                audit_raw.append({"action": "scope_verified", "at": _now()})
                if result.exit_code != 0:
                    exit_code, failure = result.exit_code, result.failure_reason or "Codex adapter failed"
                else:
                    executed_checks = {command.argv for command in command_runner.results if command.exit_code == 0}
                    missing_checks = tuple(check for check in config.required_checks if check not in executed_checks)
                    if missing_checks:
                        raise PolicyViolation(f"required validation checks were not run: {missing_checks!r}")
                    validation_passed = True
                    if time.monotonic() > deadline:
                        raise BoundExceeded("overall runner timeout exceeded")
                    else:
                        status, exit_code = "completed", 0
                        audit_raw.append({"action": "runner_completed", "at": _now()})
            except KeyboardInterrupt:
                status, failure = "interrupted", "runner interrupted"
                audit_raw.append({"action": "runner_interrupted", "at": _now()})
            except BoundExceeded as exc:
                failure = str(exc)
                status = "timed_out" if "timeout" in failure else "failed"
                audit_raw.append({"action": "runner_timeout" if status == "timed_out" else "runner_bound_exceeded", "at": _now()})
            except (RunnerError, OSError, subprocess.SubprocessError) as exc:
                status, failure = "failed", str(exc)
                audit_raw.append({"action": "runner_failed", "at": _now()})
            finally:
                if 'command_runner' in locals():
                    commands = tuple(command_runner.results)
                self.leases.release(config.run_id)
                workspace_manager.cleanup()
        ended_at = _now()
        if not stdout and commands:
            stdout = "\n".join(result.stdout for result in commands)
        if not stderr and commands:
            stderr = "\n".join(result.stderr for result in commands)
        audit = tuple(AuditRecord(item.pop("action"), item.pop("at"), item) for item in audit_raw)
        return RunnerResult(config.run_id, config.source_sha, workspace.branch if workspace else f"codex/{config.run_id}-unprepared", workspace_manager.workspace_id, status, exit_code, commands, changed, tuple(command.argv for command in commands), config.required_checks, missing_checks, validation_passed, stdout, stderr, failure, started_at, ended_at, time.monotonic() - started_clock, audit)
