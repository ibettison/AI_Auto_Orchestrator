"""Deterministic risk classification. No model or network calls are involved."""

from .contract import Risk


def classify(*, tests_pass: bool = True, scope_changed: bool = False,
             destructive: bool = False, external_side_effect: bool = False,
             human_approved: bool = False) -> Risk:
    if destructive or external_side_effect:
        # Approval is a state-machine event, never an untrusted payload flag.
        return Risk.RED
    if not tests_pass or scope_changed:
        return Risk.AMBER
    return Risk.GREEN
