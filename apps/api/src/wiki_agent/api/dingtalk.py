from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from wiki_agent.api.dependencies import (
    AppSettings,
    CurrentUser,
    DbSession,
    WikiRepo,
    require_feature,
)
from wiki_agent.integrations.dingtalk_import import import_document, import_error, query_import
from wiki_agent.integrations.dingtalk_mock import FIXTURE_VERSION, NOTICE, list_documents

router = APIRouter(prefix="/integrations/dingtalk", tags=["dingtalk simulation"])


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1, max_length=128)
    request_id: UUID


@router.get("/status")
def integration_status(user: CurrentUser, settings: AppSettings) -> dict[str, object]:
    mock = settings.dingtalk_mode == "mock"
    return {
        "mode": settings.dingtalk_mode,
        "simulation": mock,
        "real_connection": "not_implemented",
        "fixture_version": FIXTURE_VERSION if mock else None,
    }


@router.get("/documents")
def documents(user: CurrentUser, settings: AppSettings) -> dict[str, object]:
    if settings.dingtalk_mode != "mock":
        raise import_error("integration_disabled", 409)
    return {
        "mode": "mock",
        "simulation": True,
        "notice": NOTICE,
        "fixture_version": FIXTURE_VERSION,
        "items": list_documents(),
    }


@router.post("/imports", dependencies=[Depends(require_feature("wiki_editing"))])
def create_import(
    payload: ImportRequest,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    repo: WikiRepo,
    settings: AppSettings,
) -> dict[str, object]:
    result, created = import_document(
        db, repo, settings, user, payload.document_id, str(payload.request_id)
    )
    response.status_code = 201 if created else 200
    return result


@router.get("/imports/{request_id}")
def read_import(
    request_id: UUID, user: CurrentUser, db: DbSession, repo: WikiRepo
) -> dict[str, object]:
    return query_import(db, repo, user.id, str(request_id))
