"""Repository checkout synchronization and code-graph analysis."""

import asyncio
import logging
import os
import shutil
import subprocess
import uuid
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.models import CodeRelation, CodeSymbol, Document, ProjectIndexState, Repository
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)
_CHECKOUT_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}
_CHECKOUT_STALE_AFTER = timedelta(minutes=15)

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".next",
    ".nuxt",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".java",
    ".go",
    ".rs",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".html",
    ".css",
    ".scss",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".md",
    ".mdx",
    ".txt",
    ".dockerfile",
}

TEXT_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "README",
    "LICENSE",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
}

PY_SYMBOL_RE = re.compile(r"^\s*(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
TS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
CALL_RE = re.compile(r"\b([A-Za-z_][$A-Za-z0-9_]*)\s*\(")
IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.\/-]+)\s+import|import\s+([\w.\/-]+)|import\s+.*?\s+from\s+['\"]([^'\"]+)['\"])", re.MULTILINE)


def _checkout_root() -> Path:
    root = Path(settings.repo_checkout_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repo_checkout_path(repo: Repository) -> Path:
    safe_name = repo.full_name.replace("/", "__").replace("\\", "__")
    return _checkout_root() / f"{repo.id}-{safe_name}"


def _stored_checkout_path(repo: Repository) -> Path | None:
    raw_path = getattr(repo, "local_path", None)
    if not raw_path:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(str(raw_path))))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _clone_url(repo: Repository) -> str:
    if repo.clone_url:
        return repo.clone_url
    return f"https://github.com/{repo.owner}/{repo.name}.git"


def _clone_url_with_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or "@" in parsed.netloc:
        return url
    safe_token = quote(token, safe="")
    return urlunparse(parsed._replace(netloc=f"x-access-token:{safe_token}@{parsed.netloc}"))


