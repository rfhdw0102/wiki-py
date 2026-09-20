from dataclasses import replace

from wiki_agent.publishing.risk import ChangeRisk, PublicationPolicy, evaluate_change_risk


def safe_risk() -> ChangeRisk:
    return ChangeRisk(
        changed_pages=1,
        largest_changed_ratio=0.1,
        new_claims=2,
        minimum_source_trust=3,
        citations_complete=True,
        has_conflicts=False,
        has_deletions=False,
        has_renames=False,
        has_entity_merges=False,
        schema_valid=True,
        links_valid=True,
        verifier_passed=True,
        workflow_degraded=False,
        base_commit_current=True,
    )


def test_safe_change_can_auto_publish() -> None:
    assert evaluate_change_risk(safe_risk(), PublicationPolicy()) == []


def test_conflict_and_missing_citations_force_review() -> None:
    violations = evaluate_change_risk(
        replace(safe_risk(), has_conflicts=True, citations_complete=False),
        PublicationPolicy(),
    )
    assert violations == ["citations_incomplete", "unresolved_conflicts"]


def test_policy_thresholds_can_be_loaded_from_admin_config() -> None:
    policy = PublicationPolicy.from_mapping(
        {"max_changed_pages": 5, "max_changed_ratio": 0.4}
    )
    assert policy.max_changed_pages == 5
    assert policy.max_changed_ratio == 0.4
