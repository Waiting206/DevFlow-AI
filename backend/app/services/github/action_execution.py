"""Deterministic write expectations and independent GitHub read-back checks."""

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import ActionDraft, ActionExecution, AuditLog, Repository
from app.services.github.client import GitHubClient


def prepare_action(draft: ActionDraft, repo: Repository, number: int | None) -> tuple[dict, dict]:
    action = draft.draft_type.lower()
    if action in {"issue_comment", "pr_comment"}:
        action = "comment"
    if action in {"label", "add_label"}:
        action = "add_labels"
    if action not in {"comment", "create_issue", "close_issue", "add_labels", "send_report"}:
        raise ValueError(f"暂不支持执行草稿类型：{draft.draft_type}")
    if action in {"comment", "close_issue", "add_labels"} and (number is None or number < 1):
        raise ValueError("草稿缺少有效的 Issue/PR 编号")
    payload: dict[str, Any] = {}
    if action == "comment":
        payload = {"body": draft.content}
        expected = {"issue_number": number, "body": draft.content}
    elif action == "create_issue":
        title = draft.title or (draft.content.splitlines()[0][:120] if draft.content else "RepoPilot AI Issue")
        payload = {"title": title, "body": draft.content}
        for key in ("assignees", "labels"):
            values = (draft.meta or {}).get(key)
            if values:
                if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
                    raise ValueError(f"创建 Issue 的 {key} 必须为非空字符串列表")
                payload[key] = values
        expected = dict(payload)
    elif action == "close_issue":
        payload = {"state": "closed"}
        expected = {"number": number, "state": "closed"}
    elif action == "add_labels":
        labels = (draft.meta or {}).get("labels")
        if not isinstance(labels, list) or not labels:
            labels = [draft.title] if draft.title else []
        labels = [str(label) for label in labels if str(label).strip()]
        if not labels:
            raise ValueError("Label 草稿缺少 labels")
        payload = {"labels": labels}
        expected = {"number": number, "labels": labels}
    else:
        expected = {"local_record_only": True}
    return {
        "action": action,
        "owner": repo.owner,
        "repo": repo.name,
        "api_base_url": (repo.api_base_url or settings.github_api_base_url).rstrip("/"),
        "number": number,
        "payload": payload,
        "draft_type": draft.draft_type,
        "title": draft.title,
        "content": draft.content,
    }, expected


async def execute_action(client: GitHubClient, request: dict) -> dict | list:
    owner, repo, number = request["owner"], request["repo"], request["number"]
    payload, action = request["payload"], request["action"]
    if action == "comment":
        return await client.create_issue_comment(owner, repo, number, payload["body"])
    if action == "create_issue":
        options = {key: payload[key] for key in ("assignees", "labels") if key in payload}
        return await client.create_issue(owner, repo, payload["title"], payload["body"], **options)
    if action == "close_issue":
        return await client.close_issue(owner, repo, number)
    if action == "add_labels":
        return await client.add_issue_labels(owner, repo, number, payload["labels"])
    return {"status": "recorded", "message": "报告仅在 RepoPilot AI 中保存，未发送至外部系统。"}


def write_failure_status(exc: Exception) -> str:
    # A timeout/5xx may occur after GitHub accepted the write. Never retry it blindly.
    if isinstance(exc, httpx.HTTPStatusError):
        if 400 <= exc.response.status_code < 500 and exc.response.status_code != 408:
            return "failed"
    return "unknown"


