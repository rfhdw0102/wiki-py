from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RankedHit:
    document_id: str
    channel: str
    rank: int
    source_score: float
    content: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FusedHit:
    document_id: str
    score: float
    channels: tuple[str, ...]
    content: str
    metadata: dict[str, object]


def reciprocal_rank_fusion(
    result_sets: Iterable[Sequence[RankedHit]],
    *,
    rank_constant: int = 60,
    limit: int = 20,
) -> list[FusedHit]:
    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    if limit < 1:
        raise ValueError("limit must be positive")
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, RankedHit] = {}
    channels: dict[str, set[str]] = defaultdict(set)
    for result_set in result_sets:
        seen: set[str] = set()
        for position, hit in enumerate(result_set, start=1):
            if hit.document_id in seen:
                continue
            seen.add(hit.document_id)
            rank = hit.rank if hit.rank > 0 else position
            scores[hit.document_id] += 1.0 / (rank_constant + rank)
            channels[hit.document_id].add(hit.channel)
            hits.setdefault(hit.document_id, hit)
    ordered = sorted(scores, key=lambda item: (-scores[item], item))[:limit]
    return [
        FusedHit(
            document_id=document_id,
            score=scores[document_id],
            channels=tuple(sorted(channels[document_id])),
            content=hits[document_id].content,
            metadata=hits[document_id].metadata,
        )
        for document_id in ordered
    ]


def merge_fused_hits(
    result_sets: Iterable[tuple[str, Sequence[FusedHit]]],
    *,
    limit: int = 20,
) -> list[FusedHit]:
    if limit < 1:
        raise ValueError("limit must be positive")
    merged: dict[str, FusedHit] = {}
    for layer, result_set in result_sets:
        for hit in result_set:
            existing = merged.get(hit.document_id)
            metadata = {**hit.metadata, "layer": layer}
            if existing is None:
                merged[hit.document_id] = FusedHit(
                    document_id=hit.document_id,
                    score=hit.score,
                    channels=hit.channels,
                    content=hit.content,
                    metadata=metadata,
                )
                continue
            merged[hit.document_id] = FusedHit(
                document_id=hit.document_id,
                score=existing.score + hit.score,
                channels=tuple(sorted({*existing.channels, *hit.channels})),
                content=existing.content,
                metadata={
                    **existing.metadata,
                    "layer": "hybrid",
                },
            )
    layer_priority = {"wiki": 0, "hybrid": 0, "raw": 1}
    ordered = sorted(
        merged.values(),
        key=lambda hit: (
            -hit.score,
            layer_priority.get(str(hit.metadata.get("layer")), 2),
            hit.document_id,
        ),
    )
    return ordered[:limit]


def should_rerank(hits: list[FusedHit], *, fallback_used: bool) -> bool:
    if len(hits) < 3:
        return False
    return fallback_used or len(hits) >= 8 or len(hits[0].channels) < 2
