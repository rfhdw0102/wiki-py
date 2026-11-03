from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request, Response, UploadFile
from PIL import Image
from redis.asyncio import Redis
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from wiki_agent.api.auth import (
    bind_email,
    change_password,
    delete_account,
    login,
    recover_password,
    send_email_binding_code,
    update_profile,
    upload_avatar,
)
from wiki_agent.api.dependencies import get_current_user
from wiki_agent.api.schemas import (
    AccountDeletionRequest,
    EmailBindingCodeRequest,
    EmailBindingRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordRecoveryRequest,
    ProfileUpdateRequest,
)
from wiki_agent.auth import (
    AuthPurpose,
    CaptchaGenerator,
    EmailDeliveryError,
    EmailSender,
    VerificationStore,
)
from wiki_agent.config import Settings
from wiki_agent.db import Base
from wiki_agent.main import ensure_default_user
from wiki_agent.models import AuthSession, User, UserRole
from wiki_agent.security import create_token, hash_password, verify_password


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def setex(self, key: str, seconds: int, value: str) -> bool:
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool,
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        return key in self.values


def make_store(settings: Settings) -> VerificationStore:
    redis = cast(Redis, cast(Any, MemoryRedis()))
    return VerificationStore(settings, redis=redis)


def make_request(store: VerificationStore) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            verification_store=store,
        )
    )
    return Request(
        {
            "type": "http",
            "app": app,
            "client": ("127.0.0.1", 50000),
            "headers": [],
            "method": "POST",
            "path": "/",
        }
    )


@pytest.mark.asyncio
async def test_captcha_tokens_are_single_use_and_purpose_bound() -> None:
    settings = Settings(_env_file=None)
    store = make_store(settings)
    token, _ = await store.issue_captcha_token(AuthPurpose.LOGIN)

    assert not await store.consume_captcha_token(token, AuthPurpose.EMAIL_BIND)
    assert not await store.consume_captcha_token(token, AuthPurpose.LOGIN)

    token, _ = await store.issue_captcha_token(AuthPurpose.LOGIN)
    assert await store.consume_captcha_token(token, AuthPurpose.LOGIN)
    assert not await store.consume_captcha_token(token, AuthPurpose.LOGIN)


@pytest.mark.asyncio
async def test_email_code_expires_after_maximum_failed_attempts() -> None:
    settings = Settings(_env_file=None, email_code_max_attempts=3)
    store = make_store(settings)
    await store.save_email_code("person@qq.com", AuthPurpose.PASSWORD_RESET, "123456")

    assert not await store.consume_email_code(
        "person@qq.com", AuthPurpose.PASSWORD_RESET, "000000"
    )
    assert not await store.consume_email_code(
        "person@qq.com", AuthPurpose.PASSWORD_RESET, "000000"
    )
    assert not await store.consume_email_code(
        "person@qq.com", AuthPurpose.PASSWORD_RESET, "000000"
    )
    assert not await store.consume_email_code(
        "person@qq.com", AuthPurpose.PASSWORD_RESET, "123456"
    )


def test_captcha_generator_returns_frontend_ready_images() -> None:
    challenge = CaptchaGenerator().generate()

    assert challenge.background_image.startswith("data:image/jpeg;base64,")
    assert challenge.puzzle_image.startswith("data:image/png;base64,")
    assert challenge.puzzle_width == 48
    assert 0 < challenge.target_x < CaptchaGenerator.width - challenge.puzzle_width


@pytest.mark.asyncio
async def test_email_sender_fails_explicitly_when_smtp_is_unconfigured() -> None:
    sender = EmailSender(Settings(_env_file=None))

    with pytest.raises(EmailDeliveryError, match="SMTP is not configured"):
        await sender.send_verification_code(
            "person@qq.com",
            "123456",
            AuthPurpose.PASSWORD_RESET,
        )


@pytest.mark.asyncio
async def test_username_login_and_password_recovery_flow() -> None:
    settings = Settings(_env_file=None)
    store = make_store(settings)
    request = make_request(store)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(
            username="admin",
            email="person@qq.com",
            display_name="管理员",
            password_hash=hash_password("long-password-1"),
            role=UserRole.USER,
        )
        db.add(user)
        db.commit()

        captcha_token, _ = await store.issue_captcha_token(AuthPurpose.LOGIN)
        response = Response()
        logged_in = await login(
            LoginRequest(
                username="admin",
                password="long-password-1",
                captcha_token=captcha_token,
            ),
            request,
            response,
            db,
            settings,
        )
        assert logged_in.id == user.id
        assert "access_token=" in response.headers["set-cookie"]

        session = db.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
        assert session is not None
        assert session.revoked_at is None

        await store.save_email_code(
            "person@qq.com",
            AuthPurpose.PASSWORD_RESET,
            "654321",
        )
        await recover_password(
            PasswordRecoveryRequest(
                email="person@qq.com",
                password="new-long-password-2",
                email_code="654321",
            ),
            request,
            db,
        )

        db.refresh(session)
        db.refresh(user)
        assert session.revoked_at is not None
        assert verify_password("new-long-password-2", user.password_hash)


