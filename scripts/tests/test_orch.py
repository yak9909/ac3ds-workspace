import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import orch
import wslib
from conftest import FAKE_EMAIL, REAL_ROOT

GIT = wslib.git_exe(REAL_ROOT)
ISSUE_BODY = ("### スコープ\n\nacnl\n\n### 種類\n\nanalysis\n\n### 難易度\n\nnormal\n\n"
              "### 目的と完了条件\n\nある関数を調べる\n\n### 対象\n\n0x00100000\n\n### 関係する資料\n\nなし\n")
PRECHECK = "kb.py search「ある関数」: 該当なし\n"
SCORES = "根拠と結論の一致=0.9,根拠の独立性=0.9,既存の知見との整合=0.9,確度の妥当性=0.9,名前・型の妥当性=0.9"


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
        self.issue_api = {}
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
        if a[:2] == ["issue", "list"]:
            return json.dumps([{"number": 5, "title": self.issue["title"], "body": self.issue["body"],
                                "labels": self.issue["labels"]}])
        if a[:2] == ["api", "repos/o/ac3ds-re/issues/5"]:
            return json.dumps(self.issue_api)
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
    # 実際の利用枠（~/.codex の記録など）を読むと、試験の結果が使用率で変わるので差し替える
    monkeypatch.setattr(orch.usage_watch, "collect", lambda *_, **__: {})
    monkeypatch.setattr(orch, "capacity_reports", lambda *_: {p: {"recommended": 10} for p in ("claude", "codex")})
    (ws / "_local" / "precheck.md").write_text(PRECHECK, encoding="utf-8")
    # 偽の正本 IDB
    ida = ws / "_local" / "builds" / "acnl_jpn_v15" / "ida"
    ida.mkdir(parents=True)
    (ida / "acnl_jpn_v15.i64").write_bytes(b"fake")
    return ws, fake, origin


def run(*args):
    return orch.main(list(args))


def tasks(ws):
    return json.loads((ws / "_local" / "state" / "tasks.json").read_text(encoding="utf-8"))["tasks"]


def precheck_args(ws):
    return ["--precheck", str(ws / "_local" / "precheck.md")]


def read_args(ws):
    src = ws / "_local" / "read.md"
    src.write_text("ワーカーの報告\n関係する資料の本文\n", encoding="utf-8")
    return ["--read", str(src)]


