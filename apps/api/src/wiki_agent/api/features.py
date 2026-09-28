from fastapi import APIRouter
from sqlalchemy import select

from wiki_agent.api.dependencies import CurrentUser, DbSession
from wiki_agent.api.schemas import FeatureFlagView
from wiki_agent.models import FeatureFlag

router = APIRouter(prefix="/features", tags=["features"])


@router.get("", response_model=list[FeatureFlagView])
def list_enabled_features(user: CurrentUser, db: DbSession) -> list[FeatureFlag]:
    return list(db.scalars(select(FeatureFlag).order_by(FeatureFlag.key)))
