import asyncio

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.models import ActionDraft, AgentRun, Base, ChatMessage, ChatSession, Conversation, Repository
from app.services.chat_memory import SessionSealer
from app.services.conversations import touch_conversation


@pytest.fixture
def turn(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sealing.db'}", connect_args={"timeout": 0.05})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        repo = Repository(owner="test", name="chat", full_name="test/chat")
        db.add(repo)
        db.flush()
        conversation = Conversation(repo_id=repo.id, title="chat", message_count=0)
        db.add(conversation)
        db.flush()
        user = ChatMessage(repo_id=repo.id, conversation_id=conversation.id, role="user", content="create a draft")
        assistant = ChatMessage(repo_id=repo.id, conversation_id=conversation.id, role="assistant", content="draft is ready")
        draft = ActionDraft(repo_id=repo.id, draft_type="create_issue", target_type="repository", content="draft body")
        db.add_all([user, assistant, draft])
        db.flush()
        run = AgentRun(repo_id=repo.id, conversation_id=conversation.id, user_message=user.content, route="safety_draft", final_answer=assistant.content, status="success")
        db.add(run)
        touch_conversation(db, conversation, increment=2)
        yield db, factory, repo, conversation, user, assistant
    engine.dispose()


async def seal(turn, llm):
    db, _, repo, conversation, user, assistant = turn
    return await SessionSealer(db, llm=llm).seal(
        repo=repo, conversation=conversation, user_message=user,
        assistant_message=assistant, route="safety_draft", tool_calls=[],
    )


@pytest.mark.asyncio
async def test_completed_turn_is_committed_and_row_lock_released_before_memory_llm(turn):
    _, factory, _, conversation, _, _ = turn
    conversation_id = conversation.id

    class MemoryLLM:
        async def chat_json(self, *_args):
            with factory() as other:
                assert other.query(ChatMessage).count() == 2
                assert other.query(AgentRun).one().status == "success"
                assert other.query(ActionDraft).one().content == "draft body"
                raw = other.query(ChatSession).one()
                assert raw.status == "sealing"
                assert raw.transcript_json["sealing"]["phase"] == "raw_persisted"
                # The previous implementation timed out here with database locked.
                other.execute(update(Conversation).where(Conversation.id == conversation_id).values(title="another request"))
                other.commit()
            await asyncio.sleep(0)
            return {"summary": "draft ready", "digest": "draft ready"}

    saved = await seal(turn, MemoryLLM())
    assert saved.status == "sealed"


@pytest.mark.asyncio
async def test_memory_deadline_uses_fallback_and_keeps_the_completed_answer(turn, monkeypatch):
    monkeypatch.setattr(settings, "session_memory_timeout_seconds", 0.03)

    class SlowMemory:
        async def chat_json(self, *_args):
            await asyncio.sleep(60)

    saved = await asyncio.wait_for(seal(turn, SlowMemory()), timeout=1)
    assert saved.status == "sealed_with_warnings"
    assert "TimeoutError: exceeded 0.03s" in saved.transcript_json["sealing"]["errors"][0]
    assert saved.digest
    with turn[1]() as other:
        assert other.query(ChatMessage).filter_by(role="assistant").one().content == "draft is ready"
        assert other.query(ActionDraft).count() == 1


@pytest.mark.asyncio
async def test_cancel_during_memory_does_not_roll_back_answer_or_draft(turn):
    entered = asyncio.Event()

    class SlowMemory:
        async def chat_json(self, *_args):
            entered.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(seal(turn, SlowMemory()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    turn[0].rollback()
    with turn[1]() as other:
        assert other.query(ChatMessage).count() == 2
        assert other.query(ActionDraft).count() == 1
        assert other.query(ChatSession).one().digest
