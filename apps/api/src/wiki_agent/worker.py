from celery import Celery

from wiki_agent.config import get_settings

settings = get_settings()
celery_app = Celery("wiki_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    imports=("wiki_agent.tasks",),
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    task_default_queue="default",
    task_routes={
        "wiki_agent.parse_source": {"queue": "parser"},
        "wiki_agent.prepare_compilation_source": {"queue": "parser"},
    },
    worker_prefetch_multiplier=1,
    timezone="UTC",
)


@celery_app.task(name="wiki_agent.health")  # type: ignore[untyped-decorator]
def health() -> dict[str, str]:
    return {"status": "ok"}
