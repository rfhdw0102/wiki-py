from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PublicationPolicy:
    max_changed_pages: int = 3
    max_changed_ratio: float = 0.2
    max_new_claims: int = 20
    minimum_source_trust: int = 2

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PublicationPolicy":
        policy = cls(
            max_changed_pages=_integer(value, "max_changed_pages", 3),
            max_changed_ratio=_number(value, "max_changed_ratio", 0.2),
            max_new_claims=_integer(value, "max_new_claims", 20),
            minimum_source_trust=_integer(value, "minimum_source_trust", 2),
        )
        if policy.max_changed_pages < 1 or policy.max_new_claims < 0:
            raise ValueError("publication count thresholds must be non-negative")
        if not 0 <= policy.max_changed_ratio <= 1:
            raise ValueError("max_changed_ratio must be between 0 and 1")
        if not 0 <= policy.minimum_source_trust <= 3:
            raise ValueError("minimum_source_trust must be between 0 and 3")
        return policy


@dataclass(frozen=True)
class ChangeRisk:
    changed_pages: int
    largest_changed_ratio: float
    new_claims: int
    minimum_source_trust: int
    citations_complete: bool
    has_conflicts: bool
    has_deletions: bool
    has_renames: bool
    has_entity_merges: bool
    schema_valid: bool
    links_valid: bool
    verifier_passed: bool
    workflow_degraded: bool
    base_commit_current: bool


def evaluate_change_risk(risk: ChangeRisk, policy: PublicationPolicy) -> list[str]:
    violations: list[str] = []
    checks = (
        (risk.changed_pages > policy.max_changed_pages, "too_many_changed_pages"),
        (risk.largest_changed_ratio > policy.max_changed_ratio, "change_ratio_too_large"),
        (risk.new_claims > policy.max_new_claims, "too_many_new_claims"),
        (risk.minimum_source_trust < policy.minimum_source_trust, "source_trust_too_low"),
        (not risk.citations_complete, "citations_incomplete"),
        (risk.has_conflicts, "unresolved_conflicts"),
        (risk.has_deletions, "contains_deletions"),
        (risk.has_renames, "contains_renames"),
        (risk.has_entity_merges, "contains_entity_merges"),
        (not risk.schema_valid, "schema_invalid"),
        (not risk.links_valid, "links_invalid"),
        (not risk.verifier_passed, "verifier_failed"),
        (risk.workflow_degraded, "workflow_degraded"),
        (not risk.base_commit_current, "stale_base_commit"),
    )
    for failed, code in checks:
        if failed:
            violations.append(code)
    return violations


def _number(value: Mapping[str, object], key: str, default: float) -> float:
    raw = value.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{key} must be a number")
    return float(raw)


def _integer(value: Mapping[str, object], key: str, default: int) -> int:
    raw = value.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} must be an integer")
    return raw
