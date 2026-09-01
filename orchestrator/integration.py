"""Small Slice A integration boundary for the offline bounded runner."""

from dataclasses import dataclass

from .contract import Event, EventType, Snapshot
from .runner import BoundedRunner, RunnerConfig, RunnerResult
from .state_machine import Orchestrator


@dataclass(frozen=True)
class CoordinatedRun:
    runner: RunnerResult
    snapshot: Snapshot


class RunnerCoordinator:
    """Maps one bounded worker attempt to append-only Slice A events."""

    def __init__(self, runner: BoundedRunner):
        self.runner = runner

    @staticmethod
    def _event(config: RunnerConfig, sequence: int, expected_version: int, event_type: EventType, **payload: object) -> Event:
        return Event(f"{config.run_id}-{sequence}-{event_type.value}", event_type, config.run_id, sequence, expected_version, config.source_sha, f"{config.run_id}-attempt-{sequence}", payload)

    def run(self, config: RunnerConfig) -> CoordinatedRun:
        state_machine = Orchestrator(config.run_id, config.source_sha, config.max_review_cycles)
        state_machine.apply(self._event(config, 1, 0, EventType.START, objective=config.objective))
        result = self.runner.run(config)
        if result.status == "completed" and result.validation_passed:
            snapshot = state_machine.apply(self._event(config, 2, 1, EventType.IMPLEMENTED, tests_pass=True, branch=result.branch, workspace_id=result.workspace_id))
        else:
            snapshot = state_machine.apply(self._event(config, 2, 1, EventType.RUNNER_FAILED, tests_pass=False, failure_reason=result.failure_reason or result.status))
        return CoordinatedRun(result, snapshot)