def error_summary(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"GitHub HTTP {exc.response.status_code}"
    # Do not persist client headers, credentials or provider URLs from exceptions.
    return f"{type(exc).__name__}: GitHub 请求未能完成"


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


async def verify_action(client: GitHubClient, request: dict, expected: dict, result: Any) -> dict:
    """Only GET requests are permitted here; successful writes alone prove nothing."""
    action, owner, repo = request["action"], request["owner"], request["repo"]
    check: dict[str, Any] = {"checked_at": datetime.now(timezone.utc).isoformat()}
    if action == "send_report":
        return {**check, "status": "not_applicable", "message": "仅本地记录，未执行外部发送，无 GitHub 验证。"}
    response = result if isinstance(result, dict) else {}
    number = request["number"] if action != "create_issue" else _number(response.get("number"))
    comment_id = _number(response.get("id")) if action == "comment" else None
    if (action == "comment" and comment_id is None) or (action == "create_issue" and number is None):
        return {
            **check, "status": "unknown",
            "message": "未取得新资源的编号，无法唯一定位并验证；不会重复执行写操作。",
        }
    path = (
        f"/repos/{owner}/{repo}/issues/comments/{comment_id}"
        if action == "comment" else f"/repos/{owner}/{repo}/issues/{number}"
    )
    check["resource_path"] = path
    try:
        actual = (
            await client.get_issue_comment(owner, repo, comment_id)
            if action == "comment" else await client.get_issue(owner, repo, number)
        )
        if not isinstance(actual, dict):
            raise ValueError("Unexpected GitHub response")
        checks: dict[str, bool] = {}
        observed: dict[str, Any] = {"id": actual.get("id"), "html_url": actual.get("html_url")}
        if action == "comment":
            # Bind the comment to the expected Issue/PR as well as its exact content.
            issue_url = f"{request['api_base_url']}/repos/{owner}/{repo}/issues/{number}"
            observed.update(body=actual.get("body"), issue_url=actual.get("issue_url"))
            checks = {
                "id_matches": _number(actual.get("id")) == comment_id,
                "target_matches": str(actual.get("issue_url") or "").rstrip("/") == issue_url,
                "body_matches": actual.get("body") == expected["body"],
            }
        else:
            observed["number"] = actual.get("number")
            checks["number_matches"] = _number(actual.get("number")) == number
            if action == "create_issue":
                observed.update(title=actual.get("title"), body=actual.get("body"))
                checks.update(
                    title_matches=actual.get("title") == expected["title"],
                    body_matches=(actual.get("body") or "") == expected["body"],
                    is_issue="pull_request" not in actual,
                )
                for key, field in (("assignees", "login"), ("labels", "name")):
                    if expected.get(key):
                        values = [row.get(field) if isinstance(row, dict) else row for row in actual.get(key) or []]
                        observed[key] = values
                        checks[f"{key}_present"] = (
                            {value.casefold() for value in expected[key]}.issubset({str(value).casefold() for value in values})
                            if key == "assignees" else set(expected[key]).issubset(values)
                        )
            elif action == "close_issue":
                observed["state"] = actual.get("state")
                checks["state_matches"] = actual.get("state") == expected["state"]
            else:
                labels = [row.get("name") if isinstance(row, dict) else row for row in actual.get("labels") or []]
                observed["labels"] = labels
                checks["labels_present"] = set(expected["labels"]).issubset(labels)
        verified = all(checks.values())
        return {
            **check, "status": "verified" if verified else "failed",
            "message": "回读结果符合预期。" if verified else "已读取 GitHub 数据，但与预期不一致。",
            "observed": observed, "checks": checks,
        }
    except Exception as exc:
        return {**check, "status": "unknown", "message": "无法读取并确认 GitHub 实际结果。", "error": error_summary(exc)}


def recover_interrupted_actions(db: Session) -> int:
    """Single-worker startup recovery never resends an uncertain external write."""
    executions = db.query(ActionExecution).filter(
        (ActionExecution.write_status == "running")
        | ActionExecution.verification_status.in_(["pending", "checking"])
    ).all()
    for execution in executions:
        if execution.write_status == "running":
            execution.write_status = "unknown"
            execution.write_error = "服务中断，无法确认 GitHub 写入结果；不会自动重发。"
            execution.draft.status = "unknown"
            execution.draft.error_message = execution.write_error
        execution.verification_status = "unknown"
        check = {
            "status": "unknown", "checked_at": datetime.now(timezone.utc).isoformat(),
            "message": "服务中断，需重新回读验证。",
        }
        history = execution.verification_checks
        if history and history[-1]["status"] == "checking":
            check = {**history[-1], **check}
            history = history[:-1]
        execution.verification_checks = [*history, check]
        db.add(AuditLog(
            user_id=None, repo_id=execution.repo_id,
            action="action_draft.execute.recovered", target_type="action_execution",
            target_id=str(execution.id), status="unknown",
            request_json={"draft_id": str(execution.draft_id), "execution_id": str(execution.id)},
            result_json={"message": "服务启动时恢复中断记录，未重发 GitHub 写入。"},
        ))
    if executions:
        db.commit()
    return len(executions)
