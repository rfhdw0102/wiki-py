from wiki_agent.search.fusion import (
    FusedHit,
    RankedHit,
    merge_fused_hits,
    reciprocal_rank_fusion,
)


def hit(document_id: str, channel: str, rank: int) -> RankedHit:
    return RankedHit(document_id, channel, rank, 1.0, document_id)


def test_rrf_rewards_cross_channel_hits() -> None:
    results = reciprocal_rank_fusion(
        [
            [hit("shared", "bm25", 2), hit("lexical", "bm25", 1)],
            [hit("shared", "graph", 1), hit("graph-only", "graph", 2)],
        ]
    )
    assert results[0].document_id == "shared"
    assert results[0].channels == ("bm25", "graph")


def test_rrf_validates_configuration() -> None:
    try:
        reciprocal_rank_fusion([], rank_constant=0)
    except ValueError as exc:
        assert "rank_constant" in str(exc)
    else:
        raise AssertionError("expected invalid rank constant to fail")


def test_merge_fused_hits_preserves_wiki_and_raw_evidence() -> None:
    results = merge_fused_hits(
        [
            (
                "wiki",
                [FusedHit("wiki-page", 0.02, ("bm25",), "Wiki", {})],
            ),
            (
                "raw",
                [
                    FusedHit(
                        "raw:span",
                        0.03,
                        ("raw_bm25", "raw_vector"),
                        "Raw",
                        {},
                    )
                ],
            ),
        ],
        limit=10,
    )

    assert [hit.document_id for hit in results] == ["raw:span", "wiki-page"]
    assert results[0].metadata["layer"] == "raw"
    assert results[1].metadata["layer"] == "wiki"
