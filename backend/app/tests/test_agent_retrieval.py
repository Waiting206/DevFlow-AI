import asyncio
import json
import threading
import uuid

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes.chat import _build_conversation_tools, _conversation_response, _conversation_sse
from app.core.config import settings
from app.db.models import Base, ChatMessage, Conversation, Document, EvidenceItem, KnowledgeBaseConfig, Repository
from app.schemas.chat import ChatRequest
from app.services.agents.chat_agent import ChatAgent
from app.services.chat_memory import EvidenceStore
from app.services.rag.agent_retrieval import AgentKnowledgeRetriever
from app.services.rag.knowledge_base import retrieve_knowledge
from app.services.rag.vector_store import MilvusUnavailableError


@pytest.fixture
def scope(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    monkeypatch.setattr(settings, "embedding_model", "test-deterministic")
    monkeypatch.setattr(settings, "embedding_dimensions", 16)
    monkeypatch.setattr(settings, "milvus_enabled", False)
    monkeypatch.setattr(settings, "llm_api_key", None)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner="pilot", name="rag", full_name="pilot/rag")
        db.add(repo)
        db.flush()
        conversation = Conversation(repo_id=repo.id, title="Retrieval regression")
        config = KnowledgeBaseConfig(
            repo_id=repo.id, embedding_provider="deterministic", embedding_model="test-deterministic",
            embedding_dimensions=16, retrieval_method="fulltext", rerank_enabled=False,
            top_k=2, score_threshold_enabled=False,
        )
        db.add_all([conversation, config])
        db.commit()
        yield db, repo, conversation, config
    engine.dispose()


def document(db, repo, text="Refresh token retries must use an idempotency key."):
    row = Document(repo_id=repo.id, source_type="knowledge_file", source_id=uuid.uuid4(), title="Token retry policy", content=text, meta={"path": "auth/retry.md"})
    db.add(row)
    db.commit()
    return row


@pytest.mark.asyncio
async def test_chat_and_qa_share_configured_pipeline(scope):
    db, repo, conversation, config = scope
    row = document(db, repo)
    qa_items, qa_trace, _ = await retrieve_knowledge(db, repo.id, "token retries")
    store = EvidenceStore(db)
    chat_items = await store.search(repo.id, conversation.id, "token retries", limit=8)
    assert [item["id"] for item in qa_items] == [item["id"] for item in chat_items] == [str(row.id)]
    for key in ("retrieval_method", "rerank_enabled", "score_threshold_enabled", "top_k"):
        assert qa_trace[key] == store.retrieval_trace[key]
    assert store.retrieval_trace["top_k"] == config.top_k
    assert [stage["name"] for stage in qa_trace["pipeline"]["stages"]] == [stage["name"] for stage in store.retrieval_trace["pipeline"]["stages"]]


@pytest.mark.asyncio
async def test_slow_embedding_does_not_block_event_loop(scope, monkeypatch):
    db, repo, _conversation, config = scope
    config.retrieval_method = "hybrid"
    db.commit()
    released = threading.Event()

    def slow_embedding(*args, **kwargs):
        released.wait(timeout=0.5)
        return [0.0] * 16

    monkeypatch.setattr("app.services.rag.knowledge_base.embed_knowledge_query", slow_embedding)
    task = asyncio.create_task(AgentKnowledgeRetriever(db).search(repo.id, "token retries"))
    try:
        await asyncio.sleep(0.01)
        assert not task.done(), "Other requests must run while embedding is still waiting"
    finally:
        released.set()
        await task


@pytest.mark.asyncio
async def test_chat_obeys_configured_qwen_rerank_and_parent_expansion(scope, monkeypatch):
    db, repo, _conversation, config = scope
    row = document(db, repo)
    config.rerank_enabled = True
    config.rerank_provider = "qwen_api"
    config.score_threshold_enabled = True
    config.score_threshold = 0.8
    db.commit()
    calls = []

    async def rerank(query, items, active_config, limit):
        calls.append((query, active_config.rerank_provider, limit))
        return [{**item, "score": 0.95, "rerank_score": 0.95} for item in items]

    monkeypatch.setattr("app.services.rag.knowledge_base._qwen_rerank", rerank)
    result = await AgentKnowledgeRetriever(db).search(repo.id, "token retries")
    assert result.items[0]["id"] == str(row.id)
    assert calls == [("token retries", "qwen_api", 2)]
    assert "parent_expansion" in [stage["name"] for stage in result.trace["pipeline"]["stages"]]
    assert result.trace["answer_gate"]["decision"] == "answer"


