from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
#程序查询、添加和修改数据库记录时使用的工作窗口 准备数据库连接，并给其他代码提供一个可以查询、添加、修改数据的 Session，使用完再关闭它。
"""
Base：让程序能描述类和数据库表之间的对应关系。
engine：管理与数据库的连接。
Session：通过这些对应关系，实际查询、添加和修改记录。
"""
class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
