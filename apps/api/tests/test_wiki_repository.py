import fcntl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wiki_agent.wiki.repository import (
    PageExistsError,
    PublishBusyError,
    PublishResult,
    StaleWikiRevisionError,
    WikiRepository,
    WikiRepositoryError,
)
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata, WikiPage


def page(title: str, page_id: str) -> str:
    now = datetime.now(UTC)
    return WikiPage(
        metadata=PageMetadata(
            id=page_id,
            type=PageKind.SOURCE,
            title=title,
            created=now,
            updated=now,
            citations={"origin": Citation(source_revision_id="rev-1", span_id="all")},
        ),
        body=f"# {title}\n\nCompiled from source.[^origin]",
    ).to_markdown()


def test_publish_is_versioned_and_rejects_stale_base(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    result = repo.publish(
        {"sources/manual.md": page("Manual", "manual")},
        base_commit=base,
        author_name="Test User",
        author_email="test@example.com",
        message="Compile manual",
    )
    assert result.commit == repo.head()
    assert repo.read_page("sources/manual.md").metadata.title == "Manual"
    assert repo.history("sources/manual.md")[0]["subject"] == "Compile manual"
    with pytest.raises(StaleWikiRevisionError):
        repo.publish(
            {"sources/other.md": page("Other", "other")},
            base_commit=base,
            author_name="Test User",
            author_email="test@example.com",
            message="Stale draft",
        )


def test_schema_is_versioned_in_the_knowledge_repository(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    assert "Knowledge compilation rules" in repo.read_schema()
    result = repo.publish_schema(
        "# Updated rules\n\nEvery claim needs evidence.",
        base_commit=base,
        author_name="Admin",
        author_email="admin@example.com",
        message="Update compilation rules",
    )
    assert result.commit == repo.head()
    assert repo.read_schema().startswith("# Updated rules")


def publish_change(repo: WikiRepository, base: str, *, schema: bool = False) -> PublishResult:
    if schema:
        return repo.publish_schema(
            "# New rules\n\nKeep the evidence.",
            base_commit=base,
            author_name="Test User",
            author_email="test@example.com",
            message="Update schema",
        )
    return repo.publish(
        {"sources/new.md": page("New", "new")},
        base_commit=base,
        author_name="Test User",
        author_email="test@example.com",
        message="Publish new page",
    )


def test_create_only_adds_one_page_and_default_still_updates_and_deletes(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    original = page("Original", "original")
    old = repo.publish(
        {"sources/old.md": original},
        base_commit=base,
        author_name="Test User",
        author_email="test@example.com",
        message="Existing page",
    )
    old_history = repo.history("sources/old.md")
    created = repo.publish(
        {"sources/new.md": page("New", "new")},
        base_commit=old.commit,
        author_name="Test User",
        author_email="test@example.com",
        message="Create one page",
        create_only=True,
    )
    assert created.commit == repo.head()
    assert created.changed_paths == ("sources/new.md",)
    assert repo.list_pages() == ["sources/new.md", "sources/old.md"]
    assert repo._run("show", "HEAD:wiki/sources/old.md").stdout == original
    assert repo.history("sources/old.md") == old_history
    assert repo.read_page("sources/new.md").metadata.title == "New"
    assert repo._run("diff-tree", "--no-commit-id", "--name-only", "-r", created.commit).stdout == (
        "wiki/sources/new.md\n"
    )
    assert repo._run("ls-files", ".git/publish.lock").stdout == ""

    updated = repo.publish(
        {"sources/old.md": page("Updated", "original"), "sources/new.md": None},
        base_commit=created.commit,
        author_name="Test User",
        author_email="test@example.com",
        message="Default update and delete",
    )
    assert updated.changed_paths == ("sources/new.md", "sources/old.md")
    assert repo.list_pages() == ["sources/old.md"]
    assert repo.read_page("sources/old.md").metadata.title == "Updated"


@pytest.mark.parametrize(
    "existing", ["tracked", "tracked_missing", "untracked", "directory", "symlink"]
)
def test_create_only_rejects_git_or_worktree_path_and_preserves_old_pages(
    tmp_path: Path, existing: str
) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    original = page("Original", "original")
    files = {"sources/old.md": original}
    if existing.startswith("tracked"):
        files["sources/new.md"] = page("Previous import", "previous-import")
    base = repo.publish(
        files,
        base_commit=base,
        author_name="Test User",
        author_email="test@example.com",
        message="Existing pages",
    ).commit
    target = repo.wiki_root / "sources/new.md"
    if existing == "tracked_missing":
        target.unlink()
    elif existing == "untracked":
        target.write_text("Do not overwrite this untracked file.", encoding="utf-8")
    elif existing == "directory":
        target.mkdir()
    elif existing == "symlink":
        target.symlink_to("missing.md")
    before_status = repo._run("status", "--porcelain").stdout
    old_history = repo.history("sources/old.md")
    import_history = repo.history("sources/new.md")
    with pytest.raises(PageExistsError, match="sources/new.md"):
        repo.publish(
            {"sources/new.md": page("Replacement", "replacement")},
            base_commit=base,
            author_name="Test User",
            author_email="test@example.com",
            message="Must not overwrite",
            create_only=True,
        )
    assert repo.head() == base
    assert repo._run("status", "--porcelain").stdout == before_status
    assert repo._run("show", "HEAD:wiki/sources/old.md").stdout == original
    assert repo.history("sources/old.md") == old_history
    assert repo.history("sources/new.md") == import_history
    if existing == "untracked":
        assert target.read_text(encoding="utf-8") == "Do not overwrite this untracked file."
    elif existing == "symlink":
        assert target.is_symlink()
    elif existing == "directory":
        assert target.is_dir()
    elif existing == "tracked_missing":
        assert not target.exists()
    else:
        assert repo.read_page("sources/new.md").metadata.title == "Previous import"


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"sources/new.md": None},
        {"sources/new.md": "one", "sources/other.md": "two"},
        {"sources/new.md": "one", "sources/old.md": None},
        {"sources/new.md": "one", "sources//new.md": "alias"},
    ],
)
def test_create_only_requires_one_non_delete_path(
    tmp_path: Path, files: dict[str, str | None]
) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    with pytest.raises(WikiRepositoryError):
        repo.publish(
            files,
            base_commit=base,
            author_name="Test User",
            author_email="test@example.com",
            message="Invalid create-only shape",
            create_only=True,
        )
    assert repo.head() == base
    assert repo.list_pages() == []


