"""Terminal classifications: exit code, result label, and comment template.

Every failure reason has a written, actionable issue comment — that is what
makes "hand off to a human" cheap instead of a mystery.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    label: str  # converged | needs-human | config-error | ...
    comment: str  # template file stem under prompts/comments/
    level: str = "warn"


FAILURES: dict[str, Outcome] = {
    "converged": Outcome(0, "converged", "converged", "info"),
    "already_fixed": Outcome(0, "converged", "already_fixed", "info"),
    "dry_run": Outcome(0, "dry-run", "generic", "info"),
    "max_iterations_exceeded": Outcome(12, "needs-human", "max_iterations", "warn"),
    "stalled": Outcome(11, "needs-human", "stalled", "warn"),
    "no_changes": Outcome(10, "needs-human", "no_changes", "warn"),
    "tests_failed": Outcome(10, "needs-human", "tests_failed", "warn"),
    "fixer_error": Outcome(10, "needs-human", "fixer_error", "error"),
    "reviewer_error": Outcome(10, "needs-human", "reviewer_error", "error"),
    "verdict_mismatch": Outcome(10, "needs-human", "verdict_mismatch", "error"),
    "protected_path_violation": Outcome(10, "needs-human", "protected_paths", "error"),
    "contract_tampered": Outcome(10, "needs-human", "tampered_contract", "error"),
    "tests_weakened": Outcome(10, "needs-human", "tests_weakened", "error"),
    "diff_too_large": Outcome(10, "needs-human", "diff_too_large", "warn"),
    "scope_too_broad": Outcome(10, "needs-human", "scope_too_broad", "warn"),
    "review_only": Outcome(10, "needs-human", "review_only", "warn"),
    "push_rejected": Outcome(10, "needs-human", "push_rejected", "error"),
    "lock_held": Outcome(14, "retry-later", "generic", "warn"),
    "unauthorized_labeler": Outcome(15, "config-error", "unauthorized_labeler", "warn"),
    "auth_error": Outcome(3, "config-error", "auth_error", "error"),
    "rate_limited": Outcome(13, "retry-later", "rate_limited", "warn"),
    "timeout": Outcome(10, "needs-human", "timeout", "error"),
    "cli_error": Outcome(10, "needs-human", "fixer_error", "error"),
    "bad_json": Outcome(10, "needs-human", "reviewer_error", "error"),
    "config_error": Outcome(2, "config-error", "config_error", "error"),
    "cancelled": Outcome(5, "cancelled", "generic", "warn"),
    "internal_error": Outcome(4, "needs-human", "generic", "error"),
}


def for_reason(reason: str) -> Outcome:
    return FAILURES.get(reason, FAILURES["internal_error"])


def is_success(reason: str) -> bool:
    return for_reason(reason).exit_code == 0
