import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.action_drafts import (
    cancel_action_draft,
    confirm_action_draft,
    create_action_draft,
    router,
    verify_action_draft,
)
from app.core.config import settings
from app.db.models import ActionDraft, ActionExecution, AuditLog, Base, Repository, User
from app.db.session import get_db
from app.schemas.action_drafts import ActionDraftCreateRequest, ActionDraftResponse
from app.services.github.action_execution import recover_interrupted_actions


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'actions.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        user = User(name="reviewer", role="owner")
        repo = Repository(owner="owner", name="repo", full_name="owner/repo", api_base_url="https://api.github.test")
        db.add_all([user, repo])
        db.commit()
        yield db, factory, user, repo
    engine.dispose()


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        client_type = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))
    return install


async def draft_for(store, action="close_issue", **overrides):
    db, _, user, repo = store
    data = dict(repo_id=repo.id, draft_type=action, target_type="issue", title="Title", content="Body", metadata={"target_number": 12, "labels": ["bug", "auth"]})
    data.update(overrides)
    return await create_action_draft(ActionDraftCreateRequest(**data), db=db, user=user)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create_issue", "issue_comment", "pr_comment", "comment", "close_issue", "label", "add_label", "add_labels"])
async def test_write_then_independent_get_and_durable_records(store, transport, action):
    db, factory, user, _ = store
    draft = await draft_for(store, action)
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method != "GET":
            # This must already be committed in a separate connection before POST/PATCH.
            with factory() as other:
                entry = other.query(ActionExecution).one()
                assert entry.write_status == "running"
                assert other.get(ActionDraft, draft.id).status == "executing"
                assert other.query(AuditLog).filter_by(action="action_draft.execute.start").count() == 1
                assert entry.request_json["content"] == "Body"
            if action in {"label", "add_label", "add_labels"}:
                return httpx.Response(200, json=[{"name": "bug"}, {"name": "auth"}])
            return httpx.Response(201, json={"id": 77, "number": 12, "state": "closed"})
        # Write response must survive even if verification fails after this point.
        with factory() as other:
            entry = other.query(ActionExecution).one()
            assert entry.write_status == "succeeded"
            assert entry.write_result is not None
            assert entry.verification_status == "checking"
            assert entry.verification_checks[-1]["status"] == "checking"
            assert entry.verification_checks[-1]["checked_by"] == str(user.id)
        if "comment" in action:
            assert request.url.path == "/repos/owner/repo/issues/comments/77"
            data = {"id": 77, "body": "Body", "issue_url": "https://api.github.test/repos/owner/repo/issues/12"}
        else:
            assert request.url.path == "/repos/owner/repo/issues/12"
            data = {"number": 12, "title": "Title", "body": "Body", "state": "closed", "labels": [{"name": "bug"}, {"name": "auth"}, {"name": "extra"}]}
        return httpx.Response(200, json=data)

    transport(handle)
    result = await confirm_action_draft(draft.id, db=db, user=user)
    saved = result["draft"].execution
    assert result["draft"].status == "executed"
    assert saved.verification_status == "verified"
    assert len(saved.verification_checks) == 1
    assert saved.operator_id == user.id
    assert calls[0][0] in {"POST", "PATCH"}
    assert calls[1][0] == "GET"
    assert len(calls) == 2
    assert ActionDraftResponse.model_validate(result["draft"]).execution.id == saved.id
    with factory() as other:
        persisted = other.query(ActionExecution).one()
        assert persisted.verification_checks[0]["checks"]
        assert persisted.verified_at is not None
        assert other.query(AuditLog).filter_by(target_id=str(saved.id)).count() == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("action,actual", [
    ("create_issue", {"number": 12, "title": "wrong", "body": "Body"}),
    ("issue_comment", {"id": 77, "body": "wrong", "issue_url": "https://api.github.test/repos/owner/repo/issues/12"}),
    ("issue_comment", {"id": 77, "body": "Body", "issue_url": "https://api.github.test/repos/owner/repo/issues/99"}),
    ("close_issue", {"number": 12, "state": "open"}),
    ("add_labels", {"number": 12, "labels": [{"name": "bug"}]}),
])
async def test_successful_write_response_cannot_replace_read_back_verification(store, transport, action, actual):
    db, _, user, _ = store
    draft = await draft_for(store, action)
    transport(lambda r: httpx.Response(200, json=actual if r.method == "GET" else {"id": 77, "number": 12, "state": "closed"}))

    result = await confirm_action_draft(draft.id, db=db, user=user)

    assert result["draft"].status == "executed"
    assert result["draft"].execution.write_status == "succeeded"
    assert result["draft"].execution.verification_status == "failed"
    assert not all(result["draft"].execution.verification_checks[-1]["checks"].values())


