import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import orch


def context(tmp_path):
    root = tmp_path
    repo = root / "ac3ds-re"
    (repo / "meta").mkdir(parents=True)
    (repo / "meta" / "builds.toml").write_text(
        '[builds.one]\nscope = "acnl"\n[builds.archive]\nnote = "保管だけ"\n', encoding="utf-8")
    (repo / "tools" / "sync").mkdir(parents=True)
    (repo / "tools" / "sync" / "export_all.py").write_text("", encoding="utf-8")
    data = root / "_local"
    ida = data / "builds" / "one" / "ida"
    ida.mkdir(parents=True)
    (ida / "one.i64").write_bytes(b"canonical")
    (data / "state").mkdir()
    return SimpleNamespace(root=root, data=data, state=data / "state", local={"idalib_python": "fake-python"},
                           repos=lambda: {"ac3ds-re": {"role": "analysis"}})


def test_export_copy_unlock_command_and_state(tmp_path, monkeypatch):
    c = context(tmp_path)
    locked = []
    class Lock:
        def __init__(self, path, timeout):
            self.path = path
        def __enter__(self):
            locked.append(self.path)
        def __exit__(self, *_):
            locked.pop()
    calls = []
    def launch(args, **kw):
        assert not locked
        assert (c.data / "builds" / "one" / "ida" / "export-work" / "one.i64").read_bytes() == b"canonical"
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(orch, "FileLock", Lock)
    monkeypatch.setattr(orch.subprocess, "run", launch)
    monkeypatch.setattr(orch, "tool_env", lambda _: {})
    first = orch.run_export(c, "one", commit="abc")
    second = orch.run_export(c, "one", ["foo", "bar"], commit="def")
    assert first["mode"] == "full" and second["mode"] == "partial"
    assert second["full_at"] == first["full_at"]
    assert calls[0][1].endswith("export_all.py")
    assert calls[1][-4:] == ["--only", "foo", "--only", "bar"]
    assert orch.export_state(c)["builds"]["one"]["commit"] == "def"
    assert orch.canonical_state(c)["builds"]["one"]["sha256"] == hashlib.sha256(b"canonical").hexdigest()


def test_canonical_checks_hash_and_ida_files(tmp_path):
    c = context(tmp_path)
    orch.record_canonical(c, "one")
    assert all(code == 0 for _, code, _ in orch.canonical_checks(c))
    ida = c.data / "builds" / "one" / "ida"
    (ida / "one.i64").write_bytes(b"changed")
    (ida / "one.id2").write_bytes(b"")
    checks = orch.canonical_checks(c)
    assert checks[0][1] == 1
    assert "SHA256" in checks[0][2] and "one.id2" in checks[0][2]


def test_canonical_unrecorded_warning_but_open_file_fails(tmp_path):
    c = context(tmp_path)
    assert orch.canonical_checks(c)[0][1] == 0
    assert "警告" in orch.canonical_checks(c)[0][2]
    (c.data / "builds" / "one" / "ida" / "one.id0").write_bytes(b"")
    assert orch.canonical_checks(c)[0][1] == 1


def test_outside_status_added_and_allowed(tmp_path, monkeypatch):
    c = context(tmp_path)
    (c.root / ".git").mkdir()
    (c.root / "ac3ds-re" / ".git").mkdir()
    statuses = {"workspace": [" M tracked.txt"], "ac3ds-re": ["?? new.txt"]}
    monkeypatch.setattr(orch, "git", lambda _, path, *args: "\n".join(statuses[path.name if path != c.root else "workspace"]))
    before = orch.outside_status(c)
    statuses["ac3ds-re"].append(" M other.txt")
    failed = orch.outside_checks(c, before, False)
    allowed = orch.outside_checks(c, before, True)
    assert len(failed) == 1 and failed[0][1] == 1 and "other.txt" in failed[0][2]
    assert allowed[0][1] == 0 and "警告" in allowed[0][2]


def test_cleanup_success_and_keep(tmp_path, monkeypatch):
    c = context(tmp_path)
    c.root.joinpath("ac3ds-re").mkdir(exist_ok=True)
    wt = c.data / "worktrees" / "ac3ds-re" / "5"
    wt.mkdir(parents=True)
    idb = c.data / "builds" / "one" / "ida" / "work" / "5" / "one.i64"
    idb.parent.mkdir(parents=True)
    idb.write_bytes(b"work")
    task = {"repo": "ac3ds-re", "worktree": str(wt), "branch": "task/5-test", "idb": {"path": str(idb)}}
    calls = []
    def fake_git(_, __, *args):
        calls.append(args)
        if args[0] == "worktree":
            wt.rmdir()
    monkeypatch.setattr(orch, "git", fake_git)
    orch.cleanup_task(c, "task", task, keep=True)
    assert wt.exists() and idb.exists() and not calls
    orch.cleanup_task(c, "task", task)
    assert not wt.exists() and not idb.parent.exists()
    assert calls == [("worktree", "remove", "--force", str(wt)), ("branch", "-D", "task/5-test")]
    assert orch.load_tasks(c)["task"]["cleanup"] == "完了"


