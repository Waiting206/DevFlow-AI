import asyncio
from copy import deepcopy

import httpx
import pytest

from app.core.config import settings
from app.schemas.workflow import WorkflowSpec, WorkflowTaskResult
from app.services.agents.chat_agent import ChatAgent
from app.services.agents.planner_agent import PlannerAgent, _PlannerLLMClient
from app.services.agents.workflow_orchestrator import WorkflowOrchestrator
from app.services.llm.prompts import PLANNER_AGENT_PROMPT


CONTEXT = {
    "repo_id": "repo-1", "has_repo": True,
    "has_issue": True, "issue_id": "issue-1", "issue_number": 12,
    "has_pr": True, "pr_id": "pr-1", "pr_number": 34,
    "has_ci_run": True, "workflow_run_id": "run-1", "workflow_run_number": 56,
}
MESSAGE = {"message": "只排查 CI，不分析 Issue，也不审查 PR 风险。"}


def plan(payload, task_ids=("repo_health", "ci_debug")):
    catalog = {task["id"]: task for task in payload["available_tasks"]}
    return {
        "goal": payload["goal"], "trigger_message": payload["trigger_message"],
        "tasks": [deepcopy(catalog[task_id]) for task_id in task_ids],
        "acceptance_criteria": ["说明 CI 首个失败位置、证据缺口与验证步骤。"],
        "max_parallel_tasks": payload["max_parallel_tasks"],
        "requires_human_confirmation": payload["requires_human_confirmation"],
    }


class FakeLLM:
    model = "same-model-as-chat"
    api_key = "test-key"

    def __init__(self, response=plan):
        self.response = response
        self.calls = []

    async def chat_json(self, prompt, payload):
        self.calls.append((prompt, payload))
        return self.response(payload)


@pytest.mark.asyncio
async def test_model_selects_intent_without_union_of_optional_keyword_tasks():
    llm = FakeLLM()
    result = await PlannerAgent(llm).run(MESSAGE, {**CONTEXT, "credentials": "must-not-be-sent"})
    baseline = PlannerAgent(use_llm=False)._rule_spec(MESSAGE, CONTEXT)
    assert {task.task_type for task in baseline.tasks} == {"repo_health", "issue_analysis", "pr_review", "ci_debug"}
    assert [task["id"] for task in result["tasks"]] == ["repo_health", "ci_debug"]
    assert result["planner_trace"]["source"] == "llm_with_rules"
    assert result["planner_trace"]["model"] == llm.model
    assert result["acceptance_criteria"][:6] == baseline.acceptance_criteria
    assert "must-not-be-sent" not in str(llm.calls)
    assert llm.calls[0][0] == PLANNER_AGENT_PROMPT


@pytest.mark.asyncio
async def test_rules_restore_mandatory_tasks_scope_and_controls():
    def response(payload):
        result = plan(payload, ("pr_review",))
        result.update(goal="model changed goal", trigger_message="changed", workflow_id="forged",
                      max_parallel_tasks=8, requires_human_confirmation=False, planner_trace={"forged": True})
        result["tasks"][0].update(input_hint={"pr_number": 999}, critical=False)
        result["tasks"][0]["claim"]["allowed_sources"] = ["diff"]
        return result

    context = {**CONTEXT, "requires_safety": True, "issue_explicit": True,
               "pr_explicit": True, "enable_rag_task": True}
    result = await PlannerAgent(FakeLLM(response)).run({"message": "合并这个 PR 并发送报告"}, context)
    tasks = {task["id"]: task for task in result["tasks"]}
    assert set(tasks) == {"repo_health", "pr_review", "issue_analysis", "ci_debug", "rag_search", "safety_review"}
    assert result["planner_trace"]["merged_rule_tasks"] == ["ci_debug", "issue_analysis", "rag_search", "repo_health", "safety_review"]
    assert result["workflow_id"] != "forged"
    assert result["goal"] == result["trigger_message"] == "合并这个 PR 并发送报告"
    assert result["max_parallel_tasks"] == 3
    assert result["requires_human_confirmation"] is True
    assert tasks["pr_review"]["input_hint"] == {"pr_number": 34}
    assert tasks["pr_review"]["critical"] is True
    assert "ci_summary" in tasks["pr_review"]["claim"]["allowed_sources"]
    assert tasks["safety_review"]["claim"]["write_intent"] is True
    assert tasks["pr_review"]["dependencies"] == ["repo_health"]