def redact_git_secrets(message: str) -> str:
    message = re.sub(r"(https?://)[^/\s]+@", r"\1[REDACTED]@", message)
    message = re.sub(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+\b", "[REDACTED]", message)
    return re.sub(r"(Authorization:\s*(?:Basic|Bearer)\s+)\S+", r"\1[REDACTED]", message, flags=re.IGNORECASE)


def git_error_message(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        message = exc.stderr or f"Git 操作失败（退出码 {exc.returncode}）"
    elif isinstance(exc, subprocess.TimeoutExpired):
        message = f"Git 操作超过 {exc.timeout} 秒未完成，请检查网络后重试。"
    else:
        message = str(exc)
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    return redact_git_secrets(message.strip())[:2000]


def _checkout_has_head(path: Path) -> bool:
    return path.is_dir() and _is_git_work_tree(path) and bool(_git_text(["rev-parse", "--verify", "HEAD"], path))


def repository_checkout_state(repo: Repository) -> dict:
    """Readiness is independent of Issue/PR sync and the document scan state."""
    path = _stored_checkout_path(repo) or _repo_checkout_path(repo)
    status = getattr(repo, "checkout_status", None) or "pending"
    error = getattr(repo, "checkout_error", None)
    updated = getattr(repo, "checkout_updated_at", None)
    if status == "downloading":
        at = updated.replace(tzinfo=timezone.utc) if updated and updated.tzinfo is None else updated
        if at and datetime.now(timezone.utc) - at > _CHECKOUT_STALE_AFTER:
            status, error = "failed", "上次源码准备未完成或已中断，请重试下载。"
    elif status != "failed":
        # Local paths have already been validated by the connect endpoint. This
        # also keeps existing local/document-only installations compatible.
        local_ready = getattr(repo, "checkout_mode", None) == "local" and path.is_dir()
        if local_ready or _checkout_has_head(path):
            status, error = "ready", None
        elif status == "ready":
            status, error = "failed", "本地源码不存在或检出不完整，请重新下载。"
        else:
            status = "pending"
    return {
        "repo_id": repo.id,
        "status": status,
        "error_message": redact_git_secrets(error) if error else None,
        "local_path": str(path) if path.is_dir() else None,
        "updated_at": updated,
        "commit_sha": _git_text(["rev-parse", "HEAD"], path) if path.is_dir() else None,
    }


def queue_repository_checkout(db: Session, repo: Repository) -> bool:
    active = _CHECKOUT_LOCKS.get(repo.id)
    if active is not None and active.locked():
        return False
    if repository_checkout_state(repo)["status"] == "downloading":
        return False
    index = db.query(ProjectIndexState).filter_by(repo_id=repo.id).one_or_none()
    if index is not None and index.status == "building":
        at = index.updated_at
        at = at.replace(tzinfo=timezone.utc) if at and at.tzinfo is None else at
        if not at or datetime.now(timezone.utc) - at <= _CHECKOUT_STALE_AFTER:
            return False
    repo.checkout_status = "downloading"
    repo.checkout_error = None
    repo.checkout_updated_at = datetime.now(timezone.utc)
    # Persist before returning the response or starting the background task.
    db.commit()
    return True


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise subprocess.CalledProcessError(
            exc.returncode,
            [redact_git_secrets(str(arg)) for arg in ["git", *args]],
            stderr=git_error_message(exc),
        ) from None
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(git_error_message(exc)) from None


def _is_git_work_tree(path: Path) -> bool:
    if not path.exists() or not path.is_dir() or not shutil.which("git"):
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(path),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
    )
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == path.resolve()


def _sync_checkout(repo: Repository, token: str | None) -> Path:
    stored_path = _stored_checkout_path(repo)
    checkout_mode = getattr(repo, "checkout_mode", None) or "managed"

    # 用户已有工作副本直接读取，不自动拉取或切换分支，以保留当前开发状态。
    if stored_path and checkout_mode == "local":
        if not _is_git_work_tree(stored_path):
            raise RuntimeError(f"已配置的本地仓库路径不是 git 工作树：{stored_path}")
        return stored_path

    target = stored_path or _repo_checkout_path(repo)
    clone_url = _clone_url_with_token(_clone_url(repo), token)
    branch = repo.default_branch or "main"

    if not shutil.which("git"):
        raise RuntimeError("未找到 git 可执行文件，无法克隆仓库代码")

    if (target / ".git").exists() or _is_git_work_tree(target):
        if not _is_git_work_tree(target):
            raise RuntimeError("后台源码目录不是独立 Git 工作树，请检查仓库路径后重试。")
        # A managed checkout is a read-only remote snapshot, not a developer
        # branch to merge. Keep local branches/commits recoverable and refuse
        # to overwrite modified or untracked files in a user-selected target.
        if _run_git(["status", "--porcelain", "--untracked-files=all"], cwd=target):
            raise RuntimeError("后台源码副本存在本地修改或未跟踪文件，已停止更新；请先备份或使用本地仓库模式。")
        remote_ref = f"refs/remotes/origin/{branch}"
        _run_git(["fetch", "--depth", "1", "origin", f"+refs/heads/{branch}:{remote_ref}"], cwd=target)
        commit_sha = _run_git(["rev-parse", "--verify", f"{remote_ref}^{{commit}}"], cwd=target)
        _run_git(["checkout", "--detach", "--no-overwrite-ignore", commit_sha], cwd=target)
        if _git_text(["rev-parse", "HEAD"], target) != commit_sha:
            raise RuntimeError("源码版本与本次远端提交不一致，请重试同步。")
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if stored_path and any(target.iterdir()):
            raise RuntimeError(f"Configured download target already exists and is not an empty git work tree: {target}")
        shutil.rmtree(target)
    clone_args = ["clone", "--depth", "1"]
    if branch:
        clone_args.extend(["--branch", branch])
    clone_args.extend([clone_url, str(target)])
    _run_git(clone_args)
    return target


def _is_text_candidate(path: Path) -> bool:
    if path.name in TEXT_FILENAMES:
        return True
    suffix = path.suffix.lower()
    return suffix in TEXT_EXTENSIONS or path.name.lower().endswith("dockerfile")


def _read_text(path: Path, max_bytes: int = 300_000) -> str | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return None
    if not data or len(data) > max_bytes or b"\x00" in data[:4096]:
        return None
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "latin-1"]:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _language_for(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or path.name


def _git_text(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _symbol_matches(path: Path, text: str) -> list[dict[str, object]]:
    language = _language_for(path)
    matches = []
    if path.suffix.lower() == ".py":
        for match in PY_SYMBOL_RE.finditer(text):
            matches.append({"name": match.group(2), "kind": match.group(1), "language": language, "start_line": _line_for_offset(text, match.start())})
    elif path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        for match in TS_SYMBOL_RE.finditer(text):
            declaration = match.group(0)
            kind = "class" if "class " in declaration else "interface" if "interface " in declaration else "type" if "type " in declaration else "function"
            matches.append({"name": match.group(1), "kind": kind, "language": language, "start_line": _line_for_offset(text, match.start())})
    return matches


# 扫描 checkout 中的文件，只将代码符号和关系存入数据库；源码保留在本地目录。
def _sync_code_graph(
    db: Session,
    repo: Repository,
    checkout_path: Path,
    *,
    branch: str | None,
    commit_sha: str | None,
    pr_id: uuid.UUID | None = None,
    pr_number: int | None = None,
    max_files: int = 500,
) -> int:
    db.query(CodeRelation).filter(CodeRelation.repo_id == repo.id, CodeRelation.branch == branch, CodeRelation.pr_id == pr_id, CodeRelation.pr_number == pr_number).delete(synchronize_session=False)
    db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo.id, CodeSymbol.branch == branch, CodeSymbol.pr_id == pr_id, CodeSymbol.pr_number == pr_number).delete(synchronize_session=False)
    symbol_rows: list[CodeSymbol] = []
    files_seen = 0
    for path in checkout_path.rglob("*"):
        if files_seen >= max_files:
            break
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(checkout_path).parts):
            continue
        if not _is_text_candidate(path):
            continue
        text = _read_text(path, max_bytes=180_000)
        if not text:
            continue
        symbols = _symbol_matches(path, text)
        if not symbols:
            continue
        relative_path = path.relative_to(checkout_path).as_posix()
        files_seen += 1
        for item in symbols:
            row = CodeSymbol(
                repo_id=repo.id,
                path=relative_path,
                name=str(item["name"]),
                kind=str(item["kind"]),
                language=str(item["language"]),
                start_line=int(item["start_line"]),
                end_line=None,
                branch=branch,
                commit_sha=commit_sha,
                pr_id=pr_id,
                pr_number=pr_number,
                meta={},
            )
            db.add(row)
            symbol_rows.append(row)
    db.flush()
    by_name = {row.name: row for row in symbol_rows}
    for path in checkout_path.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(checkout_path).parts) or not _is_text_candidate(path):
            continue
        text = _read_text(path, max_bytes=180_000)
        if not text:
            continue
        relative_path = path.relative_to(checkout_path).as_posix()
        import_targets = [next((part for part in match.groups() if part), "") for match in IMPORT_RE.finditer(text)]
        for target in [item for item in import_targets if item]:
            db.add(
                CodeRelation(
                    repo_id=repo.id,
                    source_name=relative_path,
                    target_name=target,
                    relation_type="imports",
                    path=relative_path,
                    branch=branch,
                    pr_id=pr_id,
                    pr_number=pr_number,
                    meta={},
                )
            )
        local_symbols = [row for row in symbol_rows if row.path == relative_path]
        source = local_symbols[0] if local_symbols else None
        seen_calls: set[str] = set()
        for match in CALL_RE.finditer(text):
            target_name = match.group(1)
            if target_name in seen_calls or target_name in {"if", "for", "while", "return", "print", "len", "str", "int"}:
                continue
            seen_calls.add(target_name)
            target = by_name.get(target_name)
            db.add(
                CodeRelation(
                    repo_id=repo.id,
                    source_symbol_id=source.id if source else None,
                    target_symbol_id=target.id if target else None,
                    source_name=source.name if source else relative_path,
                    target_name=target_name,
                    relation_type="calls",
                    path=relative_path,
                    branch=branch,
                    pr_id=pr_id,
                    pr_number=pr_number,
                    meta={"line": _line_for_offset(text, match.start())},
                )
            )
    return len(symbol_rows)


