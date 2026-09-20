from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from wiki_agent.workflows import (
    CompilationServices,
    CompiledPages,
    VerificationOutcome,
    build_compilation_graph,
)


def services(*, violations: list[str] | None = None) -> CompilationServices:
    return CompilationServices(
        load_source=lambda revision_id: f"source:{revision_id}",
        current_revision=lambda: "base-commit",
        compile_pages=lambda source, revision_id: CompiledPages(
            files={"sources/source.md": source},
            summary=f"Compiled {revision_id}",
            conflicts=[],
        ),
        link_pages=lambda files, base_commit: files,
        detect_conflicts=lambda files, conflicts: conflicts,
        verify=lambda revision_id, files, conflicts, base_commit: VerificationOutcome(
            issues=[],
            risk_violations=violations or [],
        ),
    )


def test_low_risk_compilation_auto_approves() -> None:
    graph = build_compilation_graph(services())
    result = graph.invoke(
        {
            "source_revision_id": "revision-1",
            "requested_policy": "low_risk_auto_publish",
        }
    )
    assert result["status"] == "approved"
    assert result["files"] == {"sources/source.md": "source:revision-1"}


def test_risk_violation_interrupts_for_review() -> None:
    checkpointer = InMemorySaver()
    graph = build_compilation_graph(
        services(violations=["citations_incomplete"]), checkpointer
    )
    config = {"configurable": {"thread_id": "run-1"}}
    result = graph.invoke(
        {
            "source_revision_id": "revision-1",
            "requested_policy": "low_risk_auto_publish",
        },
        config,
    )
    assert result["__interrupt__"]
    resumed = graph.invoke(Command(resume="approve"), config)
    assert resumed["review_decision"] == "approve"
    assert resumed["status"] == "approved"