def invalid_case(payload, change):
    candidate = plan(payload, ("repo_health", "pr_review", "ci_debug"))
    change(candidate)
    return candidate


@pytest.mark.parametrize(("change", "reason"), [
    (lambda p: p["tasks"].append(deepcopy(p["tasks"][0])), "duplicate_task"),
    (lambda p: p["tasks"][1].update(agent_name="shell_agent"), "unavailable_task"),
    (lambda p: p["tasks"][1].update(id="custom_id"), "unavailable_task"),
    (lambda p: p["tasks"][1]["claim"].update(target_id="other-repo-pr"), "scope_violation"),
    (lambda p: p["tasks"][1]["claim"].update(target_type="repository"), "scope_violation"),
    (lambda p: p["tasks"][1]["claim"]["allowed_sources"].append("shell"), "scope_violation"),
    (lambda p: p["tasks"][1]["claim"].update(write_intent=True), "scope_violation"),
    (lambda p: p["tasks"][1].update(dependencies=["nonexistent"]), "invalid_dependencies"),
    (lambda p: (p["tasks"][1].update(dependencies=["ci_debug"]),
                p["tasks"][2].update(dependencies=["pr_review"])), "cyclic_dependencies"),
    (lambda p: p["tasks"][2].update(dependencies=["ci_debug"]), "cyclic_dependencies"),
    (lambda p: p["tasks"][1].update(objective=" "), "invalid_objective"),
    (lambda p: p.update(acceptance_criteria=["x" * 601]), "invalid_acceptance_criteria"),
    (lambda p: p.update(tasks=[]), "invalid_tasks"),
    (lambda p: p.update(max_parallel_tasks=99), "invalid_structure"),
])
@pytest.mark.asyncio
async def test_invalid_model_plan_falls_back_without_partial_execution(change, reason):
    agent = PlannerAgent(FakeLLM(lambda payload: invalid_case(payload, change)))
    result = await agent.run(MESSAGE, CONTEXT)
    baseline = agent._rule_spec(MESSAGE, CONTEXT).model_dump(mode="json")
    for field in ("goal", "trigger_message", "tasks", "acceptance_criteria", "max_parallel_tasks", "requires_human_confirmation"):
        assert result[field] == baseline[field]
    assert result["planner_trace"]["source"] == "rules_fallback"
    assert result["planner_trace"]["fallback_reason"] == reason


@pytest.mark.parametrize("response", [{}, [], {"tasks": []}, None])
@pytest.mark.asyncio
async def test_empty_or_malformed_response_returns_rule_plan(response):
    result = await PlannerAgent(FakeLLM(lambda _: response)).run(MESSAGE, CONTEXT)
    assert result["planner_trace"]["source"] == "rules_fallback"
    assert len(result["tasks"]) == 4


@pytest.mark.asyncio
async def test_no_key_and_disabled_mode_never_call_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", None)
    result = await PlannerAgent().run(MESSAGE, CONTEXT)
    assert result["planner_trace"]["fallback_reason"] == "missing_api_key"
    llm = FakeLLM(lambda _: pytest.fail("disabled planner called provider"))
    result = await PlannerAgent(llm, use_llm=False).run(MESSAGE, CONTEXT)
    assert result["planner_trace"]["source"] == "rules"


@pytest.mark.asyncio
async def test_timeout_cancels_request_and_falls_back():
    cancelled = asyncio.Event()

    class SlowLLM(FakeLLM):
        async def chat_json(self, prompt, payload):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    result = await PlannerAgent(SlowLLM(), timeout_seconds=0.01).run(MESSAGE, CONTEXT)
    assert cancelled.is_set()
    assert result["planner_trace"]["fallback_reason"] == "timeout"


@pytest.mark.asyncio
async def test_provider_exception_is_sanitized_in_trace():
    class BrokenLLM(FakeLLM):
        async def chat_json(self, prompt, payload):
            raise RuntimeError("Bearer secret-key should never appear in trace")

    result = await PlannerAgent(BrokenLLM()).run(MESSAGE, CONTEXT)
    assert result["planner_trace"]["fallback_reason"] == "llm_error"
    assert "secret-key" not in str(result)


