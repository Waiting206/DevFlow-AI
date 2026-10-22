from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from app.services.context_compression import prompt_cache_model_kwargs
from app.services.llm.client import LLMClient
from app.services.llm.prompts import PLANNER_AGENT_PROMPT, PLANNER_PROMPT_VERSION

from app.schemas.workflow import WorkflowClaim, WorkflowObservation, WorkflowSpec, WorkflowTask, WorkflowTaskResult
from app.services.agents.base import BaseAgent


logger = logging.getLogger(__name__)


class _PlanRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _PlannerLLMClient(LLMClient):
    """Planner-only bounded JSON transport; shared ChatAgent/client defaults stay intact."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.usage: dict[str, int] = {}

    async def chat_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.usage = {}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 4096,
            **prompt_cache_model_kwargs(),
        }
        # Official DeepSeek thinking defaults can exceed a planning latency budget.
        # Provider-specific flags never go to other OpenAI-compatible endpoints.
        if urlparse(self.base_url).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"}, json=payload,
            )
            response.raise_for_status()
            data = response.json()
        raw_usage = data.get("usage") or {}
        self.usage = {key: raw_usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                      if type(raw_usage.get(key)) is int and raw_usage[key] >= 0}
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise _PlanRejected("truncated_response")
        return json.loads(choice["message"]["content"])


class PlannerAgent(BaseAgent):
    name = "planner_agent"
    description = "根据用户目标和可用仓库信号，构建有边界的工程工作流规格。"
    available_tools = ["create_workflow_spec"]

    def __init__(self, llm: Any = None, *, use_llm: bool = True, timeout_seconds: float = 20.0) -> None:
        self.llm = llm if llm is not None else _PlannerLLMClient(timeout_seconds)
        self.use_llm = use_llm
        self.timeout_seconds = timeout_seconds

    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        baseline = self._rule_spec(input_data, context)
        trace: dict[str, Any] = {
            "source": "rules_fallback" if self.use_llm else "rules",
            "model": self.llm.model,
            "prompt_version": PLANNER_PROMPT_VERSION,
            "merged_rule_tasks": [],
        }
        spec = baseline
        try:
            if not self.use_llm:
                trace["fallback_reason"] = "disabled"
            elif not getattr(self.llm, "api_key", True):
                trace["fallback_reason"] = "missing_api_key"
            else:
                catalog = self._task_catalog(context)
                # Preserve the existing RAG trigger boundary instead of adding
                # expensive retrieval to every broad engineering review.
                if not context.get("enable_rag_task") and not self._mentions_history(baseline.trigger_message.lower()):
                    catalog.pop("rag_search", None)
                required = self._required_tasks(baseline, catalog, context)
                # Only identifiers/availability go to the planner; no logs, diffs,
                # credentials, team profiles, memory, or arbitrary client context.
                payload = {
                    "goal": baseline.goal,
                    "trigger_message": baseline.trigger_message,
                    "available_tasks": [task.model_dump(mode="json") for task in catalog.values()],
                    "required_task_ids": sorted(required),
                    "acceptance_criteria": baseline.acceptance_criteria,
                    "max_parallel_tasks": baseline.max_parallel_tasks,
                    "requires_human_confirmation": baseline.requires_human_confirmation,
                }
                candidate = await asyncio.wait_for(
                    self.llm.chat_json(PLANNER_AGENT_PROMPT, payload), timeout=self.timeout_seconds
                )
                spec, merged = self._merge_plan(candidate, baseline, catalog, required)
                trace.update(source="llm_with_rules", merged_rule_tasks=merged)
        except (TimeoutError, httpx.TimeoutException):
            trace["fallback_reason"] = "timeout"
        except _PlanRejected as exc:
            trace["fallback_reason"] = exc.code
        except (ValidationError, TypeError, ValueError):
            trace["fallback_reason"] = "invalid_structure"
        except Exception as exc:
            # Do not persist HTTP bodies/URLs or provider exception messages.
            trace.update(fallback_reason="llm_error", error_type=type(exc).__name__)
            logger.warning("Planner fell back to rules (%s)", type(exc).__name__)
        trace["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        trace["task_count"] = len(spec.tasks)
        usage = getattr(self.llm, "usage", None)
        if usage and self.use_llm:
            trace["usage"] = dict(usage)
        spec.planner_trace = trace
        return spec.model_dump(mode="json")

    def _task_catalog(self, context: dict[str, Any]) -> dict[str, WorkflowTask]:
        # Reuse the original rules to define every executable task and claim.
        all_tasks = self._rule_spec({"message": "multi-agent history"}, context).tasks
        return {task.id: task for task in all_tasks}

    def _required_tasks(
        self, baseline: WorkflowSpec, catalog: dict[str, WorkflowTask], context: dict[str, Any]
    ) -> set[str]:
        required = {"repo_health", "safety_review"} & catalog.keys()
        for flag, task_id in (
            ("issue_explicit", "issue_analysis"),
            ("pr_explicit", "pr_review"),
            ("workflow_run_explicit", "ci_debug"),
            ("enable_rag_task", "rag_search"),
        ):
            if context.get(flag) and task_id in catalog:
                required.add(task_id)
        if self._mentions_merge(baseline.trigger_message.lower()) and "ci_debug" in catalog:
            required.add("ci_debug")
        return required

    def _merge_plan(
        self, candidate: Any, baseline: WorkflowSpec, catalog: dict[str, WorkflowTask], required: set[str]
    ) -> tuple[WorkflowSpec, list[str]]:
        if not isinstance(candidate, dict) or not candidate:
            raise _PlanRejected("empty_response")
        planned = WorkflowSpec.model_validate(candidate)
        if not planned.tasks or len(planned.tasks) > len(catalog):
            raise _PlanRejected("invalid_tasks")
        tasks: dict[str, WorkflowTask] = {}
        for task in planned.tasks:
            canonical = catalog.get(task.id)
            if canonical is None or task.task_type != task.id or task.agent_name != canonical.agent_name:
                raise _PlanRejected("unavailable_task")
            if task.id in tasks:
                raise _PlanRejected("duplicate_task")
            if (
                task.claim.target_type != canonical.claim.target_type
                or task.claim.target_id != canonical.claim.target_id
                or not set(task.claim.allowed_sources).issubset(canonical.claim.allowed_sources)
                or (task.claim.write_intent and not canonical.claim.write_intent)
            ):
                raise _PlanRejected("scope_violation")
            if not task.objective.strip() or len(task.objective) > 1200:
                raise _PlanRejected("invalid_objective")
            # Model controls intent and scheduling; all executable scope is server-owned.
            tasks[task.id] = canonical.model_copy(deep=True, update={
                "objective": task.objective.strip(),
                "dependencies": list(dict.fromkeys(task.dependencies)),
                "critical": canonical.critical or task.critical or any(
                    item.id == task.id and item.critical for item in baseline.tasks
                ),
            })
        merged = sorted(required - tasks.keys())
        for task_id in merged:
            tasks[task_id] = catalog[task_id].model_copy(deep=True)
        if "repo_health" in tasks:
            if tasks["repo_health"].dependencies:
                raise _PlanRejected("invalid_dependencies")
            for task in tasks.values():
                if task.id not in {"repo_health", "safety_review"} and "repo_health" not in task.dependencies:
                    task.dependencies.insert(0, "repo_health")
        self._validate_graph(tasks)
        if len(planned.acceptance_criteria) > 12 or any(
            not item.strip() or len(item) > 600 for item in planned.acceptance_criteria
        ):
            raise _PlanRejected("invalid_acceptance_criteria")
        spec = baseline.model_copy(deep=True)
        spec.tasks = list(tasks.values())
        spec.acceptance_criteria = list(dict.fromkeys([
            *baseline.acceptance_criteria, *planned.acceptance_criteria,
        ]))
        # Keep original goal/IDs/time/concurrency/confirmation; model cannot disable gates.
        return spec, merged

    @staticmethod
    def _validate_graph(tasks: dict[str, WorkflowTask]) -> None:
        remaining = set(tasks)
        completed: set[str] = set()
        if any(set(task.dependencies) - tasks.keys() for task in tasks.values()):
            raise _PlanRejected("invalid_dependencies")
        while remaining:
            ready = {task_id for task_id in remaining if set(tasks[task_id].dependencies) <= completed}
            if not ready:
                raise _PlanRejected("cyclic_dependencies")
            remaining -= ready
            completed |= ready

    def _rule_spec(self, input_data: dict[str, Any], context: dict[str, Any]) -> WorkflowSpec:
        message = str(input_data.get("message") or input_data.get("goal") or "").strip()
        lowered = message.lower()
        tasks: list[WorkflowTask] = []
        issue_label = f"Issue #{context['issue_number']}" if context.get("issue_number") is not None else "当前 Issue"
        pr_label = f"PR #{context['pr_number']}" if context.get("pr_number") is not None else "当前 PR"
        ci_label = (
            f"CI/Workflow Run #{context['workflow_run_number']}"
            if context.get("workflow_run_number") is not None
            else "最新失败的 CI 运行"
        )
        has_ci_run = context.get("has_ci_run", context.get("has_failed_ci"))

        if context.get("has_repo", True):
            tasks.append(
                WorkflowTask(
                    id="repo_health",
                    agent_name="repo_health_agent",
                    task_type="repo_health",
                    objective="在专用分析前捕获仓库基线。",
                    claim=WorkflowClaim(
                        target_type="repository",
                        target_id=context.get("repo_id"),
                        allowed_sources=["issues", "pull_requests", "workflow_runs"],
                        reason="共享仓库快照可以让后续 Agent 结论有证据支撑。",
                    ),
                )
            )

        broad = self._is_broad_engineering_question(lowered)
        if context.get("has_issue") and (broad or self._mentions_issue(lowered)):
            tasks.append(
                WorkflowTask(
                    id="issue_analysis",
                    agent_name="issue_analyst_agent",
                    task_type="issue_analysis",
                    objective=f"分析 {issue_label} 的分诊结论、优先级、负责人、证据和下一步动作。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="issue",
                        target_id=context.get("issue_id"),
                        allowed_sources=["issue", "similar_issues", "code", "documents", "team_members"],
                        reason="判断负责人和优先级需要明确 Issue 范围。",
                    ),
                    input_hint={"issue_number": context.get("issue_number")},
                )
            )

        if context.get("has_pr") and (broad or self._mentions_pr(lowered)):
            tasks.append(
                WorkflowTask(
                    id="pr_review",
                    agent_name="pr_review_agent",
                    task_type="pr_review",
                    objective=f"审查 {pr_label} diff、风险、阻塞问题和测试建议。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="pull_request",
                        target_id=context.get("pr_id"),
                        allowed_sources=["pull_request", "diff", "review_comments", "ci_summary", "documents"],
                        reason="合并就绪度和风险判断需要明确 PR 范围。",
                    ),
                    input_hint={"pr_number": context.get("pr_number")},
                    critical=self._mentions_merge(lowered),
                )
            )

        if has_ci_run and (broad or self._mentions_ci(lowered) or self._mentions_merge(lowered)):
            tasks.append(
                WorkflowTask(
                    id="ci_debug",
                    agent_name="ci_debug_agent",
                    task_type="ci_debug",
                    objective=f"分析 {ci_label}，并判断是否阻塞合并或发布。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="workflow_run",
                        target_id=context.get("workflow_run_id"),
                        allowed_sources=["workflow_run", "jobs", "logs", "recent_prs", "code"],
                        reason="任何合并或就绪度结论都需要先看 CI 失败上下文。",
                    ),
                    input_hint={"workflow_run_number": context.get("workflow_run_number")},
                    critical=True,
                )
            )

        if context.get("enable_rag_task") or self._mentions_history(lowered):
            tasks.append(
                WorkflowTask(
                    id="rag_search",
                    agent_name="rag_context_agent",
                    task_type="rag_search",
                    objective="检索与目标相关的历史 Issue、PR、失败 CI 日志、项目文档和已批准知识。",
                    dependencies=["repo_health"] if self._has_task(tasks, "repo_health") else [],
                    claim=WorkflowClaim(
                        target_type="knowledge",
                        target_id=context.get("repo_id"),
                        allowed_sources=["rag_documents", "approved_memory", "uploaded_knowledge"],
                        reason="历史证据可以避免重复分析和遗漏回归风险。",
                    ),
                )
            )

        if context.get("requires_safety"):
            tasks.append(
                WorkflowTask(
                    id="safety_review",
                    agent_name="safety_agent",
                    task_type="safety_review",
                    objective="判断写入或外部发送风险，并生成安全草稿而不是执行副作用。",
                    claim=WorkflowClaim(
                        target_type="external_action",
                        target_id=context.get("repo_id"),
                        allowed_sources=["user_request", "repository_snapshot"],
                        write_intent=True,
                        reason="写入类操作必须经过人工确认。",
                    ),
                    critical=True,
                )
            )

        if len(tasks) == 1 and tasks[0].task_type == "repo_health":
            if context.get("has_pr"):
                tasks.append(self._fallback_pr_task(context))
            elif has_ci_run:
                tasks.append(self._fallback_ci_task(context))
            elif context.get("has_issue"):
                tasks.append(self._fallback_issue_task(context))

        spec = WorkflowSpec(
            goal=message or "运行一次工程协作工作流。",
            trigger_message=message,
            acceptance_criteria=[
                "所有被选中的 Agent 都必须停留在声明的 claim 范围内。",
                "每个专用结果都应包含证据、置信度，或在证据不足时说明明确原因。",
                "Observer 必须在最终综合前标出跨 Agent 冲突。",
                "最终综合必须保留 What、Why、Tradeoff、Open Questions、Next Action。",
                "PR 审查必须按 P1/P2/P3 分级；P1/P2 未解决前不能建议直接合入。",
                "外部写操作必须返回草稿，并要求人工确认。",
            ],
            tasks=tasks,
            requires_human_confirmation=bool(context.get("requires_safety")),
        )
        return spec

    async def replan(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        previous_spec = WorkflowSpec.model_validate(input_data["spec"])
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        observation = WorkflowObservation.model_validate(input_data["observation"])
        tasks = [task.model_copy(deep=True) for task in previous_spec.tasks]
        task_types = {task.task_type for task in tasks}
        successful_task_ids = {result.task_id for result in results if result.status == "success"}
        finding_types = {finding.finding_type for finding in observation.findings}

        if "missing_safety_review" in finding_types and "safety_review" not in task_types:
            tasks.append(
                WorkflowTask(
                    id=self._unique_task_id(tasks, "safety_review"),
                    agent_name="safety_agent",
                    task_type="safety_review",
                    objective="Review write or external-send risk before producing a final recommendation.",
                    dependencies=self._dependency_if_successful(successful_task_ids, "repo_health"),
                    claim=WorkflowClaim(
                        target_type="external_action",
                        target_id=context.get("repo_id"),
                        allowed_sources=["user_request", "repository_snapshot"],
                        write_intent=True,
                        reason="Replan added a missing safety gate after Observer flagged it.",
                    ),
                    critical=True,
                )
            )

        needs_context_recovery = bool(
            finding_types
            & {
                "task_error",
                "task_skipped",
                "evidence_gap",
                "low_confidence",
                "owner_needs_review",
                "team_profile_missing",
            }
        )
        if needs_context_recovery and "rag_search" not in task_types:
            tasks.append(
                WorkflowTask(
                    id=self._unique_task_id(tasks, "rag_search"),
                    agent_name="rag_context_agent",
                    task_type="rag_search",
                    objective="Recover missing evidence by searching historical issues, pull requests, failed CI logs, project documents, and approved knowledge.",
                    dependencies=self._dependency_if_successful(successful_task_ids, "repo_health"),
                    claim=WorkflowClaim(
                        target_type="knowledge",
                        target_id=context.get("repo_id"),
                        allowed_sources=["rag_documents", "approved_memory", "uploaded_knowledge"],
                        reason="Replan added historical context because Observer found weak evidence or confidence.",
                    ),
                )
            )

        if context.get("has_ci_run", context.get("has_failed_ci")) and "ci_debug" not in task_types and self._replan_needs_ci(previous_spec, results, observation):
            tasks.append(
                WorkflowTask(
                    id=self._unique_task_id(tasks, "ci_debug"),
                    agent_name="ci_debug_agent",
                    task_type="ci_debug",
                    objective="Check the latest failed CI run before final merge or release guidance.",
                    dependencies=self._dependency_if_successful(successful_task_ids, "repo_health"),
                    claim=WorkflowClaim(
                        target_type="workflow_run",
                        target_id=context.get("workflow_run_id"),
                        allowed_sources=["workflow_run", "jobs", "logs", "recent_prs", "code"],
                        reason="Replan added CI debugging because the current decision may depend on failed automation.",
                    ),
                    input_hint={"workflow_run_number": context.get("workflow_run_number")},
                    critical=True,
                )
            )

        replanned = previous_spec.model_copy(deep=True)
        replanned.tasks = tasks
        replanned.planner_trace = {**previous_spec.planner_trace, "replan_source": "rules"}
        return replanned.model_dump(mode="json")

    def _fallback_issue_task(self, context: dict[str, Any]) -> WorkflowTask:
        label = f"Issue #{context['issue_number']}" if context.get("issue_number") is not None else "最新 Issue"
        return WorkflowTask(
            id="issue_analysis",
            agent_name="issue_analyst_agent",
            task_type="issue_analysis",
            objective=f"分析 {label}，因为它是当前最强的可用工程信号。",
            dependencies=["repo_health"],
            claim=WorkflowClaim(target_type="issue", target_id=context.get("issue_id"), allowed_sources=["issue", "team_members"]),
            input_hint={"issue_number": context.get("issue_number")},
        )

    def _fallback_pr_task(self, context: dict[str, Any]) -> WorkflowTask:
        label = f"PR #{context['pr_number']}" if context.get("pr_number") is not None else "最新 PR"
        return WorkflowTask(
            id="pr_review",
            agent_name="pr_review_agent",
            task_type="pr_review",
            objective=f"审查 {label}，因为它是当前最强的可用工程信号。",
            dependencies=["repo_health"],
            claim=WorkflowClaim(target_type="pull_request", target_id=context.get("pr_id"), allowed_sources=["pull_request", "diff", "ci_summary"]),
            input_hint={"pr_number": context.get("pr_number")},
        )

    def _fallback_ci_task(self, context: dict[str, Any]) -> WorkflowTask:
        label = (
            f"CI/Workflow Run #{context['workflow_run_number']}"
            if context.get("workflow_run_number") is not None
            else "最新失败的 CI 运行"
        )
        return WorkflowTask(
            id="ci_debug",
            agent_name="ci_debug_agent",
            task_type="ci_debug",
            objective=f"分析 {label}，因为它是当前最强的可用工程信号。",
            dependencies=["repo_health"],
            claim=WorkflowClaim(target_type="workflow_run", target_id=context.get("workflow_run_id"), allowed_sources=["workflow_run", "jobs", "logs"]),
            input_hint={"workflow_run_number": context.get("workflow_run_number")},
            critical=True,
        )

    def _has_task(self, tasks: list[WorkflowTask], task_id: str) -> bool:
        return any(task.id == task_id for task in tasks)

    def _unique_task_id(self, tasks: list[WorkflowTask], base: str) -> str:
        existing = {task.id for task in tasks}
        if base not in existing:
            return base
        index = 2
        while f"{base}_{index}" in existing:
            index += 1
        return f"{base}_{index}"

    def _dependency_if_successful(self, successful_task_ids: set[str], task_id: str) -> list[str]:
        return [task_id] if task_id in successful_task_ids else []

    def _replan_needs_ci(
        self,
        spec: WorkflowSpec,
        results: list[WorkflowTaskResult],
        observation: WorkflowObservation,
    ) -> bool:
        text = " ".join(
            [
                spec.goal,
                spec.trigger_message,
                *(result.summary for result in results),
                *(finding.message for finding in observation.findings),
            ]
        ).lower()
        return self._mentions_ci(text) or self._mentions_merge(text)

    def _mentions_issue(self, text: str) -> bool:
        return any(token in text for token in ["issue", "bug", "需求", "问题", "负责人", "分配", "优先级"])

    def _mentions_pr(self, text: str) -> bool:
        return any(token in text for token in ["pr", "pull request", "diff", "review", "合并", "代码审查", "风险"])

    def _mentions_ci(self, text: str) -> bool:
        return any(token in text for token in ["ci", "workflow", "action", "build", "test", "失败", "流水线", "构建"])

    def _mentions_merge(self, text: str) -> bool:
        return any(token in text for token in ["merge", "合并", "能不能上", "能不能发", "readiness", "release"])

    def _mentions_history(self, text: str) -> bool:
        return any(token in text for token in ["history", "similar", "rag", "knowledge", "历史", "相似", "知识库", "记忆"])

    def _is_broad_engineering_question(self, text: str) -> bool:
        return any(
            token in text
            for token in [
                "multi-agent",
                "workflow",
                "协作",
                "工程状态",
                "阻塞",
                "风险",
                "优先处理",
                "下一步",
                "整体",
                "全局",
                "当前仓库",
                "能不能合并",
            ]
        )