def test_create_only_rejects_stale_base_from_another_instance(tmp_path: Path) -> None:
    first = WikiRepository(tmp_path / "knowledge")
    base = first.initialize()
    second = WikiRepository(first.root)
    published = publish_change(first, base)
    old_history = first.history("sources/new.md")
    for path in ("sources/new.md", "sources/other.md"):
        with pytest.raises(StaleWikiRevisionError):
            second.publish(
                {path: page("Other", "other")},
                base_commit=base,
                author_name="Test User",
                author_email="test@example.com",
                message="Stale create-only",
                create_only=True,
            )
    assert second.head() == published.commit
    assert second.list_pages() == ["sources/new.md"]
    assert second.read_page("sources/new.md").metadata.title == "New"
    assert second.history("sources/new.md") == old_history


@pytest.mark.parametrize("first_schema", [False, True])
@pytest.mark.parametrize("second_schema", [False, True])
def test_independent_instances_share_page_and_schema_lock_and_recheck_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_schema: bool, second_schema: bool
) -> None:
    first = WikiRepository(tmp_path / "knowledge")
    base = first.initialize()
    second = WikiRepository(first.root)
    clone_entered = threading.Event()
    release_clone = threading.Event()
    contender_blocked = threading.Event()
    real_clone = first._clone
    real_flock = fcntl.flock

    def paused_clone(destination: Path) -> None:
        clone_entered.set()
        assert release_clone.wait(timeout=10)
        real_clone(destination)

    def observed_flock(fd: int, operation: int) -> None:
        try:
            real_flock(fd, operation)
        except BlockingIOError:
            contender_blocked.set()
            raise

    monkeypatch.setattr(first, "_clone", paused_clone)
    monkeypatch.setattr(fcntl, "flock", observed_flock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(publish_change, first, base, schema=first_schema)
        try:
            assert clone_entered.wait(timeout=5)
            second_result = executor.submit(publish_change, second, base, schema=second_schema)
            assert contender_blocked.wait(timeout=3)
            assert not second_result.done()
        finally:
            release_clone.set()
        published = first_result.result(timeout=10)
        with pytest.raises(StaleWikiRevisionError):
            second_result.result(timeout=10)
    assert second.head() == published.commit
    assert first._run("rev-list", "--count", "HEAD").stdout.strip() == "2"
    if first_schema:
        assert second.read_schema() == "# New rules\n\nKeep the evidence."
        assert second.list_pages() == []
    else:
        assert second.read_page("sources/new.md").metadata.title == "New"
        assert "Knowledge compilation rules" in second.read_schema()


@pytest.mark.parametrize("schema", [False, True])
def test_publication_busy_after_five_seconds_and_process_exit_releases_lock(
    tmp_path: Path, schema: bool
) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    lock_path = repo.root / ".git/publish.lock"
    script = (
        "import fcntl, sys; "
        "lock = open(sys.argv[1], 'a'); "
        "fcntl.flock(lock.fileno(), fcntl.LOCK_EX); "
        "print('locked', flush=True); "
        "sys.stdin.readline()"
    )
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(PublishBusyError, match="5 seconds"):
            publish_change(repo, base, schema=schema)
        assert 5 <= time.monotonic() - started < 8
        assert repo.head() == base
        assert repo.list_pages() == []
        assert "Knowledge compilation rules" in repo.read_schema()
    finally:
        _, stderr = holder.communicate(input="\n", timeout=5)
        assert holder.returncode == 0, stderr
    published = publish_change(repo, base, schema=schema)
    assert published.commit == repo.head()


@pytest.mark.parametrize("schema", [False, True])
def test_publication_releases_file_and_thread_locks_after_failure(
    tmp_path: Path, schema: bool
) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    with pytest.raises((WikiRepositoryError, ValueError)):
        if schema:
            repo.publish_schema(
                repo.read_schema(),
                base_commit=base,
                author_name="Test User",
                author_email="test@example.com",
                message="No effective schema changes",
            )
        else:
            repo.publish(
                {"sources/bad.md": "Invalid markdown"},
                base_commit=base,
                author_name="Test User",
                author_email="test@example.com",
                message="Invalid tree",
                create_only=True,
            )
    assert repo.head() == base
    with (repo.root / ".git/publish.lock").open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    assert repo._write_lock.acquire(blocking=False)
    repo._write_lock.release()
    assert publish_change(repo, base, schema=schema).commit == repo.head()
