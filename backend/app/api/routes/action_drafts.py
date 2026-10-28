import asyncio
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.models import ActionDraft, ActionExecution, AuditLog, Issue, PullRequest, Repository, User
from app.db.session import get_db
from app.schemas.action_drafts import ActionDraftConfirmResponse, ActionDraftCreateRequest, ActionDraftResponse, AuditLogResponse
from app.services.github.client import GitHubClient
from app.services.github.action_execution import error_summary, execute_action, prepare_action, verify_action, write_failure_status
from app.services.permissions import require_permission, write_audit_log

router = APIRouter()


def _target_number(db: Session, draft: ActionDraft) -> int | None:
    meta = draft.meta or {}
    raw = meta.get("target_number") or meta.get("issue_number") or meta.get("pr_number")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if draft.target_id and draft.target_type == "issue":
        issue = db.get(Issue, draft.target_id)
        return issue.number if issue else None
    if draft.target_id and draft.target_type in {"pull_request", "pr"}:
        pr = db.get(PullRequest, draft.target_id)
        return pr.number if pr else None
    return None


def _github_client(repo: Repository, request: dict) -> GitHubClient:
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
    return GitHubClient(token=token, base_url=request["api_base_url"])


def _execution_audit(db: Session, execution: ActionExecution, user: User, action: str, status: str, result: dict) -> AuditLog:
    return write_audit_log(
        db, user=user, repo_id=execution.repo_id, action=action,
        target_type="action_execution", target_id=str(execution.id), status=status,
        request_json={"draft_id": str(execution.draft_id), "execution_id": str(execution.id)},
        result_json=result,
    )


async def _verify_execution(db: Session, execution: ActionExecution, repo: Repository, user: User) -> AuditLog:
    claimed = db.execute(
        update(ActionExecution).where(
            ActionExecution.id == execution.id,
            ActionExecution.write_status != "running",
            ActionExecution.verification_status != "checking",
        ).values(verification_status="checking").execution_options(synchronize_session=False)
    ).rowcount
    if not claimed:
        db.rollback()
        raise HTTPException(status_code=409, detail="操作仍在执行或验证中，请稍后刷新")
    db.refresh(execution)
    started = {
        "status": "checking", "message": "开始独立回读验证。",
        "checked_at": datetime.now(timezone.utc).isoformat(), "checked_by": str(user.id),
    }
    execution.verification_checks = [*execution.verification_checks, started]
    _execution_audit(db, execution, user, "action_draft.verify.start", "running", started)
    db.commit()
    try:
        check = await verify_action(
            _github_client(repo, execution.request_json), execution.request_json,
            execution.expected_state, execution.write_result,
        )
    except asyncio.CancelledError:
        execution.verification_status = "unknown"
        check = {
            **started,
            "status": "unknown", "message": "验证被中断，可稍后重新验证。",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        execution.verification_checks = [*execution.verification_checks[:-1], check]
        _execution_audit(db, execution, user, "action_draft.verify", "unknown", check)
        db.commit()
        raise
    except Exception as exc:
        check = {"status": "unknown", "message": "验证未能完成。", "error": error_summary(exc), "checked_at": datetime.now(timezone.utc).isoformat()}
    check = {**started, **check, "completed_at": datetime.now(timezone.utc).isoformat()}
    execution.verification_status = check["status"]
    execution.verified_at = datetime.now(timezone.utc)
    execution.verification_checks = [*execution.verification_checks[:-1], check]
    audit = _execution_audit(db, execution, user, "action_draft.verify", check["status"], check)
    db.commit()
    return audit


@router.post("", response_model=ActionDraftResponse)
async def create_action_draft(
    payload: ActionDraftCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:create")),
) -> ActionDraft:
    repo = db.get(Repository, payload.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    draft = ActionDraft(
        repo_id=payload.repo_id,
        draft_type=payload.draft_type,
        target_type=payload.target_type,
        target_id=payload.target_id,
        title=payload.title,
        content=payload.content,
        risk_level=payload.risk_level,
        created_by=user.id,
        meta=payload.metadata,
    )
    db.add(draft)
    write_audit_log(
        db,
        user=user,
        repo_id=payload.repo_id,
        action="action_draft.create",
        target_type=payload.target_type,
        target_id=str(payload.target_id) if payload.target_id else None,
        request_json=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(draft)
    return draft


@router.get("", response_model=list[ActionDraftResponse])
async def list_action_drafts(
    repo_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),
) -> list[ActionDraft]:
    query = db.query(ActionDraft)
    if repo_id:
        query = query.filter(ActionDraft.repo_id == repo_id)
    if status:
        query = query.filter(ActionDraft.status == status)
    return query.order_by(ActionDraft.created_at.desc()).limit(100).all()


@router.get("/audit-logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    repo_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("settings:manage")),
) -> list[AuditLog]:
    query = db.query(AuditLog)
    if repo_id:
        query = query.filter(AuditLog.repo_id == repo_id)
    return query.order_by(AuditLog.created_at.desc()).limit(100).all()


@router.get("/{draft_id}", response_model=ActionDraftResponse)
async def get_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(require_permission("repo:read")),
) -> ActionDraft:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    return draft


@router.post("/{draft_id}/cancel", response_model=ActionDraftResponse)
async def cancel_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:create")),
) -> ActionDraft:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    if draft.status != "pending_confirmation":
        raise HTTPException(status_code=400, detail="只有待确认草稿可以取消")
    changed = db.execute(
        update(ActionDraft).where(ActionDraft.id == draft_id, ActionDraft.status == "pending_confirmation")
        .values(status="cancelled").execution_options(synchronize_session=False)
    ).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(status_code=409, detail="草稿状态已变化，请刷新")
    write_audit_log(db, user=user, repo_id=draft.repo_id, action="action_draft.cancel", target_type=draft.target_type, target_id=str(draft.id))
    db.commit()
    db.refresh(draft)
    return draft


