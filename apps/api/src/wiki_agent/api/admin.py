from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select, update

from wiki_agent.api.dependencies import AdminUser, DbSession, WikiRepo
from wiki_agent.api.schemas import (
    CreateUserRequest,
    FeatureFlagUpdate,
    FeatureFlagView,
    ModelAssignmentUpdate,
    ModelAssignmentView,
    ModelProfileCreate,
    ModelProfileView,
    PasswordResetRequest,
    SchemaUpdate,
    SourceTrustUpdate,
    SourceView,
    UserStatusUpdate,
    UserView,
)
from wiki_agent.config import get_settings
from wiki_agent.llm import ModelFactory
from wiki_agent.models import (
    AgentRun,
    AuditLog,
    AuthSession,
    FeatureFlag,
    ModelAssignment,
    ModelKind,
    ModelProfile,
    Source,
    User,
    WikiIndexState,
)
from wiki_agent.security import SecretBox, SecretEncryptionError, hash_password
from wiki_agent.wiki.indexer import WikiIndexer
from wiki_agent.wiki.repository import StaleWikiRevisionError, WikiRepositoryError

router = APIRouter(prefix="/admin", tags=["admin"])
MODEL_PURPOSES = {"planning", "compilation", "answer", "embedding", "reranker"}
MODEL_PURPOSE_KINDS = {
    "planning": ModelKind.CHAT,
    "compilation": ModelKind.CHAT,
    "answer": ModelKind.CHAT,
    "embedding": ModelKind.EMBEDDING,
    "reranker": ModelKind.RERANKER,
}


@router.get("/users", response_model=list[UserView])
def list_users(admin: AdminUser, db: DbSession) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at)))


@router.post("/users", response_model=UserView, status_code=status.HTTP_201_CREATED)
def create_user(payload: CreateUserRequest, admin: AdminUser, db: DbSession) -> User:
    email = payload.email.lower()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(
        email=email,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.flush()
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="user.created",
            resource_type="user",
            resource_id=user.id,
            details={"role": user.role.value},
        )
    )
    db.commit()
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserView)
def update_user_status(
    user_id: str,
    payload: UserStatusUpdate,
    admin: AdminUser,
    db: DbSession,
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and not payload.is_active:
        raise HTTPException(status_code=409, detail="Administrators cannot disable themselves")
    user.is_active = payload.is_active
    if not user.is_active:
        db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="user.status_updated",
            resource_type="user",
            resource_id=user.id,
            details={"is_active": user.is_active},
        )
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_user_password(
    user_id: str,
    payload: PasswordResetRequest,
    admin: AdminUser,
    db: DbSession,
) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.password)
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="user.password_reset",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    db.commit()


@router.get("/features", response_model=list[FeatureFlagView])
def list_features(admin: AdminUser, db: DbSession) -> list[FeatureFlag]:
    return list(db.scalars(select(FeatureFlag).order_by(FeatureFlag.key)))


@router.put("/features/{key}", response_model=FeatureFlagView)
def update_feature(
    key: str,
    payload: FeatureFlagUpdate,
    admin: AdminUser,
    db: DbSession,
) -> FeatureFlag:
    flag = db.get(FeatureFlag, key)
    if flag is None:
        flag = FeatureFlag(key=key)
    flag.enabled = payload.enabled
    flag.config = payload.config
    db.add(flag)
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="feature.updated",
            resource_type="feature_flag",
            resource_id=key,
            details={"enabled": payload.enabled, "config": payload.config},
        )
    )
    db.commit()
    db.refresh(flag)
    return flag


@router.get("/models", response_model=list[ModelProfileView])
def list_model_profiles(admin: AdminUser, db: DbSession) -> list[ModelProfile]:
    return list(db.scalars(select(ModelProfile).order_by(ModelProfile.name)))


@router.post("/models", response_model=ModelProfileView, status_code=status.HTTP_201_CREATED)
def create_model_profile(
    payload: ModelProfileCreate,
    admin: AdminUser,
    db: DbSession,
) -> ModelProfile:
    if db.scalar(select(ModelProfile.id).where(ModelProfile.name == payload.name)) is not None:
        raise HTTPException(status_code=409, detail="Model profile name already exists")
    try:
        ciphertext = SecretBox(get_settings().encryption_key).encrypt(payload.api_key)
    except SecretEncryptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    profile = ModelProfile(
        name=payload.name,
        kind=payload.kind,
        base_url=payload.base_url.rstrip("/"),
        model_name=payload.model_name,
        api_key_ciphertext=ciphertext,
        enabled=payload.enabled,
        config=payload.config,
    )
    db.add(profile)
    db.flush()
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="model_profile.created",
            resource_type="model_profile",
            resource_id=profile.id,
            details={"name": profile.name, "kind": profile.kind.value},
        )
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/model-assignments", response_model=list[ModelAssignmentView])
def list_model_assignments(admin: AdminUser, db: DbSession) -> list[ModelAssignment]:
    return list(db.scalars(select(ModelAssignment).order_by(ModelAssignment.purpose)))


