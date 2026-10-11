#!/usr/bin/env python3
"""Create an isolated checkout without copying local credentials or runtime data."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "Git 操作失败")
    return result


def directory_name(name: str) -> str:
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name.replace('/', '-')}-{digest}"


def create_worktree(root: Path, name: str, parent: str = ".worktrees", base: str = "") -> Path:
    root = root.resolve()
    if not re.fullmatch(r"(?:feat|feature|fix|chore|docs|refactor|test|perf|build|ci)/[a-z0-9]+(?:[-/][a-z0-9]+)*", name):
        raise ValueError("NAME 应为有业务含义的小写分支名，例如 feature/task-verification。")
    tokens = re.split(r"[-/]", name)[1:]
    if any(token in {"tmp", "temp", "wip", "codex"} or re.fullmatch(r"p\d+|phase\d+", token) for token in tokens) or re.search(r"(?:/|-)phase-\d+(?:/|-|$)", name):
        raise ValueError("分支名不能使用 tmp、wip、codex 或阶段编号，请说明实际工作内容。")
    git(root, "check-ref-format", "--branch", name)
    actual_root = Path(git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual_root != root:
        raise ValueError("请从项目仓库根目录创建工作树。")
    relative = Path(parent)
    if not parent or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("PARENT 必须是仓库内的相对目录，不能包含 ..。")
    parent_path = (root / relative).resolve()
    if parent_path == root or not parent_path.is_relative_to(root):
        raise ValueError("PARENT 必须留在当前仓库内，不能通过符号链接指向外部。")
    if git(root, "check-ignore", "--quiet", "--", f"{relative.as_posix()}/", check=False).returncode:
        raise ValueError(f"PARENT 必须已被 Git 忽略：{parent}")
    if git(root, "status", "--porcelain", "--untracked-files=all").stdout:
        raise ValueError("当前工作树有未提交修改，请先保存为提交，再创建隔离工作树。")
    if git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0:
        raise ValueError(f"分支已存在：{name}")
    target = parent_path / directory_name(name)
    if target.exists() or target.is_symlink():
        raise ValueError(f"工作树目录已存在：{target}")
    if not base:
        has_main = git(root, "show-ref", "--verify", "--quiet", "refs/heads/main", check=False).returncode == 0
        base = "main" if has_main else "HEAD"
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").stdout.strip()
    parent_path.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-b", name, "--", str(target), commit)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="创建隔离开发工作树（不复制密钥、资料或虚拟环境）")
    parser.add_argument("--name", default=os.getenv("NAME", ""))
    parser.add_argument("--parent", default=os.getenv("PARENT") or ".worktrees")
    parser.add_argument("--base", default=os.getenv("BASE", ""))
    args = parser.parse_args()
    try:
        target = create_worktree(ROOT, args.name, args.parent, args.base)
    except (ValueError, OSError) as exc:
        print(f"无法创建工作树：{exc}", file=sys.stderr)
        return 2
    print(f"工作树已创建：{target}\n分支：{args.name}\n在新目录运行 make dev 初始化并启动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