@router.post("/{draft_id}/confirm", response_model=ActionDraftConfirmResponse)
async def confirm_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:approve")),
) -> dict:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    if draft.status != "pending_confirmation":
        raise HTTPException(status_code=400, detail="只有待确认草稿可以执行")
    repo = db.get(Repository, draft.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    try:
        request, expected = prepare_action(draft, repo, _target_number(db, draft))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Commit the claim and immutable request before issuing any external write.
    changed = db.execute(
        update(ActionDraft).where(ActionDraft.id == draft_id, ActionDraft.status == "pending_confirmation")
        .values(status="executing", confirmed_by=user.id).execution_options(synchronize_session=False)
    ).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(status_code=409, detail="草稿已被确认或取消，请刷新")
    execution = ActionExecution(
        draft_id=draft.id, repo_id=repo.id, operator_id=user.id,
        request_json=request, expected_state=expected,
    )
    db.add(execution)
    db.flush()
    _execution_audit(db, execution, user, "action_draft.execute.start", "running", {"expected_state": expected})
    db.commit()
    db.refresh(draft)
    try:
        result = await execute_action(_github_client(repo, request), request)
    except asyncio.CancelledError:
        execution.write_status = draft.status = "unknown"
        execution.write_error = draft.error_message = "执行被中断，GitHub 可能已接收操作；不会自动重发。"
        execution.verification_status = "unknown"
        _execution_audit(db, execution, user, "action_draft.execute.interrupted", "unknown", {"error": execution.write_error})
        db.commit()
        raise
    except Exception as exc:
        execution.write_status = write_failure_status(exc)
        execution.write_error = error_summary(exc)
        draft.status = execution.write_status
        draft.error_message = execution.write_error
    else:
        execution.write_status = "succeeded"
        execution.write_result = result
        draft.status = "executed"
        draft.execution_result = result if isinstance(result, dict) else {"result": result}
    execution.write_completed_at = draft.executed_at = datetime.now(timezone.utc)
    if execution.write_status == "failed":
        execution.verification_status = "not_applicable"
        execution.verification_checks = [{
            "status": "not_applicable", "message": "GitHub 明确拒绝写入，未执行结果验证。",
            "checked_at": datetime.now(timezone.utc).isoformat(), "checked_by": str(user.id),
        }]
    audit = _execution_audit(
        db, execution, user, f"action_draft.confirm.{draft.draft_type}",
        "success" if execution.write_status == "succeeded" else execution.write_status,
        {"response": execution.write_result, "error": execution.write_error},
    )
    # Persist the write response even if the subsequent independent read fails.
    db.commit()
    audit_id = audit.id
    if execution.write_status != "failed":
        await _verify_execution(db, execution, repo, user)
    db.refresh(draft)
    return {"draft": draft, "audit_id": audit_id}


@router.post("/{draft_id}/verify", response_model=ActionDraftConfirmResponse)
async def verify_action_draft(
    draft_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("draft:approve")),
) -> dict:
    draft = db.get(ActionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="未找到安全草稿")
    execution = draft.execution
    if execution is None:
        raise HTTPException(status_code=400, detail="此草稿没有可验证的执行记录；旧记录不补记为已验证")
    if execution.write_status == "failed":
        raise HTTPException(status_code=400, detail="GitHub 已明确拒绝该写入，无待验证结果")
    repo = db.get(Repository, execution.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    audit = await _verify_execution(db, execution, repo, user)
    db.refresh(draft)
    return {"draft": draft, "audit_id": audit.id}
