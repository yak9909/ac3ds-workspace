import io
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
        self.protection = ["audit"]  # main の保護で必須の検査（None なら保護なし）
        self.checks_script = [[{"name": "audit", "bucket": "pass"}]]  # 呼ぶたびに次を返し、最後を繰り返す（None は no checks）
        self.checks_calls = 0
        self.passed = False  # 直近の pr checks が全部通っていたか（merge の拒否に使う）
        self.view_lag = 0  # 最初の何回の pr view を古い head として返すか
        self.view_calls = 0
        self.fail = {}  # "pr ready" などの語 -> 残りの失敗回数

    def head_branch(self):
        br = next(c for c in self.calls if c[0][:2] == ["pr", "create"])[0]
        return br[br.index("--head") + 1]

    def __call__(self, args, input_text=None):
        self.calls.append((list(args), input_text))
        a = args
        if self.fail.get(" ".join(a[:2])):
            self.fail[" ".join(a[:2])] -= 1
            raise orch.GhError("gh %s: 試験の失敗" % " ".join(a[:2]))
        if a[:2] == ["issue", "view"]:
            return json.dumps(self.issue)
        if a[:2] == ["pr", "create"]:
            return "https://github.com/x/y/pull/%d\n" % self.next_pr
        if a[:2] == ["issue", "create"]:
            return "https://github.com/x/y/issues/12\n"
        if a[:2] == ["pr", "view"]:
            self.view_calls += 1
            if self.view_calls <= self.view_lag:
                return json.dumps({"headRefOid": "0" * 40})  # push の反映が遅く、古い head が見えている
            return json.dumps({"headRefOid": g(self.origin, "rev-parse", "refs/heads/" + self.head_branch()).strip()})
        if a[:2] == ["pr", "checks"]:
            items = self.checks_script[min(self.checks_calls, len(self.checks_script) - 1)]
            self.checks_calls += 1
            if items is None:
                raise orch.GhError("gh pr checks: no checks reported on the branch")
            self.passed = bool(items) and all(i["bucket"] in ("pass", "skipping") for i in items)
            if any(i["bucket"] == "pending" for i in items):
                raise orch.GhError("gh pr checks: 保留あり", json.dumps(items), 8)  # gh は保留で終了コード 8
            return json.dumps(items)
        if a[:2] == ["pr", "merge"]:
            if self.protection and not self.passed:
                raise orch.GhError("gh pr merge: main の保護で拒否（必須の検査が通っていない）")
            head = self.head_branch()
            cl = self.tmp / "mergeclone"
            if cl.exists():
                shutil.rmtree(cl)
            g(self.tmp, "clone", str(self.origin), str(cl))
            g(cl, "merge", "--no-ff", "-m", "merge", "origin/" + head)
            g(cl, "push", "origin", "HEAD:main")
            return ""
        if a[:1] == ["api"] and a[-1].endswith("/branches/main/protection"):
            if self.protection is None:
                raise orch.GhError("gh api: Branch not protected (HTTP 404)")
            return json.dumps({"required_status_checks": {"strict": False, "contexts": self.protection}})
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
    monkeypatch.setattr(orch, "CHECK_APPEAR_SEC", 1)
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
    assert tasks(ws)["ac3ds-re#5"]["attempts"] == 1  # 変更が無ければ番号は進まない
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    assert run("save", "5", "--message", "最初") == 0
    assert len(fake.named("pr", "create")) == 1
    args = fake.named("pr", "create")[0][0]
    assert "--draft" in args and "Closes #5" in args[args.index("--body") + 1]
    assert tasks(ws)["ac3ds-re#5"]["pr"] == 7
    assert g(wt, "log", "-1", "--format=%s").strip() == "試行 1: 最初"
    assert "Co-Authored-By: Claude Opus 5.5" in g(wt, "log", "-1", "--format=%B")
    (wt / "docs" / "b.md").write_text("b\n", encoding="utf-8")
    assert run("save", "5") == 0
    assert len(fake.named("pr", "create")) == 1
    assert g(wt, "log", "-1", "--format=%s").strip() == "試行 2: 作業の保存"
    assert tasks(ws)["ac3ds-re#5"]["attempts"] == 3


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
    assert g(wt, "log", "-1", "--format=%s").strip() == "試行 2: 仕上げ"  # save の後の番号
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