@pytest.mark.asyncio
async def test_login_rejects_replayed_captcha_token() -> None:
    settings = Settings(_env_file=None)
    store = make_store(settings)
    request = make_request(store)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(
            User(
                email="person@qq.com",
                display_name="测试用户",
                password_hash=hash_password("long-password-1"),
                role=UserRole.USER,
            )
        )
        db.commit()
        token, _ = await store.issue_captcha_token(AuthPurpose.LOGIN)
        payload = LoginRequest(
            username="person@qq.com",
            password="long-password-1",
            captcha_token=token,
        )
        await login(payload, request, Response(), db, settings)

        with pytest.raises(HTTPException) as exc_info:
            await login(payload, request, Response(), db, settings)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_user_can_update_profile_avatar_and_password(tmp_path) -> None:
    settings = Settings(_env_file=None, storage_root=tmp_path)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(
            email="person@qq.com",
            display_name="原名称",
            password_hash=hash_password("long-password-1"),
            role=UserRole.USER,
            must_change_password=True,
        )
        db.add(user)
        db.flush()
        session = AuthSession(
            user_id=user.id,
            refresh_token_hash="hash",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(session)
        db.commit()

        updated = update_profile(ProfileUpdateRequest(display_name="新名称"), user, db)
        assert updated.display_name == "新名称"

        image_bytes = BytesIO()
        Image.new("RGB", (64, 64), "#2c7a65").save(image_bytes, format="PNG")
        avatar = await upload_avatar(
            user,
            settings,
            db,
            UploadFile(filename="avatar.png", file=BytesIO(image_bytes.getvalue())),
        )
        assert avatar.avatar_path is not None
        assert (tmp_path / avatar.avatar_path).is_file()

        response = Response()
        message = change_password(
            PasswordChangeRequest(
                current_password="long-password-1",
                new_password="new-long-password-2",
            ),
            response,
            user,
            db,
        )
        db.refresh(session)
        assert message.message == "密码已修改，请重新登录"
        assert session.revoked_at is not None
        assert verify_password("new-long-password-2", user.password_hash)
        assert user.must_change_password is False


def test_account_deletion_anonymizes_user_and_revokes_access(tmp_path) -> None:
    settings = Settings(_env_file=None, storage_root=tmp_path)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(
            email="person@qq.com",
            display_name="测试用户",
            password_hash=hash_password("long-password-1"),
            role=UserRole.USER,
        )
        db.add(user)
        db.commit()

        result = delete_account(
            AccountDeletionRequest(current_password="long-password-1"),
            Response(),
            user,
            db,
            settings,
        )

        assert result.message == "账号已注销"
        assert user.is_active is False
        assert user.email == f"deleted-{user.id}@deleted.invalid"
        assert user.display_name == "已注销用户"


def test_default_user_is_created_once_with_empty_email_and_forced_password_change() -> None:
    settings = Settings(_env_file=None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = ensure_default_user(db, settings)
        assert user is not None
        assert user.username == "admin"
        assert user.email is None
        assert user.must_change_password is True
        assert verify_password("WikiAdmin123!", user.password_hash)
        db.commit()

        assert ensure_default_user(db, settings) is None
        db.refresh(user)
        assert user.email is None


def test_first_login_recommendation_does_not_block_business_endpoints() -> None:
    settings = Settings(_env_file=None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(
            username="admin",
            email=None,
            display_name="管理员",
            password_hash=hash_password("WikiAdmin123!"),
            role=UserRole.USER,
            must_change_password=True,
        )
        db.add(user)
        db.commit()
        token = create_token(user.id, "access", settings, timedelta(minutes=15))

        request = make_request(make_store(settings))
        request.scope["path"] = "/api/v1/wiki/pages"
        request.scope["method"] = "GET"
        assert get_current_user(
            request,
            db,
            settings,
            authorization=f"Bearer {token}",
        ).id == user.id

        me_request = make_request(make_store(settings))
        me_request.scope["path"] = "/api/v1/auth/me"
        me_request.scope["method"] = "GET"
        assert get_current_user(
            me_request,
            db,
            settings,
            authorization=f"Bearer {token}",
        ).id == user.id


@pytest.mark.asyncio
async def test_email_can_be_bound_once_after_verification() -> None:
    settings = Settings(_env_file=None)
    store = make_store(settings)
    request = make_request(store)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class CapturingSender:
        code: str | None = None

        async def send_verification_code(
            self,
            recipient: str,
            code: str,
            purpose: AuthPurpose,
        ) -> None:
            assert recipient == "person@qq.com"
            assert purpose is AuthPurpose.EMAIL_BIND
            self.code = code

    sender = CapturingSender()
    request.app.state.email_sender = sender
    with Session(engine) as db:
        user = User(
            username="admin",
            email=None,
            display_name="管理员",
            password_hash=hash_password("long-password-1"),
            role=UserRole.USER,
        )
        db.add(user)
        db.commit()

        await send_email_binding_code(
            EmailBindingCodeRequest(email="person@qq.com"),
            request,
            user,
            db,
        )
        assert sender.code is not None
        bound_user = await bind_email(
            EmailBindingRequest(email="person@qq.com", email_code=sender.code),
            request,
            user,
            db,
        )
        assert bound_user.email == "person@qq.com"

        with pytest.raises(HTTPException) as exc_info:
            await bind_email(
                EmailBindingRequest(email="other@qq.com", email_code=sender.code),
                request,
                user,
                db,
            )
        assert exc_info.value.status_code == 409
