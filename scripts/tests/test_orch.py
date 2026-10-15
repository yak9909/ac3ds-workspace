import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import orch
import wslib
from conftest import FAKE_EMAIL, REAL_ROOT

GIT = wslib.git_exe(REAL_ROOT)
ISSUE_BODY = ("### スコープ\n\nacnl\n\n### 種類\n\nanalysis\n\n### 難易度\n\nnormal\n\n"
              "### 目的と完了条件\n\nある関数を調べる\n\n### 対象\n\n0x00100000\n\n### 関係する資料\n\nなし\n")


def g(cwd, *args):
    r = subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


class FakeGh:
    def __init__(self, tmp, origin):
        self.calls = []
        self.tmp = tmp
        self.origin = origin
        self.issue = {"title": "関数を調べる", "body": ISSUE_BODY, "labels": [{"name": "acnl"}], "state": "OPEN"}
        self.next_pr = 7

    def __call__(self, args, input_text=None):
        self.calls.append((list(args), input_text))
        a = args
        if a[:2] == ["issue", "view"]:
            return json.dumps(self.issue)
        if a[:2] == ["pr", "create"]:
            return "https://github.com/x/y/pull/%d\n" % self.next_pr
        if a[:2] == ["issue", "create"]:
            return "https://github.com/x/y/issues/12\n"
        if a[:2] == ["pr", "checks"]:
            return json.dumps([{"name": "audit", "bucket": "pass"}])
        if a[:2] == ["pr", "merge"]:
            br = next(c for c in self.calls if c[0][:2] == ["pr", "create"])[0]
            head = br[br.index("--head") + 1]
            cl = self.tmp / "mergeclone"
            if cl.exists():
                shutil.rmtree(cl)
            g(self.tmp, "clone", str(self.origin), str(cl))
            g(cl, "merge", "--no-ff", "-m", "merge", "origin/" + head)
            g(cl, "push", "origin", "HEAD:main")
            return ""
        if a[:1] == ["api"] and "labels?per_page=100" in a[1]:
            return "[]"
        return ""

    def named(self, *prefix):
        return [c for c in self.calls if c[0][:len(prefix)] == list(prefix)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "--bare", "-b", "main", str(origin))
    (ws / "workspace.toml").write_text(
        'data = "_local"\n[repos.ac3ds-re]\nrole = "analysis"\nremote = "https://github.com/o/ac3ds-re.git"\n'
        '[local_only]\nfiles = ["workspace.local.toml"]\n', encoding="utf-8")
    (ws / "workspace.local.toml").write_text("", encoding="utf-8")
    shutil.copy(REAL_ROOT / "workers.toml", ws / "workers.toml")
    shutil.copytree(REAL_ROOT / "templates", ws / "templates")
    re_ = ws / "ac3ds-re"
    (re_ / "tools").mkdir(parents=True)
    for n in ("kb.py", "audit.py"):
        shutil.copy(REAL_ROOT / "ac3ds-re" / "tools" / n, re_ / "tools" / n)
    shutil.copytree(REAL_ROOT / "ac3ds-re" / "meta", re_ / "meta")
    shutil.copy(REAL_ROOT / "ac3ds-re" / ".gitignore", re_ / ".gitignore")
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com"),
                 ("AC3DS_WORKSPACE", str(ws)), ("AC3DS_GIT", GIT)):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AC3DS_REPO", raising=False)
    (ws / "_local" / "state").mkdir(parents=True)
    monkeypatch.setenv("AC3DS_STATE", str(ws / "_local" / "state"))
    g(re_, "init", "-b", "main")
    g(re_, "remote", "add", "origin", str(origin))
    subprocess.run([sys.executable, str(re_ / "tools" / "kb.py"), "index"], cwd=re_, check=True, capture_output=True)
    g(re_, "add", "-A")
    g(re_, "commit", "-m", "init")
    g(re_, "push", "origin", "main")
    g(re_, "fetch", "origin")
    fake = FakeGh(tmp_path, origin)
    monkeypatch.setattr(orch, "gh_run", fake)
    monkeypatch.setattr(orch, "CHECK_POLL_SEC", 0)
    # 偽の正本 IDB
    ida = ws / "_local" / "builds" / "acnl_jpn_v15" / "ida"
    ida.mkdir(parents=True)
    (ida / "acnl_jpn_v15.i64").write_bytes(b"fake")
    return ws, fake, origin


def run(*args):
    return orch.main(list(args))


def tasks(ws):
    return json.loads((ws / "_local" / "state" / "tasks.json").read_text(encoding="utf-8"))["tasks"]


def dispatch(env, idb=True):
    a = ["dispatch", "5", "--worker", "sonnet-5.5"]
    return run(*(a + (["--idb"] if idb else [])))


