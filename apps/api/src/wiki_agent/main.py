import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import jwt
from fastapi import FastAPI, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from wiki_agent.api import (
    agents,
    auth,
    browser_import,
    features,
    graph,
    logs,
    models,
    questions,
    sources,
    wiki,
    wikis,
)
from wiki_agent.auth import CaptchaGenerator, EmailSender, VerificationStore
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import (
    Base,
    SessionLocal,
    engine,
    ensure_conversation_title_schema,
    ensure_question_checkpoint_schema,
)
from wiki_agent.models import ApiErrorLog, FeatureFlag, User, UserRole, new_id
from wiki_agent.security import decode_token, hash_password

DEFAULT_FEATURES = {
    "source_upload": True,
    "website_crawl": True,
    "graph_view": True,
    "wiki_questions": True,
    "raw_rag": True,
    "auto_publish": True,
    "wiki_editing": True,
    "wiki_rollback": True,
}


def bootstrap() -> None:
    settings = get_settings()
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
        ensure_question_checkpoint_schema(engine)
        ensure_conversation_title_schema(engine)
    with SessionLocal() as db:
        ensure_default_user(db, settings)
        for key, enabled in DEFAULT_FEATURES.items():
            if db.get(FeatureFlag, key) is None:
                config: dict[str, object] = {}
                if key == "auto_publish":
                    config = {
                        "max_changed_pages": 3,
                        "max_changed_ratio": 0.2,
                        "max_new_claims": 20,
                        "minimum_source_trust": 2,
                    }
                db.add(FeatureFlag(key=key, enabled=enabled, config=config))
        db.commit()


def ensure_default_user(db: Session, settings: Settings) -> User | None:
    if db.scalar(select(User.id).limit(1)) is not None:
        return None
    username = settings.initial_admin_username.strip().lower()
    user = User(
        id=new_id(),
        username=username,
        email=None,
        password_hash=hash_password(settings.initial_admin_password.get_secret_value()),
        display_name="管理员",
        role=UserRole.USER,
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    return user


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    bootstrap()
    verification_store = VerificationStore(settings)
    app.state.verification_store = verification_store
    app.state.captcha_generator = CaptchaGenerator()
    app.state.email_sender = EmailSender(settings)
    try:
        yield
    finally:
        await verification_store.close()


settings = get_settings()
app = FastAPI(
    title="LLM Wiki Agent API",
    version="0.1.0",
    description="Compile immutable sources into a versioned, human-readable Wiki.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_response(
    request: Request, exc: RequestValidationError,
) -> Response:
    if request.url.path.startswith("/api/v1/sources/browser-sessions"):
        return JSONResponse(
            status_code=422, content={"detail": "浏览器操作参数无效，请检查后重试。"},
        )
    return await request_validation_exception_handler(request, exc)


@app.middleware("http")
async def verify_csrf(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        uses_cookie_auth = bool(
            request.cookies.get("access_token") or request.cookies.get("refresh_token")
        )
        uses_bearer_auth = request.headers.get("authorization", "").lower().startswith("bearer ")
        if uses_cookie_auth and not uses_bearer_auth:
            cookie_token = request.cookies.get("csrf_token")
            header_token = request.headers.get("x-csrf-token")
            if (
                not cookie_token
                or not header_token
                or not secrets.compare_digest(cookie_token, header_token)
            ):
                return JSONResponse(status_code=403, content={"detail": "Invalid CSRF token"})
    token = request.cookies.get("access_token")
    authorization = request.headers.get("authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer" and credentials:
        token = credentials
    if token:
        with suppress(jwt.PyJWTError):
            request.state.user_id = decode_token(token, "access", settings)
    try:
        response = await call_next(request)
    except Exception as exc:
        record_api_error(request, type(exc).__name__, str(exc))
        raise
    if (
        response.status_code >= 400
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and not request.url.path.startswith("/api/v1/questions")
    ):
        record_api_error(
            request,
            "HTTPError",
            f"API returned HTTP {response.status_code}",
        )
    return response


def record_api_error(request: Request, error_type: str, message: str) -> None:
    if getattr(request.state, "api_error_recorded", False):
        return
    try:
        with SessionLocal() as db:
            db.add(
                ApiErrorLog(
                    user_id=getattr(request.state, "user_id", None),
                    method=request.method[:16],
                    path=request.url.path[:1024],
                    error_type=error_type[:256],
                    message=message[:8000],
                )
            )
            db.commit()
            request.state.api_error_recorded = True
    except SQLAlchemyError:
        logging.getLogger(__name__).exception("Failed to persist API error log")


app.include_router(auth.router, prefix="/api/v1")
app.include_router(models.router, prefix="/api/v1")
app.include_router(wikis.router, prefix="/api/v1")
app.include_router(browser_import.router, prefix="/api/v1")
app.include_router(sources.router, prefix="/api/v1")
app.include_router(wiki.router, prefix="/api/v1")
app.include_router(questions.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(graph.router, prefix="/api/v1")
app.include_router(features.router, prefix="/api/v1")
app.include_router(logs.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
