import json

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.action_drafts import confirm_action_draft
from app.api.routes.chat import _build_conversation_tools, _risky_action
from app.db.models import ActionDraft, Base, Conversation, Issue, PullRequest, Repository, User
from app.services.agents.chat_agent import ChatAgent, GenericToolArgs
from app.services.github.draft_builder import GitHubDraftToolArgs


@pytest.fixture
def scope():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        repo = Repository(owner="owner", name="repo", full_name="owner/repo", api_base_url="https://api.github.test")
        user = User(name="reviewer", role="owner")
        db.add_all([repo, user])
        db.flush()
        conversation = Conversation(repo_id=repo.id, title="draft test")
        db.add(conversation)
        db.commit()
        tool = _build_conversation_tools(repo, conversation, db)["safety_agent.classify_action_risk"]
        yield db, repo, conversation, user, tool
    engine.dispose()


@pytest.mark.parametrize("message,action", [
    ("帮我创建一个issue 负责人是Alice 标题是前端优化 正文是改善动画", "create_issue"),
    ("创建 一个 Issue", "create_issue"),
    ("create an issue", "create_issue"),
    ("给Issue #1评论：已经修复", "comment"),
    ("关闭 Issue #1", "close_issue"),
    ("把issue#1关闭", "close_issue"),
    ("给PR #2添加标签", "add_labels"),
    ("发送报告", "send_report"),
    ("给Issue #1评论，正文是请创建一个issue", "comment"),
    ("给Issue #1评论：请创建一个issue", "comment"),
])
def test_action_detection_handles_chinese_and_does_not_classify_by_body(message, action):
    assert _risky_action(message) == action


@pytest.mark.asyncio
async def test_original_chinese_create_request_is_not_a_comment_and_needs_no_target(scope):
    db, _, _, _, tool = scope
    result = await tool({"message": "帮我创建一个issue 负责人是Alice 标题是前端优化 正文是改善动画", "action": "comment"}, {})
    draft = db.query(ActionDraft).one()
    assert result["route"] == "safety_draft"
    assert draft.draft_type == "create_issue"
    assert draft.target_type == "repository"
    assert draft.target_id is None
    assert "target_number" not in draft.meta
    assert draft.title == "前端优化"
    assert draft.content == "改善动画"
    assert draft.meta["assignees"] == ["Alice"]
    assert "编号由 GitHub 分配" in result["answer"]
    assert "尚未写入 GitHub" in result["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message,target_type,number", [
    ("给Issue #1评论：已经修复", "issue", 1),
    ("给PR #2评论：测试通过", "pull_request", 2),
    ("comment on https://github.com/owner/repo/issues/5", "issue", 5),
    ("评论 https://github.com/owner/repo/pull/8", "pull_request", 8),
])
async def test_explicit_target_is_preserved_even_without_synchronized_records(scope, message, target_type, number):
    db, _, _, _, tool = scope
    result = await tool({"message": message, "content": "comment body"}, {})
    assert result["route"] == "safety_draft"
    draft = db.query(ActionDraft).one()
    assert draft.target_id is None
    assert draft.target_type == target_type
    assert draft.meta["target_number"] == number


