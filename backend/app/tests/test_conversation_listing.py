import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db.models import Base, ChatMessage, Conversation, Repository
from app.services.conversations import backfill_all_default_conversations, list_conversations


def test_listing_existing_conversations_does_not_write():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        repo = Repository(owner="pilot", name="demo", full_name="pilot/demo")
        db.add(repo)
        db.flush()
        conversation = Conversation(repo_id=repo.id, message_count=1)
        db.add(conversation)
        db.flush()
        db.add(ChatMessage(repo_id=repo.id, conversation_id=conversation.id, role="user", content="历史消息"))
        db.commit()
        repo_id, conversation_id = repo.id, conversation.id

        statements = []

        @event.listens_for(engine, "before_cursor_execute")
        def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().split()[0].upper())

        # A list read must not acquire row locks while a streaming reply is open.
        for _ in range(2):
            conversations = list_conversations(db, repo_id)
            db.commit()
            assert [item.id for item in conversations] == [conversation_id]
            assert conversations[0].message_count == 1

        assert set(statements) == {"SELECT"}


def test_startup_still_backfills_legacy_messages():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        repo = Repository(owner="pilot", name="legacy", full_name="pilot/legacy")
        db.add(repo)
        db.flush()
        message = ChatMessage(repo_id=repo.id, role="user", content="迁移前的消息")
        db.add(message)
        db.commit()

        backfill_all_default_conversations(db)

        conversations = list_conversations(db, repo.id)
        assert len(conversations) == 1
        assert message.conversation_id == conversations[0].id
        assert conversations[0].message_count == 1
