from uuid import uuid4

import pytest
from test_dingtalk_mock import C_PAGE, NOTE, A, env, post

__all__ = ["env"]


@pytest.mark.parametrize("kind", ["stale", "existing"])
def test_import_conflicts_preserve_old_pages(env, monkeypatch, kind):
    from wiki_agent.wiki.repository import StaleWikiRevisionError

    client, _, repo, _, _ = env
    original = repo.publish
    action = str(uuid4())
    path = f"sources/dingtalk-mock-{action}.md"
    old_c = repo.read_page("sources/existing-c.md")

    def conflict(files, **kwargs):
        if kind == "stale":
            raise StaleWikiRevisionError("Concurrent writer advanced HEAD")
        # A pre-existing independent page at the deterministic target must not be overwritten.
        original(
            {path: C_PAGE},
            base_commit=repo.head(),
            author_name="Other writer",
            author_email="writer@example.test",
            message="Independent page",
        )
        kwargs["base_commit"] = repo.head()
        return original(files, **kwargs)

    monkeypatch.setattr(repo, "publish", conflict)
    response = post(client, request_id=action)
    assert response.status_code == 409
    assert response.json()["detail"]["published"] is False
    assert response.json()["detail"]["code"] == (
        "wiki_revision_conflict" if kind == "stale" else "page_exists"
    )
    assert repo.read_page("sources/existing-c.md") == old_c
    if kind == "existing":
        assert repo.read_page(path).body == "Existing C stays unchanged."


def test_exception_after_ff_merge_is_verified_before_success(env, monkeypatch):
    client, _, repo, _, _ = env
    original = repo.publish

    def publish_then_raise(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("Response failed after ff merge")

    monkeypatch.setattr(repo, "publish", publish_then_raise)
    response = post(client)
    assert response.status_code == 201, response.text
    assert repo.read_page(response.json()["page_path"]).body == A + "\n\n" + NOTE


def test_ordinary_draft_can_still_publish(env):
    client, _, repo, _, _ = env
    ordinary = C_PAGE.replace("existing-c", "ordinary-page").replace("Existing C", "Ordinary")
    response = client.post(
        "/api/v1/drafts",
        json={
            "base_commit": repo.head(),
            "files": {"sources/ordinary.md": ordinary},
            "title": "Ordinary",
        },
    )
    assert response.status_code == 201, response.text
    draft_id = response.json()["id"]
    result = client.post(
        f"/api/v1/drafts/{draft_id}/publish", json={"message": "Ordinary draft publish"}
    )
    assert result.status_code == 200, result.text
    assert repo.read_page("sources/ordinary.md").metadata.title == "Ordinary"
