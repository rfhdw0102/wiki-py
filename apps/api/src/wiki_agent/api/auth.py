import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import Annotated, cast

from fastapi import APIRouter, Cookie, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from wiki_agent.api.dependencies import AppSettings, CurrentUser, DbSession
from wiki_agent.api.schemas import (
    AccountDeletionRequest,
    CaptchaChallengeView,
    CaptchaVerifyRequest,
    CaptchaVerifyResponse,
    EmailBindingCodeRequest,
    EmailBindingRequest,
    EmailCodeRequest,
    LoginRequest,
    MessageView,
    PasswordChangeRequest,
    PasswordRecoveryRequest,
    ProfileUpdateRequest,
    UserView,
)
from wiki_agent.auth import (
    AuthPurpose,
    CaptchaChallengeState,
    CaptchaGenerator,
    EmailDeliveryError,
    EmailSender,
    VerificationStore,
)
from wiki_agent.models import (
    AuditLog,
    AuthSession,
    ModelAssignment,
    ModelProfile,
    User,
    new_id,
)
from wiki_agent.security import (
    create_refresh_token,
    create_token,
    hash_password,
    hash_refresh_token,
    refresh_session_id,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_SIZE = 512


def _verification_store(request: Request) -> VerificationStore:
    return cast(VerificationStore, request.app.state.verification_store)


def _captcha_generator(request: Request) -> CaptchaGenerator:
    return cast(CaptchaGenerator, request.app.state.captcha_generator)


def _email_sender(request: Request) -> EmailSender:
    return cast(EmailSender, request.app.state.email_sender)


def _client_host(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _normalize_email(email: object) -> str:
    return str(email).strip().lower()


@router.get("/captcha/challenge", response_model=CaptchaChallengeView)
async def create_captcha_challenge(
    request: Request,
) -> CaptchaChallengeView:
    store = _verification_store(request)
    allowed = await store.check_rate_limit(
        "captcha",
        _client_host(request),
        limit=30,
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="验证码请求过于频繁，请稍后再试")

    challenge = _captcha_generator(request).generate()
    challenge_id, expires_in = await store.save_captcha_challenge(
        CaptchaChallengeState(target_x=challenge.target_x, purpose=AuthPurpose.LOGIN)
    )
    return CaptchaChallengeView(
        challenge_id=challenge_id,
        background_image=challenge.background_image,
        puzzle_image=challenge.puzzle_image,
        image_width=CaptchaGenerator.width,
        image_height=CaptchaGenerator.height,
        puzzle_width=challenge.puzzle_width,
        puzzle_height=challenge.puzzle_height,
        puzzle_top=challenge.puzzle_top,
        expires_in=expires_in,
    )


@router.post("/captcha/verify", response_model=CaptchaVerifyResponse)
async def verify_captcha(
    payload: CaptchaVerifyRequest,
    request: Request,
    settings: AppSettings,
) -> CaptchaVerifyResponse:
    store = _verification_store(request)
    challenge = await store.consume_captcha_challenge(payload.challenge_id)
    valid_trail = payload.trail[-1][0] > payload.trail[0][0]
    if (
        challenge is None
        or payload.duration < settings.captcha_min_duration_ms
        or abs(payload.x - challenge.target_x) > settings.captcha_position_tolerance_px
        or not valid_trail
    ):
        raise HTTPException(status_code=400, detail="滑块验证失败，请重试")
    token, expires_in = await store.issue_captcha_token(challenge.purpose)
    return CaptchaVerifyResponse(captcha_token=token, expires_in=expires_in)


@router.post("/email-code", response_model=MessageView)
async def send_email_code(
    payload: EmailCodeRequest,
    request: Request,
    db: DbSession,
) -> MessageView:
    if payload.purpose is not AuthPurpose.PASSWORD_RESET:
        raise HTTPException(status_code=422, detail="不支持该邮箱验证用途")

    email = _normalize_email(payload.email)

    store = _verification_store(request)
    allowed = await store.check_rate_limit(
        "email-code-ip",
        _client_host(request),
        limit=10,
        window_seconds=600,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="验证码请求过于频繁，请稍后再试")
    if not await store.acquire_email_cooldown(email, payload.purpose):
        raise HTTPException(status_code=429, detail="验证码发送过于频繁，请稍后再试")

    user = db.scalar(select(User).where(User.email == email))
    should_send = user is not None and user.is_active
    if should_send:
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            await _email_sender(request).send_verification_code(
                email,
                code,
                payload.purpose,
            )
        except EmailDeliveryError as exc:
            await store.release_email_cooldown(email, payload.purpose)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="邮件验证码服务暂时不可用，请稍后再试",
            ) from exc
        await store.save_email_code(email, payload.purpose, code)
        db.add(
            AuditLog(
                actor_id=user.id if user is not None else None,
                action="auth.email_code_sent",
                resource_type="user",
                resource_id=user.id if user is not None else None,
                details={"purpose": payload.purpose.value},
            )
        )
        db.commit()

    return MessageView(message="如果该邮箱符合条件，验证码将发送到对应邮箱")


@router.post("/login", response_model=UserView)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: AppSettings,
) -> User:
    store = _verification_store(request)
    if not await store.consume_captcha_token(payload.captcha_token, AuthPurpose.LOGIN):
        raise HTTPException(status_code=400, detail="滑块验证已失效，请重新验证")
    username = payload.username.strip().lower()
    ip_allowed = await store.check_rate_limit(
        "login-ip",
        _client_host(request),
        limit=10,
        window_seconds=300,
    )
    username_allowed = await store.check_rate_limit(
        "login-username",
        username,
        limit=10,
        window_seconds=300,
    )
    if not ip_allowed or not username_allowed:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    user = db.scalar(
        select(User).where((User.username == username) | (User.email == username))
    )
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
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
            details={"session_created": True},
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