def dispatch(env, idb=True):
    ws = env[0]
    a = ["dispatch", "5", "--worker", "sonnet-5.5", *precheck_args(ws)]
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
    assert "python scripts/orch.py dispatch 12 --worker ..." in capsys.readouterr().out
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
    assert fake.named("api", "repos/o/ac3ds-re/issues/5")
    t = tasks(ws)["ac3ds-re#5"]
    wt = Path(t["worktree"])
    assert wt.is_dir() and (wt / ".task").is_dir()
    assert t["branch"].startswith("task/5-") and t["state"] == "dispatched" and t["attempts"] == 1
    cur = g(wt, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert cur == t["branch"]
    idb = Path(t["idb"]["path"])
    assert idb.read_bytes() == b"fake" and "work" in idb.parts
    text = (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    assert "ある関数を調べる" in text and str(idb) in text and "事前に引いた既存の知見: kb.py search" in text
    # 再試行は作業ツリーを再利用して試行回数が増える
    assert dispatch(env) == 0
    assert tasks(ws)["ac3ds-re#5"]["attempts"] == 2


def test_dispatch_requires_precheck_option(env, capsys):
    ws, fake, _ = env
    with pytest.raises(SystemExit):
        run("dispatch", "5", "--worker", "sonnet-5.5")
    assert "--precheck" in capsys.readouterr().err
    assert not fake.calls and not (ws / "_local" / "state" / "tasks.json").exists()


@pytest.mark.parametrize("content", [None, "", " \n\t\n"])
def test_dispatch_refuses_empty_precheck_before_any_work(env, capsys, content):
    ws, fake, _ = env
    path = ws / "_local" / "empty.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert run("dispatch", "5", "--worker", "sonnet-5.5", "--precheck", str(path)) == 1
    assert "事前確認の結果が空" in capsys.readouterr().err
    assert not fake.calls and not (ws / "_local" / "state" / "tasks.json").exists()
    assert not (ws / "_local" / "orchestrator" / "dispatch" / "5.md").exists()


def test_dispatch_auto_addresses_go_into_precheck(env, monkeypatch):
    ws, _, _ = env
    asked = []

    def fake_lookup(c, value):
        asked.append(value)
        return "KB結果 0x%08X" % value

    monkeypatch.setattr(orch, "lookup_addr", fake_lookup)
    path = ws / "_local" / "addr.md"
    # 本文の 0x00100000 と同じ値は重複させない。0x00200000 は新しい値。0x000FFFFF と 0x100 は引かない
    path.write_text("手掛かり 0x00200000 と 0x00100000、対象外 0x000FFFFF と 0x100\n", encoding="utf-8")
    assert run("dispatch", "5", "--worker", "sonnet-5.5", "--precheck", str(path)) == 0
    assert asked == [0x00100000, 0x00200000]
    text = (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    assert "手掛かり 0x00200000" in text and "自動で引いた番地" in text
    assert "- 0x00100000\n  KB結果 0x00100000" in text and "- 0x00200000\n  KB結果 0x00200000" in text


def test_auto_addresses_rules():
    texts = ("0x00100000 と 0x00100000、0x000FFFFF、0x100", "番地0x00200000。a0x00300000 と 0x0030000g", None)
    assert orch.auto_addresses(*texts) == [0x00100000, 0x00200000]
    assert orch.auto_addresses("0x00FFFFFF 0x00100000 0x01000000") == [0x00FFFFFF, 0x00100000, 0x01000000]


def test_auto_addresses_capped_at_30(env, monkeypatch):
    ws, _, _ = env
    calls = []
    monkeypatch.setattr(orch, "lookup_addr", lambda c, v: calls.append(v) or "KB")
    path = ws / "_local" / "many.md"
    path.write_text("".join("0x%08X\n" % (0x00100000 + i) for i in range(32)), encoding="utf-8")
    assert run("dispatch", "5", "--worker", "sonnet-5.5", "--precheck", str(path)) == 0
    assert len(calls) == orch.AUTO_ADDR_MAX == 30
    text = (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    assert "ほか 2 件は引かない" in text


def test_dispatch_continues_when_kb_fails(env):
    ws, _, _ = env
    (ws / "ac3ds-re" / "tools" / "kb.py").write_text(
        "import sys\nprint('試験の失敗', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8")
    assert dispatch(env) == 0
    text = (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    assert "引けなかった（kb.py が終了コード 3: 試験の失敗）" in text


def test_lookup_addr_without_ac3ds_re(tmp_path):
    c = SimpleNamespace(root=tmp_path, data=tmp_path, state=tmp_path, git=GIT)
    assert orch.lookup_addr(c, 0x00100000) == "引けなかった（ac3ds-re が無い）"


@pytest.mark.parametrize("kind", ["pr", "closed"])
def test_dispatch_rejects_pr_or_closed_issue_without_creating_files(env, monkeypatch, capsys, kind):
    ws, fake, _ = env
    if kind == "pr":
        fake.issue_api = {"pull_request": {"url": "https://api.github.com/repos/o/ac3ds-re/pulls/5"}}
    else:
        fake.issue["state"] = "CLOSED"

    def unexpected_git(*args, **kwargs):
        pytest.fail("dispatch が Git を呼んだ")

    monkeypatch.setattr(orch, "git", unexpected_git)
    monkeypatch.setattr(orch, "git_ok", unexpected_git)
    assert dispatch(env) == 1
    err = capsys.readouterr().err
    assert ("#5 は PR" if kind == "pr" else "Issue #5 は開いていない") in err
    assert fake.named("api", "repos/o/ac3ds-re/issues/5")
    assert len(fake.named("issue", "view")) == (0 if kind == "pr" else 1)
    assert not (ws / "_local" / "worktrees" / "ac3ds-re" / "5").exists()
    assert not (ws / "_local" / "builds" / "acnl_jpn_v15" / "ida" / "work" / "5").exists()
    assert not (ws / "_local" / "state" / "tasks.json").exists()
    assert not (ws / "_local" / "orchestrator" / "dispatch" / "5.md").exists()
    assert not (ws / "ac3ds-re" / ".git" / "refs" / "heads" / "task").exists()


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


def test_stamp_rejects_changed_canonical_and_open_file(env, capsys):
    ws, _, _, _ = ready(env)
    c = orch.Ctx()
    orch.record_canonical(c, "acnl_jpn_v15")
    ida = ws / "_local" / "builds" / "acnl_jpn_v15" / "ida"
    (ida / "acnl_jpn_v15.i64").write_bytes(b"changed")
    (ida / "acnl_jpn_v15.id1").write_bytes(b"")
    assert run("stamp", "5") == 0
    assert not tasks(ws)["ac3ds-re#5"]["stamp_ok"]
    out = capsys.readouterr().out
    assert "SHA256" in out and "acnl_jpn_v15.id1" in out


def test_stamp_outside_change_can_be_allowed(env, capsys):
    ws, _, _, _ = ready(env)
    main = ws / "ac3ds-re"
    # 直下の未追跡ファイルは既定で無視されるので、追跡済みのファイルを変える。
    outside = main / ".gitignore"
    outside.write_text(outside.read_text(encoding="utf-8") + "\n# worker change\n", encoding="utf-8")
    assert run("stamp", "5") == 0
    assert not tasks(ws)["ac3ds-re#5"]["stamp_ok"]
    assert ".gitignore" in capsys.readouterr().out
    assert run("stamp", "5", "--allow-outside") == 0
    assert tasks(ws)["ac3ds-re#5"]["stamp_ok"]
    assert "警告" in capsys.readouterr().out


def test_accept_order_errors(env):
    ws, fake, origin, wt = ready(env)
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1  # stamp 前
    run("stamp", "5")
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "late.md").write_text("late\n", encoding="utf-8")
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1  # stamp 後の変更
    assert not fake.named("pr", "merge")


def test_accept_dry_run_writes_nothing(env, capsys):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    before = (tasks(ws), g(wt, "rev-parse", "HEAD"), len(fake.calls))
    assert run("accept", "5", *read_args(ws), "--scores", SCORES, "--dry-run") == 0
    assert (tasks(ws), g(wt, "rev-parse", "HEAD"), len(fake.calls)) == before
    assert (wt / ".task").exists()
    assert run("reject", "5", "--reason", "x", "--dry-run") == 0
    assert len(fake.calls) == before[2]


def test_accept_full(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 0
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


def test_pause_unpause_and_refused_commands(env, capsys):
    ws, _, _ = env
    assert dispatch(env) == 0
    assert run("pause", "5", "--reason", "深い検証待ち") == 0
    task = tasks(ws)["ac3ds-re#5"]
    assert task["state"] == "paused" and task["paused_from"] == "dispatched"
    assert task["pause_reason"] == "深い検証待ち"
    assert run("status") == 0
    assert "保留" in capsys.readouterr().out
    assert run("start") == 0
    out = capsys.readouterr().out
    assert "== 保留中のタスク ==" in out and "ac3ds-re#5" in out
    assert "== 動いているタスク ==\nなし" in out
    for args in (("save", "5"), ("stamp", "5"), ("accept", "5", *read_args(ws), "--scores", SCORES),
                 ("codex", "5")):
        assert run(*args) == 1
        assert "先に unpause" in capsys.readouterr().err
    assert run("wrapup") == 0
    assert tasks(ws)["ac3ds-re#5"]["state"] == "paused"
    assert "動いているタスクなし" in capsys.readouterr().out
    assert run("unpause", "5") == 0
    task = tasks(ws)["ac3ds-re#5"]
    assert task["state"] == "dispatched" and task["paused_from"] is None
    assert run("pause", "5", "--reason", "再度保留") == 0
    assert run("unpause", "5") == 0


def test_paused_task_not_counted_for_dispatch(env, monkeypatch):
    ws, fake, _ = env
    assert dispatch(env) == 0
    assert run("pause", "5", "--reason", "待機") == 0
    original_gh = orch.gh_run
    monkeypatch.setattr(orch, "gh_run", lambda args, input_text=None:
                        "{}" if args[:2] == ["api", "repos/o/ac3ds-re/issues/6"]
                        else original_gh(args, input_text))
    monkeypatch.setattr(orch, "read_issue", lambda *args: dict(fake.issue))
    monkeypatch.setattr(orch, "capacity_reports", lambda *args: {"claude": {"recommended": 1}})

    class ReachedWorktree(Exception):
        pass

    def stop_before_git(*args, **kwargs):
        raise ReachedWorktree

    monkeypatch.setattr(orch, "git", stop_before_git)
    with pytest.raises(ReachedWorktree):
        run("dispatch", "6", "--worker", "sonnet-5.5", *precheck_args(ws))
    assert tasks(ws)["ac3ds-re#5"]["state"] == "paused"


@pytest.mark.parametrize("changed", [True, False])
def test_accept_syncs_skills_after_merge(env, monkeypatch, changed):
    ws, _, _ = env
    ready(env)
    assert run("stamp", "5") == 0
    original_git = orch.git

    def git_with_skills(c, cwd, *args, **kwargs):
        if args == ("diff", "--name-only", "origin/main^", "origin/main", "--", "skills"):
            return "skills/example/SKILL.md\n" if changed else ""
        return original_git(c, cwd, *args, **kwargs)

    monkeypatch.setattr(orch, "git", git_with_skills)
    monkeypatch.setattr(orch.sync_skills, "check", lambda root: ["workspace skills mismatch"])
    calls = []
    monkeypatch.setattr(orch.sync_skills, "sync", lambda root: calls.append(root) or [])
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 0
    assert calls == [ws]


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
    h.write_text("# 引き継ぎメモ（司令塔）\n\n## 古い節\n- a\n", encoding="utf-8")
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
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 0, capsys.readouterr().err
    assert len(fake.named("pr", "checks")) == 4
    assert fake.named("pr", "merge")


def test_accept_waits_until_pr_head_is_pushed_commit(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.view_lag = 2
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 0
    assert len(fake.named("pr", "view")) >= 3


def test_accept_stops_when_required_check_never_appears(env, capsys, monkeypatch):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    monkeypatch.setattr(orch, "CHECK_APPEAR_SEC", 0)
    fake.checks_script = [None]
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1
    assert "必須: audit" in capsys.readouterr().err
    assert not fake.named("pr", "merge")
    assert tasks(ws)["ac3ds-re#5"]["state"] == "stamped"


def test_accept_without_required_checks_warns(env, capsys, monkeypatch):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    monkeypatch.setattr(orch, "CHECK_APPEAR_SEC", 0)
    fake.protection = None
    fake.checks_script = [None]
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 0
    assert "警告: チェックが1つも無い" in capsys.readouterr().out
    assert fake.named("pr", "merge")


def test_accept_resumes_after_its_own_prep_commit(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.fail["pr ready"] = 1
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1  # 準備のコミットを作った後で失敗
    assert tasks(ws)["ac3ds-re#5"]["state"] == "stamped"
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 0
    assert tasks(ws)["ac3ds-re#5"]["state"] == "accepted"


def test_accept_refuses_change_after_prep_commit(env):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    fake.fail["pr ready"] = 1
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "x.md").write_text("x\n", encoding="utf-8")
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1
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
    assert rep.read_bytes().decode("utf-8") == "（ワーカーの最終の返答を司令塔が保存した）\n# 報告\n\n結論: 試験\n"
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
    assert run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws)) == 0
    t = tasks(ws)["ac3ds-re#5"]
    wt = Path(t["worktree"])
    assert t["provider"] == "codex" and t["worker"] == "gpt-6-sol"
    fc = use_codex(monkeypatch, codex_jsonl("途中の文", "最後の返答です"), err="WARN 注意\nERROR 試験のエラー\n")
    assert run("codex", "5") == 0
    cmd, cwd, prompt = fc.calls[0]
    assert cwd == wt
    assert cmd[:2] == ["codex", "exec"] and cmd[cmd.index("-C") + 1] == str(wt)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "-s" not in cmd and "--add-dir" not in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-6-sol"
    assert 'model_reasoning_effort="medium"' in cmd and cmd[-2:] == ["--json", "-"]
    assert 'windows.sandbox="unelevated"' not in cmd
    assert prompt == (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    log = ws / "_local" / "logs" / "codex_ac3ds-re_5_1.jsonl"
    assert log.read_text(encoding="utf-8") == fc.out
    t2 = tasks(ws)["ac3ds-re#5"]
    assert t2["codex_log"] == str(log) and t2["codex_finished_at"]
    out = capsys.readouterr().out
    assert "最後の返答です" in out and "エラー行（ERROR を含む）: 1 件" in out
    assert (wt / ".task" / "report.md").read_text(encoding="utf-8") == \
        "（ワーカーの最終の返答を司令塔が保存した）\n最後の返答です\n"


def test_codex_stopped_by_guard_keeps_log_and_resumable(env, capsys, monkeypatch):
    """実行中に利用枠が閾値に達したら止める。台帳と終了コードが合い、止めるまでの出力で --resume できる。"""
    ws, fake, _ = env
    monkeypatch.setenv("AC3DS_GUARD_THRESHOLD", "80")
    monkeypatch.setattr(orch.usage_watch, "GUARD_INTERVAL_SEC", 0.5)
    monkeypatch.setattr(orch.usage_watch, "five_hour_percent", lambda *_: 85)
    assert run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws)) == 0
    monkeypatch.setattr(orch, "find_codex", lambda: "codex")
    child = ("import json, time\n"
             "print(json.dumps({'type': 'thread.started', 'thread_id': 't9'}), flush=True)\n"
             "time.sleep(60)\n")
    monkeypatch.setattr(orch, "codex_cmd", lambda c, wt, exe, model, effort, *rest: [sys.executable, "-c", child])
    assert run("codex", "5") == orch.CODEX_STOPPED_RC
    out = capsys.readouterr().out
    assert "利用枠の 85% で Codex を止めた。続きは orch.py codex --resume で頼む" in out
    t = tasks(ws)["ac3ds-re#5"]
    assert t["codex_stopped_at_usage"] == 85 and t["state"] == "dispatched"
    log = Path(t["codex_log"])
    assert orch.codex_thread_id(log.read_text(encoding="utf-8")) == "t9"
    wt = Path(t["worktree"])
    assert not (wt / ".task" / "report.md").exists()  # 止めた回は報告を保存しない


def test_codex_keeps_existing_report_and_numbers_logs(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    (wt / ".task" / "report.md").write_text("手で書いた報告\n", encoding="utf-8")
    use_codex(monkeypatch, codex_jsonl("別の返答"))
    assert run("codex", "5") == 0
    assert run("codex", "5") == 0
    assert (wt / ".task" / "report.md").read_text(encoding="utf-8") == "手で書いた報告\n"
    assert (ws / "_local" / "logs" / "codex_ac3ds-re_5_2.jsonl").is_file()


def test_codex_failure_returns_1(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_codex(monkeypatch, "", err="ERROR 起動に失敗\n", code=1)
    assert run("codex", "5") == 1
    out = capsys.readouterr().out
    assert "終了コード: 1" in out and "agent_message が無く" in out
    assert not (wt / ".task" / "report.md").exists()
    assert tasks(ws)["ac3ds-re#5"]["codex_log"]


class FakeClaude:
    """orch.run_claude の代わり。起動せず、決めた結果を返す。"""

    def __init__(self, out, err="", code=0, stopped=False, used=None, writes_report=None):
        self.out, self.err, self.code, self.stopped, self.used = out, err, code, stopped, used
        self.writes_report = writes_report
        self.calls = []

    def __call__(self, c, cmd, cwd, prompt, log):
        self.calls.append((list(cmd), Path(cwd), prompt))
        log.write_text(self.out, encoding="utf-8")
        if self.writes_report:
            (Path(cwd) / ".task" / "report.md").write_text(self.writes_report, encoding="utf-8")
        return orch.usage_watch.GuardRun(self.code, self.out, self.err, self.stopped, self.used)


def use_claude(monkeypatch, ws, out, **kw):
    agents = ws / ".claude" / "agents"
    if not agents.exists():
        shutil.copytree(REAL_ROOT / ".claude" / "agents", agents)
    monkeypatch.setattr(orch, "find_claude", lambda: "claude")
    fc = FakeClaude(out, **kw)
    monkeypatch.setattr(orch, "run_claude", fc)
    return fc


def claude_jsonl(*results, is_error=False, subtype="success"):
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "s"})]
    lines += [json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "result": m, "duration_api_ms": 60000})
              for m in results]
    return "\n".join(lines) + "\n"