def add_record_and_symbols(wt):
    cats = tomllib.loads((wt / "meta" / "categories.toml").read_text(encoding="utf-8"))
    cat = cats["scopes"]["acnl"]["categories"][0]
    r = subprocess.run([sys.executable, str(wt / "tools" / "kb.py"), "new", "--scope", "acnl", "--category", cat,
                        "--title", "試験の記録"], cwd=wt, capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "AC3DS_STATE": str(wt.parent / "kbstate")})
    assert r.returncode == 0, r.stderr
    p = Path(r.stdout.strip())
    p.write_text(p.read_text(encoding="utf-8").replace('summary: ""', 'summary: "試験の要約"'), encoding="utf-8")
    sym = wt / "symbols" / "acnl_jpn_v15"
    sym.mkdir(parents=True)
    (sym / "functions.csv").write_text(
        "address,size,name,prototype,confidence,source,refs,clone_of\n"
        "0x00100000,4,sub_100000,,,analysis,,\n0x00100010,8,foo,,,analysis,,\n", encoding="utf-8", newline="\n")
    (wt / ".task").mkdir(exist_ok=True)
    (wt / ".task" / "report.md").write_text("報告\n", encoding="utf-8")


def test_issue(env, capsys):
    ws, fake, _ = env
    assert run("issue", "--scope", "hhd", "--kind", "analysis", "--difficulty", "easy", "--title", "調べる",
               "--goal", "g", "--target", "t", "--materials", "なし") == 0
    args, _in = fake.named("issue", "create")[0]
    assert args[args.index("--title") + 1] == "[HHD] 調べる"
    body = args[args.index("--body") + 1]
    assert "### 目的と完了条件" in body and "### 関係する資料" in body
    assert args.count("--label") == 3
    # 必須項目の欠け
    assert run("issue", "--scope", "hhd", "--kind", "analysis", "--difficulty", "easy", "--title", "x",
               "--goal", " ", "--target", "t", "--materials", "なし") == 1
    # 既に頭がある題名は重複させない
    run("issue", "--scope", "acnl", "--kind", "analysis", "--difficulty", "easy", "--title", "[ACNL] x",
        "--goal", "g", "--target", "t", "--materials", "m")
    a2 = fake.named("issue", "create")[1][0]
    assert a2[a2.index("--title") + 1] == "[ACNL] x"


def test_outgoing_rejected(env):
    ws, fake, _ = env
    n = len(fake.calls)
    assert run("issue", "--scope", "acnl", "--kind", "analysis", "--difficulty", "easy", "--title", "x",
               "--goal", "連絡は " + FAKE_EMAIL, "--target", "t", "--materials", "m") == 1
    assert len(fake.calls) == n


