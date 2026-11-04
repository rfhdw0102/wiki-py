from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from wiki_agent.config import Settings, get_settings
from wiki_agent.db import get_db
from wiki_agent.models import FeatureFlag, PersonalWiki, User
from wiki_agent.security import decode_token
from wiki_agent.wiki import WikiRepository, open_wiki_repository

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_current_user(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie()] = None,
) -> User:
    token = access_token
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials:
            token = credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        user_id = decode_token(token, "access", settings)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token"
        ) from exc
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_wiki(
    user: CurrentUser,
    db: DbSession,
    wiki_id: Annotated[str | None, Header(alias="X-Wiki-ID")] = None,
) -> PersonalWiki:
    if not wiki_id:
        raise HTTPException(status_code=400, detail="X-Wiki-ID header is required")
    wiki = db.get(PersonalWiki, wiki_id)
    if wiki is None or wiki.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Wiki not found")
    return wiki


CurrentWiki = Annotated[PersonalWiki, Depends(get_current_wiki)]


def get_wiki_repository(wiki: CurrentWiki, settings: AppSettings) -> WikiRepository:
    return open_wiki_repository(
        settings.wiki_library_root(wiki.id),
        settings.legacy_wiki_library_root(wiki.id),
    )


WikiRepo = Annotated[WikiRepository, Depends(get_wiki_repository)]

def require_feature(key: str) -> Callable[[Session], None]:
    def check_feature(db: DbSession) -> None:
        flag = db.get(FeatureFlag, key)
        if flag is None or not flag.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature is disabled: {key}",
            )

    return check_feature