def test_claude_runs_as_main_session_and_saves_the_last_result(env, capsys, monkeypatch):
    ws, fake, _ = env
    assert run("dispatch", "5", "--worker", "haiku-5.5", "--effort", "xhigh", *precheck_args(ws)) == 0
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    fc = use_claude(monkeypatch, ws, claude_jsonl("試験を待ちます", "最後の報告です"))
    assert run("claude", "5") == 0
    cmd, cwd, prompt = fc.calls[0]
    assert cwd == wt and cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5" and cmd[cmd.index("--effort") + 1] == "xhigh"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd
    # サブエージェントを持たせず、IDA を使わない作業では MCP を起動しない。
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Agent" in denied and "Task" in denied and "Monitor" in denied
    assert "--strict-mcp-config" in cmd
    # 親の CLAUDE.md の司令塔の段落を自分の事と読まない。定義の本文も渡す（front matter は渡さない）。
    system = cmd[cmd.index("--append-system-prompt") + 1]
    # ac3ds-re は analysis なので re-analyst の本文が入る（front matter は入らない）
    assert "あなたは司令塔ではありません" in system and "回答や思考は日本語で行う" in system and "name: re-analyst" not in system
    # 道具の一覧と費用の上限を渡す。
    assert cmd[cmd.index("--tools") + 1] == "Read,Edit,Write,Grep,Glob,Bash,PowerShell"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "5"
    t = tasks(ws)["ac3ds-re#5"]
    assert cmd[cmd.index("--session-id") + 1] == t["claude_session"]
    assert prompt == (ws / "_local" / "orchestrator" / "dispatch" / "5.md").read_text(encoding="utf-8")
    assert t["claude_finished_at"] and Path(t["claude_log"]) == ws / "_local" / "logs" / "claude_ac3ds-re_5_1.jsonl"
    assert "最後の報告です" in capsys.readouterr().out
    assert (wt / ".task" / "report.md").read_text(encoding="utf-8") == \
        "（ワーカーの最終の返答を司令塔が保存した）\n最後の報告です\n"