@pytest.mark.asyncio
async def test_read_timeout_and_retry_only_reads_and_keeps_both_attempts(store, transport):
    db, factory, user, _ = store
    draft = await draft_for(store)
    calls = []
    timeout = True

    def handle(request):
        calls.append(request.method)
        if request.method != "GET":
            return httpx.Response(200, json={"number": 12, "state": "closed"})
        if timeout:
            raise httpx.ReadTimeout("TOP_SECRET must not be persisted", request=request)
        return httpx.Response(200, json={"number": 12, "state": "closed"})

    transport(handle)
    await confirm_action_draft(draft.id, db=db, user=user)
    assert draft.execution.write_status == "succeeded"
    assert draft.execution.verification_status == "unknown"
    assert draft.execution.write_result["number"] == 12
    timeout = False
    await verify_action_draft(draft.id, db=db, user=user)
    assert calls == ["PATCH", "GET", "GET"]
    assert draft.execution.verification_status == "verified"
    assert [c["status"] for c in draft.execution.verification_checks] == ["unknown", "verified"]
    with factory() as other:
        assert "TOP_SECRET" not in json.dumps(other.query(ActionExecution).one().verification_checks)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected_status", [("403", "failed"), ("500", "unknown"), ("timeout", "unknown")])
async def test_write_errors_are_preserved_without_repeating_writes(store, transport, kind, expected_status):
    db, _, user, _ = store
    draft = await draft_for(store, "create_issue")
    calls = []

    def handle(request):
        calls.append(request.method)
        if kind == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(int(kind), json={"message": "error"})

    transport(handle)
    await confirm_action_draft(draft.id, db=db, user=user)
    assert draft.status == expected_status
    assert draft.execution.write_status == expected_status
    assert draft.execution.write_error
    if kind != "403":
        await verify_action_draft(draft.id, db=db, user=user)
        assert draft.execution.verification_status == "unknown"
    assert calls == ["POST"]
    with pytest.raises(HTTPException):
        await confirm_action_draft(draft.id, db=db, user=user)
    assert calls == ["POST"]


@pytest.mark.asyncio
async def test_uncertain_close_can_be_verified_by_its_known_target(store, transport):
    db, _, user, _ = store
    draft = await draft_for(store)
    calls = []

    def handle(request):
        calls.append(request.method)
        if request.method == "PATCH":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"number": 12, "state": "closed"})

    transport(handle)
    await confirm_action_draft(draft.id, db=db, user=user)
    assert draft.execution.write_status == "unknown"
    assert draft.execution.verification_status == "verified"
    assert calls == ["PATCH", "GET"]


@pytest.mark.asyncio
async def test_concurrent_confirm_and_cancel_with_stale_session_cannot_duplicate_write(store, transport):
    db, factory, user, _ = store
    draft = await draft_for(store)
    with factory() as other:
        # Keep the pre-confirmation object cached, simulating competing requests.
        stale = other.get(ActionDraft, draft.id)
        reviewer = other.get(User, user.id)

        async def handle(request):
            if request.method == "PATCH":
                assert stale.status == "pending_confirmation"
                with pytest.raises(HTTPException) as rejected:
                    await confirm_action_draft(draft.id, db=other, user=reviewer)
                assert rejected.value.status_code == 409
                with pytest.raises(HTTPException):
                    await cancel_action_draft(draft.id, db=other, user=reviewer)
                return httpx.Response(200, json={"number": 12})
            return httpx.Response(200, json={"number": 12, "state": "closed"})

        transport(handle)
        await confirm_action_draft(draft.id, db=db, user=user)
    assert db.query(ActionExecution).count() == 1
    assert draft.execution.verification_status == "verified"


