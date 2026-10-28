import asyncio
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes import evaluations as routes
from app.core.config import settings
from app.db.models import Base, EvalRun, Repository
from app.schemas.evals import EvalRunRequest, RagEvalCaseRequest
from app.services.evals import jobs


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'evals.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(jobs, "SessionLocal", factory)
    monkeypatch.setattr(jobs, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(settings, "eval_job_timeout_seconds", 2)
    with factory() as db:
        repo = Repository(owner="local", name="eval-control", full_name="local/eval-control")
        db.add(repo)
        db.commit()
        repo_id = repo.id
    yield factory, repo_id
    assert not jobs._running_tasks
    engine.dispose()


def add_job(factory, repo_id, status="queued", **extra):
    with factory() as db:
        row = EvalRun(name="lifecycle", result_json={
            "kind": "chatagent_ragas", "status": status,
            "target": {"name": "ChatAgent", "repo_id": str(repo_id)},
            "evaluation_set": {"id": "fixed", "sha256": "saved-hash"},
            **extra,
        })
        db.add(row)
        db.commit()
        return str(row.id)


def read_job(factory, eval_id):
    from uuid import UUID

    with factory() as db:
        return dict(db.get(EvalRun, UUID(eval_id)).result_json)


async def cancel(factory, eval_id):
    from uuid import UUID

    with factory() as db:
        return await routes.cancel_eval(UUID(eval_id), db=db, _user=object())


@pytest.mark.asyncio
async def test_cancel_queued_job_prevents_model_execution(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id)
    response = await cancel(factory, eval_id)
    assert response.status == "cancelled"

    async def operation(db, progress):
        pytest.fail("A cancelled queued job must never start")

    await jobs.execute_eval_job(eval_id, operation)
    assert (await cancel(factory, eval_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_running_job_stops_work_and_progress_cannot_revive_it(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def operation(db, progress):
        await progress({"stage": "ragas", "message": "scoring"})
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    worker = asyncio.create_task(jobs.execute_eval_job(eval_id, operation))
    await asyncio.wait_for(started.wait(), 1)
    await cancel(factory, eval_id)
    await asyncio.wait_for(worker, 1)
    assert stopped.is_set()
    result = read_job(factory, eval_id)
    assert result["status"] == "cancelled"
    assert result["evaluation_complete"] is False
    assert result["progress"]["message"] == "评测已取消。"


@pytest.mark.asyncio
async def test_durable_cancellation_detected_without_local_task_registry(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id)
    started = asyncio.Event()

    async def operation(db, progress):
        started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(jobs.execute_eval_job(eval_id, operation))
    await asyncio.wait_for(started.wait(), 1)
    jobs._running_tasks.pop(eval_id)  # Simulate cancellation in another worker.
    await cancel(factory, eval_id)
    await asyncio.wait_for(worker, 1)
    assert read_job(factory, eval_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_late_success_does_not_overwrite_cancellation(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id)
    started = asyncio.Event()

    async def operation(db, progress):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"status": "passed", "passed": True}

    worker = asyncio.create_task(jobs.execute_eval_job(eval_id, operation))
    await asyncio.wait_for(started.wait(), 1)
    await cancel(factory, eval_id)
    await asyncio.wait_for(worker, 1)
    assert read_job(factory, eval_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_total_timeout_cleans_up_and_preserves_rerun_inputs(store, monkeypatch):
    factory, repo_id = store
    monkeypatch.setattr(settings, "eval_job_timeout_seconds", 0.03)
    saved = {"top_k": 3, "cases": [{"question": "q", "reference": "r"}]}
    eval_id = add_job(factory, repo_id, execution_input=saved)
    stopped = asyncio.Event()

    async def operation(db, progress):
        await progress({"stage": "ragas", "completed_cases": 0, "total_cases": 5})
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    await jobs.execute_eval_job(eval_id, operation)
    result = read_job(factory, eval_id)
    assert stopped.is_set()
    assert result["status"] == "error"
    assert result["error_code"] == "timeout"
    assert result["progress"]["message"] == "评测出错。"
    assert result["execution_input"] == saved
    assert result["evaluation_set"]["sha256"] == "saved-hash"
    assert result["passed"] is False


@pytest.mark.asyncio
async def test_late_result_after_dependency_swallows_timeout_cannot_pass(store, monkeypatch):
    factory, repo_id = store
    monkeypatch.setattr(settings, "eval_job_timeout_seconds", 0.02)
    eval_id = add_job(factory, repo_id)

    async def operation(db, progress):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"status": "passed", "passed": True}

    await jobs.execute_eval_job(eval_id, operation)
    assert read_job(factory, eval_id)["error_code"] == "timeout"


@pytest.mark.asyncio
async def test_exception_produces_generic_error_without_leaking_details(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id)

    async def operation(db, progress):
        raise RuntimeError("private provider diagnostic")

    await jobs.execute_eval_job(eval_id, operation)
    result = read_job(factory, eval_id)
    assert result["status"] == "error"
    assert "private provider diagnostic" not in str(result)


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_persisted_and_propagated(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id)
    started = asyncio.Event()

    async def operation(db, progress):
        started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(jobs.execute_eval_job(eval_id, operation))
    await asyncio.wait_for(started.wait(), 1)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert read_job(factory, eval_id)["status"] == "error"


def test_startup_recovery_only_changes_active_records(store):
    factory, repo_id = store
    queued = add_job(factory, repo_id)
    running = add_job(factory, repo_id, "running")
    passed = add_job(factory, repo_id, "passed", passed=True)
    cancelled = add_job(factory, repo_id, "cancelled")
    with factory() as db:
        assert jobs.recover_interrupted_evals(db) == 2
        assert jobs.recover_interrupted_evals(db) == 0
    assert read_job(factory, queued)["status"] == "error"
    assert read_job(factory, running)["error_code"] == "interrupted"
    assert read_job(factory, passed)["passed"] is True
    assert read_job(factory, cancelled)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_completed_record_is_idempotent(store):
    factory, repo_id = store
    eval_id = add_job(factory, repo_id, "passed", passed=True)
    assert (await cancel(factory, eval_id)).status == "passed"
    assert read_job(factory, eval_id)["passed"] is True


@pytest.mark.asyncio
async def test_rerun_reuses_saved_inputs_and_creates_new_history_record(store, monkeypatch):
    from uuid import UUID

    factory, repo_id = store
    calls = []

    async def fake_run(db, target_repo, **kwargs):
        calls.append((target_repo, kwargs))
        return {"status": "passed", "passed": True, "evaluation_complete": True}

    monkeypatch.setattr(routes, "run_rag_quality_eval", fake_run)
    with factory() as db:
        first_tasks = BackgroundTasks()
        first = await routes.run_rag_eval(
            EvalRunRequest(repo_id=repo_id, top_k=3, run_judge=False, cases=[
                RagEvalCaseRequest(id="saved-case", question="saved question", reference="saved reference")
            ]), first_tasks, db, object(),
        )
        await first_tasks()
        db.expire_all()
        original = dict(db.get(EvalRun, UUID(first.eval_id)).result_json)
        second_tasks = BackgroundTasks()
        second = await routes.rerun_eval(UUID(first.eval_id), second_tasks, db, object())
        assert second.eval_id != first.eval_id
        assert second.result["rerun_of"] == first.eval_id
        assert second.result["evaluation_set"] == first.result["evaluation_set"]
        assert second.result["execution_input"]["top_k"] == 3
        await second_tasks()
        db.expire_all()
        assert db.query(EvalRun).count() == 2
        assert db.get(EvalRun, UUID(first.eval_id)).result_json == original
    assert len(calls) == 2
    assert calls[1][1]["cases"][0].question == "saved question"
    assert calls[1][1]["run_judge"] is False
    assert calls[1][1]["limit"] == 3


@pytest.mark.asyncio
async def test_legacy_smoke_can_be_rerun_without_claiming_exact_replay(store):
    from uuid import UUID

    factory, repo_id = store
    eval_id = add_job(factory, repo_id, "error", evaluation_set={"source": "auto_smoke"})
    with factory() as db:
        tasks = BackgroundTasks()
        response = await routes.rerun_eval(UUID(eval_id), tasks, db, object())
        assert response.status == "queued"
        assert response.result["execution_input"]["top_k"] == 5
        assert "重新生成" in response.result["evaluation_set"]["description"]
        # No model requests: this test only checks submission and metadata.


@pytest.mark.asyncio
async def test_active_rerun_and_unknown_ids_are_rejected(store):
    from uuid import UUID

    factory, repo_id = store
    eval_id = add_job(factory, repo_id, "running")
    with factory() as db:
        with pytest.raises(HTTPException) as active:
            await routes.rerun_eval(UUID(eval_id), BackgroundTasks(), db, object())
        assert active.value.status_code == 409
        with pytest.raises(HTTPException) as missing:
            await routes.cancel_eval(uuid4(), db, object())
        assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_rescore_uses_same_timeout_control_and_can_be_rerun(store, monkeypatch):
    from uuid import UUID

    factory, repo_id = store
    monkeypatch.setattr(settings, "eval_job_timeout_seconds", 0.03)

    async def stalled_rescore(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(routes, "rescore_rag_result", stalled_rescore)
    case = {"id": "case", "question": "q", "reference": "r", "response": "a", "ragas_errors": {"faithfulness": "failed"}}
    source_id = add_job(factory, repo_id, "error", cases=[case])
    with factory() as db:
        tasks = BackgroundTasks()
        response = await routes.rescore_eval(UUID(source_id), tasks, db, object())
        await tasks()
        result = read_job(factory, response.eval_id)
        assert result["error_code"] == "timeout"
        assert result["run_mode"] == "rescore"
        assert result["cases"] == [case]
        db.expire_all()
        rerun = await routes.rerun_eval(UUID(response.eval_id), BackgroundTasks(), db, object())
        assert rerun.result["rescore_of"] == source_id
        assert rerun.result["rerun_of"] == response.eval_id


@pytest.mark.asyncio
async def test_http_submission_cancel_and_rerun_contract(store, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.db.session import get_db

    factory, repo_id = store
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/evals")

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    started = asyncio.Queue()

    async def controlled_run(db, repo_id, **kwargs):
        await kwargs["progress_callback"]({"stage": "ragas", "message": "scoring"})
        await started.put(True)
        await asyncio.Event().wait()

    monkeypatch.setattr(routes, "run_rag_quality_eval", controlled_run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval-test") as client:
        submission = asyncio.create_task(client.post("/api/evals/rag/run", json={
            "repo_id": str(repo_id), "top_k": 3, "run_judge": False,
            "cases": [{"question": "saved question", "reference": "saved reference"}],
        }))
        await asyncio.wait_for(started.get(), 1)
        history = (await client.get("/api/evals", params={"repo_id": str(repo_id)})).json()
        first_id = history[0]["eval_id"]
        conflict = await client.post(f"/api/evals/{first_id}/rerun")
        assert conflict.status_code == 409
        response = await client.post(f"/api/evals/{first_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert (await asyncio.wait_for(submission, 1)).status_code == 200

        retry = asyncio.create_task(client.post(f"/api/evals/{first_id}/rerun"))
        await asyncio.wait_for(started.get(), 1)
        history = (await client.get("/api/evals")).json()
        second = next(item for item in history if item["eval_id"] != first_id)
        assert second["result"]["rerun_of"] == first_id
        detail = await client.get(f"/api/evals/{second['eval_id']}")
        assert detail.json()["result"]["execution_input"]["top_k"] == 3
        await client.post(f"/api/evals/{second['eval_id']}/cancel")
        assert (await asyncio.wait_for(retry, 1)).status_code == 200
