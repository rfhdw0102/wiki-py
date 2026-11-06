from io import BytesIO
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from wiki_agent.config import Settings
from wiki_agent.models import AuditLog, Source, SourceKind, SourceRevision
from wiki_agent.sources.article import WebArticle, WebContentError, public_article_url
from wiki_agent.sources.fetcher import FetchedPage
from wiki_agent.sources.storage import SourceObjectStore


def store_web_source(
    db: Session, settings: Settings, *, user_id: str, wiki_id: str,
    page: FetchedPage, article: WebArticle, acquisition: str,
) -> tuple[Source, SourceRevision]:
    if len(page.body) > settings.max_upload_bytes:
        raise WebContentError("too_large", "网页超过当前允许的导入大小，未保存。")
    obj = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
        BytesIO(page.body)
    )
    source = Source(
        wiki_id=wiki_id, name=article.title, kind=SourceKind.WEBSITE, created_by_id=user_id,
    )
    db.add(source)
    db.flush()
    revision = SourceRevision(
        source_id=source.id, sha256=obj.sha256,
        object_path=str(obj.path.relative_to(settings.storage_root.resolve())),
        media_type=page.content_type, size_bytes=obj.size_bytes,
        metadata_json={
            "requested_url": public_article_url(page.requested_url or page.final_url),
            "final_url": public_article_url(page.final_url),
            "host": urlparse(page.final_url).hostname,
            "article_title": article.title,
            "extractor": article.extractor,
            "acquisition": acquisition,
        },
    )
    db.add(revision)
    db.flush()
    source.current_revision_id = revision.id
    db.add(AuditLog(
        actor_id=user_id, action="source.web_added",
        resource_type="source_revision", resource_id=revision.id,
        details={"name": article.title, "url": public_article_url(page.final_url)},
    ))
    return source, revision