def test_accept_waits_for_checks_of_pushed_commit(env, capsys):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.checks_script = [None, None, [{"name": "audit", "bucket": "pending"}], [{"name": "audit", "bucket": "pass"}]]
    assert run("accept", "5", "--scores", "a=5") == 0, capsys.readouterr().err
    assert len(fake.named("pr", "checks")) == 4
    assert fake.named("pr", "merge")


def test_accept_waits_until_pr_head_is_pushed_commit(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.view_lag = 2
    assert run("accept", "5", "--scores", "a=5") == 0
    assert len(fake.named("pr", "view")) >= 3


def test_accept_stops_when_required_check_never_appears(env, capsys, monkeypatch):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    monkeypatch.setattr(orch, "CHECK_APPEAR_SEC", 0)
    fake.checks_script = [None]
    assert run("accept", "5", "--scores", "a=5") == 1
    assert "必須: audit" in capsys.readouterr().err
    assert not fake.named("pr", "merge")
    assert tasks(ws)["ac3ds-re#5"]["state"] == "stamped"


def test_accept_without_required_checks_warns(env, capsys, monkeypatch):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    monkeypatch.setattr(orch, "CHECK_APPEAR_SEC", 0)
    fake.protection = None
    fake.checks_script = [None]
    assert run("accept", "5", "--scores", "a=5") == 0
    assert "警告: チェックが1つも無い" in capsys.readouterr().out
    assert fake.named("pr", "merge")


def test_accept_resumes_after_its_own_prep_commit(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.fail["pr ready"] = 1
    assert run("accept", "5", "--scores", "a=5") == 1  # 準備のコミットを作った後で失敗
    assert tasks(ws)["ac3ds-re#5"]["state"] == "stamped"
    assert run("accept", "5", "--scores", "a=5") == 0
    assert tasks(ws)["ac3ds-re#5"]["state"] == "accepted"


def test_accept_refuses_change_after_prep_commit(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.fail["pr ready"] = 1
    assert run("accept", "5", "--scores", "a=5") == 1
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "x.md").write_text("x\n", encoding="utf-8")
    assert run("accept", "5", "--scores", "a=5") == 1
    assert not fake.named("pr", "merge")


def test_git_gets_tool_venv_first_on_path(env, monkeypatch):
    ws, fake, _ = env
    d = ws / "_local" / "venv" / "tools" / ("Scripts" if os.name == "nt" else "bin")
    d.mkdir(parents=True)
    real_run = subprocess.run
    envs = []

    def spy(args, **kw):
        envs.append(kw.get("env"))
        return real_run(args, **kw)

    monkeypatch.setattr(orch.subprocess, "run", spy)
    orch.git(orch.Ctx(), ws, "--version")
    assert envs[-1] and Path(envs[-1]["PATH"].split(os.pathsep)[0]).resolve() == d.resolve()
    shutil.rmtree(ws / "_local" / "venv")
    orch.git(orch.Ctx(), ws, "--version")
    assert envs[-1]["PATH"] == os.environ["PATH"]


def test_last_agent_message_both_formats():
    old = json.dumps({"id": "0", "msg": {"type": "agent_message", "message": "旧"}})
    new = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "新"}})
    assert orch.last_agent_message(old + "\n" + new + "\nnot json\n") == "新"
    assert orch.last_agent_message("not json\n") is None