def test_dispatch(env):
    ws, fake, _ = env
    assert dispatch(env) == 0
    t = tasks(ws)["ac3ds-re#5"]
    wt = Path(t["worktree"])
    assert wt.is_dir() and (wt / ".task").is_dir()
    assert t["branch"].startswith("task/5-") and t["state"] == "dispatched" and t["attempts"] == 1
    cur = g(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert cur == t["branch"]
    idb = Path(t["idb"]["path"])
    assert idb.read_bytes() == b"fake" and "work" in idb.parts
    text = (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    assert "ある関数を調べる" in text and str(idb) in text and "事前に引いた既存の知見: なし" in text
    # 再試行は作業ツリーを再利用して試行回数が増える
    assert dispatch(env) == 0
    assert tasks(ws)["ac3ds-re#5"]["attempts"] == 2


def test_dispatch_no_canonical_idb(env):
    ws, fake, _ = env
    (ws / "_local" / "builds" / "acnl_jpn_v15" / "ida" / "acnl_jpn_v15.i64").unlink()
    assert dispatch(env) == 1


def test_save(env):
    ws, fake, _ = env
    dispatch(env)
    t = tasks(ws)["ac3ds-re#5"]
    wt = Path(t["worktree"])
    assert run("save", "5") == 0  # 変更なし
    assert not fake.named("pr", "create")
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    assert run("save", "5", "--message", "最初") == 0
    assert len(fake.named("pr", "create")) == 1
    args = fake.named("pr", "create")[0][0]
    assert "--draft" in args and "Closes #5" in args[args.index("--body") + 1]
    assert tasks(ws)["ac3ds-re#5"]["pr"] == 7
    assert "Co-Authored-By: Claude Opus 5.5" in g(wt, "log", "-1", "--format=%B")
    (wt / "docs" / "b.md").write_text("b\n", encoding="utf-8")
    assert run("save", "5") == 0
    assert len(fake.named("pr", "create")) == 1


def test_save_refused_when_idb_open(env):
    ws, fake, _ = env
    dispatch(env)
    t = tasks(ws)["ac3ds-re#5"]
    wt = Path(t["worktree"])
    (Path(t["idb"]["path"]).parent / "acnl_jpn_v15.id0").write_bytes(b"x")
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    assert run("save", "5") == 1
    assert not fake.named("pr", "create")


def ready(env):
    ws, fake, origin = env
    dispatch(env)
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    add_record_and_symbols(wt)
    assert run("save", "5") == 0
    return ws, fake, origin, wt


def test_stamp_summary(env, capsys):
    ws, fake, origin, wt = ready(env)
    assert run("stamp", "5") == 0
    out = capsys.readouterr().out
    assert "試験の記録" in out and "試験の要約" in out
    assert "functions.csv: 追加 2" in out and "0x00100010" in out
    assert "report.md: あり" in out
    t = tasks(ws)["ac3ds-re#5"]
    assert t["state"] == "stamped" and t["stamp_ok"], out
    assert (ws / "_local" / "orchestrator" / "stamp" / "5.md").exists()
    assert "by: sonnet-5.5" in next((wt / "findings").rglob("ACNL-F*.md")).read_text(encoding="utf-8")


def test_accept_order_errors(env):
    ws, fake, origin, wt = ready(env)
    assert run("accept", "5", "--scores", "a=5") == 1  # stamp 前
    run("stamp", "5")
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "late.md").write_text("late\n", encoding="utf-8")
    assert run("accept", "5", "--scores", "a=5") == 1  # stamp 後の変更
    assert not fake.named("pr", "merge")


def test_accept_dry_run_writes_nothing(env, capsys):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    before = (tasks(ws), g(wt, "rev-parse", "HEAD"), len(fake.calls))
    assert run("accept", "5", "--scores", "a=5", "--dry-run") == 0
    assert (tasks(ws), g(wt, "rev-parse", "HEAD"), len(fake.calls)) == before
    assert (wt / ".task").exists()
    assert run("reject", "5", "--reason", "x", "--dry-run") == 0
    assert len(fake.calls) == before[2]


def test_accept_full(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    assert run("accept", "5", "--scores", "正確さ=5,根拠=4") == 0
    t = tasks(ws)["ac3ds-re#5"]
    assert t["state"] == "accepted" and t["idb_import"] == "未実装"
    assert not (wt / ".task").exists()
    assert fake.named("pr", "ready") and fake.named("pr", "merge")
    cm = fake.named("pr", "comment")[0][0]
    assert "合格" in cm[cm.index("--body") + 1]
    re_ = ws / "ac3ds-re"
    files = g(re_, "ls-tree", "-r", "--name-only", "main").split()
    assert any(f.startswith("findings/acnl/") and f.endswith(".md") and "F0" in f for f in files)
    assert "symbols/acnl_jpn_v15/functions.csv" in files
    assert not any(f.startswith(".task/") for f in files)


def test_reject(env):
    ws, fake, origin, wt = ready(env)
    assert run("reject", "5", "--reason", "根拠が足りない") == 0
    assert tasks(ws)["ac3ds-re#5"]["state"] == "rejected"
    assert fake.named("pr", "close") and "--delete-branch" in fake.named("pr", "close")[0][0]
    ws2 = ws
    assert run("reject", "5", "--reason", "again") == 1
    assert ws2


def test_reject_outgoing(env):
    ws, fake, origin, wt = ready(env)
    assert run("reject", "5", "--reason", "連絡は " + FAKE_EMAIL) == 1
    assert not fake.named("pr", "close")


def test_wrapup_handoff(env):
    ws, fake, origin = env
    dispatch(env)
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    (wt / ".task" / "notes.md").write_text("## 経過\nx\n## 次の1手\n0x100000 の呼び出し元を見る\n", encoding="utf-8")
    h = ws / "_local" / "orchestrator" / "handoff.md"
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text("# 引き継ぎメモ（オーケストラ）\n\n## 古い節\n- a\n", encoding="utf-8")
    assert run("wrapup") == 0
    text = h.read_text(encoding="utf-8")
    assert text.startswith("# 引き継ぎメモ")
    assert text.index("中断") < text.index("古い節")
    assert "ac3ds-re#5" in text and "呼び出し元を見る" in text
    assert tasks(ws)["ac3ds-re#5"]["state"] == "saved"


def test_setup_github_dry_and_status(env, capsys):
    ws, fake, _ = env
    assert run("setup-github", "ac3ds-re", "--dry-run") == 0
    assert not fake.calls
    assert run("setup-github", "ac3ds-re") == 0
    assert fake.named("api", "-X", "PUT")
    assert run("status") == 0
