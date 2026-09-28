import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from wiki_agent.api import (
    admin,
    agents,
    auth,
    features,
    graph,
    knowledge_gaps,
    sources,
    wiki,
)
from wiki_agent.config import get_settings
from wiki_agent.db import Base, SessionLocal, engine
from wiki_agent.llm import ModelFactory
from wiki_agent.models import FeatureFlag, User, UserRole, WikiIndexState
from wiki_agent.security import hash_password
from wiki_agent.wiki import WikiRepository
from wiki_agent.wiki.indexer import WikiIndexer

DEFAULT_FEATURES = {
    "source_upload": True,
    "website_crawl": True,
    "graph_view": True,
    "wiki_questions": True,
    "raw_rag": True,
    "auto_publish": False,
    "wiki_editing": True,
    "wiki_rollback": True,
}


def bootstrap() -> None:
    settings = get_settings()
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
    repository = WikiRepository(settings.knowledge_root)
    commit = repository.initialize()
    with SessionLocal() as db:
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
        if settings.bootstrap_admin_email and settings.bootstrap_admin_password:
            email = settings.bootstrap_admin_email.lower()
            if db.scalar(select(User.id).where(User.email == email)) is None:
                db.add(
                    User(
                        email=email,
                        display_name="Administrator",
                        password_hash=hash_password(settings.bootstrap_admin_password),
                        role=UserRole.ADMIN,
                    )
                )
        db.commit()
        index_state = db.get(WikiIndexState, "published")
        if index_state is None or index_state.commit_sha != commit:
            factory = ModelFactory(db, settings)
            embedding_profile_id = factory.assigned_profile_id("embedding")
            embeddings = (
                factory.embeddings(embedding_profile_id)
                if embedding_profile_id is not None
                else None
            )
            WikiIndexer(repository).rebuild(db, embeddings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    bootstrap()
    yield


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


@app.middleware("http")
async def verify_csrf(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        uses_cookie_auth = bool(
            request.cookies.get("access_token") or request.cookies.get("refresh_token")
        )
        uses_bearer_auth = request.headers.get("authorization", "").lower().startswith(
            "bearer "
        )
        if uses_cookie_auth and not uses_bearer_auth:
            cookie_token = request.cookies.get("csrf_token")
            header_token = request.headers.get("x-csrf-token")
            if (
                not cookie_token
                or not header_token
                or not secrets.compare_digest(cookie_token, header_token)
            ):
                return JSONResponse(status_code=403, content={"detail": "Invalid CSRF token"})
    return await call_next(request)


app.include_router(auth.router, prefix="/api/v1")
app.include_router(sources.router, prefix="/api/v1")
app.include_router(wiki.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(graph.router, prefix="/api/v1")
app.include_router(features.router, prefix="/api/v1")
app.include_router(knowledge_gaps.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