def test_report_from_file_and_stdin(env, capsys, monkeypatch, tmp_path):
    ws, fake, _ = env
    dispatch(env)
    rep = Path(tasks(ws)["ac3ds-re#5"]["worktree"]) / ".task" / "report.md"
    src = tmp_path / "r.md"
    src.write_bytes("# 報告\r\n\r\n結論: 試験\r\n".encode("utf-8"))
    assert run("report", "5", "--from", str(src)) == 0
    assert rep.read_bytes().decode("utf-8") == "（ワーカーの最終の返答をオーケストラが保存した）\n# 報告\n\n結論: 試験\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO("標準入力の報告\n".encode("utf-8")), encoding="utf-8"))
    assert run("report", "5", "--from", "-") == 0
    assert "上書き" in capsys.readouterr().out
    assert rep.read_bytes().decode("utf-8").endswith("標準入力の報告\n")


def test_report_refused(env, tmp_path):
    ws, fake, _ = env
    dispatch(env)
    rep = Path(tasks(ws)["ac3ds-re#5"]["worktree"]) / ".task" / "report.md"
    src = tmp_path / "r.md"
    src.write_text("連絡は " + FAKE_EMAIL + "\n", encoding="utf-8")
    assert run("report", "5", "--from", str(src)) == 1  # GitHub へ出る本文の検査
    assert not rep.exists()
    src.write_text("本文\n", encoding="utf-8")
    assert run("reject", "5", "--reason", "試験") == 0
    assert run("report", "5", "--from", str(src)) == 1  # 却下後は保存しない
    assert not rep.exists()


class FakeCodex:
    """orch.run_codex の代わり。起動せず、決めた結果を返す。"""

    def __init__(self, out, err="", code=0):
        self.out, self.err, self.code = out, err, code
        self.calls = []

    def __call__(self, c, cmd, cwd, prompt):
        self.calls.append((list(cmd), Path(cwd), prompt))
        return self.code, self.out, self.err


def use_codex(monkeypatch, out, err="", code=0):
    monkeypatch.setattr(orch, "find_codex", lambda: "codex")
    fc = FakeCodex(out, err, code)
    monkeypatch.setattr(orch, "run_codex", fc)
    return fc


def codex_jsonl(*msgs):
    lines = [json.dumps({"type": "thread.started", "thread_id": "t1"})]
    lines += [json.dumps({"type": "item.completed", "item": {"id": "i%d" % k, "type": "agent_message", "text": m}})
              for k, m in enumerate(msgs)]
    return "\n".join(lines) + "\n"


