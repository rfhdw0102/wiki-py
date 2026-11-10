from datetime import UTC, datetime

from langchain_core.embeddings import Embeddings
from sqlalchemy import delete, select
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
    def __init__(self, repository: WikiRepository, wiki_id: str) -> None:
        self.repository = repository
        self.wiki_id = wiki_id

    def rebuild(
        self,
        db: Session,
        embeddings: Embeddings | None = None,
        *,
        embedding_profile_id: str | None = None,
        reuse_embeddings: bool = False,
        commit_transaction: bool = True,
    ) -> str:
        if reuse_embeddings and (embeddings is not None or embedding_profile_id is not None):
            raise ValueError("reuse_embeddings cannot be combined with an embedding service")
        if embeddings is None and embedding_profile_id is not None:
            raise ValueError("embedding_profile_id requires an embedding service")
        if embeddings is not None and embedding_profile_id is None:
            raise ValueError("embedding service requires embedding_profile_id")
        commit = self.repository.head()
        paths = self.repository.list_pages()
        pages = {path: self.repository.read_page(path, commit) for path in paths}
        previous_documents = (
            {document.path: document for document in db.scalars(
                select(WikiDocument).where(WikiDocument.wiki_id == self.wiki_id)
            )}
            if reuse_embeddings else {}
        )
        if reuse_embeddings:
            previous_state = db.get(WikiIndexState, (self.wiki_id, "published"))
            embedding_profile_id = (
                previous_state.embedding_profile_id if previous_state is not None else None
            )
        vectors = (
            embeddings.embed_documents([page.body for page in pages.values()])
            if embeddings is not None and pages
            else [
                previous_documents[path].embedding
                if path in previous_documents and previous_documents[path].content == page.body
                else None
                for path, page in pages.items()
            ]
        )
        if len(vectors) != len(pages):
            raise ValueError("embedding service returned an unexpected number of vectors")

        db.execute(delete(WikiCitation).where(WikiCitation.wiki_id == self.wiki_id))
        db.execute(delete(KnowledgeEdge).where(KnowledgeEdge.wiki_id == self.wiki_id))
        db.execute(delete(WikiDocument).where(WikiDocument.wiki_id == self.wiki_id))
        db.execute(delete(KnowledgeNode).where(KnowledgeNode.wiki_id == self.wiki_id))

        nodes_by_path: dict[str, KnowledgeNode] = {}
        for (path, page), vector in zip(pages.items(), vectors, strict=True):
            node = KnowledgeNode(
                wiki_id=self.wiki_id,
                id=page.metadata.id,
                path=path,
                kind=page.metadata.type.value,
                title=page.metadata.title,
            )
            nodes_by_path[path] = node
            db.add(node)
            db.add(
                WikiDocument(
                    wiki_id=self.wiki_id,
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

        for path, page in pages.items():
            source_node = nodes_by_path[path]
            for target in page.links:
                target_path = f"{target}.md" if not target.endswith(".md") else target
                db.add(
                    KnowledgeEdge(
                        wiki_id=self.wiki_id,
                        source_node_id=source_node.record_id,
                        target_node_id=nodes_by_path[target_path].record_id,
                    )
                )
            for key, citation in page.metadata.citations.items():
                db.add(
                    WikiCitation(
                        wiki_id=self.wiki_id,
                        page_id=source_node.record_id,
                        citation_key=key,
                        source_revision_id=citation.source_revision_id,
                        span_id=citation.span_id,
                        label=citation.label,
                    )
                )
        state = db.get(WikiIndexState, (self.wiki_id, "published"))
        if state is None:
            state = WikiIndexState(
                wiki_id=self.wiki_id,
                name="published",
                commit_sha=commit,
            )
        state.commit_sha = commit
        state.embedding_profile_id = embedding_profile_id
        state.indexed_at = datetime.now(UTC)
        db.add(state)
        if commit_transaction:
            db.commit()
        else:
            db.flush()
        return commit
