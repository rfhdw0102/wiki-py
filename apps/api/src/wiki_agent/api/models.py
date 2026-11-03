from time import perf_counter

import httpx
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select, update

from wiki_agent.api.dependencies import CurrentUser, DbSession
from wiki_agent.api.schemas import (
    ModelAssignmentUpdate,
    ModelAssignmentView,
    ModelConnectionTestRequest,
    ModelConnectionTestResult,
    ModelProfileCreate,
    ModelProfileUpdate,
    ModelProfileView,
)
from wiki_agent.config import get_settings
from wiki_agent.models import (
    AuditLog,
    ModelAssignment,
    ModelKind,
    ModelProfile,
    PersonalWiki,
    Source,
    SourceRevision,
    SourceSpan,
    WikiIndexState,
)
from wiki_agent.security import SecretBox, SecretEncryptionError

router = APIRouter(prefix="/models", tags=["models"])

MODEL_PURPOSES = {"planning", "compilation", "answer", "embedding", "reranker"}
MODEL_PURPOSE_KINDS = {
    "planning": ModelKind.CHAT,
    "compilation": ModelKind.CHAT,
    "answer": ModelKind.CHAT,
    "embedding": ModelKind.EMBEDDING,
    "reranker": ModelKind.RERANKER,
}


@router.get("", response_model=list[ModelProfileView])
def list_model_profiles(user: CurrentUser, db: DbSession) -> list[ModelProfile]:
    return list(
        db.scalars(
            select(ModelProfile).where(ModelProfile.owner_id == user.id).order_by(ModelProfile.name)
        )
    )


