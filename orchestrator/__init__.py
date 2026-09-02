"""Slice A offline coordination primitives."""

from .contract import Event, EventType, Risk, State
from .integration import CoordinatedRun, RunnerCoordinator
from .objective_runner import ObjectiveOutcome, ObjectiveProfile, ObjectiveRunner
from .runner import BoundedRunner, FakeCodexAdapter, RunnerConfig, RunnerResult
from .state_machine import ConcurrencyError, IntegrityError, Orchestrator, StaleEventError, TransitionError

__all__ = ["BoundedRunner", "ConcurrencyError", "CoordinatedRun", "Event", "EventType", "FakeCodexAdapter", "IntegrityError", "ObjectiveOutcome", "ObjectiveProfile", "ObjectiveRunner", "Orchestrator", "Risk", "RunnerConfig", "RunnerCoordinator", "RunnerResult", "State", "StaleEventError", "TransitionError"]
