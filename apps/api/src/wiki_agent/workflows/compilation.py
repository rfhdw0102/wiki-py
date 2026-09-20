from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NotRequired, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class CompilationState(TypedDict):
    source_revision_id: str
    requested_policy: str
    source_markdown: NotRequired[str]
    base_commit: NotRequired[str]
    files: NotRequired[dict[str, str | None]]
    summary: NotRequired[str]
    conflicts: NotRequired[list[str]]
    verification_issues: NotRequired[list[str]]
    risk_violations: NotRequired[list[str]]
    review_decision: NotRequired[str]
    status: NotRequired[str]


@dataclass(frozen=True)
class CompiledPages:
    files: dict[str, str | None]
    summary: str
    conflicts: list[str]


@dataclass(frozen=True)
class VerificationOutcome:
    issues: list[str]
    risk_violations: list[str]


@dataclass(frozen=True)
class CompilationServices:
    load_source: Callable[[str], str]
    current_revision: Callable[[], str]
    compile_pages: Callable[[str, str], CompiledPages]
    link_pages: Callable[[dict[str, str | None], str], dict[str, str | None]]
    detect_conflicts: Callable[[dict[str, str | None], list[str]], list[str]]
    verify: Callable[
        [str, dict[str, str | None], list[str], str],
        VerificationOutcome,
    ]


def build_compilation_graph(
    services: CompilationServices,
    checkpointer: BaseCheckpointSaver[Any] | bool | None = None,
) -> Any:
    def load_source(state: CompilationState) -> dict[str, object]:
        return {
            "source_markdown": services.load_source(state["source_revision_id"]),
            "base_commit": services.current_revision(),
        }

    def compile_pages(state: CompilationState) -> dict[str, object]:
        proposal = services.compile_pages(
            state["source_markdown"], state["source_revision_id"]
        )
        return {
            "files": proposal.files,
            "summary": proposal.summary,
            "conflicts": proposal.conflicts,
        }

    def link_pages(state: CompilationState) -> dict[str, object]:
        return {"files": services.link_pages(state["files"], state["base_commit"])}

    def detect_conflicts(state: CompilationState) -> dict[str, object]:
        return {
            "conflicts": services.detect_conflicts(
                state["files"], state.get("conflicts", [])
            )
        }

    def verify(state: CompilationState) -> dict[str, object]:
        outcome = services.verify(
            state["source_revision_id"],
            state["files"],
            state["conflicts"],
            state["base_commit"],
        )
        violations = outcome.risk_violations
        if state["conflicts"] and "unresolved_conflicts" not in violations:
            violations = [*violations, "unresolved_conflicts"]
        return {
            "verification_issues": outcome.issues,
            "risk_violations": sorted(set(violations)),
        }

    def route_publication(state: CompilationState) -> str:
        if state["requested_policy"] == "low_risk_auto_publish" and not state[
            "risk_violations"
        ]:
            return "auto_approve"
        return "review"

    def auto_approve(state: CompilationState) -> dict[str, object]:
        return {"status": "approved"}

    def review(state: CompilationState) -> dict[str, object]:
        decision = interrupt(
            {
                "kind": "publication_review",
                "source_revision_id": state["source_revision_id"],
                "files": state["files"],
                "risk_violations": state["risk_violations"],
            }
        )
        if decision not in {"approve", "reject"}:
            raise ValueError("review decision must be 'approve' or 'reject'")
        return {"review_decision": decision, "status": f"{decision}d"}

    builder = StateGraph(CompilationState)
    builder.add_node("load_source", load_source)
    builder.add_node("compile_pages", compile_pages)
    builder.add_node("link_pages", link_pages)
    builder.add_node("detect_conflicts", detect_conflicts)
    builder.add_node("verify", verify)
    builder.add_node("auto_approve", auto_approve)
    builder.add_node("review", review)
    builder.add_edge(START, "load_source")
    builder.add_edge("load_source", "compile_pages")
    builder.add_edge("compile_pages", "link_pages")
    builder.add_edge("link_pages", "detect_conflicts")
    builder.add_edge("detect_conflicts", "verify")
    builder.add_conditional_edges(
        "verify",
        route_publication,
        {"auto_approve": "auto_approve", "review": "review"},
    )
    builder.add_edge("auto_approve", END)
    builder.add_edge("review", END)
    return builder.compile(checkpointer=checkpointer)
