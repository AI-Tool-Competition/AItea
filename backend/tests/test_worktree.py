"""Exercise real Git worktrees, including safeguards that prevent data loss."""

import importlib.util
import subprocess

import pytest
from app.configuration.paths import PROJECT_ROOT

SPEC = importlib.util.spec_from_file_location("tea_worktree", PROJECT_ROOT / "scripts/worktree.py")
worktree = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worktree)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--initial-branch=feature/bootstrap")
    git(root, "config", "user.name", "Worktree Test")
    git(root, "config", "user.email", "worktree-test@example.invalid")
    (root / ".gitignore").write_text(".worktrees/\n.env\ndata/\n.venv/\nnode_modules/\n")
    (root / "README.md").write_text("Initial project\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial project")
    return root


def test_create_defaults_to_head_without_copying_private_files(repo):
    (repo / ".env").write_text("PRIVATE=fixture")
    for directory in ("data", ".venv", "node_modules"):
        (repo / directory).mkdir()
        (repo / directory / "private").write_text("fixture")
    initial_head = git(repo, "rev-parse", "HEAD")
    target = worktree.create_worktree(repo, "feature/task-verification")
    assert target.parent == repo / ".worktrees"
    assert git(target, "branch", "--show-current") == "feature/task-verification"
    assert git(target, "rev-parse", "HEAD") == initial_head
    assert git(repo, "branch", "--show-current") == "feature/bootstrap"
    assert git(repo, "status", "--porcelain") == ""
    assert (target / "README.md").is_file()
    assert all(not (target / name).exists() for name in (".env", "data", ".venv", "node_modules"))


def test_default_main_and_explicit_base(repo):
    git(repo, "branch", "main")
    (repo / "README.md").write_text("New branch change\n")
    git(repo, "commit", "-am", "Change working branch")
    target = worktree.create_worktree(repo, "fix/preview")
    assert git(target, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")
    target = worktree.create_worktree(repo, "docs/startup", base="HEAD")
    assert git(target, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "other/thing",
        "feature/../escape",
        "feature/wip",
        "fix/p0",
        "feature/codex-work",
        "feature/phase1",
        "feature/phase-1",
        "feature/phase-1-foo",
        "feature/$(touch danger)",
    ],
)
def test_reject_invalid_names(repo, name):
    with pytest.raises(ValueError, match="分支"):
        worktree.create_worktree(repo, name)
    assert not (repo / ".worktrees").exists()


@pytest.mark.parametrize("parent", ["/tmp/tea-test", "../escape", ".worktrees/../escape", ".", "unignored"])
def test_reject_unsafe_or_unignored_parents(repo, parent):
    with pytest.raises(ValueError, match="PARENT"):
        worktree.create_worktree(repo, "feature/safe-parent", parent=parent)


def test_reject_parent_symlink_escape(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".worktrees").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        worktree.create_worktree(repo, "feature/safe-parent")
    assert not list(outside.iterdir())


def test_reject_dirty_tree_and_preserve_changes(repo):
    (repo / "pending.md").write_text("unsaved")
    with pytest.raises(ValueError, match="未提交"):
        worktree.create_worktree(repo, "feature/pending")
    assert (repo / "pending.md").read_text() == "unsaved"


def test_reject_existing_branch_and_directory(repo):
    name = "feature/existing"
    git(repo, "branch", name)
    with pytest.raises(ValueError, match="分支已存在"):
        worktree.create_worktree(repo, name)
    name = "feature/occupied"
    target = repo / ".worktrees" / worktree.directory_name(name)
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("preserve me")
    with pytest.raises(ValueError, match="目录已存在"):
        worktree.create_worktree(repo, name)
    assert (target / "keep.txt").read_text() == "preserve me"


@pytest.mark.parametrize("base", ["missing-ref", "--help", "HEAD:README.md"])
def test_reject_invalid_or_non_commit_base(repo, base):
    with pytest.raises(ValueError):
        worktree.create_worktree(repo, "feature/valid-name", base=base)
    assert not (repo / ".worktrees").exists()


def test_slug_hash_prevents_branch_path_collisions():
    assert worktree.directory_name("feature/legal-review") != worktree.directory_name("feature/legal/review")
