"""Request-scoped adapter over the shared RAG pipeline for Agent exploration."""

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.services.rag.knowledge_base import retrieve_knowledge
from app.services.rag.vector_store import MilvusUnavailableError


def investigation_hint(trace: dict[str, Any]) -> str:
    decision = (trace.get("answer_gate") or {}).get("decision")
    return {
        "answer": "候选证据可供核对；最终结论仍需结合源码、实时状态和其他工具结果。",
        "conflict": "候选证据存在冲突，请核对来源和版本，或读取源码与实时状态；不要直接选择其中一条作为结论。",
        "ask_clarification": "本次查询对象或范围不明确；先结合对话解析对象，必要时向用户澄清。",
    }.get(decision, "本次检索证据不足；可改写查询、读取工作区代码或查询 Issue/PR/CI。其他工具也无法补足时再说明无法确认。")


@dataclass
class AgentRetrievalResult:
    items: list[dict[str, Any]]
    trace: dict[str, Any]
    duration_ms: int = 0


class AgentKnowledgeRetriever:
    """Cache only project RAG within one request; never cache mutable chat memory."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._cache: dict[str, AgentRetrievalResult] = {}

    async def search(
        self,
        repo_id: uuid.UUID,
        query: str,
        *,
        top_k: int | None = None,
        source_types: list[str] | None = None,
        metadata_filters: dict[str, Any] | None = None,
    ) -> AgentRetrievalResult:
        key = json.dumps([str(repo_id), query, top_k, sorted(source_types or []), metadata_filters or {}], sort_keys=True)
        if key in self._cache:
            result = copy.deepcopy(self._cache[key])
            result.trace["cache_hit"] = True
            return result
        try:
            items, trace, duration = await retrieve_knowledge(
                self.db,
                repo_id,
                query,
                top_k=top_k,
                source_types=source_types,
                metadata_filters=metadata_filters,
                policy="agent",
                commit_config=False,
            )
            result = AgentRetrievalResult(items, {**trace, "cache_hit": False}, duration)
        except (MilvusUnavailableError, httpx.HTTPError, RuntimeError) as exc:
            # Preserve the Agent's other tools and scoped memory when RAG is
            # unavailable. Never reintroduce rejected documents as local matches.
            result = AgentRetrievalResult([], {
                "policy": "agent",
                "status": "unavailable",
                "error_type": type(exc).__name__,
                "cache_hit": False,
                "answer_gate": {"decision": "insufficient_evidence", "reason": "检索服务不可用", "evidence_count": 0},
                "gate_terminal": False,
            })
        self._cache[key] = copy.deepcopy(result)
        return result