@pytest.mark.asyncio
async def test_unavailable_targets_are_not_exposed_or_invented():
    context = {"has_repo": True, "repo_id": "repo-1"}
    llm = FakeLLM(lambda p: plan(p, ("repo_health",)))
    result = await PlannerAgent(llm).run({"message": "请检查 PR"}, context)
    assert {task["id"] for task in llm.calls[0][1]["available_tasks"]} == {"repo_health"}
    assert [task["id"] for task in result["tasks"]] == ["repo_health"]


@pytest.mark.parametrize(("message", "enabled"), [("检索相似的历史案例", False), ("检查仓库概况", True)])
@pytest.mark.asyncio
async def test_rag_retains_original_trigger_boundary(message, enabled):
    llm = FakeLLM(lambda p: plan(p, ("repo_health", "rag_search")))
    result = await PlannerAgent(llm).run({"message": message}, {**CONTEXT, "enable_rag_task": enabled})
    assert result["planner_trace"]["source"] == "llm_with_rules"
    assert "rag_search" in {task["id"] for task in result["tasks"]}


@pytest.mark.parametrize(("response_type", "reason"), [
    ("http_error", "llm_error"), ("timeout", "timeout"),
    ("truncated", "truncated_response"), ("malformed_json", "invalid_structure"),
])
@pytest.mark.asyncio
async def test_transport_failure_modes_fall_back(monkeypatch, response_type, reason):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    def handle(request):
        if response_type == "timeout":
            raise httpx.ReadTimeout("provider timeout", request=request)
        if response_type == "http_error":
            return httpx.Response(503, text="Bearer secret-key")
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "invalid"},
            "finish_reason": "length" if response_type == "truncated" else "stop",
        }]})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))
    result = await PlannerAgent().run(MESSAGE, CONTEXT)
    assert result["planner_trace"]["source"] == "rules_fallback"
    assert result["planner_trace"]["fallback_reason"] == reason
    assert "secret-key" not in str(result)


@pytest.mark.asyncio
async def test_existing_orchestrator_consumes_plan_and_exposes_trace_without_new_scheduler():
    executed = []

    def response(payload):
        result = plan(payload, ("repo_health", "pr_review", "ci_debug"))
        result["tasks"][1]["dependencies"].append("ci_debug")
        return result

    async def runner(task, spec, context):
        if task.id == "pr_review":
            assert "ci_debug" in executed
        executed.append(task.id)
        return WorkflowTaskResult(task_id=task.id, agent_name=task.agent_name, task_type=task.task_type,
                                  status="success", summary="synthetic evidence", evidence_count=2, confidence=0.9)

    llm = FakeLLM(response)
    orchestrator = WorkflowOrchestrator(
        task_runners={task_id: runner for task_id in ("repo_health", "pr_review", "ci_debug")},
        planner=PlannerAgent(llm),
    )
    result = await orchestrator.run(MESSAGE, {**CONTEXT, "max_replans": 0})
    assert executed == ["repo_health", "ci_debug", "pr_review"]
    assert result["spec"]["planner_trace"]["source"] == "llm_with_rules"
    assert result["agent_events"][0]["planner_trace"] == result["spec"]["planner_trace"]
    assert len(llm.calls) == 1
    restored = WorkflowSpec.model_validate(result["spec"])
    await orchestrator.run_spec(restored, {**CONTEXT, "max_replans": 0})
    assert len(llm.calls) == 1  # Confirmed execution must not re-plan via the LLM.


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com/v1", "https://compatible.example/v1"])
@pytest.mark.asyncio
async def test_transport_uses_chat_model_and_bounds_generation(monkeypatch, base_url):
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings, "llm_base_url", base_url)
    chat = ChatAgent()
    llm = _PlannerLLMClient(20)
    assert llm.model == chat.model.model_name == settings.llm_model
    assert llm.base_url == settings.llm_base_url

    def handle(request):
        import json
        payload = json.loads(request.content)
        assert payload["model"] == chat.model.model_name
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 4096
        assert (payload.get("thinking") == {"type": "disabled"}) == ("api.deepseek.com" in base_url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))
    assert await llm.chat_json(PLANNER_AGENT_PROMPT, {}) == {}
    assert llm.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
