from wiki_agent.search.fusion import RankedHit, reciprocal_rank_fusion


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