@pytest.mark.asyncio
async def test_filtered_project_documents_cannot_bypass_gate_via_memory(scope):
    db, repo, conversation, config = scope
    document(db, repo)
    config.score_threshold_enabled = True
    config.score_threshold = 2.0
    other = Conversation(repo_id=repo.id, title="Other conversation")
    db.add(other)
    db.flush()
    for kind, conv_id, text in [
        ("knowledge_file", None, "token retries rejected document"),
        ("issue", None, "token retries rejected issue"),
        ("memory", conversation.id, "token retries historical choice"),
        ("memory", other.id, "token retries private other conversation"),
    ]:
        db.add(EvidenceItem(repo_id=repo.id, conversation_id=conv_id, source_type=kind, source_id=str(uuid.uuid4()), title=text, content=text))
    db.commit()
    store = EvidenceStore(db)
    items = await store.search(repo.id, conversation.id, "token retries")
    assert [item["source_type"] for item in items] == ["memory"]
    assert "historical choice" in items[0]["title"]
    assert items[0]["score"] is None  # Keyword match counts are not relevance probabilities.
    assert items[0]["retrieval"]["evidence_usage"] == "historical_context"
    assert store.retrieval_trace["answer_gate"]["decision"] == "insufficient_evidence"


@pytest.mark.asyncio
async def test_agent_gate_is_nonterminal_but_qa_stays_strict(scope, monkeypatch):
    db, repo, _conversation, _config = scope
    document(db, repo)
    monkeypatch.setattr("app.services.rag.knowledge_base.evaluate_answer_gate", lambda *args, **kwargs: {"decision": "conflict", "reason": "conflicting versions"})
    qa_items, qa_trace, _ = await retrieve_knowledge(db, repo.id, "token retries")
    result = await AgentKnowledgeRetriever(db).search(repo.id, "token retries")
    assert qa_items[0]["retrieval"]["selected_for_context"] is False
    assert qa_trace["gate_terminal"] is True
    assert result.trace["gate_terminal"] is False
    assert result.items[0]["retrieval"]["evidence_usage"] == "investigation_lead"


@pytest.mark.asyncio
async def test_request_cache_isolated_from_queries_mutation_and_new_requests(scope, monkeypatch):
    db, repo, _conversation, _config = scope
    row = document(db, repo)
    from app.services.rag import agent_retrieval
    original = agent_retrieval.retrieve_knowledge
    calls = []

    async def counted(*args, **kwargs):
        calls.append(args[2])
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent_retrieval, "retrieve_knowledge", counted)
    retriever = AgentKnowledgeRetriever(db)
    first = await retriever.search(repo.id, "token retries")
    first.items[0]["snippet"] = "mutated"
    second = await retriever.search(repo.id, "token retries")
    assert second.items[0]["id"] == str(row.id)
    assert second.items[0]["snippet"] != "mutated"
    assert second.trace["cache_hit"] is True
    await retriever.search(repo.id, "idempotency key")
    await AgentKnowledgeRetriever(db).search(repo.id, "token retries")
    assert calls == ["token retries", "idempotency key", "token retries"]


@pytest.mark.asyncio
async def test_agent_config_creation_does_not_commit_callers_transaction(scope):
    db, repo, conversation, config = scope
    db.delete(config)
    db.commit()
    pending = ChatMessage(repo_id=repo.id, conversation_id=conversation.id, role="user", content="uncommitted")
    db.add(pending)
    db.flush()
    message_id = pending.id
    await AgentKnowledgeRetriever(db).search(repo.id, "token retries")
    db.rollback()
    assert db.get(ChatMessage, message_id) is None
    assert db.query(KnowledgeBaseConfig).filter_by(repo_id=repo.id).count() == 0


@pytest.mark.asyncio
async def test_source_metadata_and_repository_filters_survive_chat_adapter(scope):
    db, repo, _conversation, _config = scope
    wanted = document(db, repo)
    other = Repository(owner="pilot", name="other", full_name="pilot/other")
    db.add(other)
    db.flush()
    document(db, other)
    retriever = AgentKnowledgeRetriever(db)
    matched = await retriever.search(repo.id, "token", source_types=["knowledge_file"], metadata_filters={"path": "auth/"})
    excluded = await retriever.search(repo.id, "token", source_types=["issue"])
    wrong_path = await retriever.search(repo.id, "token", metadata_filters={"path": "billing/"})
    assert [item["id"] for item in matched.items] == [str(wanted.id)]
    assert not excluded.items
    assert not wrong_path.items


@pytest.mark.asyncio
async def test_eval_records_threshold_failure_without_resurrecting_documents(scope):
    from app.services.evals.ragas_runner import RagEvalCase, run_rag_quality_eval

    db, repo, _conversation, config = scope
    row = document(db, repo)
    config.score_threshold_enabled = True
    config.score_threshold = 2.0
    db.commit()

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="This answer has no supporting evidence.")

    result = await run_rag_quality_eval(
        db, str(repo.id),
        cases=[RagEvalCase(id="threshold", question="token retries", reference="Use idempotency.", expected_source_ids=[str(row.id)])],
        agent_factory=lambda: ChatAgent(model=Model()),
        run_judge=False,
    )
    case = result["cases"][0]
    assert result["passed"] is False
    assert case["hard_rules"]["source_hit"] is False
    assert case["retrieved_contexts"] == []
    assert case["retrieval_trace"][0]["retrieval_trace"]["answer_gate"]["decision"] == "insufficient_evidence"
    assert db.query(Conversation).filter_by(status="evaluation").count() == 0