@router.post("/password-recovery", response_model=MessageView)
async def recover_password(
    payload: PasswordRecoveryRequest,
    request: Request,
    db: DbSession,
) -> MessageView:
    email = _normalize_email(payload.email)
    store = _verification_store(request)
    if not await store.consume_email_code(
        email,
        AuthPurpose.PASSWORD_RESET,
        payload.email_code,
    ):
        raise HTTPException(status_code=400, detail="邮箱验证码错误或已失效")
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="邮箱验证码错误或已失效")

    user.password_hash = hash_password(payload.password)
    user.must_change_password = False
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="auth.password_reset",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    db.commit()
    return MessageView(message="密码已更新")


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


@router.patch("/me", response_model=UserView)
def update_profile(
    payload: ProfileUpdateRequest,
    user: CurrentUser,
    db: DbSession,
) -> User:
    user.display_name = payload.display_name.strip()
    db.add(
        AuditLog(
            actor_id=user.id,
            action="account.profile_updated",
            resource_type="user",
            resource_id=user.id,
            details={"display_name": user.display_name},
        )
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/me/email-code", response_model=MessageView)
async def send_email_binding_code(
    payload: EmailBindingCodeRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> MessageView:
    email = _normalize_email(payload.email)
    if user.email is not None:
        raise HTTPException(status_code=409, detail="邮箱已绑定，不能再次修改")
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="该邮箱已被其他账号绑定")

    store = _verification_store(request)
    if not await store.check_rate_limit(
        "email-code-ip",
        _client_host(request),
        limit=10,
        window_seconds=600,
    ):
        raise HTTPException(status_code=429, detail="验证码请求过于频繁，请稍后再试")
    if not await store.acquire_email_cooldown(email, AuthPurpose.EMAIL_BIND):
        raise HTTPException(status_code=429, detail="验证码发送过于频繁，请稍后再试")
    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        await _email_sender(request).send_verification_code(
            email,
            code,
            AuthPurpose.EMAIL_BIND,
        )
    except EmailDeliveryError as exc:
        await store.release_email_cooldown(email, AuthPurpose.EMAIL_BIND)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="邮件验证码服务暂时不可用，请稍后再试",
        ) from exc
    await store.save_email_code(email, AuthPurpose.EMAIL_BIND, code)
    db.add(
        AuditLog(
            actor_id=user.id,
            action="account.email_code_sent",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    db.commit()
    return MessageView(message="验证码已发送")


@router.post("/me/email", response_model=UserView)
async def bind_email(
    payload: EmailBindingRequest,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> User:
    if user.email is not None:
        raise HTTPException(status_code=409, detail="邮箱已绑定，不能再次修改")
    email = _normalize_email(payload.email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="该邮箱已被其他账号绑定")
    store = _verification_store(request)
    if not await store.consume_email_code(email, AuthPurpose.EMAIL_BIND, payload.email_code):
        raise HTTPException(status_code=400, detail="邮箱验证码错误或已失效")
    binding = db.execute(
        update(User)
        .where(User.id == user.id, User.email.is_(None))
        .values(email=email)
        .returning(User.id)
    ).scalar_one_or_none()
    if binding is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="邮箱已绑定，不能再次修改")
    db.add(
        AuditLog(
            actor_id=user.id,
            action="account.email_bound",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该邮箱已被其他账号绑定") from exc
    db.refresh(user)
    return user


@router.get("/me/avatar")
def read_avatar(user: CurrentUser, settings: AppSettings) -> FileResponse:
    if user.avatar_path is None:
        raise HTTPException(status_code=404, detail="尚未上传头像")
    path = (settings.storage_root / user.avatar_path).resolve()
    avatar_root = (settings.storage_root / "avatars").resolve()
    if not path.is_relative_to(avatar_root) or not path.is_file():
        raise HTTPException(status_code=404, detail="头像文件不存在")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.put("/me/avatar", response_model=UserView)
async def upload_avatar(
    user: CurrentUser,
    settings: AppSettings,
    db: DbSession,
    file: Annotated[UploadFile, File()],
) -> User:
    content = await file.read(AVATAR_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="头像文件不能为空")
    if len(content) > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="头像文件不能超过 2 MB")
    try:
        with Image.open(BytesIO(content)) as source_image:
            source_image.verify()
        with Image.open(BytesIO(content)) as source_image:
            image = ImageOps.exif_transpose(source_image)
            image.thumbnail((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="请上传有效的 PNG、JPEG 或 WebP 图片") from exc

    avatar_bytes = output.getvalue()
    relative_path = f"avatars/{user.id}/{sha256(avatar_bytes).hexdigest()}.png"
    destination = (settings.storage_root / relative_path).resolve()
    avatar_root = (settings.storage_root / "avatars").resolve()
    if not destination.is_relative_to(avatar_root):
        raise HTTPException(status_code=500, detail="头像存储路径无效")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(avatar_bytes)
    previous_path = (
        (settings.storage_root / user.avatar_path).resolve()
        if user.avatar_path is not None
        else None
    )
    user.avatar_path = relative_path
    db.add(
        AuditLog(
            actor_id=user.id,
            action="account.avatar_updated",
            resource_type="user",
            resource_id=user.id,
            details={"avatar_updated": True},
        )
    )
    db.commit()
    db.refresh(user)
    if (
        previous_path is not None
        and previous_path != destination
        and previous_path.is_relative_to(avatar_root)
        and previous_path.is_file()
    ):
        previous_path.unlink()
    return user


@router.post("/me/change-password", response_model=MessageView)
def change_password(
    payload: PasswordChangeRequest,
    response: Response,
    user: CurrentUser,
    db: DbSession,
) -> MessageView:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=422, detail="新密码不能与当前密码相同")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.add(
        AuditLog(
            actor_id=user.id,
            action="account.password_changed",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    db.commit()
    _clear_auth_cookies(response)
    return MessageView(message="密码已修改，请重新登录")


@router.delete("/me", response_model=MessageView)
def delete_account(
    payload: AccountDeletionRequest,
    response: Response,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> MessageView:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    previous_avatar = (
        (settings.storage_root / user.avatar_path).resolve()
        if user.avatar_path is not None
        else None
    )
    user.email = f"deleted-{user.id}@deleted.invalid"
    user.display_name = "已注销用户"
    user.password_hash = hash_password(secrets.token_urlsafe(48))
    user.avatar_path = None
    user.is_active = False
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    db.execute(delete(ModelAssignment).where(ModelAssignment.user_id == user.id))
    db.execute(delete(ModelProfile).where(ModelProfile.owner_id == user.id))
    db.add(
        AuditLog(
            actor_id=user.id,
            action="account.deleted",
            resource_type="user",
            resource_id=user.id,
            details={},
        )
    )
    db.commit()
    avatar_root = (settings.storage_root / "avatars").resolve()
    if (
        previous_avatar is not None
        and previous_avatar.is_relative_to(avatar_root)
        and previous_avatar.is_file()
    ):
        previous_avatar.unlink()
    _clear_auth_cookies(response)
    return MessageView(message="账号已注销")


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token", path="/api/v1/auth")
    response.delete_cookie("csrf_token")
