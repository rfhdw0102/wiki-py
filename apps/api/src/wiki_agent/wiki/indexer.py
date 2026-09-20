from datetime import UTC, datetime

from langchain_core.embeddings import Embeddings
from sqlalchemy import delete
from sqlalchemy.orm import Session

from wiki_agent.models import (
    KnowledgeEdge,
    KnowledgeNode,
    WikiCitation,
    WikiDocument,
    WikiIndexState,
)
from wiki_agent.wiki import WikiRepository


class WikiIndexer:
    def __init__(self, repository: WikiRepository) -> None:
        self.repository = repository

    def rebuild(self, db: Session, embeddings: Embeddings | None = None) -> str:
        commit = self.repository.head()
        paths = self.repository.list_pages()
        pages = {path: self.repository.read_page(path, commit) for path in paths}
        path_to_id = {path: page.metadata.id for path, page in pages.items()}
        vectors = (
            embeddings.embed_documents([page.body for page in pages.values()])
            if embeddings is not None and pages
            else [None] * len(pages)
        )
        if len(vectors) != len(pages):
            raise ValueError("embedding service returned an unexpected number of vectors")

        db.execute(delete(WikiCitation))
        db.execute(delete(KnowledgeEdge))
        db.execute(delete(WikiDocument))
        db.execute(delete(KnowledgeNode))

        for (path, page), vector in zip(pages.items(), vectors, strict=True):
            db.add(
                KnowledgeNode(
                    id=page.metadata.id,
                    path=path,
                    kind=page.metadata.type.value,
                    title=page.metadata.title,
                )
            )
            db.add(
                WikiDocument(
                    path=path,
                    page_id=page.metadata.id,
                    page_type=page.metadata.type.value,
                    title=page.metadata.title,
                    content=page.body,
                    commit_sha=commit,
                    embedding=vector,
                    metadata_json=page.metadata.model_dump(mode="json"),
                )
            )
        db.flush()

        for _path, page in pages.items():
            for target in page.links:
                target_path = f"{target}.md" if not target.endswith(".md") else target
                db.add(
                    KnowledgeEdge(
                        source_node_id=page.metadata.id,
                        target_node_id=path_to_id[target_path],
                    )
                )
            for key, citation in page.metadata.citations.items():
                db.add(
                    WikiCitation(
                        page_id=page.metadata.id,
                        citation_key=key,
                        source_revision_id=citation.source_revision_id,
                        span_id=citation.span_id,
                        label=citation.label,
                    )
                )
        state = db.get(WikiIndexState, "published")
        if state is None:
            state = WikiIndexState(name="published", commit_sha=commit)
        state.commit_sha = commit
        state.indexed_at = datetime.now(UTC)
        db.add(state)
        db.commit()
        return commit
