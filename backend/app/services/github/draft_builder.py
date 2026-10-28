"""Resolve the approved write intent and target before creating an executable draft."""

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class GitHubDraftToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, description="原始用户消息，不要改写")
    action: Literal["create_issue", "comment", "close_issue", "add_labels", "send_report"] | None = Field(
        default=None, description="明确的动作。创建新 Issue 用 create_issue，不需要已有编号；不要用 comment 代替创建。"
    )
    target_type: Literal["issue", "pull_request"] | None = Field(default=None, description="评论/关闭/标签操作的目标类型")
    target_number: int | None = Field(default=None, gt=0, description="已有 Issue/PR 的 GitHub 编号，例如 #12 填 12；不能填 UUID，不能随意选最新一条")
    title: str | None = Field(default=None, description="新 Issue 的实际标题；缺少必要信息时先向用户澄清")
    content: str | None = Field(default=None, description="新 Issue 正文、评论正文或报告内容，不要填操作说明或模板占位文本")
    labels: list[str] = Field(default_factory=list, description="需要添加的标签名称")
    assignees: list[str] = Field(default_factory=list, description="用户明确要求的 GitHub 用户名；不要猜测团队成员的登录名")


class DraftInputError(ValueError):
    pass


def infer_action(message: str) -> str | None:
    text = message.lower()
    text = re.split(r"(?:标题|title|正文|内容|body|content|负责人|assignees?)\s*(?:是|为|[:：=])", text, maxsplit=1)[0]
    comment = re.search(r"(?:评论|回复|comment|reply)(?:内容)?\s*[:：]", text)
    if comment:
        text = text[:comment.end()]
    if re.search(r"(?:create|new|open)\s+(?:(?:a|an|new)\s+)?issue\b|(?:创建|新建|新增|开)\s*(?:一个|个|一条|新的|一份)?\s*issue", text):
        return "create_issue"
    if re.search(r"close\s+(?:the\s+)?issue|关闭.{0,20}(?:issue|#\d)|(?:issue\s*#?\d+).{0,10}关闭", text):
        return "close_issue"
    if re.search(r"\blabels?\b|(?:添加|增加|设置|修改|加|改).{0,12}标签", text):
        return "add_labels"
    if re.search(r"\b(?:comment|reply|post)\b|评论|回复|留言", text):
        return "comment"
    if re.search(r"send(?:_report)?|发送.{0,10}报告|发布.{0,10}报告", text):
        return "send_report"
    return None


_FIELD_NAMES = "标题|title|正文|内容|body|content|负责人|assignees?|标签|labels?"


def _field(message: str, names: str) -> str:
    match = re.search(
        rf"(?:{names})\s*(?:是|为|[:：=])\s*(.*?)(?=\s*[,，;；]?\s*(?:{_FIELD_NAMES})\s*(?:是|为|[:：=])|$)",
        message, re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip(" \t\r\n，,；;\"'“”") if match else ""


def resolve_draft(args: dict, *, owner: str, repo: str) -> dict:
    parsed = GitHubDraftToolArgs.model_validate(args)
    message = parsed.message or ""
    # Explicit action language in the actual user message overrides a mistaken tool action.
    action = infer_action(message) or parsed.action
    if action is None:
        raise DraftInputError("请明确要创建 Issue、发表评论、关闭 Issue，还是添加标签；尚未生成可执行草稿。")
    title = _field(message, "标题|title") or (parsed.title or "").strip()
    content = _field(message, "正文|内容|body|content") or (parsed.content or "").strip()
    assignee_text = _field(message, "负责人|assignees?")
    assignees = [s for s in re.split(r"[，,、\s]+", assignee_text) if s] if assignee_text else parsed.assignees
    assignees = [s.strip().lstrip("@") for s in assignees]
    label_text = _field(message, "标签|labels?")
    labels = [s for s in re.split(r"[，,、\s]+", label_text) if s] if label_text else parsed.labels
    labels = [s.strip() for s in labels if s.strip()]
    if assignees and action != "create_issue":
        raise DraftInputError("当前仅支持在创建 Issue 时指定负责人；请将指派需求单独说明，不能混作评论或标签执行。")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) for login in assignees):
        raise DraftInputError("负责人需要 GitHub 用户名，例如 @octocat；不能用尚未映射的中文显示名。")
    number, target_type = None, None
    if action in {"comment", "close_issue", "add_labels"}:
        numbers: set[int] = set()
        types: set[str] = set()
        for link in re.findall(r"https?://[^\s<>]+", message):
            match = re.fullmatch(r"/([^/]+)/([^/]+)/(issues|pull)/(\d+)/?", urlparse(link.rstrip("，。.,")).path)
            if match:
                linked_owner, linked_repo, kind, raw = match.groups()
                if (linked_owner.casefold(), linked_repo.casefold()) != (owner.casefold(), repo.casefold()):
                    raise DraftInputError("消息中的 Issue/PR 链接不属于当前仓库，请先切换仓库。")
                numbers.add(int(raw))
                types.add("issue" if kind == "issues" else "pull_request")
        for match in re.finditer(r"(?<![a-z])(issue|pr|pull\s+request)\s*(?:编号)?\s*(?:是|为|[:：])?\s*#?\s*(\d+)", message, re.IGNORECASE):
            numbers.add(int(match.group(2)))
            types.add("issue" if match.group(1).lower() == "issue" else "pull_request")
        numbers.update(int(n) for n in re.findall(r"#\s*(\d+)", message))
        if len(numbers) > 1 or len(types) > 1:
            raise DraftInputError("消息中有多个 Issue/PR 目标，请明确这份草稿要操作哪一个。")
        if numbers and parsed.target_number is not None and parsed.target_number not in numbers:
            raise DraftInputError("工具参数中的编号与用户消息不一致，请核对目标后重试。")
        number = next(iter(numbers), parsed.target_number)
        target_type = next(iter(types), parsed.target_type or "issue")
        if types and parsed.target_type and parsed.target_type != target_type:
            raise DraftInputError("工具参数中的目标类型与用户消息不一致。")
        if number is None or number < 1:
            raise DraftInputError("请提供已有 Issue/PR 的编号（例如 Issue #1）或链接；不会自动选择最新一条。")
    if action == "create_issue" and (not title or not content):
        raise DraftInputError("创建 Issue 不需要已有编号，请补充实际标题和正文，例如：创建一个 Issue，标题是前端优化，正文是改善动画。")
    if action == "comment" and not content:
        match = re.search(r"(?:评论|回复|comment|reply)(?:内容)?\s*[:：]\s*(.+)", message, re.IGNORECASE | re.DOTALL)
        content = match.group(1).strip() if match else ""
        if not content:
            raise DraftInputError("已识别目标编号，请补充要发表的评论正文。")
    if action == "add_labels" and not labels:
        raise DraftInputError("已识别目标编号，请提供需要添加的标签名称。")
    if action == "send_report" and not content:
        raise DraftInputError("请提供要保存的报告内容；当前报告仅本地记录，未接入外部发送。")
    return {
        "action": action, "target_type": target_type, "target_number": number,
        "title": title or {"comment": "评论", "close_issue": "关闭 Issue", "add_labels": "添加标签", "send_report": "报告"}.get(action, ""),
        "content": content or (f"关闭 Issue #{number}" if action == "close_issue" else f"添加标签：{', '.join(labels)}"),
        "labels": labels, "assignees": assignees,
    }
