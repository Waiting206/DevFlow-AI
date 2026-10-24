"""Exercise synchronization against real shallow Git repositories, without GitHub."""

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Document, Repository
from app.schemas.repos import RepoSyncRequest
from app.services import code_analysis, project_indexing, repos_sync


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE, text=True).strip()


def publish(path, text):
    (path / "README.md").write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "-c", "user.name=Sync Test", "-c", "user.email=sync@example.test",
        "-c", "commit.gpgsign=false", "commit", "-m", text.splitlines()[0])
    git(path, "push", "origin", "main")
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path):
    origin, source = tmp_path / "origin.git", tmp_path / "source"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    git(source, "remote", "add", "origin", origin.as_uri())
    publish(source, "# Version one\nOriginal project context.\n")
    repo = SimpleNamespace(id=uuid.uuid4(), full_name="owner/project", default_branch="main",
                           clone_url=origin.as_uri(), checkout_mode="managed", local_path=str(tmp_path / "checkout"))
    return source, repo


def test_existing_shallow_checkout_catches_up_across_repeated_pushes(remote):
    source, repo = remote
    checkout = code_analysis._sync_checkout(repo, None)
    initial = git(checkout, "rev-parse", "HEAD")
    assert git(checkout, "rev-parse", "--is-shallow-repository") == "true"
    for version in (2, 3, 4):
        expected = publish(source, f"# Version {version}\nUpdated context {version}.\n")
        if version == 2:
            # Reproduce the broken legacy fetch: the new tip has no visible
            # ancestry in the cached shallow repository, so ff-only cannot work.
            git(checkout, "fetch", "--depth", "1", "origin", "main")
            result = subprocess.run(["git", "-C", str(checkout), "merge-base", "HEAD", "origin/main"], capture_output=True)
            assert result.returncode == 1
        code_analysis._sync_checkout(repo, None)
        assert git(checkout, "rev-parse", "HEAD") == expected
        assert (checkout / "README.md").read_text() == (source / "README.md").read_text()
    # The old local branch is retained instead of force-reset or destructive clean.
    assert git(checkout, "rev-parse", "main") == initial


