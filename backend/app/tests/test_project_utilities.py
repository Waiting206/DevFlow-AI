import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, Issue, Repository, WorkflowRun


def _load_script(filename):
    path = Path(__file__).resolve().parents[3] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


seed = _load_script("seed_demo_data.py")
images = _load_script("organize_doc_images.py")


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_new_demo_identity_is_idempotent(db):
    repo = seed.ensure_demo_repository(db)
    assert repo.full_name == "repopilot-demo/sample-api"
    assert repo.description == seed.DEMO_DESCRIPTION
    assert seed.ensure_demo_repository(db).id == repo.id
    assert db.query(Repository).count() == 1


@pytest.mark.parametrize("description", [None, "Demo repository for RepoPilot AI course", "Custom local description"])
def test_legacy_fixture_rename_preserves_ids_and_custom_content(db, description):
    repo = Repository(owner="course-demo", name="devflow-sample-api", full_name=seed.LEGACY_DEMO_FULL_NAME,
                      github_id=seed.DEMO_GITHUB_ID, description=description)
    db.add(repo)
    db.flush()
    original_id = repo.id
    issue = Issue(repo_id=repo.id, number=1, title="Keep this issue")
    original_run = WorkflowRun(repo_id=repo.id, github_run_id=301, name="Synthetic CI", status="completed",
                               html_url=f"https://github.com/{seed.LEGACY_DEMO_FULL_NAME}/actions/runs/301")
    custom_run = WorkflowRun(repo_id=repo.id, github_run_id=302, name="Custom CI", status="completed", html_url="https://ci.example/run/302")
    db.add_all([issue, original_run, custom_run])
    db.flush()
    issue_id = issue.id

    updated = seed.ensure_demo_repository(db)
    assert updated.id == original_id
    assert updated.full_name == seed.DEMO_FULL_NAME
    assert updated.owner == seed.DEMO_OWNER and updated.name == seed.DEMO_NAME
    assert updated.description == (description if description == "Custom local description" else seed.DEMO_DESCRIPTION)
    assert db.get(Issue, issue_id).repo_id == original_id
    assert original_run.repo_id == custom_run.repo_id == original_id
    assert original_run.html_url == f"https://github.com/{seed.DEMO_FULL_NAME}/actions/runs/301"
    assert custom_run.html_url == "https://ci.example/run/302"
    assert db.query(Repository).count() == 1


def test_similarly_named_repository_without_fixture_marker_is_preserved(db):
    existing = Repository(owner="course-demo", name="devflow-sample-api", full_name=seed.LEGACY_DEMO_FULL_NAME,
                          github_id=98765, description="External repository")
    db.add(existing)
    db.flush()
    result = seed.ensure_demo_repository(db)
    assert result.id != existing.id
    assert existing.full_name == seed.LEGACY_DEMO_FULL_NAME
    assert existing.description == "External repository"


def test_document_image_plan_supports_nested_folders_without_writing(tmp_path):
    folder = tmp_path / "architecture"
    folder.mkdir()
    article = folder / "system.md"
    article.write_text("# System\n\n![Diagram](diagram.png)\n", encoding="utf-8")
    referenced = folder / "diagram.png"
    orphan = folder / "unused.png"
    referenced.write_bytes(b"referenced diagram fixture")
    orphan.write_bytes(b"unreferenced diagram fixture")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    plan = images.build_plan(tmp_path)
    assert plan.active_moves[0].target == folder / "images" / "system" / "diagram.png"
    assert plan.orphan_moves[0].target == tmp_path / "_orphaned_images" / "architecture" / "unused.png"
    assert before == {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    images.apply_plan(tmp_path, plan)
    images.verify(tmp_path, expected_archive_count=1)
    assert "images/system/diagram.png" in article.read_text()
    assert (folder / "images/system/diagram.png").read_bytes() == b"referenced diagram fixture"
    assert (tmp_path / "_orphaned_images/architecture/unused.png").read_bytes() == b"unreferenced diagram fixture"


def test_image_organizer_requires_explicit_root(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["organize_doc_images.py"])
    with pytest.raises(SystemExit) as exc:
        images.main()
    assert exc.value.code == 2
