import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FRONTMATTER_SEPARATOR = "---"
WIKI_LINK_PATTERN = re.compile(r"\[\[([a-z0-9][a-z0-9/_-]*)\]\]")
CITATION_PATTERN = re.compile(r"\[\^([a-zA-Z0-9_-]+)\]")
DANGEROUS_MARKDOWN_PATTERN = re.compile(
    r"(?is)<\s*(script|iframe|object|embed|style|svg|math)\b|"
    r"\bon[a-z]+\s*=|"
    r"\]\(\s*(javascript|data):"
)


class PageKind(StrEnum):
    SOURCE = "source"
    ENTITY = "entity"
    CONCEPT = "concept"
    PENDING = "pending"


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_revision_id: str = Field(min_length=1, max_length=128)
    span_id: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=256)


class PageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,127}$")
    type: PageKind
    title: str = Field(min_length=1, max_length=256)
    created: datetime
    updated: datetime
    sources: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    citations: dict[str, Citation] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    status: str = "published"

    @field_validator("sources", "related")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_wiki_path(value)
        return sorted(set(values))

    @model_validator(mode="after")
    def validate_dates(self) -> "PageMetadata":
        if self.updated < self.created:
            raise ValueError("updated must not be earlier than created")
        return self


class WikiPage(BaseModel):
    metadata: PageMetadata
    body: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_citations(self) -> "WikiPage":
        if DANGEROUS_MARKDOWN_PATTERN.search(self.body):
            raise ValueError("Wiki page contains unsafe HTML or a dangerous link scheme")
        used = set(CITATION_PATTERN.findall(self.body))
        declared = set(self.metadata.citations)
        missing = used - declared
        if missing:
            raise ValueError(f"undeclared citations: {', '.join(sorted(missing))}")
        unused = declared - used
        if unused:
            raise ValueError(f"unused citations: {', '.join(sorted(unused))}")
        if self.metadata.type is PageKind.CONCEPT and len(set(self.metadata.sources)) < 2:
            raise ValueError("concept pages require at least two independent sources")
        if self.metadata.type is PageKind.PENDING and self.metadata.status == "published":
            raise ValueError("pending pages cannot have published status")
        return self

    @property
    def links(self) -> set[str]:
        return set(WIKI_LINK_PATTERN.findall(self.body)) | set(self.metadata.related)

    @classmethod
    def from_markdown(cls, content: str) -> "WikiPage":
        if not content.startswith(f"{FRONTMATTER_SEPARATOR}\n"):
            raise ValueError("Wiki page must start with YAML frontmatter")
        try:
            _, frontmatter, body = content.split(FRONTMATTER_SEPARATOR, 2)
        except ValueError as exc:
            raise ValueError("Wiki page has malformed YAML frontmatter") from exc
        raw: Any = yaml.safe_load(frontmatter)
        if not isinstance(raw, dict):
            raise ValueError("Wiki page frontmatter must be an object")
        return cls(metadata=PageMetadata.model_validate(raw), body=body.strip())

    def to_markdown(self) -> str:
        data = self.metadata.model_dump(mode="json", exclude_none=True)
        frontmatter = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{self.body.strip()}\n"


def validate_wiki_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise ValueError(f"invalid Wiki path: {value}")
    if len(path.parts) != 2 or path.parts[0] not in {
        "sources",
        "entities",
        "concepts",
        "pending",
    }:
        raise ValueError(f"Wiki path must be under an approved page directory: {value}")
    return path


def expected_kind_for_path(path: str) -> PageKind:
    directory = validate_wiki_path(path).parts[0]
    return {
        "sources": PageKind.SOURCE,
        "entities": PageKind.ENTITY,
        "concepts": PageKind.CONCEPT,
        "pending": PageKind.PENDING,
    }[directory]