@pytest.mark.asyncio
async def test_interruption_retains_request_and_never_resends_on_recovery(store, transport):
    db, factory, user, _ = store
    draft = await draft_for(store)
    transport(lambda r: (_ for _ in ()).throw(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await confirm_action_draft(draft.id, db=db, user=user)
    with factory() as other:
        saved = other.query(ActionExecution).one()
        assert saved.write_status == "unknown"
        assert saved.request_json["number"] == 12


@pytest.mark.asyncio
async def test_database_failure_after_write_leaves_recoverable_record(store, transport, monkeypatch):
    db, factory, user, _ = store
    draft = await draft_for(store)
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={"number": 12, "state": "closed"})

    transport(handle)
    commit = db.commit

    def fail_after_write():
        if any(isinstance(row, ActionExecution) and row.write_status == "succeeded" for row in db.identity_map.values()):
            raise RuntimeError("simulated database outage")
        commit()

    monkeypatch.setattr(db, "commit", fail_after_write)
    with pytest.raises(RuntimeError, match="database outage"):
        await confirm_action_draft(draft.id, db=db, user=user)
    db.rollback()
    with factory() as other:
        assert other.query(ActionExecution).one().write_status == "running"
        assert recover_interrupted_actions(other) == 1
        assert recover_interrupted_actions(other) == 0
        saved = other.get(ActionDraft, draft.id)
        assert saved.status == "unknown"
        await verify_action_draft(saved.id, db=other, user=other.get(User, user.id))
        assert saved.execution.verification_status == "verified"
        assert len(saved.execution.verification_checks) == 2
    assert calls == ["PATCH", "GET"]


@pytest.mark.asyncio
async def test_send_report_is_explicitly_local_only(store, transport):
    db, _, user, _ = store
    draft = await draft_for(store, "send_report")
    transport(lambda r: pytest.fail("Local report must not contact GitHub"))
    await confirm_action_draft(draft.id, db=db, user=user)
    assert draft.status == "executed"
    assert draft.execution.verification_status == "not_applicable"
    assert draft.execution_result["status"] == "recorded"


@pytest.mark.asyncio
async def test_invalid_target_is_rejected_before_claim_or_external_write(store, transport):
    db, _, user, _ = store
    draft = await draft_for(store, metadata={})
    transport(lambda r: pytest.fail("Invalid request must not reach GitHub"))
    with pytest.raises(HTTPException) as rejected:
        await confirm_action_draft(draft.id, db=db, user=user)
    assert rejected.value.status_code == 400
    assert draft.status == "pending_confirmation"
    assert db.query(ActionExecution).count() == 0


def test_http_response_history_and_verification_permissions(store, transport):
    db, factory, user, repo = store
    app = FastAPI()
    app.include_router(router, prefix="/api/action-drafts")

    def session():
        with factory() as other:
            yield other

    app.dependency_overrides[get_db] = session
    transport(lambda r: httpx.Response(200, json={"number": 12, "state": "closed"}))
    with TestClient(app) as client:
        response = client.post("/api/action-drafts", json={"repo_id": str(repo.id), "draft_type": "close_issue", "target_type": "issue", "content": "close", "metadata": {"target_number": 12}})
        assert response.status_code == 200
        draft_id = response.json()["id"]
        assert response.json()["execution"] is None
        confirmed = client.post(f"/api/action-drafts/{draft_id}/confirm")
        assert confirmed.status_code == 200
        execution_id = confirmed.json()["draft"]["execution"]["id"]
        assert confirmed.json()["draft"]["execution"]["verification_status"] == "verified"
        assert client.get(f"/api/action-drafts/{draft_id}").json()["execution"]["id"] == execution_id
        assert client.get("/api/action-drafts").json()[0]["execution"]["id"] == execution_id
        assert client.post(f"/api/action-drafts/{draft_id}/verify").status_code == 200
        assert len(client.get(f"/api/action-drafts/{draft_id}").json()["execution"]["verification_checks"]) == 2
        user.role = "viewer"
        db.commit()
        assert client.post(f"/api/action-drafts/{draft_id}/verify").status_code == 403


@pytest.mark.asyncio
async def test_legacy_draft_is_not_retroactively_marked_verified(store):
    db, _, user, repo = store
    draft = ActionDraft(repo_id=repo.id, draft_type="close_issue", target_type="issue", content="legacy", status="executed")
    db.add(draft)
    db.commit()
    with pytest.raises(HTTPException) as rejected:
        await verify_action_draft(draft.id, db=db, user=user)
    assert rejected.value.status_code == 400
    assert db.query(ActionExecution).count() == 0


@pytest.mark.asyncio
async def test_initial_record_commit_failure_prevents_external_write(store, transport, monkeypatch):
    db, factory, user, _ = store
    draft = await draft_for(store)
    draft_id = draft.id
    transport(lambda r: pytest.fail("No GitHub call before durable claim"))

    def fail_commit():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await confirm_action_draft(draft_id, db=db, user=user)
    db.rollback()
    with factory() as other:
        assert other.get(ActionDraft, draft_id).status == "pending_confirmation"
        assert other.query(ActionExecution).count() == 0


@pytest.mark.asyncio
async def test_reverification_detects_changed_state_and_keeps_previous_evidence(store, transport):
    db, _, user, _ = store
    draft = await draft_for(store)
    closed = True
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={"number": 12, "state": "closed" if closed else "open"})

    transport(handle)
    await confirm_action_draft(draft.id, db=db, user=user)
    closed = False
    await verify_action_draft(draft.id, db=db, user=user)
    assert [check["status"] for check in draft.execution.verification_checks] == ["verified", "failed"]
    assert draft.execution.verification_checks[0]["observed"]["state"] == "closed"
    assert draft.execution.verification_checks[1]["observed"]["state"] == "open"
    assert calls == ["PATCH", "GET", "GET"]


