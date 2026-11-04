import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from wiki_agent.api.dependencies import AppSettings, CurrentUser, DbSession
from wiki_agent.api.schemas import WikiCreate, WikiUpdate, WikiView
from wiki_agent.models import PersonalWiki, Source, SourceRevision, User
from wiki_agent.wiki import open_wiki_repository

router = APIRouter(prefix="/wikis", tags=["wikis"])
logger = logging.getLogger(__name__)


@router.get("", response_model=list[WikiView])
def list_wikis(user: CurrentUser, db: DbSession) -> list[PersonalWiki]:
    return list(
        db.scalars(
            select(PersonalWiki)
            .where(PersonalWiki.owner_id == user.id)
            .order_by(PersonalWiki.created_at, PersonalWiki.name)
        )
    )


@router.post("", response_model=WikiView, status_code=status.HTTP_201_CREATED)
def create_wiki(
    payload: WikiCreate,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> PersonalWiki:
    name = payload.name.strip()
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    if db.scalar(select(PersonalWiki.id).where(PersonalWiki.owner_id == user.id)):
        raise HTTPException(status_code=409, detail="每个账号只能有一个 Wiki")
    wiki = PersonalWiki(
        owner_id=user.id,
        name=name,
        description=payload.description.strip(),
    )
    db.add(wiki)
    db.flush()
    repository_root = settings.wiki_library_root(wiki.id)
    try:
        open_wiki_repository(
            repository_root,
            settings.legacy_wiki_library_root(wiki.id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(wiki)
    return wiki


@router.post("/initialize", response_model=WikiView)
def initialize_wiki(
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> PersonalWiki:
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    wiki = db.scalar(
        select(PersonalWiki)
        .where(PersonalWiki.owner_id == user.id)
        .order_by(PersonalWiki.created_at, PersonalWiki.name)
        .limit(1)
    )
    if wiki is not None:
        return wiki
    return create_wiki(WikiCreate(name="个人知识库"), user, db, settings)


@router.patch("/{wiki_id}", response_model=WikiView)
def update_wiki(
    wiki_id: str,
    payload: WikiUpdate,
    user: CurrentUser,
    db: DbSession,
) -> PersonalWiki:
    wiki = db.get(PersonalWiki, wiki_id)
    if wiki is None or wiki.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Wiki not found")
    name = payload.name.strip()
    duplicate = db.scalar(
        select(PersonalWiki.id).where(
            PersonalWiki.owner_id == user.id,
            PersonalWiki.name == name,
            PersonalWiki.id != wiki.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="已存在同名 Wiki")
    wiki.name = name
    wiki.description = payload.description.strip()
    db.commit()
    db.refresh(wiki)
    return wiki


@router.delete("/{wiki_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wiki(
    wiki_id: str,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> None:
    wiki = db.get(PersonalWiki, wiki_id)
    if wiki is None or wiki.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Wiki not found")
    repository_root = settings.wiki_library_root(wiki.id).resolve()
    if repository_root != settings.knowledge_root.resolve():
        raise HTTPException(status_code=500, detail="Invalid Wiki repository path")
    revisions = db.execute(
        select(SourceRevision.object_path, SourceRevision.sha256)
        .join(Source, Source.id == SourceRevision.source_id)
        .where(Source.wiki_id == wiki.id)
    ).all()
    object_paths = set()
    for object_path, sha256 in revisions:
        path = (settings.storage_root / object_path).resolve()
        if (
            not path.is_relative_to(settings.source_objects_root.resolve())
            or path.name != sha256
        ):
            raise HTTPException(status_code=500, detail="Invalid source object path")
        object_paths.add(path)
    repository = open_wiki_repository(
        repository_root,
        settings.legacy_wiki_library_root(wiki.id),
    )
    page_paths = repository.list_pages()
    if page_paths:
        repository.publish(
            {path: None for path in page_paths},
            base_commit=repository.head(),
            author_name=user.display_name,
            author_email=user.local_commit_email,
            message="Delete Wiki pages",
        )
    db.delete(wiki)
    db.commit()
    for path in object_paths:
        if db.scalar(
            select(SourceRevision.id).where(SourceRevision.sha256 == path.name).limit(1)
        ) is None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.exception(
                    "Wiki deleted but source object cleanup failed", extra={"wiki_id": wiki_id}
                )
                raise HTTPException(
                    status_code=500, detail="Wiki deleted, but object cleanup failed"
                ) from exc