def test_claude_resume_uses_the_same_session_and_appends_the_report(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_claude(monkeypatch, ws, claude_jsonl("一回目"))
    assert run("claude", "5") == 0
    session = tasks(ws)["ac3ds-re#5"]["claude_session"]
    message = ws / "_local" / "followup.md"
    message.write_text("直してください\n", encoding="utf-8")
    fc = use_claude(monkeypatch, ws, claude_jsonl("二回目"))
    assert run("claude", "5", "--resume", "--message-file", str(message)) == 0
    cmd, _, prompt = fc.calls[0]
    assert cmd[cmd.index("--resume") + 1] == session and "--session-id" not in cmd and prompt == "直してください\n"
    assert tasks(ws)["ac3ds-re#5"]["claude_session"] == session
    text = (wt / ".task" / "report.md").read_text(encoding="utf-8")
    assert "一回目" in text and "続きの実行の報告" in text and text.rstrip().endswith("二回目")
    assert (ws / "_local" / "logs" / "claude_ac3ds-re_5_2.jsonl").is_file()


def test_claude_keeps_the_report_the_worker_wrote(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_claude(monkeypatch, ws, claude_jsonl("完了しました"), writes_report="自分で書いた報告\n")
    assert run("claude", "5") == 0
    assert (wt / ".task" / "report.md").read_text(encoding="utf-8") == "自分で書いた報告\n"


def test_claude_stopped_by_guard_is_resumable(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_claude(monkeypatch, ws, "", stopped=True, used=85.0, code=1)
    assert run("claude", "5") == orch.CODEX_STOPPED_RC
    assert "続きは orch.py claude --resume" in capsys.readouterr().out
    t = tasks(ws)["ac3ds-re#5"]
    assert t["claude_stopped_at_usage"] == 85.0 and t["claude_session"] and t["state"] == "dispatched"
    assert not (wt / ".task" / "report.md").exists()


def test_claude_error_result_is_not_saved_as_report(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_claude(monkeypatch, ws, claude_jsonl("利用枠の上限です", is_error=True, subtype="error_during_execution"))
    assert run("claude", "5") == 1
    assert "error_during_execution" in capsys.readouterr().out
    assert not (wt / ".task" / "report.md").exists()


def test_claude_refused_over_limit_for_codex_worker_and_without_session(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws))
    fc = use_claude(monkeypatch, ws, claude_jsonl("x"))
    assert run("claude", "5") == 1  # Codex のワーカーには使わない
    assert not fc.calls
    run("reject", "5", "--reason", "試験")
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    message = ws / "_local" / "followup.md"
    message.write_text("続き\n", encoding="utf-8")
    assert run("claude", "5", "--resume", "--message-file", str(message)) == 1  # 続ける相手が無い
    assert run("claude", "5", "--resume") == 1  # 伝える事が無い
    monkeypatch.setattr(orch.capacity, "read_provider_usage", lambda *_: {"five_hour_used_percent": 85})
    assert run("claude", "5") == 1  # 上限以上では起動しない
    assert not fc.calls
    assert "85" in capsys.readouterr().err


def review_message(ws):
    message = ws / "_local" / "review-request.md"
    message.write_text("記録を確かめてください\n", encoding="utf-8")
    return str(message)


def test_review_starts_a_new_session_and_leaves_the_worker_alone(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_claude(monkeypatch, ws, claude_jsonl("ワーカーの報告"))
    assert run("claude", "5") == 0
    worker_session = tasks(ws)["ac3ds-re#5"]["claude_session"]
    report_before = (wt / ".task" / "report.md").read_bytes()
    fc = use_claude(monkeypatch, ws, claude_jsonl("判定: 確認"))
    assert run("review", "5", "--effort", "high", "--message-file", review_message(ws)) == 0
    cmd, cwd, prompt = fc.calls[0]
    assert cwd == wt and prompt == "記録を確かめてください\n"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5" and cmd[cmd.index("--effort") + 1] == "high"
    system = cmd[cmd.index("--append-system-prompt") + 1]
    assert "あなたは司令塔ではありません" in system and "あなたは検証役です" in system
    assert "Agent" in cmd[cmd.index("--disallowedTools") + 1].split(",")
    t = tasks(ws)["ac3ds-re#5"]
    assert t["claude_session"] == worker_session and t["review_session"] == cmd[cmd.index("--session-id") + 1]
    assert t["review_session"] != worker_session
    assert Path(t["review_log"]) == ws / "_local" / "logs" / "review_ac3ds-re_5_1.jsonl" and t["review_finished_at"]
    assert (wt / ".task" / "report.md").read_bytes() == report_before
    assert (ws / "_local" / "orchestrator" / "review" / "5.md").read_text(encoding="utf-8") == "判定: 確認\n"
    assert "accept --reviewed" in capsys.readouterr().out


def test_review_resume_uses_the_same_session_and_keeps_the_first_report(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    fc = use_claude(monkeypatch, ws, claude_jsonl("x"))
    assert run("review", "5", "--effort", "high", "--resume", "--message-file", review_message(ws)) == 1  # 続ける相手が無い
    assert not fc.calls
    use_claude(monkeypatch, ws, claude_jsonl("判定: 一部確認"))
    assert run("review", "5", "--effort", "high", "--message-file", review_message(ws)) == 0
    session = tasks(ws)["ac3ds-re#5"]["review_session"]
    fc = use_claude(monkeypatch, ws, claude_jsonl("判定: 確認"))
    assert run("review", "5", "--effort", "high", "--resume", "--message-file", review_message(ws)) == 0
    cmd = fc.calls[0][0]
    assert cmd[cmd.index("--resume") + 1] == session and "--session-id" not in cmd
    text = (ws / "_local" / "orchestrator" / "review" / "5.md").read_text(encoding="utf-8")
    assert "判定: 一部確認" in text and "続きの実行の報告" in text and text.rstrip().endswith("判定: 確認")


def test_review_stopped_error_and_limit_save_no_report(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws))  # Codex のワーカーの結果も検証できる
    report = ws / "_local" / "orchestrator" / "review" / "5.md"
    use_claude(monkeypatch, ws, "", stopped=True, used=85.0, code=1)
    assert run("review", "5", "--effort", "high", "--message-file", review_message(ws)) == orch.CODEX_STOPPED_RC
    assert "続きは orch.py review --resume" in capsys.readouterr().out and not report.exists()
    assert tasks(ws)["ac3ds-re#5"]["review_stopped_at_usage"] == 85.0
    use_claude(monkeypatch, ws, claude_jsonl("上限です", is_error=True, subtype="error_during_execution"))
    assert run("review", "5", "--effort", "high", "--message-file", review_message(ws)) == 1 and not report.exists()
    fc = use_claude(monkeypatch, ws, claude_jsonl("x"))
    monkeypatch.setattr(orch.capacity, "read_provider_usage", lambda *_: {"five_hour_used_percent": 85})
    assert run("review", "5", "--effort", "high", "--message-file", review_message(ws)) == 1 and not fc.calls
    assert "85" in capsys.readouterr().err


def test_review_warns_when_files_change_and_dry_run_prints_only(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", "--idb", *precheck_args(ws))
    before = tasks(ws)["ac3ds-re#5"]
    fc = use_claude(monkeypatch, ws, claude_jsonl("判定: 確認"), writes_report="書き換えた\n")
    assert run("review", "5", "--dry-run", "--effort", "medium", "--message-file", review_message(ws)) == 0
    out = capsys.readouterr().out
    assert "claude-opus-5-5" in out and "--strict-mcp-config" not in out  # IDB の作業では MCP を残す
    assert not fc.calls and tasks(ws)["ac3ds-re#5"] == before
    assert run("review", "5", "--effort", "medium", "--message-file", review_message(ws)) == 0
    assert "作業ツリーのファイルを変えた" in capsys.readouterr().out


def test_claude_dry_run_prints_only_and_idb_task_keeps_mcp(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "sonnet-5.5", "--idb", *precheck_args(ws))
    before = tasks(ws)["ac3ds-re#5"]
    fc = use_claude(monkeypatch, ws, claude_jsonl("x"))
    assert run("claude", "5", "--dry-run", "--model", "claude-opus-5-5", "--effort", "high") == 0
    out = capsys.readouterr().out
    assert "claude-opus-5-5" in out and "--strict-mcp-config" not in out
    assert "ToolSearch" in out  # IDA を使う作業では MCP の道具を探す道具も足す
    assert not fc.calls and tasks(ws)["ac3ds-re#5"] == before


def test_claude_env_drops_the_parent_session(env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "x")
    child = orch.claude_env(orch.Ctx())
    assert "CLAUDECODE" not in child and "CLAUDE_CODE_ENTRYPOINT" not in child and child["CLAUDE_CONFIG_DIR"] == "x"
    # 文脈の窓とファイル読み取りの上限は、親の CLAUDE* を落とした後で足す。
    assert child["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "100000"
    assert child["CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"] == "8000"


def test_claude_new_session_from_message_file_without_resume(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    use_claude(monkeypatch, ws, claude_jsonl("一回目"))
    assert run("claude", "5") == 0
    first = tasks(ws)["ac3ds-re#5"]["claude_session"]
    message = ws / "_local" / "redo.md"
    message.write_text("最初からやり直してください\n", encoding="utf-8")
    fc = use_claude(monkeypatch, ws, claude_jsonl("二回目"))
    assert run("claude", "5", "--message-file", str(message)) == 0
    cmd, _, prompt = fc.calls[0]
    assert prompt == "最初からやり直してください\n" and "--resume" not in cmd
    second = cmd[cmd.index("--session-id") + 1]
    assert second != first and tasks(ws)["ac3ds-re#5"]["claude_session"] == second


def test_claude_stopped_by_budget_is_resumable_and_keeps_no_report(env, monkeypatch, capsys):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    use_claude(monkeypatch, ws, claude_jsonl("途中です", is_error=True, subtype="error_max_budget_usd"))
    assert run("claude", "5") == orch.CODEX_STOPPED_RC
    assert "--resume" in capsys.readouterr().out
    assert tasks(ws)["ac3ds-re#5"]["claude_session"] and not (wt / ".task" / "report.md").exists()


def test_claude_log_stats_dedups_requests_and_ignores_output_tokens(tmp_path):
    def asst(mid, inp, cache_c, cache_r, out):
        return json.dumps({"type": "assistant", "message": {"id": mid, "usage": {
            "input_tokens": inp, "cache_creation_input_tokens": cache_c,
            "cache_read_input_tokens": cache_r, "output_tokens": out}}})
    log = tmp_path / "claude_ac3ds-re_5_1.jsonl"
    log.write_text("\n".join([
        asst("m1", 10, 100, 0, 1), asst("m1", 10, 100, 0, 999),  # 同じ要求が 2 行に分かれる
        asst("m2", 5, 0, 300, 2),
        json.dumps({"type": "result", "total_cost_usd": 0.5}),
        json.dumps({"type": "result", "total_cost_usd": 1.25}),
        "not json"]) + "\n", encoding="utf-8")
    s = orch.claude_log_stats(log)
    assert s == {"cost": 1.25, "requests": 2, "max_context": 305, "cumulative": False}
    log2 = tmp_path / "claude_ac3ds-re_5_2.jsonl"
    log2.write_text(asst("m3", 1, 0, 0, 1) + "\n" + json.dumps({"type": "result", "total_cost_usd": 0.75}) + "\n",
                    encoding="utf-8")
    t = orch.task_claude_stats(tmp_path, "ac3ds-re#5")
    assert t == {"cost": 2.0, "requests": 3, "max_context": 305}
    assert orch.task_claude_stats(tmp_path, "ac3ds-re#6")["requests"] == 0


def _cost_log(path, cost, asst_cache, model_cache):
    lines = [json.dumps({"type": "assistant", "message": {"id": "m", "usage": {"cache_read_input_tokens": asst_cache}}}),
             json.dumps({"type": "assistant", "message": {"id": "m", "usage": {"cache_read_input_tokens": asst_cache}}}),
             json.dumps({"type": "result", "total_cost_usd": cost,
                         "modelUsage": {"x": {"cacheReadInputTokens": model_cache}}})]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_task_claude_stats_counts_cumulative_resume_cost_once(tmp_path):
    # 累積（modelUsage のキャッシュ読み出しがログの assistant の合計より大きい）
    _cost_log(tmp_path / "claude_r_72_1.jsonl", 4.31, 100, 100)
    _cost_log(tmp_path / "claude_r_72_2.jsonl", 6.86, 50, 150)
    assert round(orch.task_claude_stats(tmp_path, "r#72")["cost"], 2) == 6.86
    # 前の起動が止められ累積にならなかった _2、その累積の _3
    _cost_log(tmp_path / "claude_r_83_1.jsonl", 0.67, 100, 100)
    _cost_log(tmp_path / "claude_r_83_2.jsonl", 0.47, 80, 80)
    _cost_log(tmp_path / "claude_r_83_3.jsonl", 0.79, 20, 100)
    assert round(orch.task_claude_stats(tmp_path, "r#83")["cost"], 2) == 1.46


def test_claude_resume_budget_adds_previous_cumulative_cost(env, monkeypatch):
    ws, fake, _ = env
    run("dispatch", "5", "--worker", "haiku-5.5", *precheck_args(ws))
    first_log = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "一回目",
                            "total_cost_usd": 2.5}) + "\n"
    fc = use_claude(monkeypatch, ws, first_log)
    assert run("claude", "5") == 0
    cmd = fc.calls[0][0]
    first = float(cmd[cmd.index("--max-budget-usd") + 1])
    message = ws / "_local" / "more.md"
    message.write_text("続き\n", encoding="utf-8")
    fc = use_claude(monkeypatch, ws, claude_jsonl("二回目"))
    assert run("claude", "5", "--resume", "--message-file", str(message)) == 0
    cmd = fc.calls[0][0]
    assert float(cmd[cmd.index("--max-budget-usd") + 1]) == pytest.approx(first + 2.5)


def test_stats_rows_shows_saves_not_next_commit_number_and_cost(tmp_path):
    (tmp_path / "claude_ac3ds-re_5_1.jsonl").write_text(
        json.dumps({"type": "result", "total_cost_usd": 0.3}) + "\n", encoding="utf-8")
    rows = orch.stats_rows({"ac3ds-re#5": {"worker": "m", "state": "accepted", "attempts": 3}}, tmp_path)
    assert rows["m"]["saves"] == 2 and rows["m"]["attempts"] == 3 and rows["m"]["cost"] == 0.3


def test_save_pushes_and_creates_pr_when_only_unpushed_commits(env):
    ws, fake, _ = env
    dispatch(env)
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    g(wt, "add", "-A")
    g(wt, "commit", "-m", "ワーカーが自分でコミット")
    assert run("save", "5") == 0  # 作業ツリーは綺麗だが、push も PR も済んでいない
    assert len(fake.named("pr", "create")) == 1
    assert tasks(ws)["ac3ds-re#5"]["pr"] == 7
    assert run("save", "5") == 0  # 今度は何もしない
    assert len(fake.named("pr", "create")) == 1


def test_verify_writes_pack_and_reports_pass_fail(env, capsys):
    ws, fake, _ = env
    dispatch(env)
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    (wt / "docs").mkdir(exist_ok=True)
    (wt / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    (wt / ".task").mkdir(exist_ok=True)
    (wt / ".task" / "report.md").write_text("報告の本文\n", encoding="utf-8")
    toml = ws / "workspace.toml"
    text = toml.read_text(encoding="utf-8")
    toml.write_text(text.replace('[repos.ac3ds-re]', '[repos.ac3ds-re]\nverify = ["python -c \\"print(1)\\"", '
                                 '"python -c \\"import sys; sys.exit(3)\\"", "python -c \\"print(0)\\" {files}"]', 1),
                    encoding="utf-8")
    assert run("verify", "5") == 1
    out = capsys.readouterr().out
    assert "不合格" in out and "終了コード 3" in out
    pack = ws / "_local" / "orchestrator" / "pack" / "ac3ds-re-5.md"
    assert str(pack) in out
    body = pack.read_text(encoding="utf-8")
    assert "終了コード: 3" in body and "git diff --stat" in body and "docs/a.md" in body and "報告の本文" in body
    # 画面には合否の要約とパスだけ（差分や報告の本文は出さない）
    assert "報告の本文" not in out and "+a" not in out


def test_worker_agent_follows_repo_role():
    from types import SimpleNamespace
    c = SimpleNamespace(ws={"repos": {"a": {"role": "analysis"}, "d": {"role": "derived"}}})
    assert orch.worker_agent(c, "a") == "re-analyst" and orch.worker_agent(c, "d") == "tooling"


def test_stats_summary_lists_models(env):
    ws, fake, _ = env
    dispatch(env)
    c = orch.Ctx()
    assert any("件数 1" in l for l in orch.stats_summary(c))


def test_verify_requires_commands(env):
    ws, fake, _ = env
    dispatch(env)
    toml = ws / "workspace.toml"
    toml.write_text("\n".join(l for l in toml.read_text(encoding="utf-8").splitlines()
                              if not l.startswith("verify")) + "\n", encoding="utf-8")
    assert run("verify", "5") == 1


def test_last_claude_result_takes_the_last():
    assert orch.last_claude_result(claude_jsonl("一", "二"))["result"] == "二"
    assert orch.last_claude_result('{"type":"assistant"}\nnot json\n') is None


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
    run("dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws))
    before = tasks(ws)["ac3ds-re#5"]
    fc = use_codex(monkeypatch, codex_jsonl("x"))
    assert run("codex", "5", "--dry-run", "--model", "gpt-6-luna", "--effort", "xhigh") == 0
    out = capsys.readouterr().out
    assert "gpt-6-luna" in out and "xhigh" in out and "--dangerously-bypass-approvals-and-sandbox" in out
    assert "--add-dir" not in out and "unelevated" not in out
    assert not fc.calls and not (ws / "_local" / "logs").exists()
    assert tasks(ws)["ac3ds-re#5"] == before


@pytest.mark.parametrize("with_idb", [True, False])
def test_codex_dry_run_always_full_access(env, monkeypatch, capsys, with_idb):
    ws, _, _ = env
    args = ["dispatch", "5", "--worker", "gpt-6-sol", *precheck_args(ws)]
    assert run(*(args + (["--idb"] if with_idb else []))) == 0
    monkeypatch.setattr(orch, "find_codex", lambda: "codex")
    assert run("codex", "5", "--dry-run") == 0
    output = capsys.readouterr().out
    command = output.split("dry-run: 実行しない。コマンド:\n", 1)[1].splitlines()[-2]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert " -s " not in command and "--add-dir" not in command
    assert 'windows.sandbox=' not in command and "writable_roots" not in command


@pytest.fixture
def codex_unit(tmp_path, monkeypatch):
    class Context:
        root = tmp_path
        data = tmp_path / "_local"
        state = data / "state"

    c = Context()
    c.state.mkdir(parents=True)
    wt = tmp_path / "worktree"
    wt.mkdir()
    task = {"repo": "ac3ds-re", "issue": 5, "provider": "codex", "state": "dispatched",
            "worker": "gpt-6-sol", "effort": "medium", "worktree": str(wt), "idb": None}
    monkeypatch.setattr(orch, "Ctx", lambda: c)
    monkeypatch.setattr(orch, "need_repo", lambda *_: {})
    monkeypatch.setattr(orch, "get_task", lambda *_: task)
    monkeypatch.setattr(orch, "update_task", lambda _c, _key, **fields: task.update(fields))
    monkeypatch.setattr(orch, "find_codex", lambda: "codex")
    return c, wt, task


@pytest.mark.parametrize("with_idb", [False, True])
def test_codex_resume_uses_last_log_and_message_file(codex_unit, monkeypatch, tmp_path, capsys, with_idb):
    c, wt, task = codex_unit
    previous = tmp_path / "previous.jsonl"
    previous.write_text(codex_jsonl("前の返答"), encoding="utf-8")
    task["codex_log"] = str(previous)
    if with_idb:
        task["idb"] = {"path": str(tmp_path / "work.i64")}
        states = iter([{"HEAD": "before"}, {"HEAD": "after"}])
        monkeypatch.setattr(orch, "git_launch_state", lambda *_: next(states))
    message = tmp_path / "message.txt"
    message.write_text("続きの依頼\n", encoding="utf-8")
    fc = use_codex(monkeypatch, codex_jsonl("続きの返答"))
    assert orch.main(["codex", "5", "--resume", "--message-file", str(message)]) == 0
    cmd, cwd, prompt = fc.calls[0]
    assert cmd[:4] == ["codex", "exec", "resume", "t1"]
    assert cwd == wt and prompt == "続きの依頼\n"
    assert "-C" not in cmd and "-s" not in cmd and "--add-dir" not in cmd
    assert cmd[-2:] == ["--json", "-"]
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert not any("sandbox_workspace_write.writable_roots" in arg for arg in cmd)
    assert 'windows.sandbox="unelevated"' not in cmd
    if with_idb:
        assert "HEAD:" in task["git_violation"]
    log = c.data / "logs" / "codex_ac3ds-re_5_1.jsonl"
    assert log.read_text(encoding="utf-8") == fc.out
    assert task["codex_log"] == str(log) and task["codex_finished_at"]
    assert "続きの返答" in capsys.readouterr().out


def test_codex_resume_accepts_stdin(codex_unit, monkeypatch):
    _, _, task = codex_unit
    previous = Path(task["worktree"]) / "previous.jsonl"
    previous.write_text(codex_jsonl("前"), encoding="utf-8")
    task["codex_log"] = str(previous)
    monkeypatch.setattr(sys, "stdin", io.StringIO("標準入力の依頼\n"))
    fc = use_codex(monkeypatch, codex_jsonl("後"))
    assert orch.main(["codex", "5", "--resume", "--message-file", "-"]) == 0
    assert fc.calls[0][2] == "標準入力の依頼\n"


def test_codex_resume_refuses_missing_log(codex_unit, monkeypatch, tmp_path, capsys):
    message = tmp_path / "message.txt"
    message.write_text("続き", encoding="utf-8")
    fc = use_codex(monkeypatch, codex_jsonl("実行しない"))
    assert orch.main(["codex", "5", "--resume", "--message-file", str(message)]) == 1
    assert "ログが無い" in capsys.readouterr().err
    assert not fc.calls


def test_codex_idb_records_git_commit_violation(env, monkeypatch, capsys):
    ws, _, _ = env
    assert run("dispatch", "5", "--worker", "gpt-6-sol", "--idb", *precheck_args(ws)) == 0
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


# ---------- 採点の項目と合格の基準（accept）----------
ITEMS = orch.EVAL_ITEMS["analysis"]
DERIVED_SCORES = "完了条件の達成=0.9,設計書との整合=0.9,既存の知見との整合=0.9,試験の妥当性=0.9,決まりの順守=0.9"


def test_eval_items_are_the_two_rubrics():
    assert ITEMS == ("根拠と結論の一致", "根拠の独立性", "既存の知見との整合", "確度の妥当性", "名前・型の妥当性")
    assert orch.EVAL_ITEMS["derived"] == ("完了条件の達成", "設計書との整合", "既存の知見との整合", "試験の妥当性", "決まりの順守")


@pytest.mark.parametrize("role,items", [("analysis", ITEMS), ("derived", orch.EVAL_ITEMS["derived"])])
def test_check_scores_accepts_items_of_the_role(role, items):
    orch.check_scores({k: 0.9 for k in items}, role, 0.6)
    orch.check_scores({k: 0.6 for k in items}, role, 0.6)  # 基準ちょうどは合格


@pytest.mark.parametrize("scores,role,msg", [
    ({k: 0.9 for k in ITEMS[:4]}, "analysis", "不足"),                              # 項目が足りない
    ({**{k: 0.9 for k in ITEMS}, "正確さ": 0.9}, "analysis", "余分"),               # 余分な項目
    ({k: 0.9 for k in orch.EVAL_ITEMS["derived"]}, "analysis", "不足"),           # 派生の項目を解析に付けた
    ({**{k: 0.9 for k in ITEMS}, ITEMS[0]: 1.5}, "analysis", "0〜1"),
    ({**{k: 0.9 for k in ITEMS}, ITEMS[0]: -0.1}, "analysis", "0〜1"),
    ({**{k: 0.9 for k in ITEMS}, ITEMS[0]: float("nan")}, "analysis", "0〜1"),
    ({**{k: 0.9 for k in ITEMS}, ITEMS[0]: 0.5}, "analysis", "合格の基準"),
    ({k: 0.9 for k in ITEMS}, "tooling", "採点表が無い役"),
])
def test_check_scores_refuses(scores, role, msg):
    with pytest.raises(orch.OrchError, match=msg):
        orch.check_scores(scores, role, 0.6)


def test_parse_scores_rejects_duplicate_names():
    with pytest.raises(orch.OrchError, match="重複"):
        orch.parse_scores("a=0.9,a=0.8")


def test_pass_min_comes_from_workers_toml():
    assert orch.pass_min(SimpleNamespace(workers={"evaluation": {"pass_min": 0.6}})) == 0.6
    with pytest.raises(orch.OrchError, match="pass_min"):
        orch.pass_min(SimpleNamespace(workers={}))


def test_accept_refuses_wrong_score_names(env, capsys):
    ws, fake, origin, wt = ready(env)
    run("stamp", "5")
    assert run("accept", "5", *read_args(ws), "--scores", "a=0.9") == 1
    assert "採点の項目が違う" in capsys.readouterr().err
    assert not fake.named("pr", "merge")


# ---------- 派生の Issue に解析の情報を書かない（issue）----------
def add_derived_repo(ws, api_text=None):
    with (ws / "workspace.toml").open("a", encoding="utf-8") as f:
        f.write('\n[repos.gohan]\nrole = "derived"\nremote = "https://github.com/yak9909/gohan.git"\n')
    (ws / "gohan").mkdir()
    if api_text is not None:
        (ws / "gohan" / "spec").mkdir()
        (ws / "gohan" / "spec" / "game_api.toml").write_text(api_text, encoding="utf-8")


@pytest.mark.parametrize("goal", ["ACNL-F0123 を参照", "hhd-f0001 の続き", "ac3ds-re の記録を見る"])
def test_derived_issue_refuses_analysis_ids_and_names(env, capsys, goal):
    ws, fake, _ = env
    add_derived_repo(ws)
    n = len(fake.calls)
    assert run("issue", "--repo", "gohan", "--scope", "acnl", "--kind", "analysis", "--difficulty", "easy",
               "--title", "x", "--goal", goal, "--target", "t", "--materials", "m") == 1
    assert "解析の情報" in capsys.readouterr().err
    assert len(fake.calls) == n


def test_derived_issue_without_analysis_info_is_created(env):
    ws, fake, _ = env
    add_derived_repo(ws)
    assert run("issue", "--repo", "gohan", "--scope", "hhd", "--kind", "analysis", "--difficulty", "easy",
               "--title", "調べる", "--goal", "g", "--target", "t", "--materials", "なし") == 0
    args, _ = fake.named("issue", "create")[0]
    assert args[args.index("--repo") + 1] == "yak9909/gohan"


# ---------- 深い検証の要否（stamp・accept）----------
def base_symbols(ws):
    """main に symbols の基準を入れる（作業の差分で名前・型の変更を作るため）。"""
    re_ = ws / "ac3ds-re"
    fun = re_ / "symbols" / "acnl_jpn_v15" / "functions"
    typ = re_ / "symbols" / "acnl_jpn_v15" / "types"
    fun.mkdir(parents=True)
    typ.mkdir(parents=True)
    (fun / "00100000.csv").write_text(
        "address,size,name,prototype,confidence,source,refs,clone_of\n"
        "0x00100000,4,sub_100000,,,analysis,,\n0x00100010,8,keep_fn,,,analysis,,\n",
        encoding="utf-8", newline="\n")
    (typ / "local_types.h").write_text("struct ACNL_Foo {\n    int a;\n};\n", encoding="utf-8", newline="\n")
    g(re_, "add", "-A")
    g(re_, "commit", "-m", "基準の symbols")
    g(re_, "push", "origin", "main")


def symbol_change(env, api_text):
    """sub_100000 を renamed_fn に改名し、ACNL_Foo の宣言行を変えて save する。派生の spec には api_text を書く。"""
    ws, fake, origin = env
    base_symbols(ws)
    dispatch(env)
    wt = Path(tasks(ws)["ac3ds-re#5"]["worktree"])
    fun = wt / "symbols" / "acnl_jpn_v15" / "functions" / "00100000.csv"
    fun.write_text(fun.read_text(encoding="utf-8").replace("sub_100000", "renamed_fn"), encoding="utf-8", newline="\n")
    typ = wt / "symbols" / "acnl_jpn_v15" / "types" / "local_types.h"
    typ.write_text(typ.read_text(encoding="utf-8").replace("struct ACNL_Foo {", "struct ACNL_Foo { // 変更"),
                   encoding="utf-8", newline="\n")
    add_derived_repo(ws, api_text)
    assert run("save", "5") == 0
    return ws, fake, wt


@pytest.mark.parametrize("api,expected", [
    ('functions = ["renamed_fn"]\ntypes = ["ACNL_Foo"]\n', ["ACNL_Foo", "renamed_fn"]),
    ('functions = ["sub_100000"]\n', ["sub_100000"]),  # 旧名を使っていれば、改名で壊れるので対象
    ('functions = ["keep_fn"]\n', None),                 # 変わっていない名前だけなら対象ではない
])
def test_stamp_flags_deep_review_for_names_in_derived_api(env, capsys, api, expected):
    ws, fake, wt = symbol_change(env, api)
    assert run("stamp", "5") == 0
    out = capsys.readouterr().out
    t = tasks(ws)["ac3ds-re#5"]
    if expected:
        assert "深い検証の対象: %s（gohan の spec/game_api.toml）" % ", ".join(expected) in out
        assert t["deep_review_required"] is True and t["deep_review_names"] == expected
    else:
        assert "深い検証の対象" not in out and "## 深い検証の要否" in out
        assert t["deep_review_required"] is False


def test_accept_needs_reviewed_when_deep_review_required(env, capsys):
    ws, fake, wt = symbol_change(env, 'functions = ["renamed_fn"]\n')
    run("stamp", "5")
    assert run("accept", "5", *read_args(ws), "--scores", SCORES) == 1
    assert "--reviewed" in capsys.readouterr().err
    assert not fake.named("pr", "merge")
    review = ws / "_local" / "review.md"
    review.write_text("深い検証: renamed_fn を IDB で確かめた（試験）\n", encoding="utf-8")
    assert run("accept", "5", *read_args(ws), "--scores", SCORES, "--reviewed", str(review)) == 0
    bodies = [args[args.index("--body") + 1] for args, _ in fake.named("pr", "comment")]
    assert any(b.startswith("深い検証の報告:") and "renamed_fn を IDB" in b for b in bodies)
    assert tasks(ws)["ac3ds-re#5"]["state"] == "accepted"


# ---------- 推奨並列数を超える依頼（dispatch・--ignore-limit）----------
def test_ignore_limit_needs_a_reason(env, monkeypatch, capsys):
    ws, fake, _ = env
    monkeypatch.setattr(orch, "capacity_reports", lambda *_: {"claude": {"recommended": 0}})
    assert run("dispatch", "5", "--worker", "sonnet-5.5", *precheck_args(ws)) == 1
    assert run("dispatch", "5", "--worker", "sonnet-5.5", *precheck_args(ws), "--ignore-limit", "") == 1
    assert "理由を書く" in capsys.readouterr().err
    assert not (ws / "_local" / "state" / "tasks.json").exists()


def test_ignore_limit_reason_is_recorded(env, monkeypatch):
    ws, fake, _ = env
    monkeypatch.setattr(orch, "capacity_reports", lambda *_: {"claude": {"recommended": 0}})
    assert run("dispatch", "5", "--worker", "sonnet-5.5", *precheck_args(ws), "--ignore-limit", "試験の理由") == 0
    t = tasks(ws)["ac3ds-re#5"]
    assert t["ignore_limit_reason"] == "試験の理由"
    assert t["ignored_limits"] == ["推奨並列数 0（稼働中 0）"]


@pytest.mark.parametrize("state,refused", [("saved", False), ("stamped", False), ("dispatched", True)])
def test_parallel_limit_counts_only_running_tasks(env, monkeypatch, capsys, state, refused):
    ws, fake, _ = env
    orch.update_task(orch.Ctx(), "ac3ds-re#4", repo="ac3ds-re", issue=4, provider="claude",
                     worker="sonnet-5.5", state=state)
    monkeypatch.setattr(orch, "capacity_reports", lambda *_: {"claude": {"recommended": 1}})
    code = run("dispatch", "5", "--worker", "sonnet-5.5", *precheck_args(ws))
    assert code == (1 if refused else 0)
    if refused:
        assert "推奨並列数 1 を超える（稼働中 1" in capsys.readouterr().err


# ---------- ラベル・WebChat ----------
def label_edits(fake):
    out = []
    for args, _ in fake.named("issue", "edit"):
        flag = "--add-label" if "--add-label" in args else "--remove-label"
        out.append((flag, args[args.index(flag) + 1]))
    return out


def test_labels_include_webchat_and_busy():
    names = [n for n, _, _ in orch.LABELS]
    assert "webchat" in names and "作業中" in names


def test_setup_github_creates_webchat_labels(env):
    ws, fake, _ = env
    assert run("setup-github", "ac3ds-re") == 0
    bodies = [json.loads(i) for a, i in fake.calls if a[:3] == ["api", "-X", "POST"] and a[3].endswith("/labels")]
    assert {"webchat", "作業中"} <= {b["name"] for b in bodies}


def test_set_label_failure_is_only_a_warning(env, capsys):
    ws, fake, _ = env
    fake.fail["issue edit"] = 1
    c = orch.Ctx()
    orch.set_label(c, "ac3ds-re", 5, "作業中", False)
    assert "警告" in capsys.readouterr().out
    orch.set_label(c, "ac3ds-re", 5, "作業中", True)
    assert label_edits(fake) == [("--remove-label", "作業中"), ("--add-label", "作業中")]


def test_dispatch_adds_busy_label_and_retry_is_allowed(env):
    ws, fake, _ = env
    assert dispatch(env) == 0
    assert label_edits(fake) == [("--add-label", "作業中")]
    fake.issue["labels"].append({"name": "作業中"})  # 自分で付けた物は、再試行を断らない
    assert dispatch(env) == 0
    assert label_edits(fake) == [("--add-label", "作業中")]


@pytest.mark.parametrize("label", ["webchat", "作業中"])
def test_dispatch_refuses_labeled_issue(env, capsys, label):
    ws, fake, _ = env
    fake.issue["labels"].append({"name": label})
    assert dispatch(env) == 1
    assert label in capsys.readouterr().err
    assert not label_edits(fake) and not (ws / "_local" / "state" / "tasks.json").exists()


def test_dispatch_label_failure_does_not_stop(env, capsys):
    ws, fake, _ = env
    fake.fail["issue edit"] = 1
    assert dispatch(env) == 0
    assert "警告" in capsys.readouterr().out and tasks(ws)["ac3ds-re#5"]["state"] == "dispatched"


def test_reject_removes_busy_label(env):
    ws, fake, origin, wt = ready(env)
    assert run("reject", "5", "--reason", "足りない") == 0
    assert label_edits(fake)[-1] == ("--remove-label", "作業中")


def test_accept_removes_busy_label(env):
    ws, fake, origin, wt = ready(env)
    assert run("stamp", "5") == 0
    assert run("accept", "5", "--scores", SCORES, *read_args(ws)) == 0
    assert label_edits(fake)[-1] == ("--remove-label", "作業中")


def issue_args(*extra):
    return ["issue", "--scope", "acnl", "--kind", "analysis", "--difficulty", "easy", "--title", "調べる",
            "--goal", "ある関数の役割", "--target", "0x00100000", "--materials", "ACNL-F0001", *extra]


def test_issue_webchat_adds_label_and_instructions(env, capsys):
    ws, fake, _ = env
    assert run(*issue_args("--webchat")) == 0
    args, _in = fake.named("issue", "create")[0]
    labels = [args[i + 1] for i, x in enumerate(args) if x == "--label"]
    assert labels == ["acnl", "analysis", "easy", "webchat"]
    body = args[args.index("--body") + 1]
    assert "### 目的と完了条件" in body and "ある関数の役割" in body and "0x00100000" in body
    assert "inbox/webchat/<この Issue の番号>" in body and "webchat/<この Issue の番号>" in body
    assert "webchat verify" in capsys.readouterr().out
    # 付けなければ webchat のラベルも指示も付かない
    assert run(*issue_args()) == 0
    args2 = fake.named("issue", "create")[1][0]
    assert "webchat" not in [args2[i + 1] for i, x in enumerate(args2) if x == "--label"]
    assert "WebChat への指示" not in args2[args2.index("--body") + 1]


@pytest.mark.parametrize("body,branch,want", [
    ("Issue #12\n\n本文", "webchat/12", 12), ("  Issue #7 の下書き", "feature/x", 7), ("説明だけ", "webchat/9", 9),
    ("Issue #3", "webchat/3", 3)])
def test_webchat_issue_number(body, branch, want):
    assert orch.webchat_issue_number(body, branch) == want


@pytest.mark.parametrize("body,branch", [("説明だけ", "feature/x"), ("Issue #3", "webchat/4"), ("", "")])
def test_webchat_issue_number_refused(body, branch):
    with pytest.raises(orch.OrchError):
        orch.webchat_issue_number(body, branch)


def test_webchat_outside():
    assert orch.webchat_outside(["inbox/webchat/5/01.md", "inbox/webchat/5/02.md"], 5) == []
    assert orch.webchat_outside(["inbox/webchat/5/01.md", "tools/kb.py", "inbox/webchat/55/01.md"], 5) == [
        "tools/kb.py", "inbox/webchat/55/01.md"]


class WebchatGh:
    """FakeGh に、WebChat の PR（ブランチ webchat/5）の応答を足した物。"""

    def __init__(self, fake, origin, tmp):
        self.fake, self.origin, self.tmp = fake, origin, tmp
        self.info = {"number": 9, "state": "OPEN", "body": "Issue #5\n\n下書き", "headRefName": "webchat/5",
                     "isDraft": False, "isCrossRepository": False, "files": [{"path": "inbox/webchat/5/01.md"}]}
        self.calls = fake.calls

    def named(self, *prefix):
        return self.fake.named(*prefix)

    def __call__(self, args, input_text=None):
        if args[:2] == ["pr", "view"]:
            self.calls.append((list(args), input_text))
            if args[-1].endswith("headRefOid"):
                return json.dumps({"headRefOid": g(self.origin, "rev-parse", "refs/heads/webchat/5").strip()})
            return json.dumps(self.info)
        if args[:3] == ["api", "-X", "PUT"] and args[3].endswith("/pulls/9/merge"):
            self.calls.append((list(args), input_text))
            cl = self.tmp / "mergeclone"
            if cl.exists():
                shutil.rmtree(cl)
            g(self.tmp, "clone", str(self.origin), str(cl))
            g(cl, "merge", "--no-ff", "-m", "merge", "origin/webchat/5")
            g(cl, "push", "origin", "HEAD:main")
            return ""
        return self.fake(args, input_text)


@pytest.fixture
def webchat(env, monkeypatch, tmp_path):
    ws, fake, origin = env
    p = ws / "workspace.toml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        'role = "analysis"\n', 'role = "analysis"\nverify = ["python -c pass"]\n', 1), encoding="utf-8")
    re_ = ws / "ac3ds-re"
    g(re_, "checkout", "-q", "-b", "webchat/5")
    d = re_ / "inbox" / "webchat" / "5"
    d.mkdir(parents=True)
    (d / "01.md").write_text("下書き\n", encoding="utf-8")
    g(re_, "add", "-A")
    g(re_, "commit", "-m", "draft")
    g(re_, "push", "origin", "webchat/5")
    g(re_, "checkout", "-q", "main")
    g(re_, "branch", "-D", "webchat/5")
    gh = WebchatGh(fake, origin, tmp_path)
    monkeypatch.setattr(orch, "gh_run", gh)
    return ws, gh, origin


@pytest.mark.parametrize("change", [
    {"files": [{"path": "inbox/webchat/5/01.md"}, {"path": "tools/kb.py"}]},
    {"files": [{"path": "inbox/webchat/6/01.md"}]}, {"files": []}, {"body": "Issue #6"},
    {"body": "説明だけ", "headRefName": "other"}, {"state": "CLOSED"}, {"isCrossRepository": True}])
def test_webchat_refuses_bad_pr(webchat, monkeypatch, capsys, change):
    ws, gh, origin = webchat
    gh.info.update(change)
    monkeypatch.setattr(orch, "prepare_webchat", lambda *_: pytest.fail("作業ツリーを作った"))
    assert run("webchat", "verify", "9") == 1
    assert capsys.readouterr().err.startswith("エラー")


def test_webchat_verify_checks_out_and_runs(webchat, capsys):
    ws, gh, origin = webchat
    assert run("webchat", "verify", "9") == 0
    out = capsys.readouterr().out
    wt = ws / "_local" / "worktrees" / "ac3ds-re" / "webchat-5"
    assert (wt / "inbox" / "webchat" / "5" / "01.md").is_file() and "合格" in out and "01.md" in out
    assert g(wt, "branch", "--show-current").strip() == "webchat/5"


def test_webchat_verify_fails_when_command_fails(webchat, capsys):
    ws, gh, origin = webchat
    p = ws / "workspace.toml"
    p.write_text(p.read_text(encoding="utf-8").replace('"python -c pass"', "'python -c \"raise SystemExit(3)\"'"),
                 encoding="utf-8")
    assert run("webchat", "verify", "9") == 1
    assert "不合格" in capsys.readouterr().out


def fake_intake(monkeypatch):
    real = orch.run_py
    calls = []

    def run_py(c, wt, script, args):
        calls.append(list(args))
        if args[0] == "intake":
            (wt / "inbox" / "webchat" / "5" / "01.md").unlink()
            f = wt / "findings" / "acnl" / "0000" / "ACNL-F0001.md"
            f.parent.mkdir(parents=True)
            f.write_text("記録\n", encoding="utf-8")
            return 0, "ACNL-F0001"
        if args[0] == "lint":
            return 0, ""
        return real(c, wt, script, args)

    monkeypatch.setattr(orch, "run_py", run_py)
    return calls


def test_webchat_accept_full(webchat, monkeypatch, capsys):
    ws, gh, origin = webchat
    calls = fake_intake(monkeypatch)
    assert run("webchat", "accept", "9", "--scores", SCORES, *read_args(ws)) == 0
    out = capsys.readouterr().out
    assert ["intake", "--issue", "5", "--by", "gpt-6-sol"] in calls and "ACNL-F0001" in out
    comment = [a for a, _ in gh.named("pr", "comment")][0]
    assert comment[2] == "9" and "合格" in comment[-1] and "根拠と結論の一致=0.9" in comment[-1]
    assert gh.named("api", "-X", "PUT")
    assert label_edits(gh) == [("--remove-label", "作業中")]
    assert gh.named("issue", "close")[0][0][2] == "5"
    cl = ws / "check"
    g(ws, "clone", str(origin), str(cl))
    assert (cl / "findings" / "acnl" / "0000" / "ACNL-F0001.md").is_file()
    assert not (cl / "inbox" / "webchat" / "5" / "01.md").exists()
    assert not (ws / "_local" / "worktrees" / "ac3ds-re" / "webchat-5").exists()


def test_webchat_accept_stops_when_intake_fails(webchat, monkeypatch, capsys):
    ws, gh, origin = webchat
    real = orch.run_py
    monkeypatch.setattr(orch, "run_py", lambda c, wt, s, a: (1, "エラー: 下書きが不備") if a[0] == "intake" else real(c, wt, s, a))
    assert run("webchat", "accept", "9", "--scores", SCORES, *read_args(ws)) == 1
    assert "intake" in capsys.readouterr().err
    assert not gh.named("api", "-X", "PUT") and not gh.named("pr", "comment") and not label_edits(gh)


def test_webchat_accept_refuses_bad_scores_and_dry_run_writes_nothing(webchat, capsys):
    ws, gh, origin = webchat
    assert run("webchat", "accept", "9", "--scores", "根拠と結論の一致=0.9", *read_args(ws)) == 1
    n = len(gh.calls)
    assert run("webchat", "accept", "9", "--scores", SCORES, *read_args(ws), "--dry-run") == 0
    assert "dry-run" in capsys.readouterr().out
    assert not [c for c in gh.calls[n:] if c[0][0] in ("api", "issue")]
    assert not (ws / "_local" / "worktrees" / "ac3ds-re" / "webchat-5").exists()


def test_webchat_reject(webchat):
    ws, gh, origin = webchat
    assert run("webchat", "verify", "9") == 0
    assert run("webchat", "reject", "9", "--reason", "連絡は " + FAKE_EMAIL) == 1
    assert run("webchat", "reject", "9", "--reason", "根拠が足りない") == 0
    assert "--delete-branch" in gh.named("pr", "close")[0][0]
    assert gh.named("pr", "comment")[0][0][-1] == "却下: 根拠が足りない"
    assert label_edits(gh) == [("--remove-label", "作業中")] and not gh.named("issue", "close")
    assert not (ws / "_local" / "worktrees" / "ac3ds-re" / "webchat-5").exists()