def test_cleanup_empty_directory_retry_and_warning(tmp_path, monkeypatch):
    c = context(tmp_path)
    wt = c.data / "worktrees" / "ac3ds-re" / "5"
    wt.mkdir(parents=True)
    task = {"repo": "ac3ds-re", "worktree": str(wt), "branch": "task/5-test", "idb": None}
    monkeypatch.setattr(orch, "git", lambda *args: None)
    original = Path.rmdir
    attempts = []
    def flaky(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError("locked")
        return original(path)
    monkeypatch.setattr(Path, "rmdir", flaky)
    monkeypatch.setattr(orch.time, "sleep", lambda _: None)
    orch.cleanup_task(c, "task", task)
    assert len(attempts) == 2 and not wt.exists()
    assert orch.load_tasks(c)["task"]["cleanup"] == "完了"
    wt.mkdir(parents=True)
    monkeypatch.setattr(Path, "rmdir", lambda _: (_ for _ in ()).throw(PermissionError("locked")))
    orch.cleanup_task(c, "task", task)
    assert orch.load_tasks(c)["task"]["cleanup"] == "警告"
    assert "locked" in orch.load_tasks(c)["task"]["cleanup_errors"][0]


def test_changed_function_names(tmp_path, monkeypatch):
    c = context(tmp_path)
    old = "address,name,prototype\n1,old,int()\n2,steady,void()\n3,unchanged,void()\n"
    new = "address,name,prototype\n1,new,int()\n2,steady,int()\n3,unchanged,void()\n4,added,void()\n"
    def fake_git(_, __, *args, **kw):
        if args[0] == "diff":
            return "symbols/one/functions.csv\nsymbols/one/data.csv\n"
        return old if args[1].startswith("merge^") else new
    monkeypatch.setattr(orch, "git", fake_git)
    assert orch.changed_function_names(c, c.root / "ac3ds-re", "merge") == {
        "one": ["added", "new", "steady"]}


def test_accept_refresh_uses_only_and_records_failure(tmp_path, monkeypatch):
    c = context(tmp_path)
    index = c.data / "builds" / "one" / "export" / "index.txt"
    index.parent.mkdir()
    index.write_text("index", encoding="utf-8")
    monkeypatch.setattr(orch, "changed_function_names", lambda *_: {"one": ["new", "typed"]})
    calls = []
    monkeypatch.setattr(orch, "run_export", lambda *args: calls.append(args))
    monkeypatch.setattr(orch, "update_task", lambda *args, **kw: calls.append(kw))
    orch.refresh_export_after_accept(c, "task", "merge")
    assert calls[0][1:] == ("one", ["new", "typed"], "merge")
    assert calls[1]["export_refresh"] == "完了"
    def fail(*_):
        raise orch.OrchError("fake failure")
    monkeypatch.setattr(orch, "run_export", fail)
    calls.clear()
    orch.refresh_export_after_accept(c, "task", "merge")
    assert calls[0]["export_refresh"] == "失敗" and calls[0]["export_error"] == "fake failure"


def test_start_export_schedule(tmp_path, monkeypatch, capsys):
    c = context(tmp_path)
    monkeypatch.setattr(orch, "Ctx", lambda: c)
    monkeypatch.setattr(orch, "load_tasks", lambda _: {})
    monkeypatch.setattr(orch, "gh_run", lambda *_: "[]")
    monkeypatch.setattr(orch, "mirror", lambda _: None)
    c.repos = lambda: {}
    orch.cmd_start(None)
    assert "全関数の書き出し one: 未実施 ← 作り直す" in capsys.readouterr().out
    (c.state / "export.json").write_text(json.dumps({"builds": {"one": {
        "full_at": orch.now()}}}), encoding="utf-8")
    orch.cmd_start(None)
    assert "全関数の書き出し one:" in capsys.readouterr().out


def test_dispatch_export_line(tmp_path, monkeypatch):
    c = context(tmp_path)
    monkeypatch.setattr(orch, "load_tasks", lambda _: {"x": {"state": "accepted", "idb_import": "取り込み済み",
        "symbols_builds": ["one"], "merge_commit": "new", "accepted_at": "2026-01-01"}})
    index = c.data / "builds" / "one" / "export" / "index.txt"
    assert orch.dispatch_export_line(c, "one") == ""
    index.parent.mkdir()
    index.write_text("index", encoding="utf-8")
    (c.state / "export.json").write_text(json.dumps({"builds": {"one": {"commit": "new"}}}), encoding="utf-8")
    assert "以後に変わった名前" not in orch.dispatch_export_line(c, "one")
    (c.state / "export.json").write_text(json.dumps({"builds": {"one": {"commit": "old"}}}), encoding="utf-8")
    monkeypatch.setattr(orch, "git_ok", lambda *_: True)
    assert "書き出しは old 時点。以後に変わった名前は symbols を見る" in orch.dispatch_export_line(c, "one")