@router.post("", response_model=ModelProfileView, status_code=status.HTTP_201_CREATED)
def create_model_profile(
    payload: ModelProfileCreate,
    user: CurrentUser,
    db: DbSession,
) -> ModelProfile:
    existing = db.scalar(
        select(ModelProfile.id).where(
            ModelProfile.owner_id == user.id,
            ModelProfile.name == payload.name,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="模型配置名称已存在")
    try:
        ciphertext = SecretBox(get_settings().encryption_key).encrypt(payload.api_key)
    except SecretEncryptionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    profile = ModelProfile(
        owner_id=user.id,
        name=payload.name,
        provider="openai-compatible",
        kind=payload.kind,
        base_url=payload.base_url.rstrip("/"),
        model_name=payload.model_name,
        api_key_ciphertext=ciphertext,
        enabled=payload.enabled,
        config={},
    )
    db.add(profile)
    db.flush()
    db.add(
        AuditLog(
            actor_id=user.id,
            action="model_profile.created",
            resource_type="model_profile",
            resource_id=profile.id,
            details={"name": profile.name, "kind": profile.kind.value},
        )
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/test", response_model=ModelConnectionTestResult)
def test_model_connection(
    payload: ModelConnectionTestRequest,
    user: CurrentUser,
    db: DbSession,
) -> ModelConnectionTestResult:
    api_key = payload.api_key
    if api_key is None and payload.profile_id is not None:
        profile = db.scalar(
            select(ModelProfile).where(
                ModelProfile.id == payload.profile_id,
                ModelProfile.owner_id == user.id,
            )
        )
        if profile is None:
            raise HTTPException(status_code=404, detail="模型配置不存在")
        try:
            api_key = SecretBox(get_settings().encryption_key).decrypt(profile.api_key_ciphertext)
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not api_key:
        raise HTTPException(status_code=422, detail="请输入 API Key 后再测试连接")

    started_at = perf_counter()

    def result(healthy: bool, message: str) -> ModelConnectionTestResult:
        return ModelConnectionTestResult(
            healthy=healthy,
            message=message,
            latency_ms=max(1, round((perf_counter() - started_at) * 1000)),
        )

    try:
        response = httpx.get(
            f"{payload.base_url.rstrip('/')}/models",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=5,
        )
    except httpx.TimeoutException:
        return result(False, "连接超时，请检查 API 地址或网络状态")
    except httpx.RequestError:
        return result(False, "连接失败，请检查 API 地址是否正确")

    if response.status_code in {401, 403}:
        return result(False, "API Key 无效或没有访问权限")
    if response.status_code == 429:
        return result(True, "连接成功，服务当前触发限流")
    if not response.is_success:
        return result(False, f"服务返回 HTTP {response.status_code}")

    try:
        response_data = response.json()
    except ValueError:
        return result(False, "响应不是有效 JSON，不符合 OpenAI 兼容协议")
    models = response_data.get("data") if isinstance(response_data, dict) else None
    if not isinstance(models, list):
        return result(False, "响应缺少 models.data，不符合 OpenAI 兼容协议")
    model_ids = {
        item.get("id")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if payload.model_name not in model_ids:
        return result(False, f"连接成功，但未找到模型 {payload.model_name}")
    return result(True, f"连接成功，模型 {payload.model_name} 可用")


@router.put("/{profile_id}", response_model=ModelProfileView)
def update_model_profile(
    profile_id: str,
    payload: ModelProfileUpdate,
    user: CurrentUser,
    db: DbSession,
) -> ModelProfile:
    profile = db.scalar(
        select(ModelProfile).where(
            ModelProfile.id == profile_id,
            ModelProfile.owner_id == user.id,
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    duplicate = db.scalar(
        select(ModelProfile.id).where(
            ModelProfile.owner_id == user.id,
            ModelProfile.name == payload.name,
            ModelProfile.id != profile.id,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="模型配置名称已存在")
    embedding_configuration_changed = profile.kind is ModelKind.EMBEDDING and (
        profile.base_url != payload.base_url.rstrip("/")
        or profile.model_name != payload.model_name
        or payload.api_key is not None
    )
    profile.name = payload.name
    profile.provider = "openai-compatible"
    profile.base_url = payload.base_url.rstrip("/")
    profile.model_name = payload.model_name
    profile.enabled = payload.enabled
    profile.config = {}
    if payload.api_key is not None:
        try:
            profile.api_key_ciphertext = SecretBox(get_settings().encryption_key).encrypt(
                payload.api_key
            )
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    embedding_assignment = db.get(ModelAssignment, (user.id, "embedding"))
    if (
        embedding_configuration_changed
        and embedding_assignment is not None
        and embedding_assignment.model_profile_id == profile.id
    ):
        owned_wiki_ids = select(PersonalWiki.id).where(PersonalWiki.owner_id == user.id)
        db.execute(
            update(WikiIndexState)
            .where(WikiIndexState.wiki_id.in_(owned_wiki_ids))
            .values(embedding_profile_id=None)
        )
        owned_revision_ids = (
            select(SourceRevision.id)
            .join(Source, Source.id == SourceRevision.source_id)
            .where(Source.wiki_id.in_(owned_wiki_ids))
        )
        db.execute(
            update(SourceSpan)
            .where(
                SourceSpan.source_revision_id.in_(owned_revision_ids),
                SourceSpan.embedding_profile_id == profile.id,
            )
            .values(embedding_profile_id=None)
        )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="model_profile.updated",
            resource_type="model_profile",
            resource_id=profile.id,
            details={"name": profile.name, "kind": profile.kind.value},
        )
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_profile(
    profile_id: str,
    user: CurrentUser,
    db: DbSession,
) -> None:
    profile = db.scalar(
        select(ModelProfile).where(
            ModelProfile.id == profile_id,
            ModelProfile.owner_id == user.id,
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    db.add(
        AuditLog(
            actor_id=user.id,
            action="model_profile.deleted",
            resource_type="model_profile",
            resource_id=profile.id,
            details={"name": profile.name},
        )
    )
    db.execute(
        delete(ModelAssignment).where(
            ModelAssignment.user_id == user.id,
            ModelAssignment.model_profile_id == profile.id,
        )
    )
    db.delete(profile)
    db.commit()


@router.get("/assignments", response_model=list[ModelAssignmentView])
def list_model_assignments(user: CurrentUser, db: DbSession) -> list[ModelAssignment]:
    return list(
        db.scalars(
            select(ModelAssignment)
            .where(ModelAssignment.user_id == user.id)
            .order_by(ModelAssignment.purpose)
        )
    )


@router.put("/assignments/{purpose}", response_model=ModelAssignmentView)
def update_model_assignment(
    purpose: str,
    payload: ModelAssignmentUpdate,
    user: CurrentUser,
    db: DbSession,
) -> ModelAssignment:
    if purpose not in MODEL_PURPOSES:
        raise HTTPException(status_code=422, detail=f"不支持的模型用途：{purpose}")
    profile = db.scalar(
        select(ModelProfile).where(
            ModelProfile.id == payload.model_profile_id,
            ModelProfile.owner_id == user.id,
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    if profile.kind is not MODEL_PURPOSE_KINDS[purpose]:
        raise HTTPException(
            status_code=422,
            detail=f"该用途需要 {MODEL_PURPOSE_KINDS[purpose].value} 类型模型",
        )
    assignment = db.get(ModelAssignment, (user.id, purpose))
    if assignment is None:
        assignment = ModelAssignment(
            user_id=user.id,
            purpose=purpose,
            model_profile_id=profile.id,
        )
    else:
        assignment.model_profile_id = profile.id
    db.add(assignment)
    db.add(
        AuditLog(
            actor_id=user.id,
            action="model_assignment.updated",
            resource_type="model_assignment",
            resource_id=f"{user.id}:{purpose}",
            details={"model_profile_id": profile.id},
        )
    )
    db.commit()
    db.refresh(assignment)
    return assignment
