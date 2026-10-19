import asyncio
import logging

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

logger = logging.getLogger(__name__)


async def periodic_repo_sync_loop() -> None:
    await asyncio.sleep(5)
    while settings.auto_sync_enabled:
        db = SessionLocal()
        try:
            repos = db.query(Repository).filter(Repository.provider.in_(["github", "github_compatible"])).all()
            repo_ids = [repo.id for repo in repos]
        finally:
            db.close()

        for repo_id in repo_ids:
            try:
                # 后台自行构造参数：三个同步开关使用默认 True，数量读取自动同步配置。
                await sync_repository_by_id(repo_id, RepoSyncRequest(limit=settings.auto_sync_limit))
            except Exception as exc:
                logger.warning("Periodic repo sync failed for %s: %s", repo_id, exc)

        # 间隔从本轮所有仓库处理结束后开始计算，并非严格按固定时刻执行。
        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))
