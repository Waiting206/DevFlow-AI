"""Lifecycle controls for the application's in-process evaluation worker."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import EvalRun
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"queued", "running"}
_running_tasks: dict[str, asyncio.Task] = {}
POLL_SECONDS = 1.0


def _update_active(db: Session, row: EvalRun, result: dict) -> bool:
    # The conditional update prevents a late progress/result write from undoing
    # cancellation, including when another request/process updated the record.
    changed = db.execute(
        update(EvalRun)
        .where(EvalRun.id == row.id, EvalRun.result_json["status"].as_string().in_(ACTIVE_STATUSES))
        .values(result_json=result)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    db.expire_all()
    return bool(changed)


def _terminal_result(result: dict, status: str, code: str) -> dict:
    return {
        **result,
        "status": status,
        "execution_status": status,
        "quality_status": "incomplete",
        "evaluation_complete": False,
        "passed": False,
        "error_code": code,
        "progress": {
            **(result.get("progress") or {}),
            "stage": status,
            "message": "评测已取消。" if status == "cancelled" else "评测出错。",
            "updated_at": datetime.now(UTC).isoformat(),
        },
    }


def cancel_eval_job(db: Session, row: EvalRun) -> None:
    if (row.result_json or {}).get("status") in ACTIVE_STATUSES:
        _update_active(db, row, _terminal_result(row.result_json, "cancelled", "cancelled"))
    task = _running_tasks.get(str(row.id))
    if task and not task.done():
        task.cancel()


def recover_interrupted_evals(db: Session) -> int:
    """Reconcile orphaned jobs at startup of the supported single-worker server."""
    rows = db.query(EvalRun).filter(EvalRun.result_json["status"].as_string().in_(ACTIVE_STATUSES)).all()
    recovered = 0
    for row in rows:
        recovered += _update_active(db, row, _terminal_result(row.result_json, "error", "interrupted"))
    return recovered


async def execute_eval_job(
    eval_id: str,
    operation: Callable[[Session, Callable[..., Awaitable[None]]], Awaitable[dict]],
) -> None:
    """Bound both full evaluations and Judge retries; persist every terminal path."""
    job_uuid = UUID(eval_id)

    def read_result() -> dict:
        with SessionLocal() as state_db:
            row = state_db.get(EvalRun, job_uuid)
            return dict(row.result_json or {}) if row else {}

    def save(transform: Callable[[dict], dict]) -> bool:
        with SessionLocal() as state_db:
            row = state_db.get(EvalRun, job_uuid)
            return bool(row and _update_active(state_db, row, transform(dict(row.result_json or {}))))

    async def progress(payload: dict) -> None:
        if not save(lambda old: {
            **old,
            "status": "running",
            "execution_status": "running",
            "progress": {**payload, "updated_at": datetime.now(UTC).isoformat()},
        }):
            raise asyncio.CancelledError

    async def monitor(task: asyncio.Task) -> None:
        while not task.done():
            await asyncio.sleep(POLL_SECONDS)
            if read_result().get("status") not in ACTIVE_STATUSES:
                task.cancel()
                return

    if read_result().get("status") not in ACTIVE_STATUSES:
        return  # Cancellation may arrive before Starlette starts the job.
    db = SessionLocal()
    task = asyncio.create_task(operation(db, progress), name=f"eval-{eval_id}")
    deadline = asyncio.get_running_loop().time() + settings.eval_job_timeout_seconds
    _running_tasks[eval_id] = task
    watcher = asyncio.create_task(monitor(task))
    try:
        result = await asyncio.wait_for(task, timeout=settings.eval_job_timeout_seconds)
        if asyncio.get_running_loop().time() >= deadline:
            # A dependency may swallow CancelledError and return a late result.
            # It still must not turn an expired evaluation into a success.
            save(lambda old: _terminal_result(old, "error", "timeout"))
            return
        save(lambda old: {
            **old,
            **result,
            "progress": {
                "stage": "completed",
                "message": "评测已完成。",
                "updated_at": datetime.now(UTC).isoformat(),
            },
        })
    except TimeoutError:
        logger.warning("Evaluation timed out: eval_id=%s", eval_id)
        save(lambda old: _terminal_result(old, "error", "timeout"))
    except asyncio.CancelledError:
        save(lambda old: _terminal_result(old, "error", "interrupted"))
        if asyncio.current_task().cancelling():
            raise
    except Exception:
        logger.exception("Evaluation failed: eval_id=%s", eval_id)
        save(lambda old: _terminal_result(old, "error", "execution_error"))
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Evaluation status monitor failed: eval_id=%s", eval_id)
        finally:
            _running_tasks.pop(eval_id, None)
            db.close()
