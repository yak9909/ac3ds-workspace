import io
from types import SimpleNamespace

import pytest

import orch
from conftest import FAKE_EMAIL


READ = "ワーカーの報告\n関係する資料の本文"
SCORES = ",".join(name + "=0.9" for name in orch.EVAL_ITEMS["analysis"])


@pytest.fixture
def accept_unit(tmp_path, monkeypatch):
    task = {"state": "stamped", "stamp_ok": True, "stamped_head": "head",
            "worktree": str(tmp_path / "worktree"), "pr": 7, "branch": "task/5"}
    calls = []
    monkeypatch.setattr(orch, "Ctx", lambda: SimpleNamespace(root=tmp_path, git="unused"))
    monkeypatch.setattr(orch, "need_repo", lambda *_: {"role": "analysis"})
    monkeypatch.setattr(orch, "get_task", lambda *_: task)
    monkeypatch.setattr(orch, "pass_min", lambda *_: 0.6)

    def fake_git(*args):
        calls.append(("git", args[2:]))
        return ""

    monkeypatch.setattr(orch, "git", fake_git)
    monkeypatch.setattr(orch, "head", lambda *_: "head")
    monkeypatch.setattr(orch, "repo_slug", lambda *_: "o/repo")
    monkeypatch.setattr(orch, "tool_env", lambda *_: {})
    monkeypatch.setattr(orch.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(orch, "commit_all", lambda *_: None)
    monkeypatch.setattr(orch, "update_task", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "wait_checks", lambda *_: None)
    monkeypatch.setattr(orch, "read_usage", lambda *_: {})
    monkeypatch.setattr(orch.sync_skills, "check", lambda *_: False)
    monkeypatch.setattr(orch, "cleanup_task", lambda *_: None)
    monkeypatch.setattr(orch, "report_consolidation", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "gh_run", lambda args: calls.append(("gh", args)))
    return tmp_path / "read.md", calls


@pytest.mark.parametrize("dry", [False, True])
def test_accept_requires_read(capsys, dry):
    with pytest.raises(SystemExit) as exc:
        orch.main(["accept", "5", "--scores", SCORES] + (["--dry-run"] if dry else []))
    assert exc.value.code == 2
    assert "--read" in capsys.readouterr().err


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("body,empty", [("", True), (" \n\t\n", True),
                                        ("報告だけ\n", False), ("\n報告だけ\n \n", False)])
def test_accept_refuses_insufficient_read(accept_unit, capsys, body, empty, dry):
    src, calls = accept_unit
    src.write_text(body, encoding="utf-8")
    assert orch.main(["accept", "5", "--scores", SCORES, "--read", str(src)]
                     + (["--dry-run"] if dry else [])) == 1
    expected = ("採点の前に読んだ物の一覧が空: " + str(src) if empty else
                "読んだ物は 2 つ以上書く（報告だけで採点しない）")
    assert expected in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("source", ["file", "stdin"])
@pytest.mark.parametrize("dry", [False, True])
def test_accept_read_in_score_comment(accept_unit, monkeypatch, capsys, source, dry):
    src, calls = accept_unit
    if source == "stdin":
        monkeypatch.setattr(orch.sys, "stdin", io.StringIO(READ))
        path = "-"
    else:
        src.write_text(READ, encoding="utf-8-sig", newline="\r\n")
        path = str(src)
    assert orch.main(["accept", "5", "--scores", SCORES, "--read", path]
                     + (["--dry-run"] if dry else [])) == 0
    if dry:
        assert "採点の前に読んだ物:" in capsys.readouterr().out
        assert not any(kind == "gh" for kind, _ in calls)
    else:
        bodies = [args[args.index("--body") + 1] for kind, args in calls
                  if kind == "gh" and args[:2] == ["pr", "comment"]]
        assert len(bodies) == 1
        assert "採点: " in bodies[0]
        assert "採点の前に読んだ物:\n\n" + READ in bodies[0]


def test_accept_read_checked_after_scores(accept_unit, monkeypatch):
    def unexpected_read(*_):
        pytest.fail("点数を確かめる前に一覧を読んだ")

    monkeypatch.setattr(orch, "read_source", unexpected_read)
    with pytest.raises(orch.OrchError, match="採点の項目が違う"):
        orch.cmd_accept(SimpleNamespace(repo="ac3ds-re", issue=5, scores="x=0.9", read="unused"))


def test_accept_read_passes_outgoing_check(accept_unit, capsys):
    src, calls = accept_unit
    src.write_text(READ + "\n連絡は " + FAKE_EMAIL, encoding="utf-8")
    assert orch.main(["accept", "5", "--scores", SCORES, "--read", str(src)]) == 1
    assert "GitHub に書く本文に問題があるので書かない" in capsys.readouterr().err
    assert not any(kind == "gh" for kind, _ in calls)
