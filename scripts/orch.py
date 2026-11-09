#!/usr/bin/env python3
"""司令塔の道具。台帳（_local/state/tasks.json）を軸に、Issue・依頼・Codex の起動・報告の保存・保存・採点用の要約・取り込み・却下を行う。

GitHub への操作は gh_run() の1か所に集め、試験で差し替える。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.parse
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402
import capacity  # noqa: E402
import export_game_api  # noqa: E402
import usage_watch  # noqa: E402
import sync_skills  # noqa: E402
import wslib  # noqa: E402

try:
    from filelock import FileLock
except ImportError:  # 依存が無い環境でも読み込めるように
    FileLock = None

SCOPES = {"acnl": "[ACNL]", "hhd": "[HHD]", "3ds": "[3DS]"}
KINDS = ["analysis", "import", "triage", "consolidate", "reanalyze", "review", "tooling", "report"]
DIFFS = ["easy", "normal", "hard"]
# 作業の途中の状態（ACTIVE）。dispatched は、ワーカーが作業中（動いている）。
# saved は保存済み、stamped は stamp 済みで、どちらもワーカーは止まって評価を待つ。
ACTIVE = ("dispatched", "saved", "stamped")
RUNNING = ("dispatched",)  # 並列数で数える「動いている」state
# 採点表の項目（workers.toml の [evaluation].pass_min で判定する）。役ごとに、この順で並ぶ。
EVAL_ITEMS = {
    "analysis": ("根拠と結論の一致", "根拠の独立性", "既存の知見との整合", "確度の妥当性", "名前・型の妥当性"),
    "derived": ("完了条件の達成", "設計書との整合", "既存の知見との整合", "試験の妥当性", "決まりの順守"),
}
DEEP_REVIEW_SPEC = "spec/game_api.toml"  # 派生リポジトリの直下にある、ゲーム API の定義
DEFAULT_REPO = "ac3ds-re"
CHECK_POLL_SEC = 15
CHECK_APPEAR_SEC = 90  # push したコミットに検査が現れるまで待つ上限
CHECK_TIMEOUT_SEC = 600

LABEL_WEBCHAT = "webchat"  # WebChat が担当する Issue
LABEL_BUSY = "作業中"  # ワーカーが取って作業している Issue（重複の防止）

LABELS = (
    [(s, "1d76db", "スコープ") for s in SCOPES]
    + [(k, "5319e7", "種類") for k in KINDS]
    + [(d, "0e8a16", "難易度") for d in DIFFS]
    + [(LABEL_WEBCHAT, "fbca04", "WebChat が担当"), (LABEL_BUSY, "d93f0b", "ワーカーが作業中")]
)
WEBCHAT_MODEL = "gpt-6-sol"  # WebChat のモデル名（kb.py intake --by に渡す）
WEBCHAT_INBOX = "inbox/webchat"  # WebChat の記録の下書きの置き場（解析リポジトリの中）


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
    # Git for Windows の全体の設定 core.fsmonitor=true だと、作業ツリーごとに常駐の処理が残り続ける
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    env.update({"GIT_CONFIG_COUNT": str(n + 1), "GIT_CONFIG_KEY_%d" % n: "core.fsmonitor",
                "GIT_CONFIG_VALUE_%d" % n: "false"})
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


def canonical_path(c: Ctx, build: str) -> Path:
    return c.data / "builds" / build / "ida" / (build + ".i64")


def canonical_state(c: Ctx) -> dict:
    p = c.state / "canonical.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def record_canonical(c: Ctx, build: str) -> None:
    path = canonical_path(c, build)
    with path.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    c.state.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(c.state / "canonical.json.lock"), timeout=30) if FileLock else None
    with lock if lock else nullcontext():
        state = canonical_state(c)
        state.setdefault("builds", {})[build] = {"sha256": digest, "at": now()}
        p = c.state / "canonical.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, p)


def outside_status(c: Ctx) -> dict[str, list[str]]:
    places = {"workspace": c.root, **{name: c.root / name for name in c.repos()}}
    return {name: git(c, path, "status", "--porcelain").splitlines()
            for name, path in places.items() if (path / ".git").exists()}


def _is_link(entry) -> bool:
    return entry.is_symlink() or entry.is_junction()


def _scan_links(path: Path) -> list[Path]:
    """path の下のリンク（ジャンクション・シンボリックリンク）を、辿らずに集める。"""
    found: list[Path] = []
    stack = [Path(path)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except FileNotFoundError:
            continue
        with it:
            for e in it:
                if _is_link(e):
                    found.append(Path(e.path))
                elif e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
    return found


def unlink_links(path) -> list[Path]:
    """path の下のリンクを、リンクそのものだけ消す（先の中身には触らない）。消したリンクの一覧を返す。
    Git for Windows の worktree remove がジャンクションを辿って先を消す事故を防ぐ。"""
    path = Path(path)
    removed = _scan_links(path)
    for link in removed:
        try:
            try:  # ジャンクション・ディレクトリのシンボリックリンクはリンク自体を rmdir
                os.rmdir(link)
            except OSError:
                os.unlink(link)  # ファイルのシンボリックリンク
        except OSError as e:
            raise OrchError("リンクを外せない: %s: %s" % (link, e))
    left = _scan_links(path)
    if left:
        raise OrchError("リンクが残っている: " + ", ".join(str(p) for p in left[:5]))
    return removed


def remove_worktree_safely(c: Ctx, repo_dir, wt, check: bool = True) -> None:
    """リンクを外してから git worktree remove --force。"""
    wt = Path(wt)
    if wt.exists():
        unlink_links(wt)
    extra = {} if check else {"check": False}
    git(c, repo_dir, "worktree", "remove", "--force", str(wt), **extra)


def cleanup_task(c: Ctx, key: str, t: dict, keep: bool = False) -> None:
    if keep:
        update_task(c, key, cleanup="保持", cleanup_errors=[])
        return
    errors = []
    repo_dir = c.root / t["repo"]
    wt = Path(t["worktree"])
    try:
        remove_worktree_safely(c, repo_dir, wt)
    except OrchError as e:
        errors.append(str(e))
    if wt.exists():
        for attempt in range(3):
            try:
                wt.rmdir()  # Git が中身を消した後の空フォルダだけを対象にする
                break
            except OSError as e:
                if attempt == 2:
                    errors.append("作業ツリーの空フォルダ: %s: %s" % (wt, e))
                else:
                    time.sleep(0.2)
    try:
        git(c, repo_dir, "branch", "-D", t["branch"])
    except OrchError as e:
        errors.append(str(e))
    if t.get("idb"):
        path = Path(t["idb"]["path"]).parent
        if path.exists():
            try:
                shutil.rmtree(path)
            except OSError as e:
                errors.append("IDB 作業用コピー: %s: %s" % (path, e))
    update_task(c, key, cleanup="警告" if errors else "完了", cleanup_errors=errors)
    for error in errors:
        print("警告: 片付け: " + error)


def tkey(repo: str, n) -> str:
    return "%s#%s" % (repo, n)


def get_task(c: Ctx, repo: str, n) -> dict:
    t = load_tasks(c).get(tkey(repo, n))
    if not t:
        raise OrchError("台帳にタスクが無い: %s（先に dispatch）" % tkey(repo, n))
    return t


def require_unpaused(t: dict) -> None:
    if t.get("state") == "paused":
        raise OrchError("保留中のタスク。先に unpause する")


def cmd_pause(a) -> int:
    c = Ctx()
    t = get_task(c, a.repo, a.issue)
    if t.get("state") not in ACTIVE:
        raise OrchError("状態が %s。動いているタスクだけ pause できる" % t.get("state"))
    key = tkey(a.repo, a.issue)
    update_task(c, key, state="paused", paused_from=t["state"], pause_reason=a.reason, paused_at=now())
    print("%s: 保留（%s）" % (key, a.reason))
    return 0


def cmd_unpause(a) -> int:
    c = Ctx()
    t = get_task(c, a.repo, a.issue)
    if t.get("state") != "paused" or t.get("paused_from") not in ACTIVE:
        raise OrchError("有効な保留状態でない: %s" % t.get("state"))
    key = tkey(a.repo, a.issue)
    update_task(c, key, state=t["paused_from"], paused_from=None, pause_reason=None, paused_at=None)
    print("%s: 保留を解除（%s）" % (key, t["paused_from"]))
    return 0


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


def capacity_reports(c: Ctx, extra_issue=None) -> dict:
    issues = []
    for repo in c.repos():
        raw = gh_run(["issue", "list", "--repo", repo_slug(c, repo), "--state", "open", "--limit", "100",
                      "--json", "number,title,body,labels"])
        try:
            listed = json.loads(raw)
        except ValueError as e:
            raise OrchError("open Issue の一覧が JSON でない: %s" % repo) from e
        for item in listed:
            item["repo"] = repo
            issues.append(item)
    if extra_issue and not any(i["repo"] == extra_issue["repo"] and i["number"] == extra_issue["number"] for i in issues):
        issues.append(extra_issue)
    tasks = load_tasks(c)
    usage = {provider: capacity.read_provider_usage(c.state, provider) for provider in ("claude", "codex")}
    current = datetime.datetime.now(datetime.timezone.utc)
    limits = c.workers.get("limits", {})
    return {
        provider: capacity.calculate(
            provider, issues, tasks, c.workers.get("workers", {}), limits,
            usage[provider].get("five_hour_used_percent"),
            usage[provider].get("resets_at"), current,
            capacity.codex_limit_until(c.data / "logs", current, usage["codex"].get("resets_at"))
            if provider == "codex" else None)
        for provider in ("claude", "codex")
    }


def cmd_capacity(a) -> int:
    c = Ctx()
    for provider, report in capacity_reports(c).items():
        fmt = lambda x: "不明" if x is None else "%.1f%%" % x
        print("%s: 使用率 %s、余裕 %s、1つ分の消費 %.1f%%、推奨 %d" % (
            provider, fmt(report["usage"]), fmt(report["headroom"]), report["cost"], report["recommended"]))
        for model, cost in report["model_costs"].items():
            print("  %s: %.1f%%" % (model, cost))
        for issue in report["candidates"]:
            print("  候補 %s#%s %s" % (issue["repo"], issue["number"], issue.get("title", "")))
        for overlap in report["overlaps"]:
            print("  重複 %s#%s と %s: %s" % (overlap["repo"], overlap["number"], ", ".join(overlap["with"]),
                                       ", ".join("0x%X" % n for n in overlap["addresses"])))
        if report["limit_until"]:
            print("  usage limit のリセット: %s" % report["limit_until"].isoformat())
    return 0


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


def set_label(c: Ctx, repo: str, n, name: str, on: bool) -> None:
    """Issue のラベルを付ける/外す。失敗は警告にとどめ、本処理は止めない。"""
    try:
        gh_run(["issue", "edit", str(n), "--repo", repo_slug(c, repo), "--add-label" if on else "--remove-label", name])
    except OrchError as e:
        print("警告: Issue #%s のラベル「%s」を%sけられなかった: %s" % (n, name, "付" if on else "外", e))


def label_names(issue: dict) -> set[str]:
    return {lb.get("name") for lb in issue.get("labels", [])}


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
            gh_run(["api", "-X", "PATCH", "repos/%s/labels/%s" % (slug, urllib.parse.quote(name)), "--input", "-"], body)
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
def export_state(c: Ctx) -> dict:
    p = c.state / "export.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def export_builds(c: Ctx) -> list[str]:
    p = c.root / DEFAULT_REPO / "meta" / "builds.toml"
    if not p.is_file():
        raise OrchError("builds.toml が無い: %s" % p)
    builds = tomllib.loads(p.read_text(encoding="utf-8")).get("builds", {})
    return [name for name, info in builds.items() if "保管だけ" not in info.get("note", "")]


def run_export(c: Ctx, build: str, only: list[str] | None = None, commit: str | None = None) -> dict:
    if build not in export_builds(c):
        raise OrchError("解析対象ではないビルド: %s" % build)
    if FileLock is None:
        raise OrchError("filelock が無いので正本の IDB を安全に写せない")
    py = c.local.get("idalib_python")
    if not py:
        raise OrchError("workspace.local.toml に idalib_python が無い")
    repo = c.root / DEFAULT_REPO
    script = repo / "tools" / "sync" / "export_all.py"
    if not script.is_file():
        raise OrchError("書き出しの道具が無い: %s" % script)
    canon = c.data / "builds" / build / "ida" / (build + ".i64")
    if not canon.is_file():
        raise OrchError("正本の IDB が無い: %s" % canon)
    commit = commit or git(c, repo, "rev-parse", "main").strip()
    copy = canon.parent / "export-work" / canon.name
    c.state.mkdir(parents=True, exist_ok=True)
    with FileLock(str(c.state / ("idb-%s.lock" % build)), timeout=60):
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canon, copy)
        record_canonical(c, build)
    out = c.data / "builds" / build / "export"
    args = [py, str(script), "--idb", str(copy), "--build", build, "--out", str(out),
            "--symbols-commit", commit]
    for name in only or []:
        args += ["--only", name]
    started = time.monotonic()
    r = subprocess.run(args, cwd=str(repo), env=tool_env(c), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise OrchError("書き出し失敗(%s): %s" % (build, (r.stderr or r.stdout).strip()[-500:]))
    elapsed = round(time.monotonic() - started, 3)
    with FileLock(str(c.state / "export.json.lock"), timeout=30):
        state = export_state(c)
        old = state.setdefault("builds", {}).get(build, {})
        finished_at = now()
        entry = {"commit": commit, "at": finished_at, "mode": "partial" if only else "full", "seconds": elapsed,
                 "full_at": old.get("full_at") if only else finished_at}
        state["builds"][build] = entry
        p = c.state / "export.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, p)
    return entry


def cmd_export(a) -> int:
    c = Ctx()
    builds = [a.build] if a.build else export_builds(c)
    commit = git(c, c.root / DEFAULT_REPO, "rev-parse", "main").strip()
    for build in builds:
        entry = run_export(c, build, a.only, commit)
        print("%s: 書き出し完了（%s、%s 秒）" % (build, entry["mode"], entry["seconds"]))
    return 0


def saves_of(t: dict) -> int:
    """保存した回数。attempts は「次に使うコミット番号」（dispatch で 1）なので、1 引く。"""
    return max(int(t.get("attempts") or 0) - 1, 0)


def cmd_status(a) -> int:
    c = Ctx()
    ts = load_tasks(c)
    if not ts:
        print("台帳: タスクなし")
    for k, t in ts.items():
        if t.get("state") != "paused":
            print("%s  %s  %s 保存%s回 PR=%s" % (k, t.get("state"), t.get("worker"), saves_of(t), t.get("pr") or "-"))
    paused = {k: t for k, t in ts.items() if t.get("state") == "paused"}
    if paused:
        print("== 保留中のタスク ==")
        for k, t in paused.items():
            print("%s  保留（%s、理由: %s）  %s 保存%s回 PR=%s" % (
                k, t.get("paused_from"), t.get("pause_reason"), t.get("worker"), saves_of(t), t.get("pr") or "-"))
    return 0


def claude_log_stats(path: Path) -> dict:
    """Claude のワーカーのログ（stream-json）1 本から、費用・要求の数・最大の文脈を集める。

    - 費用: result の total_cost_usd は 1 回の起動の中で増えていくので、最大の値を取る。
    - cumulative: 費用が前のログからの累積か（上の注を参照）。
    - 要求: assistant の message.id で重複を除く（同じ要求が複数の行に分かれて出る）。
    - 文脈: 要求ごとの入力側のトークン（入力＋キャッシュの作成＋キャッシュの読み出し）の最大。
      output_tokens は途中の値なので使わない。
    """
    cost, ids, context = 0.0, {}, 0
    model_cache = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"cost": 0.0, "requests": 0, "max_context": 0, "cumulative": False}
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "result":
            try:
                cost = max(cost, float(d.get("total_cost_usd") or 0))
            except (TypeError, ValueError):
                pass
            usage = d.get("modelUsage")
            if isinstance(usage, dict):
                model_cache = sum(int(v.get("cacheReadInputTokens") or 0) for v in usage.values() if isinstance(v, dict))
        elif d.get("type") == "assistant" and isinstance(d.get("message"), dict):
            m = d["message"]
            u = m.get("usage") or {}
            if m.get("id"):
                ids[m["id"]] = int(u.get("cache_read_input_tokens") or 0)
            used = sum(int(u.get(k) or 0) for k in
                       ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            context = max(context, used)
    # --resume のログの費用は、前の起動からの累積の時とそうでない時がある。最後の result の
    # modelUsage のキャッシュ読み出しが、このログの assistant の合計より大きければ累積。
    return {"cost": cost, "requests": len(ids), "max_context": context,
            "cumulative": model_cache > sum(ids.values())}


def _log_number(p: Path) -> int:
    m = re.search(r"_(\d+)\.jsonl$", p.name)
    return int(m.group(1)) if m else 0


def task_claude_stats(logs: Path | None, key: str) -> dict:
    """タスク（"repo#n"）の claude_<repo>_<n>_*.jsonl を全部足す（文脈は最大）。"""
    out = {"cost": 0.0, "requests": 0, "max_context": 0}
    if logs is None or "#" not in key:
        return out
    repo, n = key.rsplit("#", 1)
    prev = 0.0
    for p in sorted(logs.glob("claude_%s_%s_*.jsonl" % (repo, n)), key=_log_number):
        s = claude_log_stats(p)
        out["cost"] += s["cost"] - prev if s["cumulative"] else s["cost"]
        prev = s["cost"]
        out["requests"] += s["requests"]
        out["max_context"] = max(out["max_context"], s["max_context"])
    return out


def stats_rows(tasks, logs: Path | None = None):
    rows = {}
    for key, t in tasks.items():
        model = t.get("worker")
        if not model:
            continue
        row = rows.setdefault(model, {"count": 0, "accepted": 0, "rejected": 0,
                                      "attempts": 0, "saves": 0, "violations": {}, "duration_hours": [],
                                      "cost": 0.0, "requests": 0, "max_context": 0, "per_task": {}})
        row["count"] += 1
        state = t.get("state")
        if state in ("accepted", "rejected"):
            row[state] += 1
        row["attempts"] += int(t.get("attempts") or 0)
        row["saves"] += saves_of(t)
        cs = task_claude_stats(logs, key)
        row["per_task"][key] = cs
        row["cost"] += cs["cost"]
        row["requests"] += cs["requests"]
        row["max_context"] = max(row["max_context"], cs["max_context"])
        for key, value in t.items():
            if value and (key.endswith("_violation") or key == "violations"):
                row["violations"][key] = row["violations"].get(key, 0) + 1
        start = capacity.instant(t.get("dispatched_at"))
        end = capacity.instant(t.get("accepted_at") or t.get("rejected_at") or t.get("codex_finished_at")
                               or t.get("claude_finished_at"))
        if start and end and end >= start:
            row["duration_hours"].append((end - start).total_seconds() / 3600)
    return rows


def cmd_stats(a) -> int:
    c = Ctx()
    rows = stats_rows(load_tasks(c), c.data / "logs")
    if not rows:
        print("台帳: タスクなし")
    for model, row in sorted(rows.items()):
        n = row["count"]
        durations = row["duration_hours"]
        violations = ", ".join("%s=%d" % item for item in sorted(row["violations"].items())) or "なし"
        duration = "%.2f時間" % (sum(durations) / len(durations)) if durations else "不明"
        print("%s: 件数 %d、合格 %d、却下 %d、合格率 %.1f%%、平均保存 %.2f回、違反 %s、平均所要 %s、"
              "費用 $%.2f、要求 %d、最大の文脈 %d" % (
                  model, n, row["accepted"], row["rejected"], row["accepted"] * 100 / n,
                  row["saves"] / n, violations, duration, row["cost"], row["requests"], row["max_context"]))
        for key, cs in sorted(row["per_task"].items()):
            if cs["requests"] or cs["cost"]:
                print("  %s: 費用 $%.2f、要求 %d、最大の文脈 %d" % (key, cs["cost"], cs["requests"], cs["max_context"]))
    return 0


def stats_summary(c: Ctx) -> list[str]:
    """start で出す数行。モデルごとの件数・合格・費用・最大の文脈。"""
    rows = stats_rows(load_tasks(c), c.data / "logs")
    return ["%s: 件数 %d、合格 %d、費用 $%.2f、要求 %d、最大の文脈 %d" % (
        m, r["count"], r["accepted"], r["cost"], r["requests"], r["max_context"])
        for m, r in sorted(rows.items())] or ["台帳: タスクなし"]


def cmd_start(a) -> int:
    c = Ctx()
    usage_watch.collect(c.root)
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
    paused = {k: t for k, t in load_tasks(c).items() if t.get("state") == "paused"}
    print("== 保留中のタスク ==")
    for k, t in paused.items():
        print("%s  %s  理由: %s" % (k, t.get("paused_from"), t.get("pause_reason")))
    if not paused:
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
    print("== 測定 ==")
    for line in stats_summary(c):
        print(line)
    mirror(c)
    return 0


# ---------- issue ----------
def issue_body(scope: str, kind: str, difficulty: str, goal: str, target: str, materials: str) -> str:
    return ("### スコープ\n\n%s\n\n### 種類\n\n%s\n\n### 難易度\n\n%s\n\n### 目的と完了条件\n\n%s\n\n"
            "### 対象\n\n%s\n\n### 関係する資料\n\n%s\n") % (
                scope, kind, difficulty, goal.strip(), target.strip(), materials.strip())


def unconsolidated_counts(repo_dir: Path) -> dict[tuple[str, str], int]:
    """kb.py と同じ記録配置を読み、未消化だけを数える。索引は更新前のことがあるため使わない。"""
    base = repo_dir / "findings"
    if not base.is_dir():
        raise OrchError("findings/ が無い: %s" % repo_dir)
    counts: dict[tuple[str, str], int] = {}
    for scope_dir in sorted(base.iterdir()):
        if not scope_dir.is_dir() or scope_dir.name not in SCOPES:
            continue
        for folder in sorted(scope_dir.iterdir()):
            if not folder.is_dir() or not re.fullmatch(r"\d+-\d+", folder.name):
                continue
            for path in sorted(folder.glob("*.md")):
                raw = path.read_text(encoding="utf-8")
                match = re.match(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", raw, re.S)
                if not match:
                    raise OrchError("記録の frontmatter が無い: %s" % path)
                try:
                    meta = yaml.safe_load(match.group(1))
                except yaml.YAMLError as e:
                    raise OrchError("記録の frontmatter が読めない: %s: %s" % (path, e)) from e
                if not isinstance(meta, dict):
                    raise OrchError("記録の frontmatter が辞書ではない: %s" % path)
                category = meta.get("category")
                if (meta.get("state") != "active" or meta.get("consolidated_into")
                        or not isinstance(category, str) or not category or category in ("_triage", "_product")):
                    continue
                key = (scope_dir.name, category)
                counts[key] = counts.get(key, 0) + 1
    return counts


def consolidation_threshold(c: Ctx, override: int | None) -> int:
    value = override if override is not None else c.workers.get("limits", {}).get("consolidate_threshold", 10)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OrchError("consolidate_threshold は 0 以上の整数にする")
    return value


def consolidation_candidates(c: Ctx, repo: str, threshold: int | None = None) -> list[tuple[str, str, int]]:
    need_repo(c, repo)
    limit = consolidation_threshold(c, threshold)
    counts = unconsolidated_counts(c.root / repo)
    return [(scope, category, count) for (scope, category), count in sorted(counts.items()) if count > limit]


def consolidation_title(scope: str, category: str) -> str:
    return "%s %s の記録を topics にまとめる" % (SCOPES[scope], category)


def same_consolidation_issue(issue: dict, scope: str, category: str) -> bool:
    if issue.get("title") == consolidation_title(scope, category):
        return True
    labels = {(lb.get("name") if isinstance(lb, dict) else lb) for lb in issue.get("labels", [])}
    if "consolidate" not in labels:
        return False
    sections = parse_sections(issue.get("body", ""))
    return (sections.get("スコープ") == scope
            and sections.get("対象") == "findings/%s の %s カテゴリ" % (scope, category))


def report_consolidation(c: Ctx, repo: str, threshold: int | None = None,
                         create: bool = False, skip_missing: bool = False) -> list[tuple[str, str, int]]:
    if skip_missing and (repo not in c.repos() or not (c.root / repo / "findings").is_dir()):
        return []
    candidates = consolidation_candidates(c, repo, threshold)
    if not candidates:
        if not skip_missing:
            print("しきい値を超えたカテゴリ: なし")
        return candidates
    if skip_missing:
        print("== 記録の消化 ==")
    open_issues = []
    if create:
        raw = gh_run(["issue", "list", "--repo", repo_slug(c, repo), "--state", "open", "--limit", "1000",
                      "--json", "number,title,body,labels"])
        try:
            open_issues = json.loads(raw)
        except ValueError as e:
            raise OrchError("open Issue の一覧が JSON でない: %s" % repo) from e
    for scope, category, count in candidates:
        title = consolidation_title(scope, category)
        print("%s/%s: 未消化 %d 件（%s）" % (scope, category, count, title))
        if not create:
            continue
        existing = next((it for it in open_issues if same_consolidation_issue(it, scope, category)), None)
        if existing:
            print("  既存の Issue #%s が開いているため作成しない" % existing.get("number", "?"))
            continue
        goal = ("%s/%s の未消化の記録を確認し、topics/%s/%s.md に現時点で分かっていることを統合する。\n"
                "格付け A 以上のワーカーが担当し、「?」の旧記録は再解析と同時に扱う（旧記録への利用枠は3割まで）。\n"
                "完了条件: 一次証拠と既存の topics を照合し、根拠となる記録を示して topics を更新する。"
                "統合・重複する記録は報告で提案する。記録の state と consolidated_into は仕上げで確定する。") % (
                    scope, category, scope, category)
        body = issue_body(scope, "consolidate", "normal", goal, "findings/%s の %s カテゴリ" % (scope, category),
                          "findings/%s/by-category/%s.md、topics/%s/%s.md、ac3ds-re の consolidate-category スキル" % (
                              scope, category, scope, category))
        outgoing(title, body)
        args = ["issue", "create", "--repo", repo_slug(c, repo), "--title", title, "--body", body]
        for label in (scope, "consolidate", "normal"):
            args += ["--label", label]
        number = pr_number(gh_run(args))
        print("  Issue を作成: #%s" % number)
        open_issues.append({"number": number, "title": title, "labels": [{"name": "consolidate"}]})
    return candidates


def cmd_consolidate_check(a) -> int:
    report_consolidation(Ctx(), a.repo, a.threshold, a.create)
    return 0


def analysis_leaks(c: Ctx, text: str) -> list[str]:
    """派生リポジトリに書いてはいけない解析の情報（記録 ID・解析リポジトリの名前とパス）。export_game_api の FORBIDDEN と同じ考え。"""
    found = {m.group(0) for m in export_game_api.FORBIDDEN.finditer(text)}
    path = str(c.root / DEFAULT_REPO)
    for p in (path, path.replace("\\", "/")):
        if p in text:
            found.add(p)
    return sorted(found)


def webchat_section() -> str:
    return ("### WebChat への指示\n\n"
            "- この Issue を取る時に、ラベル「%s」を付ける。\n"
            "- 記録の下書きは `%s/<この Issue の番号>/<連番>.md` に置く（連番は 1, 2, ... ）。"
            "ID は書かない（司令塔が振る）。\n"
            "- ブランチ `webchat/<この Issue の番号>` から PR を出し、PR 本文の先頭に `Issue #<この Issue の番号>` と書く。"
            "PR の差分は `%s/<この Issue の番号>/` の中だけにする。\n"
            "- 上の「対象」「関係する資料」の関数名・番地・既存の記録の ID を手掛かりにする。\n") % (
                LABEL_BUSY, WEBCHAT_INBOX, WEBCHAT_INBOX)


def cmd_issue(a) -> int:
    c = Ctx()
    role = need_repo(c, a.repo).get("role", "analysis")
    if a.webchat and (role != "analysis" or a.repo != DEFAULT_REPO):
        raise OrchError("--webchat は解析リポジトリ（%s）の Issue だけで使える" % DEFAULT_REPO)
    for k in ("title", "goal", "target", "materials"):
        if not (getattr(a, k) or "").strip():
            raise OrchError("必須項目が空: --%s" % k)
    title = a.title.strip()
    pre = SCOPES[a.scope]
    if not title.startswith(pre):
        title = "%s %s" % (pre, title)
    body = issue_body(a.scope, a.kind, a.difficulty, a.goal, a.target, a.materials)
    if a.webchat:
        body += "\n" + webchat_section()
    if role == "derived":
        leaks = analysis_leaks(c, title + "\n" + body)
        if leaks:
            raise OrchError("派生リポジトリの Issue に解析の情報を書かない: %s" % ", ".join(leaks))
    outgoing(title, body)
    args = ["issue", "create", "--repo", repo_slug(c, a.repo), "--title", title, "--body", body]
    for lb in (a.scope, a.kind, a.difficulty, *([LABEL_WEBCHAT] if a.webchat else [])):
        args += ["--label", lb]
    out = gh_run(args)
    number = pr_number(out)
    print("Issue を作成: #%s %s" % (number, title))
    if a.webchat:
        print("WebChat に渡す。PR が出たら: python scripts/orch.py webchat verify <PR の番号>")
        return 0
    repo_option = " --repo %s" % a.repo if a.repo != DEFAULT_REPO else ""
    print("次の依頼: python scripts/orch.py dispatch %s --worker ...%s" % (number, repo_option))
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


def dispatch_export_line(c: Ctx, build: str) -> str:
    entry = export_state(c).get("builds", {}).get(build)
    if not entry or not (c.data / "builds" / build / "export" / "index.txt").is_file():
        return ""
    commit = entry["commit"]
    line = "書き出し: %s（索引 index.txt、元の commit %s）" % (
        c.data / "builds" / build / "export", commit)
    accepted = [t for t in load_tasks(c).values() if t.get("state") == "accepted"
                and t.get("idb_import") == "取り込み済み" and build in t.get("symbols_builds", [])
                and t.get("merge_commit")]
    if accepted:
        latest = max(accepted, key=lambda t: t.get("accepted_at", ""))["merge_commit"]
        if not latest.startswith(commit) and not commit.startswith(latest) and git_ok(
                c, c.root / DEFAULT_REPO, "merge-base", "--is-ancestor", commit, latest):
            line += "。書き出しは %s 時点。以後に変わった名前は symbols を見る" % commit
    return line


# 事前確認の結果に、Issue の本文と事前確認の中の番地を kb.py addr で自動で引いて付ける。引くのは 0x00100000 以上だけ
AUTO_ADDR_MIN = 0x00100000
AUTO_ADDR_MAX = 30
ADDR_RE = re.compile(r"(?<![0-9A-Za-z_])0x([0-9A-Fa-f]+)(?![0-9A-Za-z_])")


def read_precheck(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    except UnicodeDecodeError as e:
        raise OrchError("事前確認の結果が UTF-8 で読めない: %s" % path) from e
    if not text:
        raise OrchError("事前確認の結果が空（ファイルが無い、または空白だけ: %s）。"
                        "kb.py search と addr で引いた語・番地と結果を書く" % path)
    return text


def auto_addresses(*texts: str) -> list[int]:
    """与えた順に 0x... の番地を集める。同じ値は1つにし、AUTO_ADDR_MIN 未満は除く。"""
    found: list[int] = []
    for text in texts:
        for m in ADDR_RE.finditer(text or ""):
            v = int(m.group(1), 16)
            if v >= AUTO_ADDR_MIN and v not in found:
                found.append(v)
    return found


def lookup_addr(c: Ctx, value: int) -> str:
    """ac3ds-re の kb.py addr で番地を引く。引けなくても例外にせず、「引けなかった（理由）」の文字列を返す。"""
    repo_dir = c.root / DEFAULT_REPO
    if not repo_dir.is_dir():
        return "引けなかった（%s が無い）" % DEFAULT_REPO
    kb = repo_dir / "tools" / "kb.py"
    if not kb.is_file():
        return "引けなかった（kb.py が無い）"
    try:
        code, out = run_py(c, repo_dir, kb, ["addr", "0x%08X" % value, "--ref-limit", "3"])
    except OSError as e:
        return "引けなかった（kb.py を起動できない: %s）" % e
    if code != 0:
        return "引けなかった（kb.py が終了コード %d: %s）" % (code, out.splitlines()[-1] if out else "出力なし")
    return out or "引けなかった（kb.py の出力が空）"


def auto_addr_section(c: Ctx, *texts: str) -> str:
    found = auto_addresses(*texts)
    if not found:
        return ""
    lines = ["自動で引いた番地（kb.py addr --ref-limit 3）:"]
    for v in found[:AUTO_ADDR_MAX]:
        lines.append("- 0x%08X" % v)
        lines += ["  " + x for x in lookup_addr(c, v).splitlines()]
    if len(found) > AUTO_ADDR_MAX:
        lines.append("- ほか %d 件は引かない（上限 %d 件）" % (len(found) - AUTO_ADDR_MAX, AUTO_ADDR_MAX))
    return "\n".join(lines)


def ignore_reason(a) -> str:
    """--ignore-limit <理由> の理由。付けなければ ""。理由が空の指定は拒否する。"""
    raw = getattr(a, "ignore_limit", None)
    if isinstance(raw, str) and not raw.strip():
        raise OrchError("--ignore-limit には理由を書く（台帳に残す）")
    return raw.strip() if isinstance(raw, str) else ""


def cmd_dispatch(a) -> int:
    precheck = read_precheck(a.precheck)  # 空なら、Git にも GitHub にも触る前に止める
    ignore = ignore_reason(a)
    ignored: list[str] = []  # 理由を付けて無視した制限（台帳に残す）
    c = Ctx()
    usage_watch.collect(c.root)
    role = need_repo(c, a.repo).get("role", "analysis")
    wks = c.workers.get("workers", {})
    if a.worker not in wks:
        raise OrchError("workers.toml に無いワーカー: %s（%s）" % (a.worker, ", ".join(wks)))
    w = wks[a.worker]
    lim = c.workers.get("limits", {}).get("stop_at_used_percent", 80)
    usage = capacity.read_provider_usage(c.state, w.get("provider")).get("five_hour_used_percent")
    if usage is not None and usage >= lim:
        if not ignore:
            raise OrchError("5時間枠の使用率 %s%% が上限 %s%% 以上なので依頼しない（--ignore-limit <理由> で無視）" % (usage, lim))
        ignored.append("5時間枠 %s%%（上限 %s%%）" % (usage, lim))
    raw = gh_run(["api", "repos/%s/issues/%s" % (repo_slug(c, a.repo), a.issue)])
    try:
        issue_api = json.loads(raw)
    except ValueError:
        raise OrchError("Issue #%s の種別確認結果が JSON でない" % a.issue)
    if "pull_request" in issue_api:
        raise OrchError("#%s は PR。Issue の番号を指定する" % a.issue)
    issue = read_issue(c, a.repo, a.issue)
    if issue.get("state") != "OPEN":
        raise OrchError("Issue #%s は開いていない。開いた Issue の番号を指定する" % a.issue)
    labels = label_names(issue)
    if LABEL_WEBCHAT in labels:
        raise OrchError("Issue #%s にはラベル「%s」が付いている（WebChat の担当）。ワーカーには依頼しない" % (a.issue, LABEL_WEBCHAT))
    prior = load_tasks(c).get(tkey(a.repo, a.issue))
    if LABEL_BUSY in labels and not (prior and prior.get("state") in ACTIVE):
        raise OrchError("Issue #%s にはラベル「%s」が付いている（別のワーカーが作業中）。依頼しない" % (a.issue, LABEL_BUSY))
    sec = parse_sections(issue.get("body", ""))
    scope = sec.get("スコープ", "").strip()
    if scope not in SCOPES:
        scope = next((lb["name"] for lb in issue.get("labels", []) if lb.get("name") in SCOPES), "")
    key = tkey(a.repo, a.issue)
    old = load_tasks(c).get(key)
    if old:
        require_unpaused(old)
    if not (old and old.get("state") in ACTIVE):
        provider = w.get("provider")
        current_issue = dict(issue, repo=a.repo, number=a.issue)
        report = capacity_reports(c, current_issue).get(provider)
        if report:
            # 稼働中は動いている作業（RUNNING）だけ。保存済み・stamp 済みは、ワーカーが止まって評価を待っている
            active_count = sum(t.get("provider") == provider and t.get("state") in RUNNING
                               for t in load_tasks(c).values())
            if active_count + 1 > report["recommended"]:
                msg = "%s の推奨並列数 %d を超える（稼働中 %d、--ignore-limit <理由> で無視）" % (
                    provider, report["recommended"], active_count)
                if not ignore:
                    raise OrchError(msg)
                ignored.append("推奨並列数 %d（稼働中 %d）" % (report["recommended"], active_count))
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
    export_line = ""
    if role == "analysis" and scope in SCOPES:
        try:
            export_line = dispatch_export_line(c, pick_build(c, scope))
        except OrchError:
            pass  # 3ds など、ビルドの無いスコープには書き出しが無い
    tools = "、".join(x for x in (a.tools, export_line) if x)
    auto = auto_addr_section(c, issue.get("body") or "", precheck)
    text = tpl.read_text(encoding="utf-8").format(
        repo=a.repo, goal=goal, worktree=str(wt),
        evidence=(c.root / "_local" / "evidence" / ("%s-%d" % (a.repo, a.issue))).as_posix(),
        idb_line=("、IDB の作業用コピー %s" % idb["path"]) if idb else "",
        materials=sec.get("関係する資料", "").strip() or "なし",
        precheck=precheck + ("\n\n" + auto if auto else ""),
        tools_line=("- 道具: %s" % tools) if tools else "")
    out = task_file(c, "dispatch", a.repo, a.issue)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    outside_before = outside_status(c)
    update_task(c, key, repo=a.repo, issue=a.issue, title=issue.get("title", ""), branch=branch, worktree=str(wt),
                worker=a.worker, provider=w.get("provider"), effort=a.effort or w.get("default_effort"),
                idb=idb, state="dispatched", attempts=(old.get("attempts", 0) + 1) if old else 1,
                dispatched_at=now(), usage_before=usage, body=issue.get("body", ""),
                stamped_head=None, outside_before=outside_before, ignore_limit_reason=ignore or None,
                ignored_limits=ignored)
    if LABEL_BUSY not in labels:
        set_label(c, a.repo, a.issue, LABEL_BUSY, True)
    print(text)
    print("--- 依頼文を保存: %s（ブランチ %s、%s %s）" % (out, branch, a.worker, a.effort or w.get("default_effort")))
    return 0


# ---------- save ----------
def idb_open_files(path: Path) -> list[str]:
    if not path.parent.is_dir():
        return []
    return sorted(p.name for p in path.parent.iterdir() if p.suffix.lower() in {".id0", ".id1", ".id2", ".nam", ".til"})


def canonical_checks(c: Ctx) -> list[tuple[str, int, str]]:
    recorded = canonical_state(c).get("builds", {})
    checks = []
    for build in export_builds(c):
        path = canonical_path(c, build)
        if not path.is_file():
            continue
        open_files = idb_open_files(path)
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        expected = recorded.get(build, {}).get("sha256")
        problems = []
        if expected and digest != expected:
            problems.append("SHA256 が記録と違う: %s" % path)
        if open_files:
            problems.append("IDA の作業ファイル: %s" % ", ".join(open_files))
        if not expected:
            checks.append(("正本 IDB %s" % build, 0, "警告: SHA256 の記録が無い"))
        else:
            checks.append(("正本 IDB %s" % build, 1 if problems else 0, "\n".join(problems)))
        if problems and not expected:
            checks[-1] = (checks[-1][0], 1, "\n".join(problems + ["警告: SHA256 の記録が無い"]))
    return checks


def outside_checks(c: Ctx, before: dict, allow: bool) -> list[tuple[str, int, str]]:
    current = outside_status(c)
    checks = []
    for name, rows in current.items():
        added = sorted(set(rows) - set(before.get(name, [])))
        if added:
            label = "警告" if allow else "失敗"
            checks.append(("作業ツリー外 %s" % name, 0 if allow else 1,
                           label + ":\n" + "\n".join(added)))
    return checks


def save_task(c: Ctx, t: dict, message: str | None = None) -> bool:
    require_unpaused(t)
    wt = Path(t["worktree"])
    key = tkey(t["repo"], t["issue"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    if t.get("idb"):
        op = idb_open_files(Path(t["idb"]["path"]))
        if op:
            raise OrchError("IDB のコピーが開いたまま（%s）。閉じてから save する" % ", ".join(op))
    dirty = bool(git(c, wt, "status", "--porcelain").strip())
    if not dirty:
        # 変更が無くても、コミットが push されていない・PR が無い時は、push と PR の作成までする。
        base = base_ref(c, wt)
        has_commits = int(git(c, wt, "rev-list", "--count", "%s..HEAD" % base, check=False).strip() or 0) > 0
        remote = "refs/remotes/origin/%s" % t["branch"]
        if git_ok(c, wt, "rev-parse", "--verify", "-q", remote):
            unpushed = int(git(c, wt, "rev-list", "--count", "%s..HEAD" % remote, check=False).strip() or 0) > 0
        else:
            unpushed = True
        if not has_commits or not (unpushed or not t.get("pr")):
            print("%s: 変更なし（何もしない）" % key)
            return False
        print("%s: 変更は無いが、push か PR が済んでいないので続きを行う" % key)
    n = t.get("attempts", 1)
    msg = "試行 %s: %s" % (n, message or "作業の保存")
    pr_title = pr_body = None
    if not t.get("pr"):
        footer = c.workers.get("git", {}).get("pr_footer", "")
        pr_title = t.get("title") or "Issue #%s" % t["issue"]
        pr_body = "Closes #%s\n\n%s\n\n%s" % (t["issue"], message or "試行 %s の作業内容。" % n, footer)
        outgoing(pr_title, pr_body)
    if dirty:
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
REPORT_HEADER = "（ワーカーの最終の返答を司令塔が保存した）"


def require_active(t: dict) -> None:
    require_unpaused(t)
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


def codex_cmd(c: Ctx, wt: Path, exe: str, model: str, effort: str, thread_id: str | None = None) -> list[str]:
    """Codex の起動コマンド。砂場は使わず、常に全権限で起動する（2026-11-09 の利用者の方針。
    砂場では MSYS が止まり make が通らない）。"""
    python = str((c.root / "_local/venv/tools/Scripts/python.exe").resolve())
    guard = str((c.root / "scripts/usage_guard.py").resolve())
    command = subprocess.list2cmdline([python, guard, "--provider", "codex"])
    hook = ('hooks.PostToolUse=[{matcher=".*",hooks=[{type="command",command='
            + json.dumps(command, ensure_ascii=False) + ',timeout=10}]}]')
    # 既存の資料をワーカーが自分で探せるよう、ウェブ検索を有効にする。
    hook_args = ["--dangerously-bypass-hook-trust", "-c", hook, "-c", 'web_search="live"']
    cmd = [exe, "exec", "resume", thread_id] if thread_id else [exe, "exec", "-C", str(wt)]
    cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    return cmd + hook_args + ["-m", model, "-c", 'model_reasoning_effort="%s"' % effort, "--json", "-"]


CODEX_STOPPED_RC = 3  # 利用枠の上限で Codex を止めたときの終了コード（続きは --resume）


class CodexStopped(OrchError):
    """見張りが利用枠の閾値で Codex を止めた。止めるまでの出力を持つ。"""

    def __init__(self, used_percent, code, stdout, stderr):
        super().__init__("利用枠の %g%% で Codex を止めた" % used_percent)
        self.used_percent = used_percent
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


def run_codex(c: Ctx, cmd: list[str], cwd: Path, prompt: str) -> tuple[int, str, str]:
    """codex を起動し、終わるまで待つ。実行中は利用枠を見張り、閾値以上なら止めて CodexStopped を投げる。
    試験ではこの関数を差し替える。"""
    r = usage_watch.run_with_guard(cmd, input_text=prompt, cwd=str(cwd), env=child_env(c), root=c.root, provider="codex")
    if r.stopped:
        raise CodexStopped(r.used_percent, r.returncode, r.stdout, r.stderr)
    return r.returncode, r.stdout, r.stderr


def git_launch_state(c: Ctx, wt: Path) -> dict:
    """サンドボックス無しの起動を挟んで比較する Git の状態。"""
    return {
        "HEAD": git(c, wt, "rev-parse", "HEAD").strip(),
        "ブランチ": git(c, wt, "branch", "--show-current").strip(),
        "ステージ済みファイル": git(c, wt, "diff", "--cached", "--name-only").splitlines(),
        "stash 件数": len(git(c, wt, "stash", "list").splitlines()),
    }


def git_launch_violation(before: dict, after: dict) -> str:
    return "、".join("%s: %s → %s" % (name, before[name], after[name])
                    for name in before if before[name] != after[name])


def next_log(c: Ctx, repo: str, n, kind: str = "codex") -> Path:
    d = c.data / "logs"
    d.mkdir(parents=True, exist_ok=True)
    pat = re.compile(r"%s_%s_%s_(\d+)\.jsonl$" % (kind, re.escape(repo), n))
    nums = [int(m.group(1)) for p in d.iterdir() if (m := pat.match(p.name))]
    return d / ("%s_%s_%s_%d.jsonl" % (kind, repo, n, max(nums, default=0) + 1))


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


def codex_thread_id(jsonl: str) -> str | None:
    for line in jsonl.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "thread.started" and isinstance(obj.get("thread_id"), str):
            return obj["thread_id"] or None
    return None


def cmd_codex(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    require_unpaused(t)
    key = tkey(a.repo, a.issue)
    if t.get("provider") != "codex":
        raise OrchError("台帳の provider が codex でない（%s）" % t.get("provider"))
    require_active(t)
    model = a.model or t.get("worker")
    effort = a.effort or t.get("effort")
    if not effort:
        raise OrchError("effort が台帳に無い。--effort で指定する")
    if a.resume and not a.message_file:
        raise OrchError("--resume には --message-file が要る")
    if a.message_file and not a.resume:
        raise OrchError("--message-file は --resume と一緒に指定する")
    if a.resume:
        old_log = Path(t["codex_log"]) if t.get("codex_log") else None
        if old_log is None or not old_log.is_file():
            raise OrchError("続きに使う Codex のログが無い")
        thread_id = codex_thread_id(old_log.read_text(encoding="utf-8"))
        if not thread_id:
            raise OrchError("Codex のログに thread_id が無い: %s" % old_log)
        prompt_file = a.message_file
        prompt = read_source(prompt_file)
    else:
        prompt_file = task_file(c, "dispatch", a.repo, a.issue)
        if not prompt_file.is_file():
            raise OrchError("依頼文が無い: %s" % prompt_file)
        thread_id = None
        prompt = prompt_file.read_text(encoding="utf-8")
    wt = Path(t["worktree"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    cmd = codex_cmd(c, wt, find_codex(), model, effort, thread_id)
    if a.dry_run:
        print("dry-run: 実行しない。コマンド:")
        print(subprocess.list2cmdline(cmd))
        print("依頼文（標準入力）: %s" % prompt_file)
        return 0
    log = next_log(c, a.repo, a.issue)
    before = git_launch_state(c, wt)
    stopped = None
    try:
        try:
            code, out, err = run_codex(c, cmd, wt, prompt)
        except CodexStopped as s:
            stopped = s
            code, out, err = s.code, s.stdout, s.stderr
    finally:
        violation = git_launch_violation(before, git_launch_state(c, wt))
        if violation:
            print("警告: ワーカーが Git に書き込んだ: %s" % violation)
            update_task(c, key, git_violation=violation)
    log.write_text(out, encoding="utf-8", newline="\n")
    update_task(c, key, codex_log=str(log), codex_finished_at=now())
    if stopped is not None:
        # 止めるまでの出力は保存済み（thread_id が残り、--resume で続けられる）。報告は続きの実行で保存する
        update_task(c, key, codex_stopped_at_usage=stopped.used_percent, codex_stopped_at=now())
        print("利用枠の %g%% で Codex を止めた。続きは orch.py codex --resume で頼む" % stopped.used_percent)
        print("ログ: %s" % log)
        return CODEX_STOPPED_RC
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


# ---------- claude ----------
# サブエージェントでなく、CLI のメインのセッションとしてワーカーを起動する（Agent ツールの間だけ遅いため。2026-11-02）。
# ワーカーに持たせないツール: サブエージェント・他のセッションへの連絡・予約・作業ツリーの切り替え。
# Monitor は合う行のたびにワーカーを起こして利用枠を使うので、バックグラウンドの完了の知らせだけを待たせる。
CLAUDE_DENIED_TOOLS = ("Agent", "Task", "ListAgents", "SendMessage", "SendToProject", "SendToProjectContext", "Monitor",
                       "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "RemoteTrigger", "PushNotification",
                       "EnterWorktree", "ExitWorktree", "DesignSync")
REVIEW_WORKER = "opus-5.5"  # 深い検証の役のモデル（.claude/agents/reviewer.md の model と同じ）
# 作業ツリーの親の CLAUDE.md の「メインのセッションなら司令塔」を、ワーカーが自分の事と読まないようにする。
CLAUDE_WORKER_ROLE = """このセッションは、司令塔が CLI から起動したワーカーです。あなたは司令塔ではありません。
CLAUDE.md の「メインのセッションなら、あなたは司令塔です」と、スキル orchestrate・dispatch・evaluate・finalize・wrapup は、このセッションには当てはまりません。依頼文のとおりに、自分で作業をしてください。AGENTS.md の「ワーカーとして呼ばれた場合」の規則に従ってください。
長くかかる試験やビルドは run_in_background で起動し、終わるまでターンを終えて待ってください。終わると完了の知らせで続きが始まります。最後の返答を、司令塔が報告として受け取ります。"""
# ワーカーに渡す道具（--tools）。IDA の作業では MCP の道具を探せるよう ToolSearch を足す。
CLAUDE_TOOLS = ("Read", "Edit", "Write", "Grep", "Glob", "Bash", "PowerShell")
CLAUDE_MCP_TOOLS = ("ToolSearch",)
# 文脈の自動圧縮の窓と、1 回のファイル読み取りの上限（operations.md 8.2 C1・C2）。
CLAUDE_WORKER_ENV = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS": "8000"}
DEFAULT_MAX_BUDGET_USD = 5


def find_claude() -> str:
    exe = shutil.which("claude")
    if not exe:
        raise OrchError("claude が見つからない（PATH に無い）")
    return exe


def claude_model(worker: str) -> str:
    """workers.toml のワーカーの名前（haiku-5.5）からモデルの ID（claude-haiku-5-5）を作る。"""
    return "claude-" + worker.replace(".", "-")


def claude_system_prompt(c: Ctx, agent: str) -> str:
    """ワーカーの定義（.claude/agents/<agent>.md の本文）に、役割の説明を足す。"""
    path = c.root / ".claude" / "agents" / (agent + ".md")
    if not path.is_file():
        raise OrchError("ワーカーの定義が無い: %s" % path)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    body = text.split("---\n", 2)[-1] if text.startswith("---\n") else text
    return CLAUDE_WORKER_ROLE + "\n\n" + body.strip()


def claude_cmd(exe: str, model: str, effort: str, system_prompt: str, session: str, resume: bool, use_mcp: bool,
               tools: tuple[str, ...] | None = None, max_budget_usd: float | None = None) -> list[str]:
    cmd = [exe, "-p", "--model", model, "--effort", effort, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions", "--disallowedTools", ",".join(CLAUDE_DENIED_TOOLS),
           "--append-system-prompt", system_prompt]
    if tools:
        cmd += ["--tools", ",".join(tools + (CLAUDE_MCP_TOOLS if use_mcp else ()))]
    if max_budget_usd:
        cmd += ["--max-budget-usd", "%g" % max_budget_usd]
    cmd += ["--resume", session] if resume else ["--session-id", session]
    if not use_mcp:
        cmd.append("--strict-mcp-config")  # IDA を使わない作業では MCP のサーバーを起動しない
    return cmd


def claude_env(c: Ctx) -> dict:
    """ワーカー用の環境変数。親のセッションの CLAUDE* を残すと、入れ子の起動として扱われる。"""
    env = {k: v for k, v in child_env(c).items() if not k.startswith("CLAUDE") or k == "CLAUDE_CONFIG_DIR"}
    env.update(CLAUDE_WORKER_ENV)
    return env


def worker_agent(c: Ctx, repo: str) -> str:
    """ワーカーの定義はリポジトリの役割で選ぶ（analysis は re-analyst、derived は tooling）。"""
    role = (c.ws.get("repos", {}).get(repo) or {}).get("role")
    if role == "analysis":
        return "re-analyst"
    if role == "derived":
        return "tooling"
    raise OrchError("リポジトリ %s の role が analysis でも derived でもない（%s）" % (repo, role))


def run_claude(c: Ctx, cmd: list[str], cwd: Path, prompt: str, log: Path) -> usage_watch.GuardRun:
    """claude を起動し、終わるまで待つ。ログは逐次書き、利用枠の実測は usage.json へ残す（見張りはそれを読む）。
    閾値以上なら止める。試験ではこの関数を差し替える。"""
    with log.open("w", encoding="utf-8", newline="\n") as f:
        def on_line(line):
            f.write(line)
            f.flush()
            usage_watch.record_claude_usage(c.state, line)
        return usage_watch.run_with_guard(cmd, input_text=prompt, cwd=str(cwd), env=claude_env(c), root=c.root,
                                          provider="claude", on_line=on_line)


def last_claude_result(jsonl: str) -> dict | None:
    """stream-json の最後の result。バックグラウンドの完了で続きが走ると result は複数になる。"""
    found = None
    for line in jsonl.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    return found


def refuse_over_claude_limit(c: Ctx) -> None:
    usage_watch.collect(c.root)
    limit = c.workers.get("limits", {}).get("stop_at_used_percent", 80)
    used = capacity.read_provider_usage(c.state, "claude").get("five_hour_used_percent")
    if used is not None and used >= limit:
        raise OrchError("5時間枠の使用率 %s%% が上限 %s%% 以上なので起動しない" % (used, limit))


def show_claude_run(r: usage_watch.GuardRun, model: str, effort: str, started: float, log: Path, session: str) -> tuple[int, str]:
    """止められずに終わった実行の結果を表示し、（終了コード、最後の返答）を返す。0 でなければ報告を保存しない。"""
    result = last_claude_result(r.stdout)
    print("claude の終了コード: %d（モデル %s、effort %s、%.1f 分）" % (r.returncode, model, effort, (time.time() - started) / 60))
    print("ログ: %s" % log)
    if r.returncode != 0 and r.stderr.strip():
        print("== stderr の末尾 ==")
        print("\n".join(r.stderr.strip().splitlines()[-20:]))
    if result is None:
        print("警告: result が無く、報告を保存できない")
        return 1, ""
    print("API の時間 %.1f 分、セッション %s" % ((result.get("duration_api_ms") or 0) / 60000, session))
    message = result.get("result") if isinstance(result.get("result"), str) else ""
    print("== 最後の返答 ==")
    print(message)
    if str(result.get("subtype") or "").startswith("error_max_budget"):
        # 費用の上限で止まった。利用枠で止めた時と同じく、続きを頼める。報告は保存しない。
        print("費用の上限で止まった（%s）。続きは orch.py claude --resume --message-file <f> で頼む" % result.get("subtype"))
        return CODEX_STOPPED_RC, message
    rc = 1 if r.returncode != 0 or result.get("is_error") else 0
    if rc:
        print("エラーで終わった（%s）。報告は保存しない" % result.get("subtype"))
    return rc, message


def append_report(t: dict, text: str) -> Path:
    """続きの実行の報告を、前の報告の後ろに足す。"""
    body = text.strip()
    if not body:
        raise OrchError("報告が空")
    outgoing(body)
    f = Path(t["worktree"]) / ".task" / "report.md"
    with f.open("a", encoding="utf-8", newline="\n") as out:
        out.write("\n---\n\n## 続きの実行の報告（%s）\n\n%s\n" % (now(), body))
    print("報告に追記: %s" % f)
    return f


def previous_claude_cost(c: Ctx, repo: str, n) -> float:
    """そのタスクの最新の claude ログの、result の total_cost_usd。"""
    logs = c.data / "logs"
    found = sorted(logs.glob("claude_%s_%s_*.jsonl" % (repo, n)), key=_log_number) if logs.is_dir() else []
    return claude_log_stats(found[-1])["cost"] if found else 0.0


def cmd_claude(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    require_unpaused(t)
    key = tkey(a.repo, a.issue)
    if t.get("provider") != "claude":
        raise OrchError("台帳の provider が claude でない（%s）" % t.get("provider"))
    require_active(t)
    model = a.model or claude_model(t.get("worker") or "")
    effort = a.effort or t.get("effort")
    if not effort:
        raise OrchError("effort が台帳に無い。--effort で指定する")
    if a.resume and not a.message_file:
        raise OrchError("--resume には --message-file が要る")
    if a.resume:
        session = t.get("claude_session")
        if not session:
            raise OrchError("続きに使うセッションが台帳に無い")
        prompt_file = a.message_file
        prompt = read_source(prompt_file)
    elif a.message_file:
        # 続きでなく、新しいセッションで --message-file を依頼文にする（直しの頼み方）。
        session = str(uuid.uuid4())
        prompt_file = a.message_file
        prompt = read_source(prompt_file)
    else:
        prompt_file = task_file(c, "dispatch", a.repo, a.issue)
        if not prompt_file.is_file():
            raise OrchError("依頼文が無い: %s" % prompt_file)
        session = str(uuid.uuid4())
        prompt = prompt_file.read_text(encoding="utf-8")
    wt = Path(t["worktree"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    system_prompt = claude_system_prompt(c, worker_agent(c, a.repo))
    budget = c.workers.get("limits", {}).get("max_budget_usd", DEFAULT_MAX_BUDGET_USD)
    if a.resume and budget:
        # --resume の total_cost_usd は前回からの累積。上限は「前回までの費用＋今回の分」にする。
        budget += previous_claude_cost(c, a.repo, a.issue)
    cmd = claude_cmd(find_claude(), model, effort, system_prompt, session, a.resume, bool(t.get("idb")),
                     tools=CLAUDE_TOOLS, max_budget_usd=budget)
    if a.dry_run:
        print("dry-run: 実行しない。コマンド:")
        print(subprocess.list2cmdline(cmd))
        print("依頼文（標準入力）: %s" % prompt_file)
        return 0
    refuse_over_claude_limit(c)
    log = next_log(c, a.repo, a.issue, "claude")
    update_task(c, key, claude_session=session, claude_log=str(log), claude_started_at=now())
    report = wt / ".task" / "report.md"
    report_before = report.read_bytes() if report.exists() else None
    git_before = git_launch_state(c, wt)
    started = time.time()
    try:
        r = run_claude(c, cmd, wt, prompt, log)
    finally:
        violation = git_launch_violation(git_before, git_launch_state(c, wt))
        if violation:
            print("警告: ワーカーが Git に書き込んだ: %s" % violation)
            update_task(c, key, git_violation=violation)
    update_task(c, key, claude_finished_at=now())
    if r.stopped:
        update_task(c, key, claude_stopped_at_usage=r.used_percent, claude_stopped_at=now())
        print("利用枠の %g%% でワーカーを止めた。続きは orch.py claude --resume で頼む" % r.used_percent)
        print("ログ: %s" % log)
        return CODEX_STOPPED_RC
    rc, message = show_claude_run(r, model, effort, started, log, session)
    if rc:
        return rc
    after = report.read_bytes() if report.exists() else None
    try:
        if after != report_before:
            print("報告: ワーカーが %s を書いたので、そのまま使う" % report)
        elif report_before is not None:
            append_report(t, message)
        else:
            save_report(t, message)
    except OrchError as e:
        print("エラー: 報告を保存できない: %s" % e)
        rc = 1
    return rc


def cmd_review(a) -> int:
    """深い検証の役を CLI で起動する。ワーカーのセッションと報告には触らず、報告は orchestrator/review/ に残す。"""
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    require_active(t)
    key = tkey(a.repo, a.issue)
    session = t.get("review_session") if a.resume else str(uuid.uuid4())
    if not session:
        raise OrchError("続きに使うセッションが台帳に無い")
    prompt = read_source(a.message_file)
    wt = Path(t["worktree"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    model = a.model or claude_model(REVIEW_WORKER)
    cmd = claude_cmd(find_claude(), model, a.effort, claude_system_prompt(c, "reviewer"), session, a.resume, bool(t.get("idb")))
    if a.dry_run:
        print("dry-run: 実行しない。コマンド:")
        print(subprocess.list2cmdline(cmd))
        print("依頼文（標準入力）: %s" % a.message_file)
        return 0
    refuse_over_claude_limit(c)
    log = next_log(c, a.repo, a.issue, "review")
    update_task(c, key, review_session=session, review_log=str(log), review_started_at=now())
    git_before = git_launch_state(c, wt)
    status_before = git(c, wt, "status", "--porcelain")
    started = time.time()
    try:
        r = run_claude(c, cmd, wt, prompt, log)
    finally:
        violation = git_launch_violation(git_before, git_launch_state(c, wt))
        if violation:
            print("警告: 検証役が Git に書き込んだ: %s" % violation)
            update_task(c, key, git_violation=violation)
        if git(c, wt, "status", "--porcelain") != status_before:
            print("警告: 検証役が作業ツリーのファイルを変えた。採点の前に確かめる")
    update_task(c, key, review_finished_at=now())
    if r.stopped:
        update_task(c, key, review_stopped_at_usage=r.used_percent)
        print("利用枠の %g%% で検証役を止めた。続きは orch.py review --resume で頼む" % r.used_percent)
        print("ログ: %s" % log)
        return CODEX_STOPPED_RC
    rc, message = show_claude_run(r, model, a.effort, started, log, session)
    if rc:
        return rc
    body = message.strip()
    if not body:
        print("警告: 最後の返答が空で、報告を保存できない")
        return 1
    report = task_file(c, "review", a.repo, a.issue)
    report.parent.mkdir(parents=True, exist_ok=True)
    if a.resume and report.is_file():
        body = report.read_text(encoding="utf-8").rstrip() + "\n\n---\n\n## 続きの実行の報告（%s）\n\n" % now() + body
    report.write_text(body + "\n", encoding="utf-8", newline="\n")
    print("報告: %s（orch.py accept --reviewed に渡す）" % report)
    return 0


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


def symbol_rows(c: Ctx, wt: Path, rev: str, path: str) -> dict:
    src = git(c, wt, "show", "%s:%s" % (rev, path), check=False)
    return {row["address"]: row for row in csv.DictReader(io.StringIO(src)) if row.get("address")}


def changed_symbol_names(c: Ctx, wt: Path, base: str) -> set[str]:
    """深い検証の判断に使う、symbols の変更の名前。functions・globals は名前か型（prototype・type）が変わった行の
    名前（旧名と新名）、types は変更行にある識別子（C の予約語を除く）。"""
    names: set[str] = set()
    for path in git(c, wt, "diff", "--name-only", base + "...HEAD", "--", "symbols").splitlines():
        parts = path.split("/")
        if len(parts) < 3:
            continue
        kind = parts[2].removesuffix(".csv")
        if kind in ("functions", "globals") and path.endswith(".csv"):
            old, new = symbol_rows(c, wt, base, path), symbol_rows(c, wt, "HEAD", path)
            for addr, o in old.items():
                n = new.get(addr)
                if n and any(o.get(k) != n.get(k) for k in ("name", "prototype", "type")):
                    names.update(x for x in (o.get("name"), n.get("name")) if x)
        elif kind == "types":
            for line in git(c, wt, "diff", "-U0", base + "...HEAD", "--", path).splitlines():
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                    names.update(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", line[1:]))
    return names - export_game_api.CPP_KEYWORDS


def deep_review_hits(c: Ctx, names: set[str]) -> dict[str, list[str]]:
    """作業場にある派生リポジトリの spec/game_api.toml に出てくる名前を、派生リポジトリごとに返す。"""
    hits: dict[str, list[str]] = {}
    for repo, r in c.repos().items():
        spec = c.root / repo / DEEP_REVIEW_SPEC
        if r.get("role") != "derived" or not spec.is_file():
            continue
        words = set(export_game_api.WORDS.findall(spec.read_text(encoding="utf-8", errors="replace")))
        found = sorted(names & words)
        if found:
            hits[repo] = found
    return hits


def cmd_stamp(a) -> int:
    c = Ctx()
    role = need_repo(c, a.repo).get("role", "analysis")
    t = get_task(c, a.repo, a.issue)
    require_unpaused(t)
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
    checks.extend(canonical_checks(c))
    checks.extend(outside_checks(c, t.get("outside_before", {}), a.allow_outside))
    n = t.get("attempts", 1)
    if commit_all(c, wt, "試行 %s: 仕上げ" % n):
        update_task(c, key, attempts=n + 1)  # save と同じく、コミットのたびに番号を進める
    git(c, wt, "push", "-u", "origin", t["branch"])
    sym, builds = symbols_diff(c, wt, base)
    deep = deep_review_hits(c, changed_symbol_names(c, wt, base))
    deep_names = sorted({n for ns in deep.values() for n in ns})
    deep_lines = ["深い検証の対象: %s（%s）" % (", ".join(deep_names), "、".join(
        "%s の %s" % (r, DEEP_REVIEW_SPEC) for r in deep))] if deep else ["なし"]
    ok = all(code == 0 for _, code, _ in checks)
    recs = new_records(c, wt, base)
    lines = ["# 採点用の要約: %s" % key, "", "## 変更したファイル", git(c, wt, "diff", "--stat", base + "...HEAD").rstrip() or "なし",
             git(c, wt, "status", "--short").rstrip(), "", "## 新しい記録"] + (recs or ["なし"]) + \
            ["", "## symbols の差分"] + (sym or ["なし"]) + ["", "## 深い検証の要否"] + deep_lines + ["", "## 検査の結果"]
    for n, code, out in checks:
        lines.append("- %s: %s" % (n, "OK" if code == 0 else "失敗(%d)" % code))
        if out:
            lines += ["    " + x for x in out.splitlines()[-10:]]
    lines += ["", "## 報告", ".task/report.md: %s" % ("あり" if (wt / ".task" / "report.md").exists() else "なし")]
    text = "\n".join(lines) + "\n"
    f = task_file(c, "stamp", a.repo, a.issue)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8", newline="\n")
    update_task(c, key, state="stamped", stamped_at=now(), stamped_head=head(c, wt), stamp_ok=ok,
                symbols_builds=builds, prep_head=None, deep_review_required=bool(deep), deep_review_names=deep_names)
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
        if not sep or not k.strip():
            raise OrchError("点数の形式が違う: %r（項目=点,...）" % part)
        if k.strip() in out:
            raise OrchError("点数の項目が重複: %s" % k.strip())
        try:
            out[k.strip()] = float(v)
        except ValueError:
            raise OrchError("点数の形式が違う: %r（項目=点,...）" % part)
    if not out:
        raise OrchError("--scores が空")
    return out


def pass_min(c: Ctx) -> float:
    v = c.workers.get("evaluation", {}).get("pass_min")
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise OrchError("workers.toml に [evaluation].pass_min が無い")
    return float(v)


def check_scores(scores: dict, role: str, minimum: float) -> None:
    """採点の項目が役の採点表と一致し、各点が 0〜1 で、どれも合格の基準以上であることを確かめる。"""
    items = EVAL_ITEMS.get(role)
    if items is None:
        raise OrchError("採点表が無い役: %s" % role)
    missing = [i for i in items if i not in scores]
    extra = [k for k in scores if k not in items]
    if missing or extra:
        raise OrchError("採点の項目が違う（不足: %s、余分: %s）。項目は %s" % (
            ", ".join(missing) or "なし", ", ".join(extra) or "なし", ", ".join(items)))
    for k in items:
        v = scores[k]
        if math.isnan(v) or not 0 <= v <= 1:
            raise OrchError("点数は 0〜1 にする: %s=%g" % (k, v))
        if v < minimum:
            raise OrchError("%s=%g が合格の基準 %g に届かない（どの項目も基準以上で合格）" % (k, v, minimum))


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
            else:
                record_canonical(c, b)
        finally:
            if lock and lock.is_locked:
                lock.release()
    return status


def changed_function_names(c: Ctx, repo: Path, commit: str) -> dict[str, list[str]]:
    """取り込んだ merge commit の第一親と比較し、追加・改名・型変更後の関数名を返す。"""
    paths = git(c, repo, "diff", "--name-only", commit + "^", commit, "--", "symbols").splitlines()
    result: dict[str, list[str]] = {}
    for path in paths:
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "symbols" or parts[2] != "functions.csv":
            continue
        def rows(rev):
            src = git(c, repo, "show", rev + ":" + path, check=False)
            return {row["address"]: row for row in csv.DictReader(io.StringIO(src)) if row.get("address")}
        old, new = rows(commit + "^"), rows(commit)
        names = {row["name"] for address, row in new.items() if row.get("name") and
                 (address not in old or any(row.get(k) != old[address].get(k) for k in
                  ("name", "prototype", "type")))}
        if names:
            result[parts[1]] = sorted(names)
    return result


def refresh_export_after_accept(c: Ctx, key: str, commit: str) -> None:
    try:
        changed = changed_function_names(c, c.root / DEFAULT_REPO, commit)
        for build, names in changed.items():
            if not (c.data / "builds" / build / "export" / "index.txt").is_file():
                raise OrchError("%s の全体の書き出しが無い" % build)
            run_export(c, build, names, commit)
        update_task(c, key, export_refresh="完了" if changed else "対象なし")
    except (OrchError, OSError, ValueError, subprocess.SubprocessError) as e:
        print("警告: 書き出しの部分更新に失敗: %s" % e)
        update_task(c, key, export_refresh="失敗", export_error=str(e))


def cmd_accept(a) -> int:
    c = Ctx()
    role = need_repo(c, a.repo).get("role", "analysis")
    t = get_task(c, a.repo, a.issue)
    require_unpaused(t)
    key = tkey(a.repo, a.issue)
    scores = parse_scores(a.scores)
    check_scores(scores, role, pass_min(c))
    read = read_source(a.read).strip()
    if not read:
        raise OrchError("採点の前に読んだ物の一覧が空: %s" % a.read)
    if len([line for line in read.splitlines() if line.strip()]) < 2:
        raise OrchError("読んだ物は 2 つ以上書く（報告だけで採点しない）")
    wt = Path(t["worktree"])
    if t.get("state") != "stamped":
        raise OrchError("状態が %s。先に stamp が要る" % t.get("state"))
    if not t.get("stamp_ok"):
        raise OrchError("stamp の検査に失敗がある。直して save → stamp をやり直す")
    # stamp の後の変更は、accept 自身が作った準備のコミットだけ許す（途中で失敗した accept のやり直し）
    if git(c, wt, "status", "--porcelain").strip() or head(c, wt) not in (t.get("stamped_head"), t.get("prep_head")):
        raise OrchError("stamp の後に変更がある。もう一度 stamp する")
    if t.get("deep_review_required") and not a.reviewed:
        raise OrchError("深い検証の対象がある（%s）。深い検証の報告を --reviewed <ファイル> で渡す" % (
            ", ".join(t.get("deep_review_names") or [])))
    reviewed = read_source(a.reviewed).strip() if a.reviewed else ""
    if a.reviewed and not reviewed:
        raise OrchError("深い検証の報告が空: %s" % a.reviewed)
    slug = repo_slug(c, a.repo)
    comment = "合格\n\n採点: " + ", ".join("%s=%g" % kv for kv in scores.items())
    comment += "\n\n採点の前に読んだ物:\n\n" + read
    review_comment = "深い検証の報告:\n\n" + reviewed if reviewed else ""
    if a.dry_run:
        print("dry-run: 次を行う（何も書かない）")
        steps = ["origin/main を取り込み（merge）", "索引を作り直し、.task/ を消してコミット・push"]
        if reviewed:
            steps.append("PR #%s に深い検証の報告をコメント" % t["pr"])
        steps += ["PR #%s にコメント「%s」" % (t["pr"], comment.replace("\n", " ")), "PR を ready に",
                  "検査が揃うのを待ち、完了を待つ（最大 %d 秒・%d 分）" % (CHECK_APPEAR_SEC, CHECK_TIMEOUT_SEC // 60),
                  "gh pr merge --merge",
                  "手元の main を更新", "symbols 変更(%s)があれば正本の IDB へ取り込み" % (t.get("symbols_builds") or "なし"),
                  "台帳を accepted に"]
        for s in steps:
            print("  - " + s)
        return 0
    outgoing(comment, *([review_comment] if reviewed else []))
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
    if reviewed:
        gh_run(["pr", "comment", str(t["pr"]), "--repo", slug, "--body", review_comment])
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
    set_label(c, a.repo, a.issue, LABEL_BUSY, False)
    imp = import_canonical(c, t, short) if t.get("symbols_builds") else "不要"
    update_task(c, key, state="accepted", accepted_at=now(), scores=scores, idb_import=imp, merge_commit=short,
                usage_after=read_usage(c))
    changed_skills = git(c, repo_dir, "diff", "--name-only", "origin/main^", "origin/main", "--", "skills").strip()
    if changed_skills or sync_skills.check(c.root):
        for line in sync_skills.sync(c.root):
            print(line)
    if imp == "取り込み済み":
        refresh_export_after_accept(c, key, short)
    print("%s: 取り込み完了（%s、正本の取り込み: %s）" % (key, short, imp))
    cleanup_task(c, key, t, a.keep)
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
    set_label(c, a.repo, a.issue, LABEL_BUSY, False)
    update_task(c, key, state="rejected", rejected_at=now(), reject_reason=a.reason, usage_after=read_usage(c))
    cleanup_task(c, key, t, a.keep)
    print("%s: 却下" % key)
    return 0


# ---------- wrapup ----------
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
    old = h.read_text(encoding="utf-8") if h.exists() else "# 引き継ぎメモ（司令塔）\n"
    lines = old.splitlines()
    i = 1 if lines and lines[0].startswith("# ") else 0
    new = lines[:i] + [""] + sec + [""] + lines[i:]
    h.write_text("\n".join(new).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    print("\n".join(sec))
    print("引き継ぎメモを更新: %s" % h)
    mirror(c)
    return 0


# ---------- verify ----------
VERIFY_TIMEOUT_SEC = 1800


def verify_files(c: Ctx, wt: Path, base: str) -> list[str]:
    """差分のファイル（基点との差＋未追跡）。"""
    mb = git(c, wt, "merge-base", base, "HEAD").strip() or base
    names = git(c, wt, "diff", "--name-only", mb).splitlines()
    names += git(c, wt, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({n for n in names if n and (wt / n).exists()})


def run_verify_command(c: Ctx, wt: Path, command: str, files: list[str]) -> tuple[int, str]:
    """コマンドを作業ツリーで走らせ、（終了コード、出力）を返す。{files} は差分のファイルの一覧に置き換える。"""
    parts = shlex.split(command)
    if "{files}" in parts:
        i = parts.index("{files}")
        if not files:
            return 0, "（差分のファイルが無いので走らせない）"
        parts[i:i + 1] = files
    if parts and parts[0] == "python":
        parts[0] = sys.executable
    try:
        r = subprocess.run(parts, cwd=str(wt), env=child_env(c), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=VERIFY_TIMEOUT_SEC)
    except FileNotFoundError as e:
        return 127, "起動できない: %s" % e
    except subprocess.TimeoutExpired:
        return 124, "時間切れ（%d 秒）" % VERIFY_TIMEOUT_SEC
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def verify_results(c: Ctx, wt: Path, commands: list[str], base: str) -> list[tuple[str, int, str]]:
    files = verify_files(c, wt, base)
    return [(command, *run_verify_command(c, wt, command, files)) for command in commands]


def cmd_verify(a) -> int:
    """作業ツリーでビルドと試験を LLM なしで走らせ、結果と差分を 1 つのファイルにまとめる（accept の前提にはしない）。"""
    c = Ctx()
    need_repo(c, a.repo)
    t = get_task(c, a.repo, a.issue)
    require_unpaused(t)
    wt = Path(t["worktree"])
    if not wt.is_dir():
        raise OrchError("作業ツリーが無い: %s" % wt)
    commands = (c.ws.get("repos", {}).get(a.repo) or {}).get("verify") or []
    if not commands:
        raise OrchError("workspace.toml の repos.%s に verify が無い" % a.repo)
    base = base_ref(c, wt)
    results = verify_results(c, wt, commands, base)
    mb = git(c, wt, "merge-base", base, "HEAD", check=False).strip() or base
    parts = ["# 機械の検証 %s#%s\n" % (a.repo, a.issue), "## コマンドの結果\n"]
    for command, rc, out in results:
        tail = "\n".join(out.splitlines()[-20:])
        parts.append("### `%s`\n\n終了コード: %d\n\n```\n%s\n```\n" % (command, rc, tail))
    parts.append("## git diff --stat\n\n```\n%s\n```\n" % git(c, wt, "diff", "--stat", mb, check=False).rstrip())
    parts.append("## 差分の全部\n\n```diff\n%s\n```\n" % git(c, wt, "diff", mb, check=False).rstrip())
    untracked = git(c, wt, "ls-files", "--others", "--exclude-standard", check=False).split()
    if untracked:
        parts.append("## 未追跡のファイル\n\n%s\n" % "\n".join("- " + n for n in untracked))
    rp = wt / ".task" / "report.md"
    parts.append("## .task/report.md\n\n%s\n" % (rp.read_text(encoding="utf-8").rstrip() if rp.is_file() else "（無し）"))
    pack = c.data / "orchestrator" / "pack" / ("%s-%s.md" % (a.repo, a.issue))
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text("\n".join(parts), encoding="utf-8", newline="\n")
    for command, rc, _ in results:
        print("%s  %s（終了コード %d）" % ("合格" if rc == 0 else "不合格", command, rc))
    ok = all(rc == 0 for _, rc, _ in results)
    print("機械の検証: %s" % ("合格" if ok else "不合格"))
    print("まとめ: %s" % pack)
    return 0 if ok else 1


# ---------- WebChat ----------
# WebChat（ChatGPT の Web 画面。利用者が手で操作する）は、台帳に載らない。Issue #N のラベル webchat で担当を示し、
# 下書きを inbox/webchat/N/ に置いて、ブランチ webchat/N から PR を出す。取り込みは PR 単位で行う。
def webchat_issue_number(body: str, branch: str) -> int:
    """PR 本文の先頭の `Issue #N` とブランチ名 `webchat/N` から Issue 番号を取る。両方あって食い違えば断る。"""
    m = re.match(r"\s*Issue\s*#(\d+)", body or "")
    b = re.fullmatch(r"webchat/(\d+)", branch or "")
    from_body, from_branch = (int(m.group(1)) if m else None), (int(b.group(1)) if b else None)
    if from_body is not None and from_branch is not None and from_body != from_branch:
        raise OrchError("PR 本文の Issue #%d とブランチ %s が食い違う" % (from_body, branch))
    n = from_body if from_body is not None else from_branch
    if n is None:
        raise OrchError("Issue 番号が分からない（PR 本文の先頭に `Issue #N` か、ブランチ名 `webchat/N` が要る）")
    return n


def webchat_outside(paths: list[str], n: int) -> list[str]:
    """PR の差分のうち、inbox/webchat/<N>/ の外にある物。"""
    prefix = "%s/%d/" % (WEBCHAT_INBOX, n)
    return [x for x in paths if not x.startswith(prefix)]


def read_webchat_pr(c: Ctx, repo: str, pr) -> tuple[dict, int]:
    """PR を読み、（情報、Issue 番号）を返す。開いていない PR、フォークからの PR、受け箱の外に及ぶ PR は断る。"""
    out = gh_run(["pr", "view", str(pr), "--repo", repo_slug(c, repo),
                  "--json", "number,state,body,headRefName,isDraft,isCrossRepository,files"])
    try:
        info = json.loads(out)
    except ValueError:
        raise OrchError("PR #%s の読み取り結果が JSON でない" % pr)
    if info.get("state") != "OPEN":
        raise OrchError("PR #%s は開いていない（%s）" % (pr, info.get("state")))
    if info.get("isCrossRepository"):
        raise OrchError("PR #%s はフォークからの PR。同じリポジトリのブランチから出してもらう" % pr)
    n = webchat_issue_number(info.get("body") or "", info.get("headRefName") or "")
    if info.get("headRefName") != "webchat/%d" % n:
        raise OrchError("ブランチが webchat/%d でない（%s）" % (n, info.get("headRefName")))
    paths = [f.get("path", "") for f in info.get("files") or []]
    if not paths:
        raise OrchError("PR #%s に差分のファイルが無い" % pr)
    outside = webchat_outside(paths, n)
    if outside:
        raise OrchError("PR #%s の差分が %s/%d/ の外に及ぶ: %s" % (pr, WEBCHAT_INBOX, n, ", ".join(outside[:5])))
    return info, n


def webchat_worktree(c: Ctx, repo: str, n: int) -> Path:
    return c.data / "worktrees" / repo / ("webchat-%d" % n)


def prepare_webchat(c: Ctx, repo: str, n: int) -> Path:
    """PR のブランチを作業ツリーに取り出し、origin/main に追いつかせる。追いついた後の差分が受け箱の外なら断る。"""
    repo_dir = c.root / repo
    branch = "webchat/%d" % n
    wt = webchat_worktree(c, repo, n)
    git(c, repo_dir, "fetch", "origin")
    if wt.exists():
        git(c, wt, "checkout", "-q", "--detach")  # 取り出し直す前にブランチを空ける
    else:
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(c, repo_dir, "worktree", "add", "--detach", str(wt), "origin/" + branch)
    git(c, wt, "checkout", "-q", "-B", branch, "origin/" + branch)
    mr = subprocess.run([c.git, "merge", "origin/main", "-m", "main を取り込み"], cwd=str(wt), capture_output=True,
                        text=True, env=tool_env(c))
    if mr.returncode != 0:
        subprocess.run([c.git, "merge", "--abort"], cwd=str(wt), capture_output=True, env=tool_env(c))
        raise OrchError("main の取り込みで衝突。WebChat に直してもらう:\n" + (mr.stdout + mr.stderr).strip()[-300:])
    outside = webchat_outside(git(c, wt, "diff", "--name-only", "origin/main", "HEAD").splitlines(), n)
    if outside:
        raise OrchError("ブランチの差分が %s/%d/ の外に及ぶ: %s" % (WEBCHAT_INBOX, n, ", ".join(outside[:5])))
    return wt


def remove_webchat_worktree(c: Ctx, repo: str, n: int) -> None:
    repo_dir, wt = c.root / repo, webchat_worktree(c, repo, n)
    if wt.exists():
        remove_worktree_safely(c, repo_dir, wt, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    git(c, repo_dir, "worktree", "prune", check=False)
    git(c, repo_dir, "branch", "-D", "webchat/%d" % n, check=False)


def run_webchat_verify(c: Ctx, repo: str, wt: Path, n: int) -> bool:
    commands = (c.ws.get("repos", {}).get(repo) or {}).get("verify") or []
    if not commands:
        raise OrchError("workspace.toml の repos.%s に verify が無い" % repo)
    results = verify_results(c, wt, commands, "origin/main")
    for command, rc, out in results:
        print("%s  %s（終了コード %d）" % ("合格" if rc == 0 else "不合格", command, rc))
        if rc != 0:
            print("\n".join(out.splitlines()[-20:]))
    ok = all(rc == 0 for _, rc, _ in results)
    print("機械の検証: %s" % ("合格" if ok else "不合格"))
    drafts = sorted(p.name for p in (wt / WEBCHAT_INBOX / str(n)).glob("*.md"))
    print("下書き（%d 件）: %s" % (len(drafts), wt / WEBCHAT_INBOX / str(n)))
    for d in drafts:
        print("  - " + d)
    return ok


def cmd_webchat_verify(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    info, n = read_webchat_pr(c, a.repo, a.pr)
    wt = prepare_webchat(c, a.repo, n)
    print("PR #%s（Issue #%d）を作業ツリーに取り出した: %s" % (a.pr, n, wt))
    return 0 if run_webchat_verify(c, a.repo, wt, n) else 1


def cmd_webchat_accept(a) -> int:
    c = Ctx()
    role = need_repo(c, a.repo).get("role", "analysis")
    scores = parse_scores(a.scores)
    check_scores(scores, role, pass_min(c))
    read = read_source(a.read).strip()
    if len([line for line in read.splitlines() if line.strip()]) < 2:
        raise OrchError("読んだ物は 2 つ以上書く（報告だけで採点しない）")
    info, n = read_webchat_pr(c, a.repo, a.pr)
    slug = repo_slug(c, a.repo)
    comment = "合格\n\n採点: " + ", ".join("%s=%g" % kv for kv in scores.items())
    comment += "\n\n採点の前に読んだ物:\n\n" + read
    if a.dry_run:
        print("dry-run: 次を行う（何も書かない）")
        for step in ("PR のブランチを作業ツリーに取り出し、origin/main を取り込み", "機械の検証（workspace.toml の verify）",
                     "kb.py intake --issue %d --by %s（ID を振って findings へ移す）" % (n, WEBCHAT_MODEL),
                     "結果をコミットして push", "PR #%s にコメント「%s」" % (a.pr, comment.replace("\n", " ")),
                     "検査が揃うのを待ち、PUT pulls/%s/merge で取り込み" % a.pr,
                     "Issue #%d の「%s」を外して閉じる" % (n, LABEL_BUSY), "作業ツリーを片付ける"):
            print("  - " + step)
        return 0
    outgoing(comment)
    wt = prepare_webchat(c, a.repo, n)
    if not run_webchat_verify(c, a.repo, wt, n):
        raise OrchError("機械の検証が不合格。取り込まない")
    kb = wt / "tools" / "kb.py"
    code, out = run_py(c, wt, kb, ["intake", "--issue", str(n), "--by", WEBCHAT_MODEL])
    if code != 0:
        raise OrchError("kb.py intake が失敗（何も書かれていない）:\n" + out[-600:])
    ids = [x.strip() for x in out.splitlines() if re.fullmatch(r"[A-Za-z0-9]+-F\d+", x.strip())]
    code, out = run_py(c, wt, kb, ["lint"])
    if code != 0:
        raise OrchError("intake の後の lint が失敗:\n" + out[-400:])
    commit_all(c, wt, "WebChat の記録を取り込む（Issue #%d）" % n)
    git(c, wt, "push", "origin", "webchat/%d" % n)
    sha = head(c, wt)
    gh_run(["pr", "comment", str(a.pr), "--repo", slug, "--body", comment])
    if info.get("isDraft"):
        gh_run(["pr", "ready", str(a.pr), "--repo", slug])
    wait_checks(c, slug, a.pr, sha)
    gh_run(["api", "-X", "PUT", "repos/%s/pulls/%s/merge" % (slug, a.pr), "-f", "merge_method=merge"])
    repo_dir = c.root / a.repo
    git(c, repo_dir, "fetch", "origin")
    if git(c, repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main":
        git(c, repo_dir, "merge", "--ff-only", "origin/main")
    else:
        print("警告: 手元の %s が main でないので更新しない" % a.repo)
    set_label(c, a.repo, n, LABEL_BUSY, False)
    try:
        gh_run(["issue", "close", str(n), "--repo", slug])
    except OrchError as e:
        print("警告: Issue #%d を閉じられなかった: %s" % (n, e))
    if not a.keep:
        remove_webchat_worktree(c, a.repo, n)
    print("webchat PR #%s（Issue #%d）: 取り込み完了。振った ID: %s" % (a.pr, n, ", ".join(ids) or "（出力から読み取れなかった）"))
    return 0


def cmd_webchat_reject(a) -> int:
    c = Ctx()
    need_repo(c, a.repo)
    info, n = read_webchat_pr(c, a.repo, a.pr)
    comment = "却下: " + a.reason
    if a.dry_run:
        print("dry-run: PR #%s にコメント「%s」→ 閉じる（ブランチ削除）→ Issue #%d の「%s」を外す（Issue は閉じない）" % (
            a.pr, comment, n, LABEL_BUSY))
        return 0
    outgoing(comment)
    slug = repo_slug(c, a.repo)
    gh_run(["pr", "comment", str(a.pr), "--repo", slug, "--body", comment])
    gh_run(["pr", "close", str(a.pr), "--repo", slug, "--delete-branch"])
    set_label(c, a.repo, n, LABEL_BUSY, False)
    remove_webchat_worktree(c, a.repo, n)
    print("webchat PR #%s（Issue #%d）: 却下" % (a.pr, n))
    return 0


# ---------- 作業場の作業ツリー ----------
WS_LINK = "ac3ds-re"  # 作業ツリーの中に本体へのジャンクションを張るフォルダ


def ws_worktree(c: Ctx, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9-]+", name or ""):
        raise OrchError("名前は英数字と - だけ: %r" % name)
    return c.data / "worktrees" / ("ws-" + name)


def make_junction(link: Path, target: Path) -> None:
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, OSError):
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
            if r.returncode != 0:
                raise OrchError("ジャンクションを作れない: %s" % (r.stderr or r.stdout).strip())
    else:
        os.symlink(target, link, target_is_directory=True)


def cmd_ws_add(a) -> int:
    c = Ctx()
    wt = ws_worktree(c, a.name)
    if wt.exists() or wt.is_symlink():
        raise OrchError("既にある: %s" % wt)
    git(c, c.root, "fetch", "origin")
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(c, c.root, "worktree", "add", "-b", a.branch, str(wt), "origin/main")
    local = c.root / "workspace.local.toml"
    if local.exists():
        shutil.copy2(local, wt / local.name)
    link, target = wt / WS_LINK, c.root / WS_LINK
    if link.exists() or link.is_symlink():  # 追跡されたフォルダがあれば、中身を消さずに空の時だけ外す
        unlink_links(link)
        if link.is_dir():
            link.rmdir()
    make_junction(link, target)
    print("作業ツリーを作った: %s（ブランチ %s）" % (wt, a.branch))
    return 0


def worktree_branch(c: Ctx, wt: Path) -> str:
    return git(c, wt, "rev-parse", "--abbrev-ref", "HEAD").strip()


def cmd_ws_remove(a) -> int:
    c = Ctx()
    wt = ws_worktree(c, a.name)
    if not wt.exists():
        raise OrchError("作業ツリーが無い: %s" % wt)
    branch = worktree_branch(c, wt)
    if not a.force:
        # リンクの先（本体）の状態を拾わないよう、ac3ds-re のジャンクションは除いて見る
        dirty = [l for l in git(c, wt, "status", "--porcelain", "--ignore-submodules").splitlines()
                 if l[3:].strip('"').rstrip("/") != WS_LINK]
        if dirty:
            raise OrchError("未コミットの変更がある（--force で無視）:\n" + "\n".join(dirty[:10]))
    remove_worktree_safely(c, c.root, wt)
    if wt.exists():
        try:
            wt.rmdir()
        except OSError:
            pass
    if not a.keep_branch and branch and branch != "HEAD":
        git(c, c.root, "branch", "-D", branch, check=False)
    if not (c.root / WS_LINK / ".git").exists():
        print("!!!!!! 警告: 本体の %s/.git が無い。中身が消えた恐れ。すぐ確認する !!!!!!" % WS_LINK, file=sys.stderr)
        return 1
    print("作業ツリーを消した: %s" % wt)
    return 0


# ---------- 入口 ----------
def main(argv=None) -> int:
    wslib.configure_stdio()
    p = argparse.ArgumentParser(prog="orch.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    wc = sub.add_parser("webchat", help="WebChat の PR（台帳に載らない）を取り込む").add_subparsers(dest="wcmd", required=True)

    def add_wc(name, f):
        w = wc.add_parser(name)
        w.add_argument("pr", type=int)
        w.add_argument("--repo", default=DEFAULT_REPO)
        w.set_defaults(f=f)
        return w

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
    s = add("export", cmd_export, repo=False)
    s.add_argument("--build")
    s.add_argument("--only", nargs="+", default=[])
    add("status", cmd_status, repo=False)
    add("stats", cmd_stats, repo=False)
    add("capacity", cmd_capacity, repo=False)
    add("wrapup", cmd_wrapup, repo=False)
    s = add("verify", cmd_verify)
    s.add_argument("issue", type=int)
    s = add("issue", cmd_issue)
    s.add_argument("--scope", required=True, choices=list(SCOPES))
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--difficulty", required=True, choices=DIFFS)
    for k in ("title", "goal", "target", "materials"):
        s.add_argument("--" + k, required=True)
    s.add_argument("--webchat", action="store_true", help="WebChat に渡す Issue にする（ラベル webchat、WebChat への指示を本文に足す）")
    s = add("consolidate-check", cmd_consolidate_check)
    s.add_argument("--create", action="store_true")
    s.add_argument("--threshold", type=int)
    s = add("dispatch", cmd_dispatch)
    s.add_argument("issue", type=int)
    s.add_argument("--worker", required=True)
    s.add_argument("--effort")
    s.add_argument("--idb", action="store_true")
    s.add_argument("--precheck", required=True)
    s.add_argument("--tools")
    s.add_argument("--ignore-limit", metavar="理由", help="推奨並列数・5時間枠の制限を、理由を付けて無視する（台帳に残す）")
    s = add("save", cmd_save)
    s.add_argument("issue", type=int)
    s.add_argument("--message")
    s = add("pause", cmd_pause)
    s.add_argument("issue", type=int)
    s.add_argument("--reason", required=True)
    s = add("unpause", cmd_unpause)
    s.add_argument("issue", type=int)
    s = add("report", cmd_report)
    s.add_argument("issue", type=int)
    s.add_argument("--from", dest="src", required=True)
    s = add("codex", cmd_codex, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--resume", action="store_true")
    s.add_argument("--message-file")
    s = add("claude", cmd_claude, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--model")
    s.add_argument("--effort")
    s.add_argument("--resume", action="store_true")
    s.add_argument("--message-file")
    s = add("review", cmd_review, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--model")
    s.add_argument("--effort", required=True)
    s.add_argument("--resume", action="store_true")
    s.add_argument("--message-file", required=True)
    s = add("stamp", cmd_stamp)
    s.add_argument("issue", type=int)
    s.add_argument("--allow-outside", action="store_true")
    s = add("accept", cmd_accept, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--scores", required=True)
    s.add_argument("--read", required=True, help="採点の前に読んだ物の一覧のファイル（- は標準入力）。PR のコメントに残す")
    s.add_argument("--reviewed", help="深い検証の報告のファイル（- は標準入力）。PR のコメントに残す")
    s.add_argument("--keep", action="store_true")
    s = add("reject", cmd_reject, dry=True)
    s.add_argument("issue", type=int)
    s.add_argument("--reason", required=True)
    s.add_argument("--keep", action="store_true")
    add_wc("verify", cmd_webchat_verify)
    w = add_wc("accept", cmd_webchat_accept)
    w.add_argument("--scores", required=True)
    w.add_argument("--read", required=True, help="採点の前に読んだ物の一覧のファイル（- は標準入力）。PR のコメントに残す")
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--keep", action="store_true")
    w = add_wc("reject", cmd_webchat_reject)
    w.add_argument("--reason", required=True)
    w.add_argument("--dry-run", action="store_true")
    wsp = sub.add_parser("ws", help="作業場の作業ツリー（_local/worktrees/ws-<名前>）を作る・消す").add_subparsers(
        dest="wscmd", required=True)
    w = wsp.add_parser("add", help="origin/main から作業ツリーを作り、workspace.local.toml を写し、ac3ds-re を本体へのジャンクションで張る")
    w.add_argument("name", help="英数字と - だけ")
    w.add_argument("--branch", required=True, help="作るブランチ名")
    w.set_defaults(f=cmd_ws_add)
    w = wsp.add_parser("remove", help="ジャンクションを外してから作業ツリーとブランチを消す。未コミットの変更があれば断る")
    w.add_argument("name")
    w.add_argument("--force", action="store_true", help="未コミットの変更を無視する")
    w.add_argument("--keep-branch", action="store_true", help="ローカルのブランチを残す")
    w.set_defaults(f=cmd_ws_remove)
    a = p.parse_args(argv)
    try:
        return a.f(a)
    except OrchError as e:
        print("エラー: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
