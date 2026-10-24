from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun
from app.db.session import Base, SessionLocal, engine
from app.services.rag.indexing import index_repository_documents


DEMO_OWNER = "repopilot-demo"
DEMO_NAME = "sample-api"
DEMO_FULL_NAME = f"{DEMO_OWNER}/{DEMO_NAME}"
DEMO_DESCRIPTION = "Synthetic repository for local RepoPilot AI development and workflow validation."
DEMO_GITHUB_ID = 10001
# Compatibility key for previously seeded local data, never a current project label.
LEGACY_DEMO_FULL_NAME = "course-demo/devflow-sample-api"


def ensure_demo_repository(db: Session) -> Repository:
    repo = db.query(Repository).filter(Repository.full_name == DEMO_FULL_NAME).one_or_none()
    if repo is not None:
        return repo
    repo = db.query(Repository).filter(
        Repository.full_name == LEGACY_DEMO_FULL_NAME,
        Repository.github_id == DEMO_GITHUB_ID,
    ).one_or_none()
    if repo is None:
        repo = Repository(owner=DEMO_OWNER, name=DEMO_NAME, full_name=DEMO_FULL_NAME,
                          description=DEMO_DESCRIPTION, default_branch="main", github_id=DEMO_GITHUB_ID)
        db.add(repo)
    else:
        # Rename the known synthetic fixture in place so foreign keys and chat history survive.
        repo.owner, repo.name, repo.full_name = DEMO_OWNER, DEMO_NAME, DEMO_FULL_NAME
        if not repo.description or repo.description == "Demo repository for RepoPilot AI course":
            repo.description = DEMO_DESCRIPTION
        old_prefix = f"https://github.com/{LEGACY_DEMO_FULL_NAME}/"
        for run in db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).all():
            if run.html_url and run.html_url.startswith(old_prefix):
                run.html_url = f"https://github.com/{DEMO_FULL_NAME}/" + run.html_url[len(old_prefix):]
    db.flush()
    return repo


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        repo = ensure_demo_repository(db)
        db.commit()
        db.refresh(repo)

        if db.query(Issue).filter(Issue.repo_id == repo.id).count() == 0:
            issues = [
                Issue(repo_id=repo.id, github_issue_id=101, number=1, title="Login API returns 500 when token expires", body="JWT expires and backend returns 500 instead of 401.", state="open", labels=["bug", "auth"], author="alice", assignees=[], created_at=datetime.now(timezone.utc)),
                Issue(repo_id=repo.id, github_issue_id=102, number=2, title="Add GitHub workflow sync", body="Sync recent workflow runs and expose failed CI runs in dashboard.", state="open", labels=["feature", "ci"], author="bob", assignees=["backend"], created_at=datetime.now(timezone.utc)),
                Issue(repo_id=repo.id, github_issue_id=103, number=3, title="Document local demo mode", body="README should explain how to run without GitHub token.", state="closed", labels=["documentation"], author="carol", assignees=[], created_at=datetime.now(timezone.utc)),
            ]
            db.add_all(issues)
            db.commit()

        if db.query(PullRequest).filter(PullRequest.repo_id == repo.id).count() == 0:
            pr = PullRequest(repo_id=repo.id, github_pr_id=201, number=8, title="Refactor GitHub sync service", body="Split GitHub API calls into issue, PR and actions modules.", state="open", author="dave", base_branch="main", head_branch="refactor/github-sync", created_at=datetime.now(timezone.utc))
            db.add(pr)
            db.commit()
            db.refresh(pr)
            db.add_all([
                PRFile(pr_id=pr.id, filename="backend/app/services/github/client.py", status="modified", additions=42, deletions=8, patch="+ async def get_repo(...)\n+ response.raise_for_status()"),
                PRFile(pr_id=pr.id, filename="backend/app/api/routes/repos.py", status="modified", additions=55, deletions=12, patch="+ @router.post('/connect')\n+ async def connect_repo(...)")
            ])
            db.add_all([
                PRReviewComment(pr_id=pr.id, github_comment_id=401, body="Please verify token handling is not logged.", path="backend/app/api/routes/repos.py", line=24, author="reviewer-a", created_at=datetime.now(timezone.utc)),
                PRReviewComment(pr_id=pr.id, github_comment_id=402, body="Consider splitting GitHub sync into smaller retriable steps.", path="backend/app/services/repos_sync.py", line=57, author="reviewer-b", created_at=datetime.now(timezone.utc)),
            ])

        if db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).count() == 0:
            db.add_all([
                WorkflowRun(
                    repo_id=repo.id,
                    github_run_id=301,
                    name="backend-test",
                    status="completed",
                    conclusion="failure",
                    html_url=f"https://github.com/{DEMO_FULL_NAME}/actions/runs/301",
                    jobs=[
                        {
                            "id": 9001,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "steps": [{"name": "Run backend tests", "status": "completed", "conclusion": "failure", "number": 4}],
                        }
                    ],
                    logs_text="pytest backend/app/tests/test_github_sync.py::test_sync_issues FAILED AssertionError: expected 3 issues, got 2",
                    created_at=datetime.now(timezone.utc),
                ),
                WorkflowRun(repo_id=repo.id, github_run_id=302, name="frontend-lint", status="completed", conclusion="success", html_url=f"https://github.com/{DEMO_FULL_NAME}/actions/runs/302", logs_text="", created_at=datetime.now(timezone.utc))
            ])

        db.flush()
        index_repository_documents(db, repo.id)
        db.commit()
        print(f"Seeded demo data for repo {repo.full_name} ({repo.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