def test_managed_snapshot_follows_force_push_and_deleted_files(remote):
    source, repo = remote
    (source / "removed.md").write_text("old context", encoding="utf-8")
    publish(source, "# Before rewrite\n")
    checkout = code_analysis._sync_checkout(repo, None)
    previous = git(checkout, "rev-parse", "HEAD")
    (source / "removed.md").unlink()
    (source / "README.md").write_text("# Rewritten history\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "-c", "user.name=Sync Test", "-c", "user.email=sync@example.test",
        "-c", "commit.gpgsign=false", "commit", "--amend", "--no-edit")
    git(source, "push", "--force", "origin", "main")
    code_analysis._sync_checkout(repo, None)
    assert git(checkout, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(checkout, "rev-parse", "main") == previous
    assert not (checkout / "removed.md").exists()


@pytest.mark.parametrize("filename", ["README.md", "personal-untracked.txt"])
def test_managed_snapshot_refuses_to_overwrite_local_files(remote, filename):
    source, repo = remote
    checkout = code_analysis._sync_checkout(repo, None)
    previous = git(checkout, "rev-parse", "HEAD")
    (checkout / filename).write_text("keep my local work", encoding="utf-8")
    publish(source, "# Remote changed\n")
    with pytest.raises(RuntimeError, match="本地修改或未跟踪文件"):
        code_analysis._sync_checkout(repo, None)
    assert (checkout / filename).read_text() == "keep my local work"
    assert git(checkout, "rev-parse", "HEAD") == previous


def test_git_update_failure_is_reported_and_old_files_are_retained(remote):
    source, repo = remote
    checkout = code_analysis._sync_checkout(repo, None)
    previous = git(checkout, "rev-parse", "HEAD")
    git(checkout, "remote", "set-url", "origin", (source.parent / "missing.git").as_uri())
    with pytest.raises(subprocess.CalledProcessError):
        code_analysis._sync_checkout(repo, None)
    assert git(checkout, "rev-parse", "HEAD") == previous
    assert (checkout / "README.md").is_file()


def test_nested_directory_is_not_mistaken_for_its_parent_git_repository(remote):
    source, _repo = remote
    nested = source / "cache"
    nested.mkdir()
    assert not code_analysis._is_git_work_tree(nested)
    assert code_analysis._is_git_work_tree(source)


def test_local_mode_keeps_current_branch_and_uncommitted_work(remote):
    source, repo = remote
    checkout = code_analysis._sync_checkout(repo, None)
    previous = git(checkout, "rev-parse", "HEAD")
    git(checkout, "switch", "-c", "my-local-work")
    (checkout / "README.md").write_text("keep this uncommitted draft", encoding="utf-8")
    publish(source, "# New remote version\n")
    repo.checkout_mode = "local"
    code_analysis._sync_checkout(repo, None)
    assert git(checkout, "branch", "--show-current") == "my-local-work"
    assert git(checkout, "rev-parse", "HEAD") == previous
    assert (checkout / "README.md").read_text() == "keep this uncommitted draft"


@pytest.fixture
def database(remote, tmp_path, monkeypatch):
    _source, item = remote
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sync.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.db.session.SessionLocal", factory)
    monkeypatch.setattr(code_analysis, "SessionLocal", factory)

    def no_vectors(*_args, **_kwargs):
        raise AssertionError("Repository refresh must not invoke the embedding/vector runtime")

    for name in ("embed_texts", "delete_milvus_documents", "upsert_milvus_documents"):
        monkeypatch.setattr(f"app.services.rag.vector_store.{name}", no_vectors)
    with factory() as db:
        repo = Repository(id=item.id, owner="owner", name="project", full_name=item.full_name,
                          default_branch="main", clone_url=item.clone_url, local_path=item.local_path,
                          checkout_mode="managed", provider="github")
        db.add(repo)
        db.commit()
    yield factory, item.id
    code_analysis._CHECKOUT_LOCKS.pop(item.id, None)
    engine.dispose()


FACTS_OFF = RepoSyncRequest(sync_issues=False, sync_pull_requests=False, sync_workflow_runs=False)


@pytest.mark.asyncio
async def test_background_sync_refreshes_opted_in_documents_and_skips_unchanged_scans(remote, database, monkeypatch):
    source, _item = remote
    factory, repo_id = database
    await repos_sync.sync_repository_by_id(repo_id, FACTS_OFF)
    with factory() as db:
        repo = db.get(Repository, repo_id)
        assert db.query(Document).count() == 0  # First scan remains a user choice.
        assert project_indexing.scan_project_index(db, repo)["status"] == "ready"
        assert any("Original project context" in doc.content for doc in db.query(Document).all())
    expected = publish(source, "# Fresh project context\nThe refreshed deployment instructions.\n")
    await repos_sync.sync_repository_by_id(repo_id, FACTS_OFF)
    with factory() as db:
        repo = db.get(Repository, repo_id)
        state = project_indexing.get_project_index_state(db, repo)
        assert state["status"] == "ready"
        assert state["checkout"]["commit_sha"] == expected
        docs = db.query(Document).all()
        assert any("refreshed deployment instructions" in doc.content for doc in docs)
        assert not any("Original project context" in doc.content for doc in docs)

    def no_rescan(*_args, **_kwargs):
        raise AssertionError("Unchanged documents should not be scanned again")

    monkeypatch.setattr(project_indexing, "scan_project_index", no_rescan)
    await repos_sync.sync_repository_by_id(repo_id, FACTS_OFF)
    with factory() as db:
        assert db.get(Repository, repo_id).checkout_status == "ready"


@pytest.mark.asyncio
async def test_github_api_failure_does_not_block_independent_source_sync(remote, database, monkeypatch):
    source, _item = remote
    factory, repo_id = database
    expected = publish(source, "# Git is available while the API is limited\n")

    async def api_failure(*_args):
        raise HTTPException(status_code=429, detail="API rate limited")

    monkeypatch.setattr(repos_sync, "sync_repository", api_failure)
    with pytest.raises(HTTPException, match="API rate limited"):
        await repos_sync.sync_repository_by_id(repo_id, FACTS_OFF)
    with factory() as db:
        repo = db.get(Repository, repo_id)
        state = code_analysis.repository_checkout_state(repo)
        assert state["status"] == "ready"
        assert state["commit_sha"] == expected


@pytest.mark.asyncio
async def test_failed_background_checkout_keeps_documents_and_exposes_failure(remote, database):
    source, _item = remote
    factory, repo_id = database
    await repos_sync.sync_repository_by_id(repo_id, FACTS_OFF)
    with factory() as db:
        repo = db.get(Repository, repo_id)
        project_indexing.scan_project_index(db, repo)
        before = {doc.id: doc.content for doc in db.query(Document).all()}
        git(Path(repo.local_path), "remote", "set-url", "origin", (source.parent / "missing.git").as_uri())
    await repos_sync.sync_repository_by_id(repo_id, FACTS_OFF)
    with factory() as db:
        repo = db.get(Repository, repo_id)
        state = project_indexing.get_project_index_state(db, repo)
        assert state["checkout"]["status"] == "failed"
        assert state["checkout"]["error_message"]
        assert state["can_scan"] is False
        assert {doc.id: doc.content for doc in db.query(Document).all()} == before