@pytest.mark.asyncio
async def test_rag_failure_keeps_scoped_memory_and_records_unavailability(scope, monkeypatch):
    db, repo, conversation, _config = scope
    db.add(EvidenceItem(repo_id=repo.id, conversation_id=conversation.id, source_type="memory", source_id="memory", title="token history", content="token retries"))
    db.commit()

    async def unavailable(*args, **kwargs):
        raise MilvusUnavailableError("test unavailable")

    monkeypatch.setattr("app.services.rag.agent_retrieval.retrieve_knowledge", unavailable)
    store = EvidenceStore(db)
    items = await store.search(repo.id, conversation.id, "token")
    assert items[0]["source_type"] == "memory"
    assert store.retrieval_trace["status"] == "unavailable"
    assert store.retrieval_trace["error_type"] == "MilvusUnavailableError"


@pytest.mark.asyncio
async def test_mcp_adapter_preserves_structured_retrieval_trace(scope, monkeypatch):
    db, repo, conversation, _config = scope
    trace = {"policy": "agent", "answer_gate": {"decision": "insufficient_evidence"}}

    async def call_tool(_self, name, arguments):
        assert arguments["repo_id"] == str(repo.id)
        return {"content": [{"type": "text", "text": "No evidence; inspect code."}], "structuredContent": {"retrieval_trace": trace, "retrieval_results": [], "answer_gate": trace["answer_gate"], "citations": []}}

    monkeypatch.setattr("app.api.routes.chat.StdioMcpClient.call_tool", call_tool)
    tools = _build_conversation_tools(repo, conversation, db)
    result = await tools["devflow_search_evidence"]({"query": "token"}, {})
    assert result["retrieval_trace"] == trace
    assert result["retrieval_results"] == []


@pytest.mark.asyncio
async def test_empty_retrieval_can_continue_to_workspace_and_is_evaluated(scope):
    _db, _repo, _conversation, _config = scope
    trace = {"policy": "agent", "answer_gate": {"decision": "insufficient_evidence"}, "gate_terminal": False}

    class Model:
        calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[{"name": "rag__search_similar_documents", "args": {"query": "token"}, "id": "rag"}])
            if self.calls == 2:
                return AIMessage(content="", tool_calls=[{"name": "workspace__read_file", "args": {"path": "auth.py"}, "id": "code"}])
            return AIMessage(content="auth.py confirms expired tokens return 401.")

    async def rag(_input, _context):
        return {"route": "semantic_search", "answer": "本次检索证据不足，请读取代码。", "retrieval_trace": trace, "answer_gate": trace["answer_gate"], "retrieval_results": [], "citations": []}

    async def read(_input, _context):
        return {"route": "workspace_read", "answer": "auth.py: expired tokens return 401", "citations": [{"type": "workspace_file", "id": "auth.py", "title": "auth.py"}]}

    agent = ChatAgent()
    agent.model = Model()
    result = await agent.run({"message": "Explain token expiry."}, {"tools": {"rag.search_similar_documents": rag, "workspace.read_file": read}, "evaluation_mode": True})
    assert "401" in result["answer"]
    assert len(result["tool_calls"]) == 2
    assert result["evaluation_trace"]["retrievals"][0]["retrieval_trace"] == trace


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_persists_history_and_reuses_preflight_retrieval(scope, monkeypatch, stream):
    db, repo, conversation, _config = scope
    document(db, repo)
    from app.services.rag import agent_retrieval
    original = agent_retrieval.retrieve_knowledge
    calls = []

    async def counted(*args, **kwargs):
        calls.append(args[2])
        return await original(*args, **kwargs)

    monkeypatch.setattr(agent_retrieval, "retrieve_knowledge", counted)
    payload = ChatRequest(repo_id=repo.id, conversation_id=conversation.id, message="token retries")
    if stream:
        events = [event async for event in _conversation_sse(payload, db)]
        final = [json.loads(event.split("data: ", 1)[1]) for event in events if event.startswith("event: final")][-1]
    else:
        final = await _conversation_response(payload, db)
    assert final["compression_stats"]["retrieval"]["cache_hit"] is True
    assert final["compression_stats"]["retrieval"]["retrieval_method"] == "fulltext"
    assert calls == ["token retries"]
    assert db.query(ChatMessage).filter_by(conversation_id=conversation.id).count() == 2
