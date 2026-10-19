from app.services.github.client import GitHubClient
#负责从 GitHub 获取指定仓库的 Issue 列表，并过滤掉其中的 PR，最后返回给同步代码处理。

async def list_issues(owner: str, repo: str, token: str | None = None, limit: int = 30, base_url: str | None = None) -> list[dict]:
    client = GitHubClient(token, base_url)
    items = await client.get(f"/repos/{owner}/{repo}/issues", {"state": "all", "per_page": limit})
    # GitHub 的 Issues 接口也返回 PR；只保留没有 pull_request 字段的普通 Issue。
    return [item for item in items if "pull_request" not in item]