@pytest.mark.asyncio
async def test_concurrent_verification_cannot_overwrite_another_attempt(store, transport):
    db, factory, user, _ = store
    draft = await draft_for(store)
    competing = False
    with factory() as other:
        reviewer = other.get(User, user.id)

        async def handle(request):
            if request.method == "GET" and competing:
                with pytest.raises(HTTPException) as rejected:
                    await verify_action_draft(draft.id, db=other, user=reviewer)
                assert rejected.value.status_code == 409
            return httpx.Response(200, json={"number": 12, "state": "closed"})

        transport(handle)
        await confirm_action_draft(draft.id, db=db, user=user)
        other.get(ActionDraft, draft.id)
        competing = True
        await verify_action_draft(draft.id, db=db, user=user)
    assert len(draft.execution.verification_checks) == 2
    assert draft.execution.verification_status == "verified"


@pytest.mark.asyncio
async def test_cancelled_verification_preserves_successful_write(store, transport):
    db, _, user, _ = store
    draft = await draft_for(store)

    def handle(request):
        if request.method == "GET":
            raise asyncio.CancelledError()
        return httpx.Response(200, json={"number": 12})

    transport(handle)
    with pytest.raises(asyncio.CancelledError):
        await confirm_action_draft(draft.id, db=db, user=user)
    db.refresh(draft)
    assert draft.status == "executed"
    assert draft.execution.write_status == "succeeded"
    assert draft.execution.verification_status == "unknown"
    assert draft.execution.verification_checks[-1]["checked_by"] == str(user.id)
    assert draft.execution.verification_checks[-1]["status"] == "unknown"


def test_startup_recovers_abandoned_verification_without_losing_response(store):
    db, _, user, repo = store
    draft = ActionDraft(repo_id=repo.id, draft_type="close_issue", target_type="issue", content="close", status="executed")
    db.add(draft)
    db.flush()
    execution = ActionExecution(
        draft_id=draft.id, repo_id=repo.id, operator_id=user.id,
        request_json={"number": 12}, expected_state={"state": "closed"},
        write_status="succeeded", write_result={"number": 12}, verification_status="checking",
        verification_checks=[{"status": "checking", "checked_at": "2026-10-26T00:00:00Z", "checked_by": str(user.id)}],
    )
    db.add(execution)
    db.commit()
    assert recover_interrupted_actions(db) == 1
    assert execution.write_status == "succeeded"
    assert execution.write_result == {"number": 12}
    assert execution.verification_status == "unknown"
    assert len(execution.verification_checks) == 1
    assert execution.verification_checks[0]["checked_by"] == str(user.id)
    assert draft.status == "executed"
