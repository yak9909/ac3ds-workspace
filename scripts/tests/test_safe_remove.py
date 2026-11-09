import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import orch

GIT = os.environ.get("AC3DS_GIT") or (
    "C:/Program Files/Git/cmd/git.exe" if Path("C:/Program Files/Git/cmd/git.exe").exists() else "git")
IS_WIN = os.name == "nt"


def run(cwd, *args):
    subprocess.run([GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=str(cwd),
                   check=True, capture_output=True)


def junction(link: Path, target: Path):
    if not IS_WIN:
        pytest.skip("ジャンクションは Windows だけ")
    orch.make_junction(link, target)


def symlink_or_skip(link: Path, target: Path, is_dir: bool):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作れない")


@pytest.fixture
def sentinel(tmp_path):
    s = tmp_path / "sentinel"
    s.mkdir()
    (s / "keep.txt").write_text("x")
    return s


def test_unlink_links_keeps_target(tmp_path, sentinel):
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    junction(wt / "sub" / "j", sentinel)
    symlink_or_skip(wt / "ds", sentinel, True)
    symlink_or_skip(wt / "f.txt", sentinel / "keep.txt", False)
    removed = orch.unlink_links(wt)
    assert len(removed) >= 1
    assert not (wt / "sub" / "j").exists() and not (wt / "ds").is_symlink() and not (wt / "f.txt").is_symlink()
    assert (sentinel / "keep.txt").read_text() == "x"


def test_remove_worktree_safely(tmp_path, sentinel):
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("a")
    run(repo, "add", "."), run(repo, "commit", "-m", "i")
    wt = tmp_path / "wt"
    run(repo, "worktree", "add", "-b", "b", str(wt))
    junction(wt / "link", sentinel)
    c = SimpleNamespace(git=GIT, data=tmp_path / "data")
    orch.remove_worktree_safely(c, repo, wt)
    assert not wt.exists()
    assert (sentinel / "keep.txt").read_text() == "x"


def test_ws_name_check(tmp_path):
    c = SimpleNamespace(data=tmp_path)
    for bad in ("", "a b", "../x", "a/b", "日本"):
        with pytest.raises(orch.OrchError):
            orch.ws_worktree(c, bad)
    assert orch.ws_worktree(c, "ok-1") == tmp_path / "worktrees" / "ws-ok-1"


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    run(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "root"
    run(tmp_path, "clone", str(origin), str(root))
    (root / "a.txt").write_text("a")
    run(root, "checkout", "-b", "main")
    run(root, "add", "."), run(root, "commit", "-m", "i")
    run(root, "push", "origin", "main")
    (root / "workspace.local.toml").write_text("x=1")
    (root / "ac3ds-re").mkdir()
    (root / "ac3ds-re" / ".git").write_text("gitdir: nowhere")
    c = SimpleNamespace(git=GIT, root=root, data=root / "_local")
    monkeypatch.setattr(orch, "Ctx", lambda: c)
    return c


def test_ws_add_remove(ws_env):
    c = ws_env
    if not IS_WIN:
        pytest.skip("Windows のジャンクション前提")
    assert orch.main(["ws", "add", "t1", "--branch", "tools/t1"]) == 0
    wt = c.data / "worktrees" / "ws-t1"
    assert (wt / "workspace.local.toml").exists()
    assert (wt / "ac3ds-re" / ".git").exists()
    assert orch.main(["ws", "add", "t1", "--branch", "tools/t2"]) == 1  # 既にある
    (wt / "new.txt").write_text("dirty")
    assert orch.main(["ws", "remove", "t1"]) == 1  # 未コミット
    assert wt.exists()
    assert orch.main(["ws", "remove", "t1", "--force"]) == 0
    assert not wt.exists()
    assert (c.root / "ac3ds-re" / ".git").exists()
    out = subprocess.run([GIT, "branch", "--list", "tools/t1"], cwd=str(c.root), capture_output=True, text=True)
    assert out.stdout.strip() == ""
