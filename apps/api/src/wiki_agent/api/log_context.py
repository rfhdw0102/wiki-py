import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from wiki_agent.models import AgentRun, Draft, Source, SourceRevision, WikiDocument


def _details(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _source_path_id(path: object) -> str | None:
    if isinstance(path, str) and (
        match := re.match(r"^/api/v1/sources/([a-f0-9-]{36})(?:/|$)", path)
    ):
        return match[1]
    return None


def enrich_log_context(
    items: list[dict[str, object]], db: Session, wiki_id: str, user_id: str,
) -> None:
    run_ids = {
        item["resource_id"] for item in items
        if item["resource_type"] == "agent_run" and isinstance(item["resource_id"], str)
    }
    runs = {
        row.id: row for row in db.scalars(
            select(AgentRun).where(
                AgentRun.id.in_(run_ids), AgentRun.wiki_id == wiki_id,
                AgentRun.created_by_id == user_id,
            )
        )
    } if run_ids else {}
    draft_ids = {
        item["resource_id"] for item in items
        if item["resource_type"] == "draft" and isinstance(item["resource_id"], str)
    }
    owned_drafts = set(db.scalars(select(Draft.id).where(
        Draft.id.in_(draft_ids), Draft.wiki_id == wiki_id,
    ))) if draft_ids else set()
    revision_ids: set[str] = set()
    source_ids: set[str] = set()
    all_paths: set[str] = set()
    item_paths: dict[str, list[str]] = {}
    for item in items:
        if item["resource_type"] == "source_revision" and isinstance(item["resource_id"], str):
            revision_ids.add(item["resource_id"])
        if item["resource_type"] == "source" and isinstance(item["resource_id"], str):
            source_ids.add(item["resource_id"])
        if source_id := _source_path_id(item.get("path")):
            source_ids.add(source_id)
        run = runs.get(item["resource_id"]) if isinstance(item["resource_id"], str) else None
        if run:
            revision_ids.update(run.source_revision_ids)
        raw_paths = _details(item.get("details")).get("paths")
        paths = list(dict.fromkeys(
            path for path in raw_paths
            if isinstance(path, str)
            and re.fullmatch(r"(?:sources|entities|concepts|pending)/[^?#]+\.md", path)
            and ".." not in path.split("/")
        )) if isinstance(raw_paths, list) else []
        item_paths[str(item["id"])] = paths
        all_paths.update(paths[:30])
    revisions = {
        row.revision_id: row for row in db.execute(
            select(
                SourceRevision.id.label("revision_id"), Source.name,
                SourceRevision.created_at.label("created_at"),
            ).join(Source, Source.id == SourceRevision.source_id).where(
                SourceRevision.id.in_(revision_ids), Source.wiki_id == wiki_id,
            )
        )
    } if revision_ids else {}
    originals = {
        row.id: row for row in db.execute(
            select(Source.id, Source.name, Source.current_revision_id, Source.created_at).where(
                Source.id.in_(source_ids), Source.wiki_id == wiki_id,
            )
        )
    } if source_ids else {}
    titles = {path: title for path, title in db.execute(
        select(WikiDocument.path, WikiDocument.title).where(
            WikiDocument.wiki_id == wiki_id, WikiDocument.path.in_(all_paths),
        )
    ).tuples()} if all_paths else {}
    for item in items:
        related: list[dict[str, str]] = []
        revision_id = (
            item["resource_id"]
            if item["resource_type"] == "source_revision" and isinstance(item["resource_id"], str)
            else None
        )
        run = runs.get(item["resource_id"]) if isinstance(item["resource_id"], str) else None
        if run:
            members = run.source_revision_ids
            revision_id = members[0] if members else None
            item["processing_status"] = run.status
            for member in members[1:]:
                member_revision = revisions.get(member)
                if member_revision:
                    related.append({
                        "label": member_revision.name,
                        "href": f"/sources?revision={quote(member, safe='')}",
                        "kind": "source",
                    })
        revision = revisions.get(revision_id) if isinstance(revision_id, str) else None
        original_source_id = (
            item["resource_id"]
            if item["resource_type"] == "source" and isinstance(item["resource_id"], str) else None
        )
        original_source_id = _source_path_id(item.get("path")) or original_source_id
        original = originals.get(original_source_id) if original_source_id is not None else None
        record = revision or original
        if record:
            item["resource_name"] = record.name
            target = revision_id if revision is not None else (
                original.current_revision_id if original is not None else None
            )
            if target:
                related.append({
                    "label": record.name, "href": f"/sources?revision={quote(target, safe='')}",
                    "kind": "source",
                })
            created = item["created_at"]
            item["source_saved"] = isinstance(created, datetime) and (
                _utc(record.created_at) <= _utc(created)
            )
        paths = item_paths[str(item["id"])]
        item["page_count"] = len(paths)
        link_paths = paths if item["resource_type"] != "draft" or (
            item["resource_id"] in owned_drafts
        ) else []
        for path in link_paths[:30]:
            if path in titles:
                related.append({
                    "label": titles[path], "href": f"/wiki?page={quote(path, safe='')}",
                    "kind": "page",
                })
        item["related"] = related