@pytest.mark.asyncio
async def test_does_not_substitute_latest_issue_or_pr_for_requested_target(scope):
    db, repo, _, _, tool = scope
    requested = Issue(repo_id=repo.id, number=1, title="requested")
    db.add_all([requested, Issue(repo_id=repo.id, number=99, title="latest"), PullRequest(repo_id=repo.id, number=100, title="latest PR")])
    db.flush()
    await tool({"message": "给Issue #1评论", "content": "requested comment"}, {})
    draft = db.query(ActionDraft).one()
    assert draft.target_id == requested.id
    assert draft.meta["target_number"] == 1
    assert draft.draft_type == "issue_comment"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"message": "帮我创建一个issue 负责人是Alice"},
    {"message": "评论", "content": "body"},
    {"message": "给Issue #1评论"},
    {"message": "issue编号为#1重新补充并生成草稿"},
    {"message": "给Issue #1和 #2评论", "content": "body"},
    {"message": "给Issue #1评论", "target_number": 2, "content": "body"},
    {"message": "给Issue #1评论", "target_type": "pull_request", "content": "body"},
    {"message": "评论 https://github.com/other/repo/issues/1", "content": "body"},
    {"message": "创建issue，标题是t，正文是b，负责人是张三"},
    {"message": "给Issue #1加标签"},
    {"message": "给Issue #0评论", "content": "body"},
])
async def test_missing_conflicting_or_invalid_arguments_require_clarification_without_saving_draft(scope, args):
    db, _, _, _, tool = scope
    result = await tool(args, {})
    assert result["route"] == "safety_clarification"
    assert result["requires_clarification"] is True
    assert db.query(ActionDraft).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("args,draft_type", [
    ({"message": "关闭Issue #1"}, "close_issue"),
    ({"message": "给Issue #1添加标签，标签是bug、auth"}, "add_labels"),
    ({"message": "请补充草稿", "action": "create_issue", "title": "前端优化", "content": "改善动画"}, "create_issue"),
])
async def test_other_write_actions_and_structured_followup_arguments(scope, args, draft_type):
    db, _, _, _, tool = scope
    await tool(args, {})
    draft = db.query(ActionDraft).one()
    assert draft.draft_type == draft_type
    if draft_type == "add_labels":
        assert draft.meta["labels"] == ["bug", "auth"]


def test_safety_tool_has_specific_schema_and_preserves_original_message():
    agent = ChatAgent(model=object())
    tools = agent._build_langchain_tools({"safety_agent.classify_action_risk": object(), "workspace.read_file": object()}, {})
    assert tools["safety_agent__classify_action_risk"].args_schema is GitHubDraftToolArgs
    assert tools["workspace__read_file"].args_schema is GenericToolArgs
    normalized = agent._normalize_action_input("safety_agent.classify_action_risk", {"message": "rewritten"}, {"message": "original"})
    assert normalized["message"] == "original"
    assert {"action", "target_number", "title", "content", "assignees"}.issubset(GitHubDraftToolArgs.model_json_schema()["properties"])


@pytest.mark.asyncio
@pytest.mark.parametrize("assignee_returned,verified", [(True, "verified"), (False, "failed")])
async def test_chat_create_to_confirm_and_read_back_preserves_body_and_assignee(scope, monkeypatch, assignee_returned, verified):
    db, _, _, user, tool = scope
    await tool({"message": "创建一个issue，标题是前端优化，正文是改善动画，负责人是Alice"}, {})
    db.commit()
    draft = db.query(ActionDraft).one()
    calls = []
    client_type = httpx.AsyncClient

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/repos/owner/repo/issues"
            assert json.loads(request.content) == {"title": "前端优化", "body": "改善动画", "assignees": ["Alice"]}
            return httpx.Response(201, json={"number": 37, "id": 77})
        assert request.url.path == "/repos/owner/repo/issues/37"
        return httpx.Response(200, json={"number": 37, "title": "前端优化", "body": "改善动画", "assignees": [{"login": "alice"}] if assignee_returned else []})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
    await confirm_action_draft(draft.id, db=db, user=user)
    assert calls == [("POST", "/repos/owner/repo/issues"), ("GET", "/repos/owner/repo/issues/37")]
    assert draft.execution.verification_status == verified
    assert draft.execution.expected_state["assignees"] == ["Alice"]


@pytest.mark.asyncio
async def test_structured_model_tool_call_reaches_real_chat_draft_builder(scope):
    db, _, _, _, tool = scope

    class ScriptedModel:
        calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[{
                    "name": "safety_agent__classify_action_risk", "id": "draft-call",
                    "args": {"action": "create_issue", "title": "前端优化", "content": "改善动画"},
                }])
            return AIMessage(content="已生成待确认草稿。")

    result = await ChatAgent(model=ScriptedModel()).run(
        {"message": "创建一个issue，标题是前端优化，正文是改善动画"},
        {"tools": {"safety_agent.classify_action_risk": tool}},
    )
    assert result["tool_calls"][0]["status"] == "success"
    assert db.query(ActionDraft).one().draft_type == "create_issue"
