import asyncio
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.project_index import start_memory_index_scan
from app.api.routes.repos import connect_repo, retry_repo_checkout
from app.core.config import settings
from app.db.models import Base, Document, ProjectIndexState, Repository
from app.schemas.repos import RepoConnectRequest, RepoSyncResponse
from app.services import code_analysis
from app.services.project_indexing import get_project_index_state, scan_project_index


@pytest.fixture
def scope(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(settings, "repo_checkout_dir", str(tmp_path / "managed"))
    monkeypatch.setattr(code_analysis, "SessionLocal", factory)
    with factory() as db:
        repo = Repository(owner="owner", name="repo", full_name="owner/repo", local_path=str(tmp_path / "checkout"))
        db.add(repo)
        db.commit()
        yield db, repo, factory
        code_analysis._CHECKOUT_LOCKS.pop(repo.id, None)
    engine.dispose()


def git_checkout(repo):
    from pathlib import Path
    path = Path(repo.local_path)
    path.mkdir()
    (path / "README.md").write_text("# Project\n\nA real project description.\n", encoding="utf-8")
    for args in [
        ["init", "-b", "main"],
        ["add", "README.md"],
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", "commit", "-m", "Initial"],
    ]:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


@pytest.mark.asyncio
async def test_empty_or_partial_clone_cannot_be_scanned(scope):
    from pathlib import Path
    db, repo, _factory = scope
    path = Path(repo.local_path)
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    (path / "README.md").write_text("partially downloaded", encoding="utf-8")

    state = get_project_index_state(db, repo)
    assert state["can_scan"] is False
    assert state["checkout"]["status"] == "pending"
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as error:
        await start_memory_index_scan(repo.id, tasks, db)
    assert error.value.status_code == 409
    assert not tasks.tasks
    assert db.query(ProjectIndexState).count() == 0
    assert db.query(Document).count() == 0


@pytest.mark.asyncio
async def test_download_is_persisted_before_response_and_blocks_scan(scope, monkeypatch):
    db, repo, _factory = scope

    async def get_repo(*_args, **_kwargs):
        return {"id": 123, "default_branch": "main", "clone_url": "https://github.com/owner/repo.git", "description": "Project"}

    async def sync(_db, active_repo, _payload):
        return RepoSyncResponse(repo_id=active_repo.id, status="completed", synced={"issues": 0, "pull_requests": 0, "workflow_runs": 0})

    monkeypatch.setattr("app.api.routes.repos.GitHubClient.get_repo", get_repo)
    monkeypatch.setattr("app.api.routes.repos.sync_repository", sync)
    tasks = BackgroundTasks()
    result = await connect_repo(RepoConnectRequest(owner="owner", repo="repo"), tasks, db)
    assert result.connected is True
    assert result.checkout.status == "downloading"
    assert len(tasks.tasks) == 1
    db.expire_all()
    assert db.get(Repository, repo.id).checkout_status == "downloading"
    with pytest.raises(HTTPException) as error:
        await start_memory_index_scan(repo.id, BackgroundTasks(), db)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_scan_waits_for_download_completion_even_if_directory_already_exists(scope, monkeypatch):
    db, repo, factory = scope
    path = git_checkout(repo)
    entered, release = threading.Event(), threading.Event()

    def slow_checkout(*_args):
        entered.set()
        assert release.wait(timeout=3)
        return path

    monkeypatch.setattr(code_analysis, "_sync_checkout", slow_checkout)
    monkeypatch.setattr(code_analysis, "_sync_code_graph", lambda *_args, **_kwargs: 0)
    task = asyncio.create_task(code_analysis.sync_repository_code_analysis(db, repo))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with factory() as observer:
            observed = observer.get(Repository, repo.id)
            assert observed.checkout_status == "downloading"
            assert get_project_index_state(observer, observed)["can_scan"] is False
            with pytest.raises(HTTPException) as error:
                await start_memory_index_scan(repo.id, BackgroundTasks(), observer)
            assert error.value.status_code == 409
    finally:
        release.set()
        await task

    db.refresh(repo)
    assert repo.checkout_status == "ready"
    assert get_project_index_state(db, repo)["can_scan"] is True
    assert scan_project_index(db, repo)["status"] == "ready"


@pytest.mark.asyncio
async def test_failed_download_is_visible_redacted_and_retryable(scope, monkeypatch, caplog):
    db, repo, _factory = scope
    path = git_checkout(repo)
    secret = "github_pat_fake_test_secret"

    def fail_checkout(*_args):
        raise subprocess.CalledProcessError(128, ["git", "clone"], stderr=f"fatal: https://x-access-token:{secret}@github.com/owner/repo.git could not connect")

    monkeypatch.setattr(code_analysis, "_sync_checkout", fail_checkout)
    assert await code_analysis.sync_repository_code_analysis_by_id(repo.id) is None
    db.refresh(repo)
    assert repo.checkout_status == "failed"
    assert "could not connect" in repo.checkout_error
    assert secret not in repo.checkout_error
    assert secret not in caplog.text
    state = get_project_index_state(db, repo)
    assert state["can_scan"] is False
    assert state["checkout"]["status"] == "failed"

    tasks = BackgroundTasks()
    retried = await retry_repo_checkout(repo.id, tasks, db)
    assert retried["status"] == "downloading"
    assert repo.checkout_error is None
    assert len(tasks.tasks) == 1
    with pytest.raises(HTTPException) as error:
        await retry_repo_checkout(repo.id, BackgroundTasks(), db)
    assert error.value.status_code == 409

    monkeypatch.setattr(code_analysis, "_sync_checkout", lambda *_args: path)
    monkeypatch.setattr(code_analysis, "_sync_code_graph", lambda *_args, **_kwargs: 0)
    await tasks()
    db.refresh(repo)
    assert repo.checkout_status == "ready"
    assert get_project_index_state(db, repo)["can_scan"] is True


@pytest.mark.asyncio
async def test_duplicate_scan_and_download_are_not_queued_during_scan(scope):
    db, repo, _factory = scope
    git_checkout(repo)
    first, second = BackgroundTasks(), BackgroundTasks()
    assert (await start_memory_index_scan(repo.id, first, db)).started is True
    assert (await start_memory_index_scan(repo.id, second, db)).started is False
    assert len(first.tasks) == 1 and not second.tasks
    with pytest.raises(HTTPException) as error:
        await retry_repo_checkout(repo.id, BackgroundTasks(), db)
    assert error.value.status_code == 409


def test_legacy_complete_checkout_is_usable_but_deleted_source_is_not(scope):
    import shutil
    db, repo, _factory = scope
    path = git_checkout(repo)
    assert repo.checkout_status == "pending"
    assert get_project_index_state(db, repo)["can_scan"] is True
    repo.checkout_status = "ready"
    db.commit()
    shutil.rmtree(path)
    state = get_project_index_state(db, repo)
    assert state["can_scan"] is False
    assert state["checkout"]["status"] == "failed"


@pytest.mark.asyncio
async def test_worker_does_not_mark_a_partial_clone_ready(scope, monkeypatch):
    from pathlib import Path
    db, repo, _factory = scope
    path = Path(repo.local_path)
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    monkeypatch.setattr(code_analysis, "_sync_checkout", lambda *_args: path)
    assert await code_analysis.sync_repository_code_analysis_by_id(repo.id) is None
    db.refresh(repo)
    assert repo.checkout_status == "failed"
    assert "检出不完整" in repo.checkout_error
    # The execution-time guard also prevents a background/direct caller from
    # turning an empty clone directory into a misleading ready document index.
    assert scan_project_index(db, repo)["status"] == "failed"
    assert db.query(Document).count() == 0


@pytest.mark.asyncio
async def test_abandoned_download_can_be_retried_after_restart(scope):
    db, repo, _factory = scope
    repo.checkout_status = "downloading"
    repo.checkout_updated_at = datetime.now(timezone.utc) - timedelta(minutes=16)
    db.commit()
    assert code_analysis.repository_checkout_state(repo)["status"] == "failed"
    tasks = BackgroundTasks()
    assert (await retry_repo_checkout(repo.id, tasks, db))["status"] == "downloading"
    assert len(tasks.tasks) == 1


@pytest.mark.asyncio
async def test_abandoned_scan_does_not_block_future_scan_or_checkout(scope):
    db, repo, _factory = scope
    git_checkout(repo)
    db.add(ProjectIndexState(repo_id=repo.id, status="building", updated_at=datetime.now(timezone.utc) - timedelta(minutes=16)))
    db.commit()
    assert get_project_index_state(db, repo)["status"] == "failed"
    tasks = BackgroundTasks()
    assert (await start_memory_index_scan(repo.id, tasks, db)).started is True
    db.query(ProjectIndexState).filter_by(repo_id=repo.id).update({"updated_at": datetime.now(timezone.utc) - timedelta(minutes=16)})
    db.commit()
    assert (await retry_repo_checkout(repo.id, BackgroundTasks(), db))["status"] == "downloading"


@pytest.mark.asyncio
async def test_same_repository_does_not_run_two_checkout_workers(scope, monkeypatch):
    db, repo, _factory = scope
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_analysis(_db, active_repo):
        calls.append(active_repo.id)
        entered.set()
        await release.wait()
        return 1

    monkeypatch.setattr(code_analysis, "sync_repository_code_analysis", slow_analysis)
    first = asyncio.create_task(code_analysis.sync_repository_code_analysis_by_id(repo.id))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert await code_analysis.sync_repository_code_analysis_by_id(repo.id) is None
        assert code_analysis.queue_repository_checkout(db, repo) is False
    finally:
        release.set()
        assert await first == 1
    assert calls == [repo.id]


def test_git_command_exception_never_contains_credentials(monkeypatch):
    secret = "github_pat_fake_test_secret"
    url = f"https://x-access-token:{secret}@github.com/owner/repo.git"

    def failed(*_args, **_kwargs):
        raise subprocess.CalledProcessError(128, ["git", "clone", url], stderr=f"fatal: authentication failed for {url}")

    monkeypatch.setattr(code_analysis.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError) as error:
        code_analysis._run_git(["clone", url, "/tmp/checkout"])
    assert secret not in str(error.value)
    assert secret not in error.value.stderr
    assert "authentication failed" in error.value.stderr


def test_git_timeout_message_does_not_include_command_credentials(monkeypatch):
    secret = "github_pat_fake_test_secret"
    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git", "clone", f"https://x-access-token:{secret}@github.com/repo"], 180)
    monkeypatch.setattr(code_analysis.subprocess, "run", timed_out)
    with pytest.raises(RuntimeError) as error:
        code_analysis._run_git(["clone", "private-url"])
    assert secret not in str(error.value)
    assert "180" in str(error.value)


@pytest.mark.asyncio
async def test_startup_migrates_existing_repository_checkout_columns(tmp_path, monkeypatch):
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateTable
    from app import main
    from app.db import session as db_session

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}")
    added = {"checkout_status", "checkout_error", "checkout_updated_at"}
    ddl = str(CreateTable(Repository.__table__).compile(dialect=engine.dialect))
    legacy_ddl = "\n".join(line for line in ddl.splitlines() if not any(name in line for name in added))
    rid = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(text(legacy_ddl))
        connection.execute(text("INSERT INTO repositories (id, owner, name, full_name, provider, checkout_mode) VALUES (:id, 'owner', 'legacy', 'owner/legacy', 'github', 'managed')"), {"id": rid.hex})
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(settings, "auto_sync_enabled", False)
    monkeypatch.setattr(settings, "repo_checkout_dir", str(tmp_path / "legacy-checkouts"))
    monkeypatch.setattr("app.services.rag.vector_store.milvus_runtime_status", lambda: {})
    monkeypatch.setattr("app.services.rag.vector_store.delete_milvus_documents", lambda **_kwargs: 0)
    app = main.create_app()
    try:
        await app.router.startup()
        assert added <= {column["name"] for column in inspect(engine).get_columns("repositories")}
        with factory() as db:
            repo = db.get(Repository, rid)
            assert repo.checkout_status == "pending"
            assert repo.checkout_error is None
            assert repo.checkout_updated_at is None
    finally:
        await app.router.shutdown()
        engine.dispose()
