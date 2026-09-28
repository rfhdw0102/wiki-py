import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from sqlalchemy import select

from wiki_agent.api.dependencies import AppSettings, CurrentUser, DbSession
from wiki_agent.api.schemas import LoginRequest, UserView
from wiki_agent.models import AuditLog, AuthSession, User, new_id
from wiki_agent.security import (
    create_refresh_token,
    create_token,
    hash_refresh_token,
    refresh_session_id,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserView)
def login(payload: LoginRequest, response: Response, db: DbSession, settings: AppSettings) -> User:
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    access = create_token(
        user.id, "access", settings, timedelta(minutes=settings.access_token_minutes)
    )
    session_id = new_id()
    refresh, refresh_hash = create_refresh_token(session_id)
    db.add(
        AuthSession(
            id=session_id,
            user_id=user.id,
            refresh_token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        )
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="auth.login",
            resource_type="auth_session",
            resource_id=session_id,
            details={},
        )
    )
    db.commit()
    secure = settings.env != "development"
    response.set_cookie(
        "access_token",
        access,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.access_token_minutes * 60,
    )
    response.set_cookie(
        "refresh_token",
        refresh,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1/auth",
    )
    response.set_cookie(
        "csrf_token",
        secrets.token_urlsafe(32),
        httponly=False,
        secure=secure,
        samesite="strict",
    )
    return user


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
def refresh(
    response: Response,
    db: DbSession,
    settings: AppSettings,
    refresh_token: str | None = Cookie(default=None),
) -> None:
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )
    try:
        session_id = refresh_session_id(refresh_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc
    auth_session = db.get(AuthSession, session_id)
    now = datetime.now(UTC)
    expires_at = auth_session.expires_at if auth_session is not None else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or expires_at < now
        or not secrets.compare_digest(
            auth_session.refresh_token_hash,
            hash_refresh_token(refresh_token),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    user = db.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
    access = create_token(
        user.id, "access", settings, timedelta(minutes=settings.access_token_minutes)
    )
    response.set_cookie(
        "access_token",
        access,
        httponly=True,
        secure=settings.env != "development",
        samesite="lax",
        max_age=settings.access_token_minutes * 60,
    )
    rotated_refresh, rotated_hash = create_refresh_token(auth_session.id)
    auth_session.refresh_token_hash = rotated_hash
    auth_session.expires_at = now + timedelta(days=settings.refresh_token_days)
    db.commit()
    response.set_cookie(
        "refresh_token",
        rotated_refresh,
        httponly=True,
        secure=settings.env != "development",
        samesite="strict",
        max_age=settings.refresh_token_days * 86400,
        path="/api/v1/auth",
    )
    response.set_cookie(
        "csrf_token",
        secrets.token_urlsafe(32),
        httponly=False,
        secure=settings.env != "development",
        samesite="strict",
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    db: DbSession,
    refresh_token: str | None = Cookie(default=None),
) -> None:
    if refresh_token:
        try:
            session_id = refresh_session_id(refresh_token)
        except ValueError:
            session_id = ""
        auth_session = db.get(AuthSession, session_id)
        if auth_session is not None:
            auth_session.revoked_at = datetime.now(UTC)
            db.add(
                AuditLog(
                    actor_id=auth_session.user_id,
                    action="auth.logout",
                    resource_type="auth_session",
                    resource_id=auth_session.id,
                    details={},
                )
            )
            db.commit()
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    response.delete_cookie("csrf_token")


@router.get("/me", response_model=UserView)
def me(user: CurrentUser) -> User:
    return user