def test_codex_runs_and_saves_log_and_report(env, capsys, monkeypatch):
    ws, fake, _ = env
    assert run("dispatch", "5", "--worker", "gpt-6-sol") == 0
    t = tasks(ws)["ac3ds-re#5"]
    wt = Path(t["worktree"])
    assert t["provider"] == "codex" and t["worker"] == "gpt-6-sol"
    fc = use_codex(monkeypatch, codex_jsonl("途中の文", "最後の返答です"), err="WARN 注意\nERROR 試験のエラー\n")
    assert run("codex", "5") == 0
    cmd, cwd, prompt = fc.calls[0]
    assert cwd == wt
    assert cmd[:2] == ["codex", "exec"] and cmd[cmd.index("-C") + 1] == str(wt)
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--add-dir"] == [
        str(ws / "_local" / "state")]
    assert cmd[cmd.index("-m") + 1] == "gpt-6-sol"
    assert 'model_reasoning_effort="medium"' in cmd and cmd[-2:] == ["--json", "-"]
    if os.name == "nt":
        assert 'windows.sandbox="unelevated"' in cmd
    assert prompt == (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    log = ws / "_local" / "logs" / "codex_ac3ds-re_5_1.jsonl"
    assert log.read_text(encoding="utf-8") == fc.out
    t2 = tasks(ws)["ac3ds-re#5"]
    assert t2["codex_log"] == str(log) and t2["codex_finished_at"]
    out = capsys.readouterr().out
    assert "最後の返答です" in out and "エラー行（ERROR を含む）: 1 件" in out
    assert (wt / ".task" / "report.md").read_text(encoding="utf-8") == \
        "（ワーカーの最終の返答をオーケストラが保存した）\n最後の返答です\n"


def test_codex_keeps_existing_report_and_numbers_logs(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol")
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    (wt / ".task" / "report.md").write_text("手で書いた報告\n", encoding="utf-8")
    use_codex(monkeypatch, codex_jsonl("別の返答"))
    assert run("codex", "5") == 0
    assert run("codex", "5") == 0
    assert (wt / ".task" / "report.md").read_text(encoding="utf-8") == "手で書いた報告\n"
    assert (ws / "_local" / "logs" / "codex_ac3ds-re_5_2.jsonl").is_file()


def test_codex_failure_returns_1(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol")
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_codex(monkeypatch, "", err="ERROR 起動に失敗\n", code=1)
    assert run("codex", "5") == 1
    out = capsys.readouterr().out
    assert "終了コード: 1" in out and "agent_message が無く" in out
    assert not (wt / ".task" / "report.md").exists()
    assert tasks(ws)["ac3ds-re#5"]["codex_log"]


def test_codex_refused_for_claude_worker_or_inactive_task(env, monkeypatch):
    ws, fake, _ = env
    dispatch(env)  # sonnet-5.5（claude）
    fc = use_codex(monkeypatch, codex_jsonl("x"))
    assert run("codex", "5") == 1
    assert not fc.calls
    run("reject", "5", "--reason", "試験")
    assert run("codex", "5") == 1
    assert not fc.calls


def test_codex_dry_run_prints_only(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol")
    before = tasks(ws)["ac3ds-re#5"]
    fc = use_codex(monkeypatch, codex_jsonl("x"))
    assert run("codex", "5", "--dry-run", "--model", "gpt-6-luna", "--effort", "xhigh") == 0
    out = capsys.readouterr().out
    assert "gpt-6-luna" in out and "xhigh" in out and "--add-dir" in out
    if os.name == "nt":
        assert "unelevated" in out
    assert not fc.calls and not (ws / "_local" / "logs").exists()
    assert tasks(ws)["ac3ds-re#5"] == before


@pytest.mark.parametrize("with_idb", [True, False])
def test_codex_dry_run_sandbox_mode(env, monkeypatch, capsys, with_idb):
    ws, _, _ = env
    args = ["dispatch", "5", "--worker", "gpt-6-sol"]
    assert run(*(args + (["--idb"] if with_idb else []))) == 0
    monkeypatch.setattr(orch, "find_codex", lambda: "codex")
    assert run("codex", "5", "--dry-run") == 0
    output = capsys.readouterr().out
    command = output.split("dry-run: 実行しない。コマンド:\n", 1)[1].splitlines()[-2]
    if with_idb:
        assert "サンドボックス無し（IDA を使うタスク）" in output
        assert "--dangerously-bypass-approvals-and-sandbox" in command
        assert " -s " not in command and "--add-dir" not in command
        assert 'windows.sandbox=' not in command
    else:
        assert "サンドボックス無し" not in output
        assert "--dangerously-bypass-approvals-and-sandbox" not in command
        assert " -s workspace-write" in command
        assert subprocess.list2cmdline(["--add-dir", str(ws / "_local" / "state")]) in command
        if os.name == "nt":
            assert 'windows.sandbox=' in command


def test_codex_idb_records_git_commit_violation(env, monkeypatch, capsys):
    ws, _, _ = env
    assert run("dispatch", "5", "--worker", "gpt-6-sol", "--idb") == 0
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    monkeypatch.setattr(orch, "find_codex", lambda: "codex")

    def fake_run(c, cmd, cwd, prompt):
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        (wt / ".task" / "worker-change.txt").write_text("変更\n", encoding="utf-8")
        g(wt, "add", ".task/worker-change.txt")
        g(wt, "commit", "-m", "worker commit")
        return 0, codex_jsonl("報告"), ""

    monkeypatch.setattr(orch, "run_codex", fake_run)
    assert run("codex", "5") == 0
    output = capsys.readouterr().out
    violation = tasks(ws)["ac3ds-re#5"]["git_violation"]
    assert "警告: ワーカーが Git に書き込んだ" in output
    assert "HEAD:" in violation and "HEAD:" in output
    assert g(wt, "rev-parse", "HEAD").strip() in violation