@router.put("/model-assignments/{purpose}", response_model=ModelAssignmentView)
def update_model_assignment(
    purpose: str,
    payload: ModelAssignmentUpdate,
    admin: AdminUser,
    db: DbSession,
) -> ModelAssignment:
    if purpose not in MODEL_PURPOSES:
        raise HTTPException(status_code=422, detail=f"Unsupported model purpose: {purpose}")
    profile = db.get(ModelProfile, payload.model_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    if profile.kind is not MODEL_PURPOSE_KINDS[purpose]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Model purpose {purpose} requires a "
                f"{MODEL_PURPOSE_KINDS[purpose].value} profile"
            ),
        )
    assignment = db.get(ModelAssignment, purpose)
    if assignment is None:
        assignment = ModelAssignment(purpose=purpose, model_profile_id=profile.id)
    else:
        assignment.model_profile_id = profile.id
    db.add(assignment)
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="model_assignment.updated",
            resource_type="model_assignment",
            resource_id=purpose,
            details={"model_profile_id": profile.id},
        )
    )
    db.commit()
    db.refresh(assignment)
    return assignment


@router.put("/sources/{source_id}/trust", response_model=SourceView)
def update_source_trust(
    source_id: str,
    payload: SourceTrustUpdate,
    admin: AdminUser,
    db: DbSession,
) -> Source:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source.trust_level = payload.trust_level
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="source.trust_updated",
            resource_type="source",
            resource_id=source.id,
            details={"trust_level": source.trust_level},
        )
    )
    db.commit()
    db.refresh(source)
    return source


@router.get("/jobs")
def list_jobs(admin: AdminUser, db: DbSession, limit: int = 100) -> list[dict[str, object]]:
    bounded_limit = min(max(limit, 1), 500)
    runs = db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(bounded_limit))
    return [
        {
            "id": run.id,
            "kind": run.kind,
            "status": run.status,
            "created_by_id": run.created_by_id,
            "error": run.error,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }
        for run in runs
    ]


@router.get("/audit")
def list_audit(admin: AdminUser, db: DbSession, limit: int = 100) -> list[dict[str, object]]:
    bounded_limit = min(max(limit, 1), 500)
    entries = db.scalars(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(bounded_limit)
    )
    return [
        {
            "id": entry.id,
            "actor_id": entry.actor_id,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "details": entry.details,
            "created_at": entry.created_at,
        }
        for entry in entries
    ]


@router.post("/jobs/rebuild-index")
def rebuild_wiki_index(
    admin: AdminUser,
    db: DbSession,
    repo: WikiRepo,
) -> dict[str, str]:
    factory = ModelFactory(db, get_settings())
    embedding_profile_id = factory.assigned_profile_id("embedding")
    embeddings = (
        factory.embeddings(embedding_profile_id) if embedding_profile_id is not None else None
    )
    commit = WikiIndexer(repo).rebuild(db, embeddings)
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="wiki.index_rebuilt",
            resource_type="wiki_commit",
            resource_id=commit,
            details={},
        )
    )
    db.commit()
    return {"commit": commit}


@router.get("/schema")
def read_schema(admin: AdminUser, repo: WikiRepo) -> dict[str, str]:
    return {"revision": repo.head(), "content": repo.read_schema()}


@router.put("/schema")
def update_schema(
    payload: SchemaUpdate,
    admin: AdminUser,
    db: DbSession,
    repo: WikiRepo,
) -> dict[str, str]:
    try:
        result = repo.publish_schema(
            payload.content,
            base_commit=payload.base_commit,
            author_name=admin.display_name,
            author_email=admin.email,
            message=payload.message,
        )
    except StaleWikiRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WikiRepositoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor_id=admin.id,
            action="schema.updated",
            resource_type="knowledge_schema",
            resource_id="AGENTS.md",
            details={"commit": result.commit},
        )
    )
    index_state = db.get(WikiIndexState, "published")
    if index_state is not None:
        index_state.commit_sha = result.commit
        index_state.indexed_at = datetime.now(UTC)
    db.commit()
    return {"revision": result.commit, "content": payload.content}
