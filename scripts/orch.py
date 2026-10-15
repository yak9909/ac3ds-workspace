#!/usr/bin/env python3
"""オーケストラの道具。台帳（_local/state/tasks.json）を軸に、Issue・依頼・Codex の起動・報告の保存・保存・採点用の要約・取り込み・却下を行う。

GitHub への操作は gh_run() の1か所に集め、試験で差し替える。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402
import sync_skills  # noqa: E402
import wslib  # noqa: E402

try:
    from filelock import FileLock
except ImportError:  # 依存が無い環境でも読み込めるように
    FileLock = None

SCOPES = {"acnl": "[ACNL]", "hhd": "[HHD]", "3ds": "[3DS]"}
KINDS = ["analysis", "import", "triage", "consolidate", "reanalyze", "review", "tooling", "report"]
DIFFS = ["easy", "normal", "hard"]
ACTIVE = ("dispatched", "saved", "stamped")
DEFAULT_REPO = "ac3ds-re"
CHECK_POLL_SEC = 15
CHECK_APPEAR_SEC = 90  # push したコミットに検査が現れるまで待つ上限
CHECK_TIMEOUT_SEC = 600

LABELS = (
    [(s, "1d76db", "スコープ") for s in SCOPES]
    + [(k, "5319e7", "種類") for k in KINDS]
    + [(d, "0e8a16", "難易度") for d in DIFFS]
    + [("blocked", "b60205", "止まっている"), ("needs-user", "d93f0b", "利用者の判断が要る"),
       ("spot-check", "fbca04", "抜き取り確認"), ("rule-proposal", "c5def5", "規則の提案")]
)


class OrchError(Exception):
    pass


class GhError(OrchError):
    def __init__(self, msg, stdout="", code=1):
        super().__init__(msg)
        self.stdout = stdout
        self.code = code


# ---------- 環境 ----------
class Ctx:
    def __init__(self):
        self.root = wslib.workspace_root()
        self.ws = wslib.load_workspace(self.root)
        self.local = wslib.load_local(self.root)
        p = self.root / "workers.toml"
        self.workers = tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        self.data = self.root / self.ws.get("data", "_local")
        self.state = self.data / "state"
        self.git = wslib.git_exe(self.root)

    def repos(self):
        return wslib.existing_repos(self.root)


def now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def need_repo(c: Ctx, repo: str) -> dict:
    r = c.repos()
    if repo not in r:
        raise OrchError("リポジトリが無い: %s（%s）" % (repo, ", ".join(r) or "なし"))
    return r[repo]


def repo_slug(c: Ctx, repo: str) -> str:
    remote = c.ws.get("repos", {}).get(repo, {}).get("remote", "")
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", remote)
    if not m:
        raise OrchError("workspace.toml に %s の remote が無い" % repo)
    return m.group(1)


# ---------- 外部コマンド ----------
def gh_run(args, input_text=None) -> str:
    exe = os.environ.get("AC3DS_GH") or shutil.which("gh")
    if not exe:
        raise GhError("gh が見つからない")
    r = subprocess.run([exe, *args], input=input_text, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise GhError("gh %s: %s" % (" ".join(args[:3]), (r.stderr or r.stdout).strip()), r.stdout, r.returncode)
    return r.stdout


def tool_env(c: Ctx) -> dict:
    """子プロセス用の環境変数。作業場の道具の venv があれば、その Scripts（bin）を PATH の先頭に足す。
    リポジトリのフック（pre-commit の python tools/audit.py など）が PATH 上の別の Python を使わないように。"""
    env = dict(os.environ)
    d = c.data / "venv" / "tools" / ("Scripts" if os.name == "nt" else "bin")
    if d.is_dir():
        env["PATH"] = str(d) + os.pathsep + env.get("PATH", "")
    return env


def git(c: Ctx, cwd, *args, check=True) -> str:
    r = subprocess.run([c.git, "-c", "core.quotepath=off", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=tool_env(c))
    if check and r.returncode != 0:
        raise OrchError("git %s: %s" % (" ".join(args[:3]), (r.stderr or r.stdout).strip()))
    return r.stdout if r.returncode == 0 else ""


def git_ok(c: Ctx, cwd, *args) -> bool:
    return subprocess.run([c.git, *args], cwd=str(cwd), capture_output=True, env=tool_env(c)).returncode == 0


def child_env(c: Ctx) -> dict:
    """作業場の道具を子プロセスで動かすときの環境変数（状態の場所と git を渡す）。"""
    env = tool_env(c)
    env.pop("AC3DS_REPO", None)
    env.update({"AC3DS_STATE": str(c.state), "AC3DS_GIT": c.git, "PYTHONIOENCODING": "utf-8"})
    return env


def run_py(c: Ctx, wt: Path, script: Path, args: list[str]):
    r = subprocess.run([sys.executable, str(script), *args], cwd=str(wt), env=child_env(c), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def outgoing(*texts: str) -> None:
    for t in texts:
        probs = audit.check_outgoing(t)
        if probs:
            raise OrchError("GitHub に書く本文に問題があるので書かない:\n" + "\n".join(str(p) for p in probs[:5]))


# ---------- 台帳 ----------
def tasks_path(c: Ctx) -> Path:
    return c.state / "tasks.json"


@contextmanager
def ledger_lock(c: Ctx):
    c.state.mkdir(parents=True, exist_ok=True)
    if FileLock is None:
        yield
    else:
        with FileLock(str(tasks_path(c)) + ".lock", timeout=30):
            yield


def load_tasks(c: Ctx) -> dict:
    p = tasks_path(c)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("tasks", {})


def update_task(c: Ctx, key: str, **kw) -> dict:
    with ledger_lock(c):
        d = load_tasks(c)
        d.setdefault(key, {}).update(kw)
        p = tasks_path(c)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tasks": d}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, p)
        return d[key]


def tkey(repo: str, n) -> str:
    return "%s#%s" % (repo, n)


def get_task(c: Ctx, repo: str, n) -> dict:
    t = load_tasks(c).get(tkey(repo, n))
    if not t:
        raise OrchError("台帳にタスクが無い: %s（先に dispatch）" % tkey(repo, n))
    return t


def task_file(c: Ctx, kind: str, repo: str, n) -> Path:
    name = "%s.md" % n if repo == DEFAULT_REPO else "%s-%s.md" % (repo, n)
    return c.data / "orchestrator" / kind / name


def read_usage(c: Ctx):
    p = c.state / "usage.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("claude", {}).get("five_hour_used_percent")
    except (ValueError, AttributeError):
        return None


# ---------- Git 作業 ----------
def commit_all(c: Ctx, wt: Path, msg: str) -> bool:
    git(c, wt, "add", "-A")
    if not git(c, wt, "status", "--porcelain").strip():
        return False
    trailer = c.workers.get("git", {}).get("commit_trailer", "")
    git(c, wt, "commit", "-m", msg + ("\n\n" + trailer if trailer else ""))
    return True


def head(c: Ctx, wt: Path) -> str:
    return git(c, wt, "rev-parse", "HEAD").strip()


def base_ref(c: Ctx, wt: Path) -> str:
    return "origin/main" if git_ok(c, wt, "rev-parse", "--verify", "-q", "origin/main") else "main"


def parse_sections(body: str) -> dict:
    out: dict = {}
    cur = None
    for line in (body or "").splitlines():
        m = re.match(r"###\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def read_issue(c: Ctx, repo: str, n) -> dict:
    out = gh_run(["issue", "view", str(n), "--repo", repo_slug(c, repo), "--json", "title,body,labels,state"])
    try:
        return json.loads(out)
    except ValueError:
        raise OrchError("Issue #%s の読み取り結果が JSON でない" % n)


def pr_number(out: str):
    m = re.search(r"/pull/(\d+)", out) or re.search(r"(\d+)\s*$", out.strip())
    return int(m.group(1)) if m else None


# ---------- setup-github ----------
def cmd_setup_github(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    slug = repo_slug(c, a.repo)
    patch = {"allow_merge_commit": True, "allow_squash_merge": False, "allow_rebase_merge": False,
             "delete_branch_on_merge": True,
             "security_and_analysis": {"secret_scanning": {"status": "enabled"},
                                       "secret_scanning_push_protection": {"status": "enabled"}}}
    prot = {"required_status_checks": {"strict": False, "contexts": ["audit"]}, "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 0}, "restrictions": None}
    print("リポジトリ設定: マージコミットだけ許可・マージ後にブランチ削除・secret scanning とプッシュ保護")
    print("ラベル: %d 個を作成/更新" % len(LABELS))
    print("main の保護: PR 必須・必須チェック audit・管理者も対象")
    if a.dry_run:
        print("（dry-run: 何も書いていない）")
        return 0
    gh_run(["api", "-X", "PATCH", "repos/%s" % slug, "--input", "-"], json.dumps(patch))
    existing = set()
    try:
        existing = {x["name"] for x in json.loads(gh_run(["api", "repos/%s/labels?per_page=100" % slug]))}
    except (GhError, ValueError, KeyError):
        pass
    for name, color, desc in LABELS:
        body = json.dumps({"name": name, "color": color, "description": desc})
        if name in existing:
            gh_run(["api", "-X", "PATCH", "repos/%s/labels/%s" % (slug, name), "--input", "-"], body)
        else:
            gh_run(["api", "-X", "POST", "repos/%s/labels" % slug, "--input", "-"], body)
    gh_run(["api", "-X", "PUT", "repos/%s/branches/main/protection" % slug, "--input", "-"], json.dumps(prot))
    print("setup-github: 完了 (%s)" % slug)
    return 0


# ---------- GitHub の控え ----------
def mirror(c: Ctx) -> None:
    for repo in c.repos():
        try:
            slug = repo_slug(c, repo)
        except OrchError:
            continue
        for kind, sub, fields in (("issues", "issue", "number,title,state,body,comments,labels,url,updatedAt"),
                                  ("pulls", "pr", "number,title,state,body,comments,headRefName,url,updatedAt")):
            try:
                items = json.loads(gh_run([sub, "list", "--repo", slug, "--state", "all", "--limit", "30",
                                           "--json", fields]))
            except (OrchError, ValueError) as e:
                print("控え(%s %s): 取得できない: %s" % (repo, kind, str(e).splitlines()[0] if str(e) else e))
                continue
            d = c.state / "github" / repo / kind
            d.mkdir(parents=True, exist_ok=True)
            for it in items:
                (d / ("%s.json" % it["number"])).write_text(json.dumps(it, ensure_ascii=False, indent=2) + "\n",
                                                            encoding="utf-8", newline="\n")
        if repo == DEFAULT_REPO:
            try:
                items = json.loads(gh_run(["issue", "list", "--repo", slug, "--state", "open", "--limit", "200",
                                           "--json", "number,title,body,state"]))
                out = [{"number": i["number"], "title": i["title"], "body": i.get("body") or "",
                        "state": str(i.get("state", "open")).lower()} for i in items]
                c.state.mkdir(parents=True, exist_ok=True)
                (c.state / "issues.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                                                     encoding="utf-8", newline="\n")
            except (OrchError, ValueError) as e:
                print("issues.json: 取得できない: %s" % (str(e).splitlines() or [e])[0])


# ---------- start / status ----------
def periodic(c: Ctx) -> dict:
    p = c.state / "periodic.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def cmd_status(a) -> int:
    c = Ctx()
    ts = load_tasks(c)
    if not ts:
        print("台帳: タスクなし")
    for k, t in ts.items():
        print("%s  %s  %s 試行%s PR=%s" % (k, t.get("state"), t.get("worker"), t.get("attempts"), t.get("pr") or "-"))
    return 0


def cmd_start(a) -> int:
    c = Ctx()
    h = c.data / "orchestrator" / "handoff.md"
    print("== 引き継ぎメモ ==")
    if h.exists():
        lines = h.read_text(encoding="utf-8").splitlines()
        print("\n".join(lines[:80]))
        if len(lines) > 80:
            print("…（残り %d 行は %s）" % (len(lines) - 80, h))
    else:
        print("（無し）")
    print("== 動いているタスク ==")
    act = {k: t for k, t in load_tasks(c).items() if t.get("state") in ACTIVE}
    for k, t in act.items():
        print("%s  %s  %s  PR=%s" % (k, t["state"], t.get("worker"), t.get("pr") or "-"))
    if not act:
        print("なし")
    print("== open の Issue と PR ==")
    for repo in c.repos():
        try:
            slug = repo_slug(c, repo)
            for sub in ("issue", "pr"):
                items = json.loads(gh_run([sub, "list", "--repo", slug, "--state", "open", "--limit", "50",
                                           "--json", "number,title"]))
                print("%s %s: %d 件" % (repo, sub, len(items)))
                for it in items:
                    print("  #%s %s" % (it["number"], it["title"]))
        except (OrchError, ValueError) as e:
            print("%s: 取得できない（%s）" % (repo, (str(e).splitlines() or ["?"])[0]))
    print("== 使用率 ==")
    up = c.state / "usage.json"
    u = read_usage(c)
    if u is None:
        print("不明")
    else:
        print("5h %s%%（更新 %s）" % (u, datetime.datetime.fromtimestamp(up.stat().st_mtime).isoformat(timespec="seconds")))
    print("== 定期作業 ==")
    per = periodic(c)
    for name, days in (("weekly", 7), ("restore-test", 30)):
        last = per.get(name)
        if not last:
            print("%s: 未実施" % name)
            continue
        age = (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(last)).days
        print("%s: %s（%d日前）%s" % (name, last, age, " ← 間が空いた" if age >= days else ""))
    mirror(c)
    return 0


# ---------- issue ----------
def cmd_issue(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    for k in ("title", "goal", "target", "materials"):
        if not (getattr(a, k) or "").strip():
            raise OrchError("必須項目が空: --%s" % k)
    title = a.title.strip()
    pre = SCOPES[a.scope]
    if not title.startswith(pre):
        title = "%s %s" % (pre, title)
    body = ("### スコープ\n\n%s\n\n### 種類\n\n%s\n\n### 難易度\n\n%s\n\n### 目的と完了条件\n\n%s\n\n"
            "### 対象\n\n%s\n\n### 関係する資料\n\n%s\n") % (a.scope, a.kind, a.difficulty, a.goal.strip(),
                                                          a.target.strip(), a.materials.strip())
    outgoing(title, body)
    args = ["issue", "create", "--repo", repo_slug(c, a.repo), "--title", title, "--body", body]
    for lb in (a.scope, a.kind, a.difficulty):
        args += ["--label", lb]
    out = gh_run(args)
    print("Issue を作成: #%s %s" % (pr_number(out), title))
    return 0


# ---------- dispatch ----------
def short_slug(title: str) -> str:
    t = re.sub(r"^\[[^\]]*\]\s*", "", title)
    s = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:30].strip("-")
    return s or "task"


def pick_build(c: Ctx, scope: str) -> str:
    p = c.root / DEFAULT_REPO / "meta" / "builds.toml"
    builds = tomllib.loads(p.read_text(encoding="utf-8")).get("builds", {}) if p.exists() else {}
    for n, b in builds.items():
        if b.get("scope") == scope and "保管だけ" not in b.get("note", ""):
            return n
    raise OrchError("スコープ %s のビルドが builds.toml に無い" % scope)


def cmd_dispatch(a) -> int:
    c = Ctx()
    role = need_repo(c, a.repo).get("role", "analysis")
    wks = c.workers.get("workers", {})
    if a.worker not in wks:
        raise OrchError("workers.toml に無いワーカー: %s（%s）" % (a.worker, ", ".join(wks)))
    w = wks[a.worker]
    lim = c.workers.get("limits", {}).get("stop_at_used_percent", 80)
    usage = read_usage(c)
    if usage is not None and usage >= lim and not a.ignore_limit:
        raise OrchError("5時間枠の使用率 %s%% が上限 %s%% 以上なので依頼しない（--ignore-limit で無視）" % (usage, lim))
    issue = read_issue(c, a.repo, a.issue)
    sec = parse_sections(issue.get("body", ""))
    scope = sec.get("スコープ", "").strip()
    if scope not in SCOPES:
        scope = next((lb["name"] for lb in issue.get("labels", []) if lb.get("name") in SCOPES), "")
    key = tkey(a.repo, a.issue)
    old = load_tasks(c).get(key)
    repo_dir = c.root / a.repo
    wt = c.data / "worktrees" / a.repo / str(a.issue)
    branch = old["branch"] if old else "task/%s-%s" % (a.issue, short_slug(issue.get("title", "")))
    idb = old.get("idb") if old else None
    if a.idb:
        if scope not in SCOPES:
            raise OrchError("Issue のスコープが分からないので IDB を決められない")
        build = pick_build(c, scope)
        canon = c.data / "builds" / build / "ida" / ("%s.i64" % build)
        if not canon.is_file():
            raise OrchError("正本の IDB が無い: %s" % canon)
    git(c, repo_dir, "fetch", "origin", check=False)
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        if git_ok(c, repo_dir, "rev-parse", "--verify", "-q", "refs/heads/" + branch):
            git(c, repo_dir, "worktree", "add", str(wt), branch)
        else:
            git(c, repo_dir, "worktree", "add", "-b", branch, str(wt), base_ref(c, repo_dir))
    if a.idb:
        dest = c.data / "builds" / build / "ida" / "work" / str(a.issue) / ("%s.i64" % build)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(canon, dest)
        idb = {"build": build, "path": str(dest)}
    sync_skills.sync_location(wt)
    (wt / ".task").mkdir(exist_ok=True)
    tpl = c.root / "templates" / ("dispatch-derived.md" if role == "derived" else "dispatch.md")
    goal = sec.get("目的と完了条件", "").strip() or issue.get("title", "")
    if sec.get("対象"):
        goal += "\n  対象: " + sec["対象"].strip()
    text = tpl.read_text(encoding="utf-8").format(
        repo=a.repo, goal=goal, worktree=str(wt),
        idb_line=("、IDB の作業用コピー %s" % idb["path"]) if idb else "",
        materials=sec.get("関係する資料", "").strip() or "なし",
        precheck=Path(a.precheck).read_text(encoding="utf-8").strip() if a.precheck else "なし",
        tools_line=("- 道具: %s" % a.tools) if a.tools else "")
    out = task_file(c, "dispatch", a.repo, a.issue)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    update_task(c, key, repo=a.repo, issue=a.issue, title=issue.get("title", ""), branch=branch, worktree=str(wt),
                worker=a.worker, provider=w.get("provider"), effort=a.effort or w.get("default_effort"),
                idb=idb, state="dispatched", attempts=(old.get("attempts", 0) + 1) if old else 1,
                dispatched_at=now(), usage_before=usage, stamped_head=None)
    print(text)
    print("--- 依頼文を保存: %s（ブランチ %s、%s %s）" % (out, branch, a.worker, a.effort or w.get("default_effort")))
    return 0


# ---------- save ----------
def idb_open_files(path: Path) -> list[str]:
    if not path.parent.is_dir():
        return []
    return sorted(p.name for p in path.parent.iterdir() if p.suffix.lower() in {".id0", ".id1", ".nam", ".til"})


def save_task(c: Ctx, t: dict, message: str | None = None) -> bool:
    wt = Path(t["worktree"])
    key = tkey(t["repo"], t["issue"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    if t.get("idb"):
        op = idb_open_files(Path(t["idb"]["path"]))
        if op:
            raise OrchError("IDB のコピーが開いたまま（%s）。閉じてから save する" % ", ".join(op))
    if not git(c, wt, "status", "--porcelain").strip():
        print("%s: 変更なし（何もしない）" % key)
        return False
    n = t.get("attempts", 1)
    msg = "試行 %s: %s" % (n, message or "作業の保存")
    pr_title = pr_body = None
    if not t.get("pr"):
        footer = c.workers.get("git", {}).get("pr_footer", "")
        pr_title = t.get("title") or "Issue #%s" % t["issue"]
        pr_body = "Closes #%s\n\n%s\n\n%s" % (t["issue"], message or "試行 %s の作業内容。" % n, footer)
        outgoing(pr_title, pr_body)
    commit_all(c, wt, msg)
    update_task(c, key, attempts=n + 1)  # コミットしたら番号を進める（次の保存が同じ番号にならないように）
    git(c, wt, "push", "-u", "origin", t["branch"])
    fields = {"state": "saved", "saved_at": now(), "saved_head": head(c, wt), "stamped_head": None}
    if pr_title:
        out = gh_run(["pr", "create", "--draft", "--repo", repo_slug(c, t["repo"]), "--base", "main",
                      "--head", t["branch"], "--title", pr_title, "--body", pr_body])
        fields["pr"] = pr_number(out)
        print("draft の PR を作成: #%s" % fields["pr"])
    update_task(c, key, **fields)
    print("%s: 保存（%s）" % (key, msg))
    return True


def cmd_save(a) -> int:
    c = Ctx()
    save_task(c, get_task(c, a.repo, a.issue), a.message)
    return 0


# ---------- report / codex ----------
REPORT_HEADER = "（ワーカーの最終の返答をオーケストラが保存した）"


def require_active(t: dict) -> None:
    if t.get("state") not in ACTIVE:
        raise OrchError("状態が %s。動いているタスク（dispatched・saved・stamped）でだけ使う" % t.get("state"))


def read_source(src: str) -> str:
    """報告の本文を読む。- は標準入力。UTF-8（BOM 可）、改行は LF にそろえる。"""
    if src == "-":
        buf = getattr(sys.stdin, "buffer", None)
        raw = buf.read() if buf is not None else sys.stdin.read()
    else:
        p = Path(src)
        if not p.is_file():
            raise OrchError("報告の元のファイルが無い: %s" % src)
        raw = p.read_bytes()
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise OrchError("報告が UTF-8 で読めない: %s" % src)
    return raw.replace("\r\n", "\n")


def save_report(t: dict, text: str) -> Path:
    """作業ツリーの .task/report.md に保存する（先頭に出所の1行を付ける。既にあれば上書きして警告）。"""
    body = text.strip()
    if not body:
        raise OrchError("報告が空")
    outgoing(body)  # .task/ は追跡されて push されるので、GitHub へ出す本文と同じ検査を掛ける
    wt = Path(t["worktree"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    f = wt / ".task" / "report.md"
    existed = f.exists()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(REPORT_HEADER + "\n" + body + "\n", encoding="utf-8", newline="\n")
    if existed:
        print("警告: 既存の .task/report.md を上書きした")
    print("報告を保存: %s" % f)
    return f


def cmd_report(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    require_active(t)
    save_report(t, read_source(a.src))
    return 0


def find_codex() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise OrchError("codex が見つからない（PATH に無い）")
    return exe


def codex_cmd(c: Ctx, wt: Path, exe: str, model: str, effort: str) -> list[str]:
    cmd = [exe, "exec", "-C", str(wt), "-s", "workspace-write"]
    if os.name == "nt":
        cmd += ["-c", 'windows.sandbox="unelevated"']
    return cmd + ["--add-dir", str(c.state), "-m", model, "-c", 'model_reasoning_effort="%s"' % effort,
                  "--json", "-"]


def run_codex(c: Ctx, cmd: list[str], cwd: Path, prompt: str) -> tuple[int, str, str]:
    """codex を起動し、終わるまで待つ。試験ではこの関数を差し替える。"""
    r = subprocess.run(cmd, cwd=str(cwd), input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=child_env(c))
    return r.returncode, r.stdout, r.stderr


def next_log(c: Ctx, repo: str, n) -> Path:
    d = c.data / "logs"
    d.mkdir(parents=True, exist_ok=True)
    pat = re.compile(r"codex_%s_%s_(\d+)\.jsonl$" % (re.escape(repo), n))
    nums = [int(m.group(1)) for p in d.iterdir() if (m := pat.match(p.name))]
    return d / ("codex_%s_%s_%d.jsonl" % (repo, n, max(nums, default=0) + 1))


def _dicts(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _dicts(v)


def last_agent_message(jsonl: str) -> str | None:
    """JSONL の最後の agent_message の本文。item.completed の形と msg の形の両方を見る。無ければ None。"""
    found = None
    for line in jsonl.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for d in _dicts(obj):
            if d.get("type") == "agent_message":
                txt = d.get("text", d.get("message"))
                if isinstance(txt, str) and txt.strip():
                    found = txt
    return found


def cmd_codex(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    key = tkey(a.repo, a.issue)
    if t.get("provider") != "codex":
        raise OrchError("台帳の provider が codex でない（%s）" % t.get("provider"))
    require_active(t)
    model = a.model or t.get("worker")
    effort = a.effort or t.get("effort")
    if not effort:
        raise OrchError("effort が台帳に無い。--effort で指定する")
    prompt_file = task_file(c, "dispatch", a.repo, a.issue)
    if not prompt_file.is_file():
        raise OrchError("依頼文が無い: %s" % prompt_file)
    wt = Path(t["worktree"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    cmd = codex_cmd(c, wt, find_codex(), model, effort)
    if a.dry_run:
        print("dry-run: 実行しない。コマンド:")
        print(subprocess.list2cmdline(cmd))
        print("依頼文（標準入力）: %s" % prompt_file)
        return 0
    log = next_log(c, a.repo, a.issue)
    code, out, err = run_codex(c, cmd, wt, prompt_file.read_text(encoding="utf-8"))
    log.write_text(out, encoding="utf-8", newline="\n")
    update_task(c, key, codex_log=str(log), codex_finished_at=now())
    nerr = sum("ERROR" in x for x in out.splitlines() + err.splitlines())
    print("codex の終了コード: %d（モデル %s、effort %s）" % (code, model, effort))
    print("エラー行（ERROR を含む）: %d 件" % nerr)
    print("ログ: %s" % log)
    if code != 0 and err.strip():
        print("== stderr の末尾 ==")
        print("\n".join(err.strip().splitlines()[-20:]))
    rc = 0 if code == 0 else 1
    msg = last_agent_message(out)
    if msg is None:
        print("（agent_message が無い）")
    else:
        print("== 最後の agent_message ==")
        print(msg)
    rep = wt / ".task" / "report.md"
    if rep.exists():
        print("報告: %s が既にあるので保存しない" % rep)
    elif msg is None:
        print("警告: agent_message が無く、報告を保存できない")
        rc = 1
    else:
        try:
            save_report(t, msg)
        except OrchError as e:
            print("エラー: 報告を保存できない: %s" % e)
            rc = 1
    return rc


# ---------- stamp ----------
def symbols_diff(c: Ctx, wt: Path, base: str):
    names = [n for n in git(c, wt, "diff", "--name-only", base + "...HEAD", "--", "symbols").splitlines()
             if n.endswith(".csv")]
    lines: list[str] = []
    builds: set[str] = set()

    def rows(text):
        r = csv.DictReader(io.StringIO(text))
        keys = r.fieldnames or []
        kc = [k for k in (("address", "kind") if "kind" in keys else ("address",)) if k in keys] or \
             [k for k in ("function", "name") if k in keys]
        return {tuple(row.get(k, "") for k in kc): row for row in r}

    for n in names:
        builds.add(n.split("/")[1])
        old = rows(git(c, wt, "show", "%s:%s" % (base, n), check=False))
        new = rows(git(c, wt, "show", "HEAD:" + n, check=False))
        add = [k for k in new if k not in old]
        rem = [k for k in old if k not in new]
        chg = [k for k in new if k in old and new[k] != old[k]]
        nm = [k for k in chg if new[k].get("name") != old[k].get("name")]
        ty = [k for k in chg if any(new[k].get(f) != old[k].get(f) for f in ("prototype", "type"))]
        lines.append("- %s: 追加 %d・削除 %d・変更 %d（名前 %d・型 %d）" % (n, len(add), len(rem), len(chg), len(nm), len(ty)))
        ex = ["    + %s %s" % ("/".join(k), new[k].get("name", "")) for k in add]
        ex += ["    - %s %s" % ("/".join(k), old[k].get("name", "")) for k in rem]
        for k in chg:
            d = ["%s: %s -> %s" % (f, old[k].get(f), new[k].get(f)) for f in new[k] if new[k].get(f) != old[k].get(f)]
            ex.append("    ~ %s %s" % ("/".join(k), "; ".join(d)))
        lines += ex[:20]
    return lines, sorted(builds)


def new_records(c: Ctx, wt: Path, base: str) -> list[str]:
    import yaml
    out = []
    for n in git(c, wt, "diff", "--name-only", "--diff-filter=A", base + "...HEAD").splitlines():
        if re.match(r"findings/[^/]+/\d+-\d+/[^/]+\.md$", n):
            m = re.match(r"---\n(.*?)\n---", (wt / n).read_text(encoding="utf-8"), re.S)
            d = yaml.safe_load(m.group(1)) if m else {}
            out.append("- %s %s [%s] %s" % (d.get("id", n), d.get("title", ""), d.get("confidence", ""), d.get("summary", "")))
    return out


def cmd_stamp(a) -> int:
    c = Ctx()
    role = need_repo(c, a.repo).get("role", "analysis")
    t = get_task(c, a.repo, a.issue)
    key = tkey(a.repo, a.issue)
    if not t.get("pr"):
        raise OrchError("PR が無い。先に save する")
    wt = Path(t["worktree"])
    base = base_ref(c, wt)
    checks: list[tuple[str, int, str]] = []
    kb = wt / "tools" / "kb.py"
    if kb.exists():
        for name, args in (("kb stamp", ["stamp", "--model", t["worker"], "--base", base]), ("kb index", ["index"]),
                           ("kb lint", ["lint"])):
            code, out = run_py(c, wt, kb, args)
            checks.append((name, code, out))
    if role == "analysis" and (wt / "tools" / "audit.py").exists():
        code, out = run_py(c, wt, wt / "tools" / "audit.py", [])
        checks.append(("audit", code, out))
    n = t.get("attempts", 1)
    if commit_all(c, wt, "試行 %s: 仕上げ" % n):
        update_task(c, key, attempts=n + 1)  # save と同じく、コミットのたびに番号を進める
    git(c, wt, "push", "-u", "origin", t["branch"])
    sym, builds = symbols_diff(c, wt, base)
    ok = all(code == 0 for _, code, _ in checks)
    recs = new_records(c, wt, base)
    lines = ["# 採点用の要約: %s" % key, "", "## 変更したファイル", git(c, wt, "diff", "--stat", base + "...HEAD").rstrip() or "なし",
             git(c, wt, "status", "--short").rstrip(), "", "## 新しい記録"] + (recs or ["なし"]) + \
            ["", "## symbols の差分"] + (sym or ["なし"]) + ["", "## 検査の結果"]
    for n, code, out in checks:
        lines.append("- %s: %s" % (n, "OK" if code == 0 else "失敗(%d)" % code))
        if code != 0:
            lines += ["    " + x for x in out.splitlines()[-10:]]
    lines += ["", "## 報告", ".task/report.md: %s" % ("あり" if (wt / ".task" / "report.md").exists() else "なし")]
    text = "\n".join(lines) + "\n"
    f = task_file(c, "stamp", a.repo, a.issue)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8", newline="\n")
    update_task(c, key, state="stamped", stamped_at=now(), stamped_head=head(c, wt), stamp_ok=ok,
                symbols_builds=builds, prep_head=None)
    print(text)
    print("--- 保存: %s%s" % (f, "" if ok else "（検査に失敗あり。accept は拒否される）"))
    return 0


# ---------- accept ----------
def parse_scores(s: str) -> dict:
    out = {}
    for part in (s or "").split(","):
        if not part.strip():
            continue
        k, sep, v = part.partition("=")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            raise OrchError("点数の形式が違う: %r（項目=点,...）" % part)
        if not sep or not k.strip():
            raise OrchError("点数の形式が違う: %r" % part)
    if not out:
        raise OrchError("--scores が空")
    return out


def required_checks(c: Ctx, slug: str) -> list[str]:
    """main の保護で必須になっている検査の名前。保護が無ければ []。"""
    try:
        out = gh_run(["api", "repos/%s/branches/main/protection" % slug])
    except GhError as e:
        if "404" in str(e):
            return []
        raise
    try:
        rs = json.loads(out or "{}").get("required_status_checks") or {}
    except ValueError:
        raise OrchError("main の保護の読み取り結果が JSON でない")
    return list(rs.get("contexts") or []) + [x.get("context", "") for x in rs.get("checks") or []]


def pr_head_oid(slug: str, pr) -> str:
    try:
        return json.loads(gh_run(["pr", "view", str(pr), "--repo", slug, "--json", "headRefOid"])).get("headRefOid", "")
    except ValueError:
        return ""


def checks_now(slug: str, pr) -> list[dict]:
    """PR の今の検査。まだ1つも登録されていなければ []（gh は「no checks」で失敗する）。"""
    try:
        raw = gh_run(["pr", "checks", str(pr), "--repo", slug, "--json", "name,bucket"])
    except GhError as e:
        if not e.stdout.strip():
            if "no checks" in str(e):
                return []
            raise
        raw = e.stdout  # 保留や失敗があると gh は終了コードが 0 でなくても JSON を出す
    try:
        return json.loads(raw or "[]")
    except ValueError:
        return []


def wait_checks(c: Ctx, slug: str, pr, sha: str) -> None:
    """push したコミット sha の検査が現れるまで待ち（必須の検査が揃うのが条件）、完了まで待つ。"""
    need = set(required_checks(c, slug))
    end = time.time() + CHECK_APPEAR_SEC
    while True:
        items = checks_now(slug, pr) if pr_head_oid(slug, pr) == sha else []
        if items and need <= {i.get("name") for i in items}:
            break
        if time.time() > end:
            if need:
                raise OrchError("push したコミットの検査が %d 秒たっても揃わない（必須: %s）"
                                % (CHECK_APPEAR_SEC, ", ".join(sorted(need))))
            print("警告: チェックが1つも無い。続ける")
            return
        time.sleep(CHECK_POLL_SEC)
    end = time.time() + CHECK_TIMEOUT_SEC
    while True:
        items = checks_now(slug, pr)
        b = [i.get("bucket") for i in items]
        if any(x in ("fail", "cancel") for x in b):
            raise OrchError("チェックが失敗: " + ", ".join(i["name"] for i in items if i.get("bucket") in ("fail", "cancel")))
        if items and need <= {i.get("name") for i in items} and all(x in ("pass", "skipping") for x in b):
            return
        if time.time() > end:
            raise OrchError("チェックが10分で終わらない")
        time.sleep(CHECK_POLL_SEC)


def import_canonical(c: Ctx, t: dict, short: str) -> str:
    repo_dir = c.root / t["repo"]
    script = repo_dir / "tools" / "sync" / "import_symbols.py"
    if not script.exists():
        print("警告: 正本の取り込みは未実装")
        return "未実装"
    py = c.local.get("idalib_python")
    if not py:
        print("警告: workspace.local.toml に idalib_python が無いので正本への取り込みを飛ばす")
        return "正本未取り込み"
    status = "取り込み済み"
    for b in t.get("symbols_builds", []):
        canon = c.data / "builds" / b / "ida" / ("%s.i64" % b)
        if not canon.is_file():
            print("警告: 正本の IDB が無い: %s" % canon)
            status = "正本未取り込み"
            continue
        lock = FileLock(str(c.state / ("idb-%s.lock" % b)), timeout=60) if FileLock else None
        try:
            if lock:
                lock.acquire()
            bk = c.data / "builds" / b / "ida" / "backup" / ("%s-%s.i64" % (b, short))
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(canon, bk)
            r = subprocess.run([py, str(script), "--idb", str(canon), "--symbols", str(repo_dir / "symbols" / b)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", env=tool_env(c))
            if r.returncode != 0:
                print("エラー: 取り込み失敗(%s): %s" % (b, (r.stderr or r.stdout).strip()[-300:]))
                status = "取り込み失敗"
        finally:
            if lock and lock.is_locked:
                lock.release()
    return status


def cmd_accept(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    key = tkey(a.repo, a.issue)
    scores = parse_scores(a.scores)
    wt = Path(t["worktree"])
    if t.get("state") != "stamped":
        raise OrchError("状態が %s。先に stamp が要る" % t.get("state"))
    if not t.get("stamp_ok"):
        raise OrchError("stamp の検査に失敗がある。直して save → stamp をやり直す")
    # stamp の後の変更は、accept 自身が作った準備のコミットだけ許す（途中で失敗した accept のやり直し）
    if git(c, wt, "status", "--porcelain").strip() or head(c, wt) not in (t.get("stamped_head"), t.get("prep_head")):
        raise OrchError("stamp の後に変更がある。もう一度 stamp する")
    slug = repo_slug(c, a.repo)
    comment = "合格\n\n採点: " + ", ".join("%s=%g" % kv for kv in scores.items())
    if a.dry_run:
        print("dry-run: 次を行う（何も書かない）")
        for s in ("origin/main を取り込み（merge）", "索引を作り直し、.task/ を消してコミット・push",
                  "PR #%s にコメント「%s」" % (t["pr"], comment.replace("\n", " ")), "PR を ready に",
                  "検査が揃うのを待ち、完了を待つ（最大 %d 秒・%d 分）" % (CHECK_APPEAR_SEC, CHECK_TIMEOUT_SEC // 60),
                  "gh pr merge --merge",
                  "手元の main を更新", "symbols 変更(%s)があれば正本の IDB へ取り込み" % (t.get("symbols_builds") or "なし"),
                  "台帳を accepted に"):
            print("  - " + s)
        return 0
    outgoing(comment)
    git(c, wt, "fetch", "origin")
    mr = subprocess.run([c.git, "merge", "origin/main", "-m", "main を取り込み"], cwd=str(wt), capture_output=True,
                        text=True, env=tool_env(c))
    if mr.returncode != 0:
        subprocess.run([c.git, "merge", "--abort"], cwd=str(wt), capture_output=True, env=tool_env(c))
        raise OrchError("main の取り込みで衝突。手で解決する:\n" + (mr.stdout + mr.stderr).strip()[-300:])
    kb = wt / "tools" / "kb.py"
    if kb.exists():
        run_py(c, wt, kb, ["index"])
    shutil.rmtree(wt / ".task", ignore_errors=True)
    commit_all(c, wt, "取り込み準備")
    update_task(c, key, prep_head=head(c, wt))  # 以後に失敗しても、やり直しでこの準備のコミットを許す
    if kb.exists():
        code, out = run_py(c, wt, kb, ["lint"])
        if code != 0:
            raise OrchError("取り込み前の lint が失敗:\n" + out[-400:])
    git(c, wt, "push", "origin", t["branch"])
    sha = head(c, wt)
    gh_run(["pr", "comment", str(t["pr"]), "--repo", slug, "--body", comment])
    gh_run(["pr", "ready", str(t["pr"]), "--repo", slug])
    wait_checks(c, slug, t["pr"], sha)
    gh_run(["pr", "merge", str(t["pr"]), "--repo", slug, "--merge"])
    repo_dir = c.root / a.repo
    git(c, repo_dir, "fetch", "origin")
    short = git(c, repo_dir, "rev-parse", "--short", "origin/main").strip()
    if git(c, repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main":
        git(c, repo_dir, "merge", "--ff-only", "origin/main")
    else:
        print("警告: 手元の %s が main でないので更新しない" % a.repo)
    imp = import_canonical(c, t, short) if t.get("symbols_builds") else "不要"
    update_task(c, key, state="accepted", accepted_at=now(), scores=scores, idb_import=imp, merge_commit=short,
                usage_after=read_usage(c))
    print("%s: 取り込み完了（%s、正本の取り込み: %s）" % (key, short, imp))
    print("片付け（手動）:\n  git -C %s worktree remove --force %s" % (repo_dir, wt))
    if t.get("idb"):
        print("  rm -r %s" % Path(t["idb"]["path"]).parent)
    return 1 if imp == "取り込み失敗" else 0


# ---------- reject ----------
def cmd_reject(a) -> int:
    c = Ctx()
    t = get_task(c, a.repo, a.issue)
    key = tkey(a.repo, a.issue)
    if t.get("state") in ("accepted", "rejected"):
        raise OrchError("すでに %s" % t["state"])
    comment = "却下: " + a.reason
    if a.dry_run:
        print("dry-run: PR #%s にコメント「%s」→ 閉じる（ブランチ削除）→ 台帳を rejected（Issue は閉じない）" % (t.get("pr"), comment))
        return 0
    outgoing(comment)
    if t.get("pr"):
        slug = repo_slug(c, a.repo)
        gh_run(["pr", "comment", str(t["pr"]), "--repo", slug, "--body", comment])
        gh_run(["pr", "close", str(t["pr"]), "--repo", slug, "--delete-branch"])
    update_task(c, key, state="rejected", rejected_at=now(), reject_reason=a.reason, usage_after=read_usage(c))
    print("%s: 却下" % key)
    return 0


# ---------- wrapup / weekly ----------
def next_step(wt: Path) -> str:
    n = wt / ".task" / "notes.md"
    if not n.exists():
        return ""
    out, on = [], False
    for line in n.read_text(encoding="utf-8").splitlines():
        if re.match(r"#+\s", line):
            on = "次の1手" in line
            continue
        if on:
            out.append(line)
    return "\n".join(out).strip()


def cmd_wrapup(a) -> int:
    c = Ctx()
    sec = ["## %s 中断" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    act = {k: t for k, t in load_tasks(c).items() if t.get("state") in ACTIVE}
    for k, t in act.items():
        try:
            save_task(c, t)
        except OrchError as e:
            print("警告: %s の save に失敗: %s" % (k, e))
        t = load_tasks(c)[k]
        sec.append("- %s: %s、%s、作業ツリー %s" % (k, t["state"], t.get("worker"), t["worktree"]))
        ns = next_step(Path(t["worktree"]))
        if ns:
            sec += ["  次の1手:"] + ["  " + x for x in ns.splitlines()]
    if not act:
        sec.append("- 動いているタスクなし")
    h = c.data / "orchestrator" / "handoff.md"
    h.parent.mkdir(parents=True, exist_ok=True)
    old = h.read_text(encoding="utf-8") if h.exists() else "# 引き継ぎメモ（オーケストラ）\n"
    lines = old.splitlines()
    i = 1 if lines and lines[0].startswith("# ") else 0
    new = lines[:i] + [""] + sec + [""] + lines[i:]
    h.write_text("\n".join(new).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    print("\n".join(sec))
    print("引き継ぎメモを更新: %s" % h)
    mirror(c)
    return 0


def cmd_weekly(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    since = datetime.datetime.now().astimezone() - datetime.timedelta(days=7)

    def recent(t, f):
        return t.get(f) and datetime.datetime.fromisoformat(t[f]) >= since

    ts = list(load_tasks(c).values())
    acc = [t for t in ts if recent(t, "accepted_at")]
    rej = [t for t in ts if recent(t, "rejected_at")]
    retry = sum(max(t.get("attempts", 1) - 1, 0) for t in ts if recent(t, "dispatched_at") or recent(t, "accepted_at"))
    models: dict[str, int] = {}
    for t in ts:
        if recent(t, "dispatched_at") or recent(t, "accepted_at") or recent(t, "rejected_at"):
            models[t.get("worker", "?")] = models.get(t.get("worker", "?"), 0) + 1
    title = "[週報] %s" % datetime.date.today().isoformat()
    body = "直近7日の集計\n\n- 取り込み: %d\n- 却下: %d\n- 再試行: %d\n- モデル別:\n%s\n" % (
        len(acc), len(rej), retry, "\n".join("  - %s: %d" % kv for kv in sorted(models.items())) or "  - なし")
    outgoing(title, body)
    out = gh_run(["issue", "create", "--repo", repo_slug(c, a.repo), "--title", title, "--body", body, "--label", "report"])
    per = periodic(c)
    per["weekly"] = now()
    (c.state / "periodic.json").write_text(json.dumps(per, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("週報を作成: #%s" % pr_number(out))
    return 0


# ---------- 入口 ----------
def main(argv=None) -> int:
    wslib.configure_stdio()
    p = argparse.ArgumentParser(prog="orch.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, f, repo=True, dry=False):
        s = sub.add_parser(name)
        if repo:
            s.add_argument("--repo", default=DEFAULT_REPO)
        if dry:
            s.add_argument("--dry-run", action="store_true")
        s.set_defaults(f=f)
        return s

    s = add("setup-github", cmd_setup_github, repo=False, dry=True)
    s.add_argument("repo")
    add("start", cmd_start, repo=False)
    add("status", cmd_status, repo=False)
    add("wrapup", cmd_wrapup, repo=False)
    add("weekly", cmd_weekly)
    s = add("issue", cmd_issue)
    s.add_argument("--scope", required=True, choices=list(SCOPES))
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--difficulty", required=True, choices=DIFFS)
    for k in ("title", "goal", "target", "materials"):
        s.add_argument("--" + k, required=True)
    s = add("dispatch", cmd_dispatch)
    s.add_argument("issue", type=int)
    s.add_argument("--worker", required=True)
    s.add_argument("--effort")
    s.add_argument("--idb", action="store_true")
    s.add_argument("--precheck")
    s.add_argument("--tools")
    s.add_argument("--ignore-limit", action="store_true")
    s = add("save", cmd_save)
    s.add_argument("issue", type=int)
    s.add_argument("--message")
    s = add("report", cmd_report)
    s.add_argument("issue", type=int)
    s.add_argument("--from", dest="src", required=True)
    s = add("codex", cmd_codex, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--model")
    s.add_argument("--effort")
    s = add("stamp", cmd_stamp)
    s.add_argument("issue", type=int)
    s = add("accept", cmd_accept, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--scores", required=True)
    s = add("reject", cmd_reject, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--reason", required=True)
    a = p.parse_args(argv)
    try:
        return a.f(a)
    except OrchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