def purge_legacy_code_documents(db: Session, repo_id: uuid.UUID | None = None) -> int:
    query = db.query(Document).filter(Document.source_type == "code_file")
    if repo_id is not None:
        query = query.filter(Document.repo_id == repo_id)
    return query.delete(synchronize_session=False)


async def sync_repository_code_analysis(db: Session, repo: Repository) -> int:
    repo.checkout_status = "downloading"
    repo.checkout_error = None
    repo.checkout_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(repo)
    try:
        token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
        checkout_path = await asyncio.to_thread(_sync_checkout, repo, token)
        if not _checkout_has_head(checkout_path):
            raise RuntimeError("源码检出不完整，请检查下载错误后重试。")
    except Exception as exc:
        db.rollback()
        repo.checkout_status = "failed"
        repo.checkout_error = git_error_message(exc)
        repo.checkout_updated_at = datetime.now(timezone.utc)
        db.commit()
        raise
    repo.checkout_status = "ready"
    repo.checkout_error = None
    repo.checkout_updated_at = datetime.now(timezone.utc)
    repo.local_path = str(checkout_path)
    db.commit()
    branch = repo.default_branch or _git_text(["branch", "--show-current"], checkout_path) or "main"
    commit_sha = _git_text(["rev-parse", "HEAD"], checkout_path)
    symbol_count = await asyncio.to_thread(
        _sync_code_graph,
        db,
        repo,
        checkout_path,
        branch=branch,
        commit_sha=commit_sha,
    )
    purge_legacy_code_documents(db, repo.id)
    repo.clone_url = repo.clone_url or _clone_url(repo)
    db.commit()
    from app.services.project_indexing import get_project_index_state, scan_project_index

    index_state = get_project_index_state(db, repo)
    if index_state["status"] in {"stale", "failed"}:
        scan_result = scan_project_index(db, repo)
        if scan_result["status"] == "failed":
            logger.warning("Project document refresh failed for %s: %s", repo.id, scan_result["error_message"])
    return symbol_count


async def sync_repository_code_analysis_by_id(repo_id: uuid.UUID) -> int | None:
    lock = _CHECKOUT_LOCKS.setdefault(repo_id, asyncio.Lock())
    if lock.locked():
        return None
    async with lock:
        return await _sync_repository_code_analysis_by_id(repo_id)


async def _sync_repository_code_analysis_by_id(repo_id: uuid.UUID) -> int | None:
    db = SessionLocal()
    try:
        repo = db.get(Repository, repo_id)
        if repo is None:
            return None
        try:
            return await sync_repository_code_analysis(db, repo)
        except Exception as exc:
            logger.warning("Failed to sync repository code analysis for %s: %s", repo_id, git_error_message(exc))
            return None
    finally:
        db.close()
